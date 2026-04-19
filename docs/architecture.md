# Lumio Voice / Anchor Voice — Architecture & Pipeline Reference

## Overview

**Lumio Voice** is the product name for this stack; the codebase and AWS resources use the **anchor-voice** naming convention. This document describes only the **processing pipeline and AWS data plane** (ingestion, worker, storage, events) — not client applications or consoles.

Anchor Voice is an event-driven AWS pipeline that transcribes long-form audio (medical sessions) using Sarvam Saaras v3 with cross-chunk speaker diarization — entirely without an external diarization model.

Target users: doctors, therapists, psychiatrists recording clinical sessions.

---

## System Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                          AWS Cloud                              │
│                                                                 │
│  S3 (uploads/)                                                  │
│      │                                                          │
│      │ ObjectCreated event                                      │
│      ▼                                                          │
│  EventBridge Rule                                               │
│  (filter: prefix=uploads/, size > 0)                            │
│      │                                                          │
│      │ input transform → {bucket, key, size_bytes}              │
│      ▼                                                          │
│  SQS FIFO Queue                                                 │
│      │                                                          │
│      │ trigger                                                  │
│      ▼                                                          │
│  Lambda Dispatcher                                              │
│  (sqs_dispatcher)                                               │
│      │                                                          │
│      │ ECS RunTask (injects env vars)                           │
│      ▼                                                          │
│  ECS Fargate Task (4 vCPU, 16GB RAM)                            │
│  ┌───────────────────────────────────┐                          │
│  │  Pipeline (main.py)               │                          │
│  │    1. Download from S3            │                          │
│  │    2. VAD chunking (silero-vad)   │                          │
│  │    3. Parallel transcription      │──► Sarvam Saaras v3      │
│  │    4. Overlap speaker stitching   │                          │
│  │    5. Translation                 │──► Sarvam Mayura v1      │
│  │    6. Store results               │──► RDS PostgreSQL        │
│  │    7. Publish completion event    │──► SQS job-events queue  │
│  └───────────────────────────────────┘                          │
│                                                                 │
│  SQS job-events queue  ──► API backend / frontend / notifier    │
│  RDS PostgreSQL (ISOLATED subnet)                               │
│  S3 (processed/)                                                │
└─────────────────────────────────────────────────────────────────┘
```

---

## Pipeline Stages

### Stage 1 — Audio Preparation

**File:** `audio.py`

- Detects audio duration via `ffprobe`
- If file contains a video stream (e.g. `.mp4`), extracts audio-only to `.m4a` via ffmpeg
- Supported formats: `.wav`, `.mp3`, `.m4a`, `.flac`, `.ogg`, `.aac`, `.webm`, `.mp4`

---

### Stage 2 — VAD-Based Smart Chunking

**File:** `chunking.py`

Sarvam accepts a maximum of 60 minutes per request. For sessions under 60 minutes (most therapy/medical sessions), the audio is passed as a single chunk — no splitting, no stitching needed.

For audio over 60 minutes:

```
Full audio
    │
    ▼
silero-vad  ──► speech timestamps ──► inverted to silence segments
    │
    ▼
Adaptive threshold (40th percentile of silence durations, clamped 0.3s–1.5s)
    │
    ▼
Greedy split algorithm:
  - Target: 40 min per chunk
  - Search window: ±5 min around target boundary
  - Pick: longest silence gap in window → "silence_gap"
  - Fallback 1: closest silence in safe zone → "fallback_closest"
  - Fallback 2: force cut 60s before hard max → "forced_boundary"
    │
    ▼
Overlapping chunks (2 min overlap prefix on each chunk after the first)
  Chunk 0:  0s ──────────────── 2511s
  Chunk 1:  2391s ──────────── 4984s   (120s overlap with chunk 0)
  Chunk 2:  4864s ──────────── 7362s   (120s overlap with chunk 1)
  Chunk 3:  7242s ──────────── 7707s   (120s overlap with chunk 2)
