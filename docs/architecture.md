# Lumio Voice / Anchor Voice — Architecture & Pipeline Reference

## Overview

**Lumio Voice** is the product name for this stack; the codebase and AWS resources use the **anchor-voice** naming convention. This document describes only the **processing pipeline and AWS data plane** (ingestion, worker, storage, events) — not client applications or consoles.

Anchor Voice is an event-driven AWS pipeline that transcribes long-form audio (medical sessions) using Sarvam Saaras v3 and ElevenLabs Scribe v2 with cross-chunk speaker diarization — entirely without an external diarization model.

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
│  ┌────────────────────────────────────────────┐                 │
│  │  Pipeline (main.py)                        │                 │
│  │    1. Download from S3                     │                 │
│  │    2. Normalize audio                      │                 │
│  │    3. Provider lanes in parallel:          │                 │
│  │         Sarvam Saaras v3          ─────────┼─► Sarvam API    │
│  │         (VAD chunks)                       │                 │
│  │         ElevenLabs Scribe v2      ─────────┼─► ElevenLabs API│
│  │         (full audio when possible)         │                 │
│  │    4. Per-provider speaker stitching       │                 │
│  │    5. Claude per provider as soon as       ├─► Anthropic API │
│  │       that provider's STT + merge is done  │  (Claude Sonnet) │
│  │    6. Write provider outputs JSON ─────────┼─► S3 (results/) │
│  │    7. Publish pointer event ───────────────┼─► SQS events    │
│  └────────────────────────────────────────────┘                 │
│                                                                 │
│  SQS job-events queue  ──► Backend consumer (reads results JSON │
│                             from S3 via pointer in the event)   │
│  S3 bucket: uploads/ (input audio) + results/ (transcripts)     │
└─────────────────────────────────────────────────────────────────┘
```

---

## Pipeline Stages

### Stage 1 — Audio Preparation

**File:** `audio.py`

- Detects audio duration via `ffprobe`
- If file contains a video stream (e.g. `.mp4`), extracts audio-only to `.m4a` via ffmpeg
- Supported **upload** formats: `.wav`, `.mp3`, `.m4a`, `.flac`, `.ogg`, `.aac`, `.webm`, `.mp4`
- **`convert_to_mono_wav`** produces **16 kHz, mono, 16-bit PCM WAV (`pcm_s16le`)** for Sarvam; **`split_audio_segment`** time-slices that normalized master with ffmpeg **stream copy** (no per-chunk resample; see Stage 2)

---

### Stage 2 — VAD-Based Smart Chunking

**File:** `chunking.py`

Sarvam accepts a maximum of 60 minutes per request. For sessions under 60 minutes (most therapy/medical sessions), the job still uses **one** logical chunk — **no silence-based splitting and no cross-chunk stitching** — but the file is **normalized to `chunk_000.wav`** (16 kHz mono PCM) before upload, not sent as the raw upload codec.

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

**Audio sent to Sarvam (batch STT):** every upload is **WAV at 16 kHz, mono, 16-bit PCM (`pcm_s16le`)**, aligned with [Sarvam’s STT FAQ](https://docs.sarvam.ai/api-reference-docs/speech-to-text/faq) (“16 kHz or higher”, 16-bit, mono or stereo).

| Case | File | How |
|------|------|-----|
| Duration ≤ 60 min | `chunk_000.wav` | `convert_to_mono_wav(..., output_path=chunk_000.wav)` |
| Duration > 60 min | `chunk_000.wav`, `chunk_001.wav`, … | `split_audio_segment` on `{stem}_16k_mono.wav` (ffmpeg **`-c copy`**, same timeline as VAD) |

For long audio, **silero-vad** runs on **`{stem}_16k_mono.wav`**; Sarvam **`chunk_*.wav`** files are **slices of that same master** (not re-decoded from the original upload per chunk). The master is deleted after chunk files are written.

---

### Stage 3 — Provider-Parallel Transcription

**File:** `main.py`

After normalization, the worker starts independent provider lanes with `ThreadPoolExecutor(max_workers=2)`. Sarvam receives VAD chunks; Scribe v2 receives the full normalized audio when it fits the configured ElevenLabs limits and falls back to chunks only when the full audio is too large or too long:

```
chunks
  ├── Sarvam lane: Saaras codemix + translate → merge → Claude postprocess
  └── Scribe lane: full audio or chunks       → merge → Claude postprocess
```

The lanes do not wait for each other between STT and Claude. If Sarvam finishes first, Sarvam's Claude postprocess begins while Scribe v2 may still be transcribing; if Scribe finishes first, Scribe's Claude postprocess begins while Sarvam may still be running. Claude concurrency is bounded by `POSTPROCESS_MAX_CONCURRENT_PROVIDERS` (default `2`). Anthropic rate limits are account/model/tier-specific and enforced across RPM, input-token-per-minute, and output-token-per-minute limits, so the default is intentionally one Claude slot per provider lane rather than an aggressive hardcoded value.

Sarvam is required for job success. If Sarvam fails, the job fails. Scribe v2 is additive: if it fails while Sarvam succeeds, the results JSON includes `scribe_v2.status = "failed"` and still writes the Sarvam output.

### Stage 3a — Sarvam transcription + translation (dual Saaras pass)

**File:** `transcription.py`

For every chunk we submit **two** Sarvam Saaras v3 batch jobs in parallel — one for the original-language transcription and one for the English translation — and merge their outputs by timestamp overlap. Sarvam supports diarization in both `mode=codemix` and `mode=translate`, so the two passes carry the same audio's speaker structure and we don't need a separate text-translation step (Mayura) anywhere in the pipeline.

```
chunk.wav
    │
    ├── Saaras job (mode=codemix,    with_diarization=true) ─► transcription segments  (original language)
    │
    └── Saaras job (mode=translate,  with_diarization=true) ─► translation segments    (English)
                                                                  │
                                              timestamp-overlap zip
                                                                  ▼
                                       TranscriptSegment(text, translation, …)