```

The 2-minute overlap prefix is used for cross-chunk speaker stitching (Stage 4).

**ChunkInfo fields:**
- `start_time` — actual audio file start (includes overlap prefix)
- `content_start` — where this chunk's unique content begins (= previous chunk's end)
- `end_time` — end of this chunk's unique content
- `split_reason` — why the split happened at this point

---

### Stage 3 — Parallel Transcription

**File:** `transcription.py`

All chunks are submitted to Sarvam's batch API in parallel (up to `SARVAM_MAX_CONCURRENT_CHUNKS`, default 10).

```python
job = client.speech_to_text_job.create_job(
    model="saaras:v3",
    mode="codemix",           # handles Hinglish, mixed Indian languages
    language_code="unknown",  # auto-detects all 22 Indian languages + English
    with_diarization=True,    # per-chunk speaker diarization from Sarvam
)
```

Each chunk returns `TranscriptSegment` objects with:
- `speaker_id` — Sarvam's per-chunk label (e.g. "0", "1") — not globally consistent yet
- `start_time`, `end_time` — absolute seconds in full audio (chunk offset added)
- `text` — transcribed text

**Retry:** 3 attempts, exponential backoff (10s → 60s). 429 rate limit errors wait 60s before retry.

---

### Stage 4 — Cross-Chunk Speaker Stitching

**File:** `merger.py`

Sarvam assigns speaker IDs independently per chunk — `SPEAKER_0` in chunk 1 and `SPEAKER_0` in chunk 2 may be different people. The merger makes speaker IDs globally consistent using the overlap region.

```
Overlap window: chunk[i].start_time → chunk[i].content_start

Chunk 0 overlap text (last 2 min):
  SPEAKER_0: "...toh usne bola ki Iran mein..."
  SPEAKER_1: "haan, bilkul sahi..."

Chunk 1 overlap text (first 2 min):
  SPEAKER_1: "...toh usne bola ki Iran mein..."   ← same speech
  SPEAKER_0: "haan, bilkul sahi..."

rapidfuzz token_set_ratio:
  chunk1.SPEAKER_1 vs chunk0.SPEAKER_0 → score 87  ✓ match
  chunk1.SPEAKER_0 vs chunk0.SPEAKER_1 → score 84  ✓ match

Remap: chunk1.SPEAKER_1 → global SPEAKER_0
       chunk1.SPEAKER_0 → global SPEAKER_1
```

After stitching:
- Overlap duplicate segments are discarded (only `content_start` onwards kept per chunk)
- All segments sorted by `start_time`
- Speaker IDs normalized to 0-based integers in order of first appearance
- Consecutive same-speaker segments merged into one

**Confidence threshold:** matches below score 65 are not remapped — chunk-local IDs kept rather than making a wrong merge.

---

### Stage 5 — Translation

**File:** `translation.py`

**Model:** `mayura:v1` — auto source language detection, 11 Indian languages + English, formal mode.

#### Batching strategy (chunking at the text layer)

Merged speaker segments are **not** sent as one Sarvam request per segment. They are **packed into batches**; each batch is one `text.translate` call, then the response is **split** back into per-segment strings.

**Batch limits** (whichever is hit first when appending the next segment):

| Limit | Value | Rationale |
|-------|--------|-----------|
| Joined character length | **900** chars | Stays under Mayura’s ~1000 character request cap |
| Segments per batch | **10** | Hard cap so a single batch stays predictable |

**Packing:** `_build_batches` walks segments in order and greedily fills a batch until the next segment would exceed the char budget **or** the segment count would exceed 10, then starts a new batch.

**Delimiter:** segments are joined with ` ⟦S⟧ ` (Unicode bracket marker with spaces). The response is split on the **exact same** substring. This token is chosen so the model usually **preserves** it; delimiters like `||` are more often normalized or “translated” (e.g. to a word meaning “or”), which would make `split()` return the wrong number of parts.

**Flow:**

```
seg_a ⟦S⟧ seg_b ⟦S⟧ seg_c  →  [translate once]  →  part_a ⟦S⟧ part_b ⟦S⟧ part_c
                                                      split(⟦S⟧) → 3 translations