```

Why two passes instead of Mayura on the transcription text:

- Saaras `mode=translate` handles dense **Hinglish / code-switched** speech natively — it produces clean English even when the audio code-switches mid-sentence. Mayura's `auto`-source path treated romanized Hindi as English and passed it through unchanged, which is the failure this redesign fixes.
- One Sarvam product, two modes — there is no second SDK, no language-detection heuristic, and no retry-on-passthrough logic to maintain. The whole `translation.py` and `lang_detect.py` modules (≈ 700 lines) are deleted.

Each Saaras job is wrapped by `_run_saaras_job(chunk, mode=...)`:

```python
job = client.speech_to_text_job.create_job(
    model="saaras:v3",
    mode=mode,                # "codemix" or "translate"
    language_code="unknown",  # auto-detects all 22 Indian languages + English
    with_diarization=True,
)
```

Concurrency: `transcribe_all_chunks` uses a `ThreadPoolExecutor(max_workers=SARVAM_MAX_CONCURRENT_CHUNKS)` over chunks; each chunk worker spawns a small inner pool of size 2 to run the codemix and translate jobs side-by-side. The global RPM throttle in `rate_limit.throttle()` is applied per-API-call regardless of pool nesting, so `SARVAM_RPM_LIMIT` continues to bound the absolute Sarvam request rate.

Each chunk returns a list of `TranscriptSegment` whose fields after the overlap zip are:

- `speaker_id` — Sarvam's per-chunk label from the **codemix** job (e.g. `"0"`, `"1"`); the translate job's speaker IDs are deliberately discarded so the cross-chunk stitcher in Stage 4 sees a single canonical speaker timeline.
- `start_time`, `end_time` — absolute seconds in full audio (chunk offset added). Owned by the codemix pass.
- `text` — original-language transcription (codemix output: native script for monolingual speech, romanized Hinglish for code-mixed speech — Sarvam's choice).
- `translation` — English. Computed by `_zip_translation_into_segments` using **single-best-match assignment**: each translate-pass segment is assigned to the one codemix segment with maximum temporal overlap, and translate texts then accumulate on that codemix segment in chronological order. A codemix segment that no translate segment maps to has `translation == ""`.

Both Saaras jobs run on the same audio, so diarization boundaries usually agree to within ~100 ms (diarization is computed on voice embeddings, not on the generated text). The single-best-match rule is what makes the zip robust to the two passes disagreeing on speaker boundaries — a long translate segment that grazes several short codemix segments (e.g. brief backchannels like "Hmm" or overlapping-speaker interjections) gets attached to only the codemix segment it overlaps the most, instead of being duplicated across all of them. The trade-off is that codemix segments whose audio was swallowed into an adjacent translate segment will be left with empty `translation`; this is preferable to inheriting another speaker's text. If empty-translation rates on brief interjections become a problem, future refinements (speaker-aware remap, per-segment translate fallback) can be layered on.

**Retry:** each Saaras job retries up to 3 times with exponential backoff (10 s → 60 s). HTTP 429 sleeps 60 s before re-raising into the retry. A failed translate pass on a chunk does **not** fail the chunk — the codemix output is preserved with empty `translation` per segment, and the pipeline continues to the merger.

---

### Stage 3b — ElevenLabs Scribe v2 transcription

**File:** `elevenlabs_transcription.py`

When `ELEVENLABS_ENABLED=true`, the Scribe v2 lane first tries to send the full normalized audio file to ElevenLabs Speech to Text with diarization, word timestamps, `no_verbatim=true`, and glossary-derived `keyterms` when enabled. `ELEVENLABS_NUM_SPEAKERS` is empty by default so Scribe v2 infers the speaker count.

The adapter validates the full audio before upload:

- `ELEVENLABS_MAX_UPLOAD_BYTES` defaults to `3000000000` bytes (3.0 GB).
- `ELEVENLABS_MAX_DURATION_S` defaults to `36000` seconds (10 hours).
- If the full audio fits those limits, Scribe v2 receives one request and its `metadata.input_mode` is `full_audio`.
- If the full audio exceeds either limit, Scribe v2 falls back to the Sarvam chunk list and `metadata.input_mode` is `chunked`.
- `ELEVENLABS_MAX_CONCURRENT_CHUNKS` defaults to `2` and only applies to the chunked fallback path.
- 429 and 5xx failures retry with exponential backoff and respect `Retry-After` when the API provides it.

Scribe v2 does not provide Sarvam-style English translate-mode output, so its raw segment `translation` fields are empty until Claude postprocess generates `normalized_translation`; the result writer can then expose that cleaned translation on the provider segment output.

---

### Stage 4 — Per-Provider Cross-Chunk Speaker Stitching

**File:** `merger.py`

STT providers assign speaker IDs independently per chunk — `SPEAKER_0` in chunk 1 and `SPEAKER_0` in chunk 2 may be different people. Each provider lane runs the merger on its own segment list so Sarvam and Scribe v2 get separate, internally consistent speaker IDs without mixing segments across providers.

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

### Stage 5 — Provider-Lane LLM Normalisation (optional)

**File:** `postprocess.py`

After a provider finishes STT and `merge()`, an optional Claude pass cleans that provider's segment list immediately. This is chained inside each provider lane, so Sarvam and Scribe v2 can postprocess at the same time when `POSTPROCESS_MAX_CONCURRENT_PROVIDERS >= 2`. The step is skipped silently if `POSTPROCESS_ENABLED=false` or `ANTHROPIC_API_KEY` / `ANTHROPIC_SECRET_NAME` is absent — it never blocks job completion.

**What it fixes:**

| Problem | Example (before → after) |
|---|---|
| Clinical term mishearing | "cat distributing" → "catastrophising" |
| Romanised Indian-language text | "ab dekho" → "अब देखो" (Devanagari) |
| ASR filler and stutters | "I I I was thinking um maybe" → "I was thinking maybe" |
| Missing punctuation | run-on sentences → proper sentence boundaries |
| Wrong casing | "ssri" → "SSRI", "Sertraline" → "sertraline" |

**Output fields added per segment:**
- `normalized_transcript` — cleaned transcription, Indian-language text in native script
- `normalized_translation` — cleaned fluent English translation

For Scribe v2, Claude also restores romanized Indian-language text back to the native script when the base language is clear, and it produces the English translation because ElevenLabs STT does not supply Sarvam-style translate-mode output.

**Glossary:** `worker/glossary.json` is bundled in the Docker image and read at runtime. Contains two arrays:
- `corrections` — ASR mishearings with their correct form: `{"heard": "sir traline", "corrected": "sertraline"}`
- `terms` — authoritative spellings Claude must preserve: `"sertraline"`, `"CBT"`, `"sukoon"`

Override the path at runtime with `GLOSSARY_FILE_PATH`.

**Validation layers** (from `postprocess.py`):
1. Structured output via API schema enforcement (tool_calling mode)
2. Output truncation detection — splits batch in half and recurses if `stop_reason=max_tokens`
3. Up to 3 retries with correction messages appended to the conversation
4. Gap-fill: any turn Claude skips gets original text passed through unchanged
5. Total failure: `_fallback_identity` returns original text — no crash

Full design reference: [docs/llm_postprocessing.md](llm_postprocessing.md)

---

### Stage 6 — Translation coverage check

**File:** `main.py` (no separate translation module — see Stage 3).

After `merge()` produces the canonical speaker-stitched segment list, the pipeline computes a coverage metric over the in-segment `translation` field and fails the job if the empty rate exceeds a threshold:

```python
nonempty_src = sum(1 for s in merged if s.text.strip())
empty        = sum(1 for s in merged if not s.translation.strip() and s.text.strip())
fail_rate    = empty / nonempty_src if nonempty_src else 0.0
```

A non-empty `text` with empty `translation` means the translate-mode Saaras pass returned no segment whose timestamps overlapped this transcription segment. Two realistic causes:

1. The translate pass diverged enough from codemix on diarization boundaries that one transcription segment fell into a sub-second gap between translation segments. Fixable with proportional trimming if it ever becomes common.
2. The translate pass returned an empty result for the entire chunk (Sarvam-side issue). The chunk-worker logs `translate pass returned 0 segments` at WARNING and the entire chunk's translations come back empty.

If `fail_rate > TRANSLATION_FAILURE_THRESHOLD` (default **0.60**, env-overridable) the job is marked failed and the SQS message is left visible for redelivery / DLQ.

#### Refinements (only enable if real-world coverage drops)

These were considered for Phase 1 and intentionally deferred — the simple overlap zip handles the common case cleanly:

- **Proportional trimming** — when one translate segment overlaps multiple transcription segments, split its text by word-count proportional to overlap duration, instead of attaching the full string to every overlapped row. Cuts duplicate-translation noise on within-turn chopping disagreements.
- **Per-segment fallback** — if the translate pass produced 0 segments for a chunk but the codemix pass succeeded, re-submit just the affected chunk to Saaras `mode=translate` once before failing. Adds ~1 retry budget but rescues isolated translate-pass blips.

---

### Stage 7 — Results persistence (S3 claim-check)

**File:** `results_writer.py`

The worker stores **nothing** in a database. Every finished job is serialized
to one JSON object and PUT to
`s3://${S3_PROCESSED_BUCKET}/${S3_RESULTS_PREFIX}<job_id>.json`
(default prefix `results/`). The SQS completion event carries only a small
pointer to that object (see Stage 7), so downstream consumers do **one S3
GET** to read the full results.

Why a claim-check over inlining in SQS:

- SQS body limit is 1 MiB; long multi-speaker transcripts can exceed it.
  Pointers are ~0.5 KiB and don't vary with audio length.
- S3 gives durable, inspectable, replayable records. If an SQS message is
  missed or the consumer is down, the backend can reconcile by listing the
  `results/` prefix.
- Any additional consumer (search indexer, analytics, export job) reads the
  same object — results are never re-fanned out as big SQS payloads.

**Schema (v1) — S3 results JSON:**

```jsonc
{
  "schema_version": 1,
  "job_id": "4f2e9a1c-7b8d-4e3a-a1f2-0c9d5e7b3f42",
  "status": "completed",

  "source": {
    "bucket": "anchor-voice-prd-audio-...",
    "key":    "uploads/2026-04-22/session-42.mp3",
    "original_filename": "session-42.mp3"
  },

  "summary": {
    "audio_duration_seconds": 3421.47,
    "num_chunks": 2,
    "source_language": null
  },

  "timing": {
    "started_at":   "2026-04-22T10:14:02.311Z",
    "completed_at": "2026-04-22T10:31:45.827Z",
    "wall_clock_seconds": 1063.52
  },

  "sarvam": {
    "provider": "sarvam",
    "model": "saaras:v3",
    "status": "completed",
    "summary": { "num_segments": 287, "num_speakers": 2 },
    "segments": [
      {
        "segment_index": 0,
        "chunk_index": 0,
        "speaker_id": 0,
        "start_time": 0.000,
        "end_time": 8.420,
        "transcription":          "haan I I I feel like mood bahut low hai",
        "translation":            "yes I feel like mood is very low",
        "normalized_transcript":  "हाँ, I feel like mood बहुत low है।",
        "normalized_translation": "Yes, I feel like my mood has been very low.",
        "confidence": 0.942
      }
      // … one entry per Sarvam merged speaker turn
    ],
    "postprocess": {
      "model": "claude-sonnet-4-6",
      "glossary_corrections": [
        { "heard": "cat distributing", "corrected": "catastrophising" }
      ]
    }
  },
  "scribe_v2": {
    "provider": "elevenlabs",
    "model": "scribe_v2",
    "status": "completed",
    "summary": { "num_segments": 301, "num_speakers": 2 },
    "segments": [
      // same segment shape; translation/normalisation comes from Claude
    ],
    "postprocess": {
      "model": "claude-sonnet-4-6",
      "translation_source": "claude",
      "script_restoration": true
    }
  }
}
```