```

For long jobs (e.g. **~1200+ segments**), batching reduces translate RPCs by roughly an order of magnitude compared to per-segment calls (on the order of **tens of batches** per language instead of thousands — exact count depends on segment lengths).

**Per language:** target languages are normalized (e.g. `en` → `en-IN`). Each language runs **after** the previous: for each language, all batches for that language are scheduled.

**Parallelism within a language:** batches are executed with a `ThreadPoolExecutor` of **10** workers (`MAX_TRANSLATION_WORKERS`). **Every** translate call (batch or fallback) invokes `rate_limit.throttle()` before the HTTP request so all workers share the global RPM cap (see [Rate limiting](#rate-limiting)).

#### `en-IN` Indic-passthrough retry pass

Mayura v1 with `source_language_code="auto"` occasionally **returns the input unchanged** on heavily code-mixed (Hinglish / code-switched) segments when the target is `en-IN` — the output still contains Devanagari / Bengali / Tamil / etc. characters. After the main batch pass completes for `en-IN`, a second surgical pass runs:

1. **Detect (entry gate — byte-level script check)** — for every translated segment, check if the output still contains any Indic script (Devanagari `\u0900–\u097F`, Bengali `\u0980–\u09FF`, Gurmukhi `\u0A00–\u0A7F`, Gujarati `\u0A80–\u0AFF`, Oriya `\u0B00–\u0B7F`, Tamil `\u0B80–\u0BFF`, Telugu `\u0C00–\u0C7F`, Kannada `\u0C80–\u0CFF`, Malayalam `\u0D00–\u0D7F`). The output is also flagged when it's a byte-for-byte echo of an Indic-script source. This script check is cheap and deterministic — it never decides the wrong thing about *whether* to retry, only triggers the next step.
2. **Pick source code (two-tier: Lingua for shared scripts, Unicode ranges for unique ones)** — once a segment is flagged, the *source text* (not the failed output) is passed to `lang_detect.detect_source_code` which asks [`lingua-language-detector`](https://github.com/pemistahl/lingua-py) restricted to the **9** Indian languages lingua actually ships models for: Hindi, Marathi, Bengali, Gujarati, Punjabi, Tamil, Telugu, Urdu, and English. Lingua earns its keep on the only script with real ambiguity — **Devanagari (Hindi vs Marathi)**; the old code always mapped all Devanagari to `hi-IN`, bleeding Marathi quality. For **Kannada** (`\u0C80-\u0CFF`), **Malayalam** (`\u0D00-\u0D7F`), **Odia** (`\u0B00-\u0B7F`), and every other Indic script whose Unicode block is unique, the script-range regex (`_detect_indic_source`) is already 100% accurate and runs as a zero-cost fallback whenever lingua returns `None`. The fallback also fires when: lingua's top-1 confidence is below **0.75**, the text is under 10 chars, lingua isn't installed, or lingua wrongly returns `ENGLISH` on a script-confirmed Indic segment.
3. **Retry** — re-translate **only** flagged segments, one-by-one, with the **explicit** detected source code (no `auto`), `model="mayura:v1"`, `mode="formal"`. Runs through the same 10-worker pool and global RPM throttle.
4. **Long-segment safety** — if a flagged segment is **>950 chars**, it's pre-split via `_split_long_text` (sentence/word boundaries) into sub-1000-char pieces and each piece retried with the same source code, then rejoined. Prevents Mayura’s 1000-char 400 error on the retry path.
5. **Accept-only-if-better** — keep the retry only when the output is **non-empty**, contains **no Indic script**, and is **not byte-identical** to the source. Otherwise the original (passthrough) translation is kept — never overwrite with worse output.

> **Why not use Lingua on the main path?** Saaras v3 (`mode=codemix`, `language_code=unknown`) always emits native script, so the byte-level script regex is a perfect "is this non-English?" gate on the way into Mayura. Running a statistical LID on every segment would cost CPU without changing a single decision there. Lingua only earns its keep on the retry path, where the question changes from *whether* the segment is Indic to *which Indic language* (Hindi vs Marathi being the expensive confusion).

Log lines per run: `"en-IN passthrough retry: N/M segments (by source: {...})"` and `"en-IN passthrough retry: K segments repaired"`.

Observed impact on the Hinglish reference audio (1265 segments): remaining Indic-script lines dropped from **~148 (11.7%)** → **~25 (2.0%)** after the retry; the residual is segments where Mayura still can’t produce clean English (model limitation, not pipeline bug).

#### Fallbacks and edge cases

| Situation | Behavior |
|-----------|----------|
| `split` yields **≠** number of segments (delimiter lost or altered) | Warning log; **re-translate each segment in that batch** with individual API calls — no guessing |
| Request rejected for length (400, “exceed” / over limit) | `_split_long_text` splits at sentence boundaries (English `.?!`, Hindi `।`), translates sub-chunks, joins |
| HTTP **429** | Sleep 60s, retry (tenacity on `_translate_batch_text`) |
| “Unable to detect” source language | Retry same text with explicit `hi-IN` source |
| `en-IN` output still contains Indic script (Mayura auto-detect passthrough) | Second pass: Lingua picks the Mayura source code from the original text (falls back to script-range regex on low confidence / missing dep); re-translate each flagged segment with that explicit source; pre-split >950 chars first; keep retry only if cleaner |
| Batch-level exception after retries | Empty `translated_text` for every segment in that batch; pipeline continues |

---

### Stage 6 — Storage

**File:** `job_status.py`, `models.py`

Results stored in RDS PostgreSQL across three tables:

```sql
jobs          — one row per audio file (status, duration, speaker count, errors)
segments      — one row per merged speaker turn (speaker_id, start_time, end_time, text)
translations  — one row per segment per language (translated_text)
```

The segment + translation inserts AND the `jobs.status → completed` transition
happen in **one database transaction** (`store_results(..., final_status="completed")`).
Consumers never see segments without the status update or a status update without
the segments.

---

### Stage 7 — Completion event (fan-out notification)

**File:** `events.py`

After RDS is committed, the worker publishes **one SQS message** to the
`JOB_EVENTS_QUEUE_URL` queue — either `job.completed` or `job.failed`.
Downstream services (API backend, frontend notifier, Slack bot) consume this
queue instead of polling the `jobs` table.

**Publish is best-effort**: RDS is the source of truth. A publish failure is
logged and never fails the job. Consumers that miss an event can reconcile
by scanning `jobs` where `status IN ('completed', 'failed')`.

**FIFO-aware**: if the queue URL ends with `.fifo`, the publisher attaches
`MessageGroupId=job_id` and `MessageDeduplicationId=job_id:status`, so a
retried publish is deduped by SQS automatically.

**Schema:**

```jsonc
// job.completed
{
  "event": "job.completed",
  "job_id": "8f3c…",
  "status": "completed",
  "s3_bucket": "anchor-voice-uploads",
  "s3_key": "uploads/session-42.mp3",
  "original_filename": "session-42.mp3",
  "audio_duration_seconds": 1234.5,
  "num_chunks": 1,
  "num_segments": 187,
  "num_speakers": 3,
  "target_languages": ["en-IN"],
  "completed_at": "2026-04-19T07:45:12+00:00"
}