Translation is always **English**, inlined per segment under the
`translation` key. Segments whose batch failed carry `"translation": ""`.

On **failure**, no results file is written — the failure event (Stage 7)
carries the error message inline.

Objects are written with `ContentType: application/json; charset=utf-8` and
`ServerSideEncryption: AES256`.

---

### Stage 8 — Completion event (claim-check pointer)

**File:** `events.py`

After the results JSON is written, the worker publishes **one SQS message**
to the `JOB_EVENTS_QUEUE_URL` queue — either `job.completed` with a pointer
into the results bucket, or `job.failed` with an inline error message.
Downstream services (backend API, notifier, analytics) consume this queue;
there is no database to poll.

**Publish is best-effort.** The S3 results object is the durable record. A
publish failure is logged and never fails the job — consumers that miss an
event can reconcile by listing the `results/` prefix in the processed
bucket.

**FIFO-aware**: if the queue URL ends with `.fifo`, the publisher attaches
`MessageGroupId=job_id` and `MessageDeduplicationId=job_id:status`, so a
retried publish is deduped by SQS automatically.

**Schema:**

```jsonc
// job.completed — pointer only; full results in S3.
{
  "event": "job.completed",
  "job_id": "4f2e9a1c-7b8d-4e3a-a1f2-0c9d5e7b3f42",
  "status": "completed",
  "source": {
    "bucket": "anchor-voice-prd-audio-...",
    "key":    "uploads/2026-04-22/session-42.mp3",
    "original_filename": "session-42.mp3"
  },
  "results": {
    "bucket":     "anchor-voice-prd-audio-...",
    "key":        "results/4f2e9a1c-7b8d-4e3a-a1f2-0c9d5e7b3f42.json",
    "size_bytes": 312847,
    "etag":       "a1b2c3d4…"
  },
  "summary": {
    "audio_duration_seconds": 3421.47,
    "num_chunks": 2
  },
  "completed_at": "2026-04-22T10:31:45+00:00"
}

// job.failed — no results file exists; error inline.
{
  "event": "job.failed",
  "job_id": "4f2e9a1c-7b8d-4e3a-a1f2-0c9d5e7b3f42",
  "status": "failed",
  "source": {
    "bucket": "anchor-voice-prd-audio-...",
    "key":    "uploads/2026-04-22/session-42.mp3",
    "original_filename": "session-42.mp3"
  },
  "error_message": "Translation failure rate 12.3% for en exceeds threshold 5.0% (35/284 segments empty)",
  "failed_at": "2026-04-22T10:22:14+00:00"
}
```

**Consumer sketch:**

```python
import boto3, json

sqs = boto3.client("sqs")
s3  = boto3.client("s3")

resp = sqs.receive_message(QueueUrl=EVENTS_URL, MaxNumberOfMessages=10, WaitTimeSeconds=20)
for msg in resp.get("Messages", []):
    evt = json.loads(msg["Body"])
    if evt["event"] == "job.completed":
        ptr = evt["results"]
        obj = s3.get_object(Bucket=ptr["bucket"], Key=ptr["key"])
        results = json.loads(obj["Body"].read())
        handle_completed(evt, results)
    else:
        alert(evt)
    sqs.delete_message(QueueUrl=EVENTS_URL, ReceiptHandle=msg["ReceiptHandle"])
```

---

## Rate Limiting

**File:** `rate_limit.py`

All Sarvam API calls (codemix + translate Saaras passes) share a single global sliding-window rate limiter capped at `SARVAM_RPM_LIMIT` (default 100 RPM). Per-chunk concurrency doubles vs the old single-pass design, but the global throttle keeps the absolute Sarvam RPS unchanged.

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
| Sarvam 429 rate limit (either Saaras pass) | Wait 60s explicitly, then retry via `@retry` |
| Sarvam 5xx / network timeout | `@retry` exponential backoff (3 attempts, 10s–60s) |
| Chunk codemix-pass failure (after retries) | Raises immediately — job emits `job.failed` event, no results file written |
| Chunk translate-pass returns 0 segments | Logged at WARNING; chunk's transcription is preserved with empty `translation` per segment; the global coverage check may still fail the job if too many chunks are affected |
| Translation coverage below threshold | Job fails with `Translation failure rate X% for en-IN exceeds threshold Y%` and message redelivers (default `TRANSLATION_FAILURE_THRESHOLD=0.60`) |
| pyannote / diarization | Removed — not used. Sarvam provides per-chunk diarization in both modes |
| Audio over 60 min | Sarvam uses VAD chunking with overlap. Scribe v2 uses full audio up to the configured 10-hour / 3 GB guard, then falls back to chunks. |
| Video file input | ffmpeg extracts audio-only before processing |
| No silence gap found | Force-cut 60s before hard chunk limit |
| SQS visibility timeout (long jobs) | Heartbeat thread extends visibility every 5 min |
| Job stuck / crashed | CloudWatch logs for the ECS task carry the structured error; input SQS message redelivers (up to `maxReceiveCount=3`) then lands in the DLQ |
| Missed `job.completed` event | Reconcile by listing `s3://${S3_PROCESSED_BUCKET}/results/` — every completed job has a JSON object keyed by `<job_id>.json` |