// job.failed
{
  "event": "job.failed",
  "job_id": "8f3c…",
  "status": "failed",
  "s3_bucket": "anchor-voice-uploads",
  "s3_key": "uploads/session-42.mp3",
  "original_filename": "session-42.mp3",
  "error_message": "Translation failure rate 12.3% for en-IN exceeds threshold 5.0%",
  "failed_at": "2026-04-19T07:45:12+00:00"
}
```

**Consumer sketch:**

```python
import boto3, json

sqs = boto3.client("sqs")
resp = sqs.receive_message(QueueUrl=EVENTS_URL, MaxNumberOfMessages=10, WaitTimeSeconds=20)
for msg in resp.get("Messages", []):
    evt = json.loads(msg["Body"])
    if evt["event"] == "job.completed":
        # e.g. fetch segments/translations from RDS and push to frontend
        handle_completed(evt)
    else:
        alert(evt)
    sqs.delete_message(QueueUrl=EVENTS_URL, ReceiptHandle=msg["ReceiptHandle"])
```

---

## Rate Limiting

**File:** `rate_limit.py`

All Sarvam API calls (transcription + translation) share a single global sliding-window rate limiter capped at `SARVAM_RPM_LIMIT` (default 100 RPM).

```
Before every Sarvam call → throttle()
  → count requests in last 60 seconds
  → if count >= 100: sleep until oldest request is 60s old
  → else: allow and record timestamp
```

This is a sliding window (not a fixed 60s bucket), so the rate is smooth with no burst at minute boundaries.

---

## Error Handling

| Error | Handling |
|-------|----------|
| Sarvam 429 rate limit | Wait 60s explicitly, then retry via `@retry` |
| Sarvam 5xx / network timeout | `@retry` exponential backoff (3–4 attempts, 5s–60s) |
| Chunk transcription failure | Raises immediately — job marked `failed` in DB |
| Translation batch split mismatch | Re-translate each segment individually |
| Mayura 400 / input exceeds limit | Sentence-split (`।` / `.?!`), translate pieces, rejoin |
| Auto language detection failure | Retry with `hi-IN` explicit source |
| `en-IN` output still in Indic script (auto-detect passthrough) | Surgical retry with Lingua-picked Indic source (Hindi vs Marathi disambiguation, 11-language allow-list, 0.75 confidence floor), falls back to script-range regex; pre-split if >950 chars |
| Translation segment failure | Empty string stored, pipeline continues |
| pyannote / diarization | Removed — not used. Sarvam provides per-chunk diarization |
| Audio over 60 min | VAD chunking with overlap — no size limit |
| Video file input | ffmpeg extracts audio-only before processing |
| No silence gap found | Force-cut 60s before hard chunk limit |
| SQS visibility timeout (long jobs) | Heartbeat thread extends visibility every 5 min |
| Job stuck / crashed | Status readable from `jobs` table; error in `jobs.error_message` |

---

## Job Status State Machine

```
pending → downloading → chunking → transcribing → merging → translating → completed
                                                                        ↓ (any stage)
                                                                      failed
```

Invalid transitions raise `ValueError` immediately — prevents silent data corruption.

---

## Configuration

Most options are environment variables:

| Variable | Default | Description |
|---|---|---|
| `SARVAM_API_KEY` | — | Sarvam API key (required) |
| `SARVAM_RPM_LIMIT` | 100 | Requests per minute cap (transcription + translation) |
| `SARVAM_MAX_CONCURRENT_CHUNKS` | 10 | Parallel transcription workers |
| `SARVAM_BATCH_TIMEOUT_S` | 1800 | Max wait for a batch job (30 min) |
| `SARVAM_BATCH_POLL_INTERVAL_S` | 10 | How often to poll batch job status |
| `DEFAULT_TARGET_LANGUAGES` | `en` | Comma-separated Mayura target codes (e.g. `en,hi`) |
| `TARGET_CHUNK_DURATION_S` | 2400 | Target chunk size (40 min) |
| `MAX_CHUNK_DURATION_S` | 2700 | Hard max chunk size (45 min) |
| `OVERLAP_DURATION_S` | 120 | Overlap prefix for speaker stitching (2 min) |
| `SILENCE_SEARCH_WINDOW_S` | 300 | VAD split search window (±5 min) |
| `DATABASE_URL` | — | PostgreSQL connection string |
| `AWS_REGION` | ap-south-1 | AWS region |
| `JOB_EVENTS_QUEUE_URL` | — | SQS queue for `job.completed` / `job.failed` events. Unset = no publish. Supports `.fifo` suffix. |
| `TRANSLATION_FAILURE_THRESHOLD` | 0.05 | Max fraction of segments per language that may come back empty before the job is failed. |
| `SQS_HEARTBEAT_INTERVAL_S` | 300 | How often the worker extends the input SQS message visibility. |
| `SQS_HEARTBEAT_EXTEND_BY_S` | 3600 | How long to extend each heartbeat. |
| `METRICS_NAMESPACE` | `AnchorVoice` | CloudWatch namespace the worker emits EMF metrics under. |
| `METRICS_ENABLED` | `1` | Set to `0` / `false` to silence EMF emissions (useful in local dev). |

**Translation batching (code constants in `translation.py`, not env):** max **900** characters and max **10** segments per translate batch; delimiter ` ⟦S⟧ `; **10** concurrent batch workers (`MAX_TRANSLATION_WORKERS`). Details under **Stage 5 — Translation** above.

---

## Observability — CloudWatch metrics & dashboard

Runtime telemetry lands in CloudWatch with zero additional infra. The worker emits
[**Embedded Metric Format (EMF)**](https://docs.aws.amazon.com/AmazonCloudWatch/latest/monitoring/CloudWatch_Embedded_Metric_Format_Specification.html)
JSON lines to stdout; the awslogs driver ships them to CloudWatch Logs, which
auto-extracts metrics under the `AnchorVoice` namespace. **No `PutMetricData`
calls, no extra IAM, no sidecar.** If EMF is ever disabled (`METRICS_ENABLED=0`),
the same lines are simply no longer printed — everything else keeps working.

The emitter is `worker/src/pipeline/metrics.py`.

### Metrics emitted

All metrics carry the dimension `Service=worker`. Translation metrics add
`Language=<target>` so they can be sliced per language.

| Metric | Unit | Emitted when | Dimensions |
|---|---|---|---|
| `JobCompleted` | Count | Pipeline finishes successfully | `Service` |
| `JobFailed` | Count | Pipeline raises (after `mark_failed`) | `Service` |
| `JobDurationSeconds` | Seconds | Both outcomes (wall-clock from `process_job` entry) | `Service` |
| `AudioDurationSeconds` | Seconds | Both outcomes (0 if failed before `ffprobe`) | `Service` |
| `SegmentsProcessed` | Count | Both outcomes | `Service` |
| `SpeakersDetected` | Count | Both outcomes | `Service` |
| `ChunksProcessed` | Count | Both outcomes | `Service` |
| `TranslationSegments` | Count | Per language after translation | `Service`, `Language` |
| `TranslationEmptySegments` | Count | Per language after translation | `Service`, `Language` |
| `TranslationEmptyRate` | Percent | Per language after translation | `Service`, `Language` |

Dimension cardinality is deliberately bounded — no `job_id` or `s3_key` ends up
as a metric dimension.

### Dashboard

`scripts/cloudwatch-dashboard.json` ships a 12-widget dashboard that pairs the
EMF metrics with AWS-native signals:

1. **Jobs completed vs failed** (AnchorVoice, Sum/5min)
2. **Job wall-clock duration** — avg, p95, max
3. **Audio hours processed per day** (Sum / 3600)
4. **Translation empty-rate by language** — dynamic `SEARCH(...)` so new `Language`
   dimensions show up automatically without editing the dashboard
5. **Segments / speakers / chunks per job** — average
6. **Input queue depth** — visible + in-flight (AWS/SQS)
7. **DLQ depth** — should sit at 0 (AWS/SQS)
8. **Oldest input message age** — catches stuck dispatcher (AWS/SQS)
9. **Lambda dispatcher invocations / errors / throttles** (AWS/Lambda)
10. **Lambda dispatcher duration** — avg / p95 / max (AWS/Lambda)

Install / update:

```bash
make dashboard                             # uses defaults for names/region
# or
DASHBOARD_NAME=anchor-voice \
AWS_REGION=ap-south-1 \
INPUT_QUEUE_NAME=anchor-voice-jobs.fifo \
DLQ_NAME=anchor-voice-jobs-dlq.fifo \
LAMBDA_NAME=anchor-voice-dispatcher \
  ./scripts/create_dashboard.sh