---

## Job Status State Machine

```
pending → downloading → normalizing → provider lanes → completed
                                    │
                                    ├─ Scribe: full audio if allowed → merge → [normalising]
                                    └─ Sarvam: chunking → codemix + translate → merge → [normalising]
                                                                 ↓ (any required stage)
                                                                 failed
```

`normalising` is skipped silently if `POSTPROCESS_ENABLED=false` or the Anthropic API key is absent. A normalisation failure is logged as a warning and that provider continues to `completed` with empty `normalized_transcript` / `normalized_translation` fields. Scribe v2 failure does not fail the job if Sarvam succeeds; Sarvam failure still fails the job.

These states are emitted as **structured log lines** only — there is no
database row tracking them. `completed` corresponds to a results JSON in S3
plus a `job.completed` SQS event; `failed` corresponds to a `job.failed`
SQS event with `error_message` (no results file).

---

## Configuration

Most options are environment variables:

| Variable | Default | Description |
|---|---|---|
| `SARVAM_API_KEY` | — | Sarvam API key (required) |
| `SARVAM_RPM_LIMIT` | 100 | Requests per minute cap shared by both Saaras passes |
| `SARVAM_MAX_CONCURRENT_CHUNKS` | 10 | Parallel chunks (each chunk fans out to 2 Saaras jobs internally) |
| `SARVAM_BATCH_TIMEOUT_S` | 1800 | Max wait for a batch job (30 min) |
| `SARVAM_BATCH_POLL_INTERVAL_S` | 10 | How often to poll batch job status |
| `TARGET_CHUNK_DURATION_S` | 2400 | Target chunk size (40 min) |
| `MAX_CHUNK_DURATION_S` | 2700 | Hard max chunk size (45 min) |
| `OVERLAP_DURATION_S` | 120 | Overlap prefix for speaker stitching (2 min) |
| `SILENCE_SEARCH_WINDOW_S` | 300 | VAD split search window (±5 min) |
| `AWS_REGION` | ap-south-1 | AWS region |
| `S3_PROCESSED_BUCKET` | — | Bucket the worker PUTs the results JSON to. **Required** — the worker exits if unset. |
| `S3_RESULTS_PREFIX` | `results/` | Key prefix inside the bucket. Each completed job writes `<prefix><job_id>.json`. |
| `JOB_EVENTS_QUEUE_URL` | — | SQS queue for `job.completed` / `job.failed` events. Unset = no publish. Supports `.fifo` suffix. |
| `TRANSLATION_FAILURE_THRESHOLD` | 0.60 | Max fraction of substantial segments where the translate-mode pass produced no overlapping output before the job is failed. |
| `SQS_HEARTBEAT_INTERVAL_S` | 300 | How often the worker extends the input SQS message visibility. |
| `SQS_HEARTBEAT_EXTEND_BY_S` | 3600 | How long to extend each heartbeat. |
| `METRICS_NAMESPACE` | `AnchorVoice` | CloudWatch namespace the worker emits EMF metrics under. |
| `METRICS_ENABLED` | `1` | Set to `0` / `false` to silence EMF emissions (useful in local dev). |
| `POSTPROCESS_ENABLED` | `true` | Set to `false` to skip the LLM normalisation step entirely. Pipeline completes normally with empty `normalized_transcript` / `normalized_translation`. |
| `POSTPROCESS_MODEL` | `claude-sonnet-4-6` | Anthropic model for the normalisation pass. `claude-opus-4-6` for higher quality. |
| `POSTPROCESS_MAX_CONCURRENT_PROVIDERS` | `2` | Maximum provider lanes allowed to call Claude at the same time. Default `2` lets Sarvam and Scribe v2 postprocess concurrently while respecting that Anthropic RPM/ITPM/OTPM limits are account and model tier specific. |
| `ANTHROPIC_API_KEY` | — | Direct API key (local dev). In production use `ANTHROPIC_SECRET_NAME` instead. |
| `ANTHROPIC_SECRET_NAME` | `anchor-voice/anthropic-api-key` | Secrets Manager secret name for the Anthropic key. |
| `GLOSSARY_FILE_PATH` | `/app/glossary.json` | Path to the clinical glossary JSON file. Bundled in the image; override to mount a custom file. |
| `AUDIO_PREPROCESSING_MODE` | `speech_enhanced` | `speech_enhanced` applies high/low-pass filtering, speech EQ, dynamic normalisation, and limiting before transcription. Set `standard` for plain 16 kHz mono WAV conversion. |
| `AUDIO_SLOW_DOWN` | `false` | When `speech_enhanced`, optionally applies `atempo=0.94` for batch transcription. |
| `ELEVENLABS_ENABLED` | `true` | Enables a separate top-level `scribe_v2` provider output. Sarvam and Scribe v2 start in parallel after normalization, and each provider starts Claude postprocess as soon as its own STT + merge completes. |
| `ELEVENLABS_MODEL_ID` | `scribe_v2` | ElevenLabs Speech to Text model ID. |
| `ELEVENLABS_API_KEY` | — | Direct API key (local dev). In production use `ELEVENLABS_SECRET_NAME` instead. |
| `ELEVENLABS_SECRET_NAME` | `anchor-voice/elevenlabs-api-key` | Secrets Manager secret name for the ElevenLabs key. |
| `ELEVENLABS_MAX_CONCURRENT_CHUNKS` | `2` | Maximum Scribe v2 chunk requests in flight when full-audio mode exceeds the configured size/duration limits. Keep conservative; retry handles `429` / `5xx`. |
| `ELEVENLABS_LANGUAGE_CODE` | — | Optional ISO language code; empty means Scribe detects language. |
| `ELEVENLABS_NO_VERBATIM` | `true` | Removes filler words, false starts, and non-speech sounds before Claude postprocess. |
| `ELEVENLABS_NUM_SPEAKERS` | — | Optional speaker-count hint. Empty by default so Scribe v2 infers speakers up to its supported maximum. |
| `ELEVENLABS_TEMPERATURE` | `0.0` | Keeps Scribe output deterministic and accuracy-oriented. |
| `ELEVENLABS_REQUEST_TIMEOUT_S` | `1800` | Synchronous Scribe v2 request timeout in seconds. |
| `ELEVENLABS_KEYTERMS_FROM_GLOSSARY` | `true` | Sends glossary terms/correction targets as Scribe v2 keyterms, within ElevenLabs documented limits. |
| `ELEVENLABS_MAX_UPLOAD_BYTES` | `3000000000` | Per-request Scribe v2 upload guard. Full audio above this falls back to chunks; individual chunks above this fail Scribe output with a clear error. |
| `ELEVENLABS_MAX_DURATION_S` | `36000` | Per-request Scribe v2 duration guard (10 hours). Full audio above this falls back to chunks; individual chunks above this fail Scribe output with a clear error. |