```

`put-dashboard` is idempotent — re-run anytime the template changes.

### Verifying EMF works

After the first job runs under ECS Fargate:

```bash
# Should show the AnchorVoice namespace:
aws cloudwatch list-metrics --namespace AnchorVoice --region "$AWS_REGION"

# Sum of jobs over the last hour:
aws cloudwatch get-metric-statistics \
  --namespace AnchorVoice --metric-name JobCompleted \
  --dimensions Name=Service,Value=worker \
  --start-time "$(date -u -v-1H '+%Y-%m-%dT%H:%M:%SZ')" \
  --end-time   "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" \
  --period 300 --statistics Sum --region "$AWS_REGION"
```

Metric extraction is driven entirely by the log group's retention + the EMF
JSON shape — there is no additional configuration to wire up.

---

## Infrastructure (No NAT Gateway)

ECS Fargate tasks run in **public subnets** with `assignPublicIp=ENABLED` — no NAT gateway needed, saving ~$32/month.

RDS runs in **isolated subnets** accessible only from ECS via security group rules — never exposed to the internet.

Only a free S3 Gateway endpoint is used (no Interface endpoints).

---

## Local Development

```bash
# 1. Copy and fill env vars
cp .env .env.local
# Fill in: SARVAM_API_KEY, DATABASE_URL

# 2. Install dependencies
cd worker && uv sync && cd ..

# 3. Start local PostgreSQL
make db-up

# 4. Create tables — LOCAL DEV ONLY.
# In AWS, the worker self-bootstraps on every container start via
# pipeline.main → create_tables(), so this step is not needed on ECS.
DATABASE_URL=postgresql://anchorvoice:anchorvoice@localhost:5432/anchorvoice \
  uv run python scripts/init_db.py

# 5. Run pipeline locally
DATABASE_URL=postgresql://anchorvoice:anchorvoice@localhost:5432/anchorvoice \
  uv run python scripts/run_local.py audio.mp3 --languages en
```

> **Note on `scripts/`** — `scripts/init_db.py`, `scripts/run_local.py`, and
> `scripts/create_dashboard.sh` are **developer tools** that live at the repo
> root, not inside `worker/`. They are deliberately *not* copied into the
> Docker image (which has build context `worker/`). The production worker
> doesn't need them — it bootstraps its own schema from `pipeline.db` on
> every cold-start.

`scripts/run_local.py` writes three sidecar files next to the input audio:

| File | Contents |
|---|---|
| `<name>_transcript.txt` | Speaker-labelled transcript with `[HH:MM:SS]` stamps |
| `<name>_translation_<lang>.txt` | One file per `--languages` target (same layout, translated text) |
| stdout | Job id, speaker count, segment count |

## AWS Setup

End-to-end deploy is scripted — `scripts/deploy.sh` is idempotent and phase-addressable. One command stands up every resource below; re-running only touches what's changed.

```bash
export SARVAM_API_KEY='sk_...'
export RDS_MASTER_PASSWORD='Strong!Pa55word'
export AWS_REGION='ap-south-1'     # optional
export ENV='prd'                   # optional — names all resources ${APP}-${ENV}-*
make deploy                        # or: ./scripts/deploy.sh
```

Re-runs:

```bash
make deploy-image                  # rebuild image + register new task def revision
./scripts/deploy.sh lambda         # update lambda code + env only
./scripts/deploy.sh iam            # refresh IAM policies only
```

Resources the script provisions (region defaults to `ap-south-1`):

| Resource | Name | Purpose |
|---|---|---|
| S3 bucket | `${NS}-audio-${ACCOUNT}-${REGION}` | Audio uploads (`uploads/` prefix); SSE-S3, public access blocked |
| SQS FIFO — input | `${NS}-transcription-jobs.fifo` | Job queue; `VisibilityTimeout=900`, `maxReceiveCount=3`, content dedup |
| SQS FIFO — DLQ | `${NS}-transcription-jobs-dlq.fifo` | Poison-message parking |
| SQS FIFO — events | `${NS}-job-events.fifo` | `job.completed` / `job.failed` fan-out |
| Lambda | `${NS}-job-dispatcher` | SQS-triggered ECS `RunTask` dispatcher with `ReportBatchItemFailures` |
| ECS cluster + task def | `${NS}` / `${NS}-worker` | Fargate 2 vCPU / 8 GB (tune in `scripts/deploy.sh`) |
| ECR | `${APP}/worker` | Worker Docker images, scan-on-push |
| RDS PostgreSQL | `${NS}-postgres` | `db.t4g.micro`, gp3, encrypted, isolated SG |
| Secrets Manager | `${APP}/${ENV}/sarvam-api-key`, `${APP}/${ENV}/rds-credentials` | Runtime credentials fetched by worker |
| CloudWatch log groups | `/ecs/${NS}-worker`, `/aws/lambda/${LAMBDA_NAME}` | 30-day retention |
| CloudWatch dashboard | `${NS}` | EMF metrics + SQS/Lambda signals (see Observability) |
| VPC | Default VPC | Public subnets for ECS (`assignPublicIp=ENABLED`), RDS reachable only from ECS SG |
| Security groups | `${NS}-ecs`, `${NS}-rds` | RDS ingress `5432` only from ECS SG |

Networking: ECS tasks use `assignPublicIp=ENABLED` in public subnets — no NAT gateway. RDS is reachable only from the ECS SG, never the internet.

Send a test job:

```bash
make send-test f=s3://${S3_BUCKET}/uploads/rec02.m4a
aws logs tail /ecs/${NS}-worker --region ${AWS_REGION} --follow
```

---

## Data Privacy (Medical Use)

- Audio never leaves Sarvam's API and your AWS infrastructure
- No external LLM receives patient data (speaker stitching is text-similarity only)
- RDS encrypted at rest; S3 buckets private
- All AWS services used are HIPAA-eligible (requires BAA with AWS)
- Sarvam data processing agreement required before production medical use