Target-language configuration is no longer settable — translation is always English, produced by Sarvam Saaras `mode=translate` running in parallel with the codemix transcription pass. The previous `DEFAULT_TARGET_LANGUAGES` env var (and the per-upload `Target-Languages` S3 metadata override) are removed.

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
`Language=en-IN` (always English; the dimension is preserved for backward
compatibility with dashboards that filter on it).

| Metric | Unit | Emitted when | Dimensions |
|---|---|---|---|
| `JobCompleted` | Count | Pipeline finishes successfully | `Service` |
| `JobFailed` | Count | Pipeline raises (emitted from the failure handler before the `job.failed` SQS event) | `Service` |
| `JobDurationSeconds` | Seconds | Both outcomes (wall-clock from `process_job` entry) | `Service` |
| `AudioDurationSeconds` | Seconds | Both outcomes (0 if failed before `ffprobe`) | `Service` |
| `SegmentsProcessed` | Count | Both outcomes | `Service` |
| `SpeakersDetected` | Count | Both outcomes | `Service` |
| `ChunksProcessed` | Count | Both outcomes | `Service` |
| `TranslationSegments` | Count | After Stage 5 coverage check (count of non-empty source segments) | `Service`, `Language` |
| `TranslationEmptySegments` | Count | After Stage 5 (segments where the translate-mode pass produced no overlapping output) | `Service`, `Language` |
| `TranslationEmptyRate` | Percent | After Stage 5 | `Service`, `Language` |

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

No database is provisioned — results are persisted as JSON objects in the same S3 bucket used for uploads (different prefix), so the only data plane the worker talks to is S3 + SQS + Sarvam.

Only a free S3 Gateway endpoint is used (no Interface endpoints).

---

## Local Development

```bash
# 1. Copy and fill env vars
cp .env .env.local
# Fill in: SARVAM_API_KEY

# 2. Install dependencies
cd worker && uv sync && cd ..

# 3. Run pipeline locally (no DB, no AWS)
make run-local f=audio.mp3
# or:
uv run python scripts/run_local.py audio.mp3
```

> **Note on `scripts/`** — `scripts/run_local.py` and
> `scripts/create_dashboard.sh` are **developer tools** that live at the repo
> root, not inside `worker/`. They are deliberately *not* copied into the
> Docker image (which has build context `worker/`).

`scripts/run_local.py` writes two sidecar files next to the input audio:

| File | Contents |
|---|---|
| `<name>_results.json` | Same schema the worker PUTs to S3 per job (see Stage 6) |
| `<name>_transcript.txt` | Speaker-labelled transcript with `[MM:SS – MM:SS]` stamps plus the English translation |

## AWS Setup

End-to-end deploy is scripted — `scripts/deploy.sh` is idempotent and phase-addressable. One command stands up every resource below; re-running only touches what's changed.

```bash
export SARVAM_API_KEY='sk_...'
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
| S3 bucket | `${NS}-audio-${ACCOUNT}-${REGION}` | Audio uploads (`uploads/` prefix) and worker-written results (`results/` prefix); SSE-S3, public access blocked |
| SQS FIFO — input | `${NS}-transcription-jobs.fifo` | Job queue; `VisibilityTimeout=900`, `maxReceiveCount=3`, content dedup |
| SQS FIFO — DLQ | `${NS}-transcription-jobs-dlq.fifo` | Poison-message parking |
| SQS FIFO — events | `${NS}-job-events.fifo` | `job.completed` (pointer) / `job.failed` (error) fan-out |
| Lambda | `${NS}-job-dispatcher` | SQS-triggered ECS `RunTask` dispatcher with `ReportBatchItemFailures` |
| ECS cluster + task def | `${NS}` / `${NS}-worker` | Fargate 2 vCPU / 8 GB (tune in `scripts/deploy.sh`) |
| ECR | `${APP}/worker` | Worker Docker images, scan-on-push |
| Secrets Manager | `${APP}/${ENV}/sarvam-api-key` | Runtime credentials fetched by worker |
| CloudWatch log groups | `/ecs/${NS}-worker`, `/aws/lambda/${LAMBDA_NAME}` | 30-day retention |
| CloudWatch dashboard | `${NS}` | EMF metrics + SQS/Lambda signals (see Observability) |
| VPC | Default VPC | Public subnets for ECS (`assignPublicIp=ENABLED`) |
| Security groups | `${NS}-ecs` | ECS task SG (egress only — all data plane is S3 + SQS + Sarvam) |

Networking: ECS tasks use `assignPublicIp=ENABLED` in public subnets — no NAT gateway, no RDS, no private subnets required.

Send a test job:

```bash
make send-test f=s3://${S3_BUCKET}/uploads/rec02.m4a
aws logs tail /ecs/${NS}-worker --region ${AWS_REGION} --follow
```

---

## Data Privacy (Medical Use)

- Audio is sent to Sarvam. When `ELEVENLABS_ENABLED=true`, audio chunks are also sent to ElevenLabs Scribe v2.
- **Transcript text is sent to the Anthropic API** during the LLM normalisation step. Disable with `POSTPROCESS_ENABLED=false` if patient data must not leave AWS. Anthropic's API processes only text (no audio). Review Anthropic's data processing agreement and HIPAA eligibility before production medical use.
- Speaker stitching uses only text similarity (rapidfuzz) — no embeddings or external model
- S3 buckets are private with `BlockPublicAcls=true` and SSE-S3 encryption at rest; results JSONs are written with `ServerSideEncryption: AES256`
- All AWS services used are HIPAA-eligible (requires BAA with AWS)
- Sarvam data processing agreement required before production medical use
- ElevenLabs data processing agreement / BAA review required before production medical use when Scribe v2 is enabled.
