# Anchor Voice

Event-driven AWS pipeline for transcribing and translating long-form audio — medical sessions, therapy recordings, interviews — using Sarvam Saaras v3 with cross-chunk speaker diarization.

**Lumio Voice** is the product name for the end-user review experience; this repository is **anchor-voice**. The optional Streamlit app is branded **Lumio Voice — pipeline testing (Build)** (see `ui/app.py`).

## Architecture

```
S3 upload → EventBridge → SQS FIFO → Lambda → ECS Fargate
                                                    │
                                        ┌───────────┴───────────────────────┐
                                        │  Pipeline                         │
                                        │  1. VAD chunking                  │
                                        │  2. Per-chunk parallel:           │
                                        │       Saaras mode=codemix    ─────┼──► Sarvam Saaras v3
                                        │       Saaras mode=translate  ─────┼──► Sarvam Saaras v3
                                        │       (timestamp-overlap zip      │
                                        │        attaches English to        │
                                        │        each transcription seg)    │
                                        │  3. Cross-chunk speaker stitching │
                                        │  4. Write results JSON ───────────┼──► S3 (results/ prefix)
                                        │  5. Publish pointer event ────────┼──► SQS job-events queue
                                        └───────────────────────────────────┘
```

The worker has **no database**. Each finished job is persisted as a single
JSON object in S3 (claim-check pattern); the SQS completion event carries
only a small pointer to it, so downstream consumers do one S3 GET to fetch
the full results.

Full architecture, pipeline stages, error handling, and edge cases: **[docs/architecture.md](docs/architecture.md)**

## Prerequisites

- `uv` Python package manager (`brew install uv`)
- `ffmpeg` (`brew install ffmpeg`)
- Docker Desktop
- AWS CLI configured (`aws configure`)
- Sarvam API key ([sarvam.ai](https://sarvam.ai))

## Local Development

```bash
cp .env.example .env
# Fill in: SARVAM_API_KEY

cd worker && uv sync && cd ..

make run-local f=/path/to/audio.mp3
```

This writes `<stem>_results.json` (same schema the worker PUTs to S3 per job)
and `<stem>_transcript.txt` (human-readable speaker + timestamp layout)
next to the input audio. No AWS, no DB.

## Deploy to AWS

End-to-end deploy is scripted — `scripts/deploy.sh` is idempotent and
phase-addressable.

```bash
export SARVAM_API_KEY='sk_...'
export AWS_REGION='ap-south-1'     # optional (default ap-south-1)
export ENV='prd'                   # optional — names all resources ${APP}-${ENV}-*
make deploy                        # full stack
make deploy-image                  # rebuild image + new task def revision only
```

See [docs/architecture.md](docs/architecture.md) for the full AWS resource list and wiring.

### Streamlit UI (optional)

A Streamlit app under `ui/` lets you upload audio and review the diarized transcription side-by-side with the English translation. It is fully **S3-backed** — no database — and simply lists `s3://<bucket>/results/<job_id>.json` objects in the sidebar, loading the selected one on demand. After an upload it polls the `results/` prefix (matching `x-amz-meta-source-key`) and switches to the transcript the moment the worker finishes. Runs as a long-lived ECS Fargate task with a **public IP** (no ALB, no auth by default — intended for small-team / internal use).

> Speaker labels are not editable in this build (results files are immutable claim-check objects). If label editing is needed, add a sidecar `results/<job_id>.labels.json` and overlay it in `ui/app.py`.

```bash
make deploy-ui          # First-time: builds image, creates SG/IAM/task-def/service, prints URL
make deploy-ui-image    # Subsequent code changes: rolling refresh (no AWS resource churn)
make ui-ip              # Print current public URL (IP rotates when the task is replaced)
aws logs tail /ecs/anchor-voice-prd-ui --follow --region ap-south-1
```

What the UI task role has: `s3:PutObject` on `s3://<bucket>/uploads/*` (user uploads), `s3:GetObject` anywhere in the bucket (presigned URLs for audio playback + `HeadObject`/`GetObject` on results JSONs), `s3:ListBucket` (to enumerate the `results/` prefix for the sidebar), and log-group write perms. Ingress is `0.0.0.0/0:8501`; if you want to lock that down, tighten the SG rule created by `phase_ui` in `scripts/deploy.sh`.

Uploads land at `uploads/{uuid}/{filename}` which EventBridge already routes to the pipeline — no extra wiring needed.

## Project Structure

```
├── lambda/
│   └── handler.py              # SQS → ECS RunTask dispatcher (deployed by scripts/deploy.sh)
├── worker/                     # ECS Fargate container
│   ├── Dockerfile
│   ├── pyproject.toml
│   └── src/pipeline/
│       ├── main.py             # Orchestrator + SQS heartbeat (no DB)
│       ├── config.py           # All env vars + Secrets Manager
│       ├── audio.py            # Duration, video→audio extract; 16 kHz mono WAV for Sarvam
│       ├── chunking.py         # VAD-based smart splitting with overlap
│       ├── transcription.py    # Dual Saaras pass per chunk (codemix + translate) + overlap zip
│       ├── merger.py           # Cross-chunk speaker stitching
│       ├── results_writer.py   # Serialize + PUT results JSON to S3 (claim-check)
│       ├── events.py           # Publish job.completed (pointer) / job.failed (error) to SQS
│       ├── rate_limit.py       # Shared 100 RPM sliding-window limiter
│       └── metrics.py          # CloudWatch EMF emitter
├── ui/                         # Streamlit review app — S3-backed (no DB)
│   ├── app.py                  # Upload + sidebar + transcript renderer
│   ├── s3_results.py           # List + HEAD + GET helpers for results/ prefix
│   ├── Dockerfile
│   └── requirements.txt
├── scripts/
│   ├── deploy.sh               # Idempotent AWS deploy (phase-addressable)
│   ├── run_local.py            # Local pipeline runner (no AWS, no DB)
│   └── create_dashboard.sh     # Install / update CloudWatch dashboard
├── docs/
│   └── architecture.md         # Full architecture + edge case reference
└── Makefile
```

## Results Schema

One JSON object per completed job, written to
`s3://${S3_PROCESSED_BUCKET}/${S3_RESULTS_PREFIX}<job_id>.json`
(default prefix `results/`). The SQS completion event carries only a
pointer (bucket + key + size + etag) into this object.

```jsonc
{
  "schema_version": 1,
  "job_id": "4f2e9a1c-…",
  "status": "completed",
  "source":  { "bucket": "...", "key": "uploads/…", "original_filename": "…" },
  "summary": { "audio_duration_seconds": 3421.47, "num_chunks": 2, "num_segments": 287, "num_speakers": 2, "source_language": null },
  "timing":  { "started_at": "…", "completed_at": "…", "wall_clock_seconds": 1063.52 },
  "segments": [
    {
      "segment_index": 0, "chunk_index": 0, "speaker_id": 0,
      "start_time": 0.000, "end_time": 8.420,
      "transcription": "Namaste doston, aaj ke episode mein…",
      "translation":   "Hello friends, in today's episode we…",
      "confidence": 0.942
    }
  ]
}
```

Full schema and consumer sketch: [docs/architecture.md](docs/architecture.md#stage-6--results-persistence-s3-claim-check).

## Key Design Decisions

- **No database** — results are stored in S3 (one JSON per job, claim-check); the SQS event carries only a pointer. Backend consumers do one S3 GET per completion event. Missed events are recoverable by listing the `results/` prefix.
- **No pyannote / no diarization model** — Sarvam provides per-chunk diarization; overlap text matching stitches speaker IDs across chunks
- **No NAT gateway** — ECS runs in public subnets with `assignPublicIp=ENABLED`; saves ~$32/month. No RDS, no private subnets.
- **Sarvam-only, single product, two modes** — transcription uses Saaras v3 `mode=codemix`, translation uses Saaras v3 `mode=translate`. No Mayura, no language-detection sidecar (`lingua` removed). Both Saaras passes run in parallel per chunk; their outputs are merged by timestamp overlap (the codemix pass owns the canonical timeline + speaker IDs).
- **Why two Saaras passes instead of Mayura on the transcript** — Saaras `mode=translate` handles dense Hinglish / code-switched audio natively, sidestepping the failure mode where Mayura's `auto`-source detector treated romanized Hindi as English and passed it through unchanged. The whole `translation.py` + `lang_detect.py` retry/repair stack (≈ 700 lines) is gone.
- **100 RPM shared** — single sliding-window rate limiter across all Sarvam calls. The throttle is per-API-call, so the doubled per-chunk concurrency (codemix + translate) doesn't change the absolute Sarvam request rate.
- **Sarvam STT audio** — Every batch upload is **16 kHz, mono, 16-bit WAV (`pcm_s16le`)** per [Sarvam's STT FAQ](https://docs.sarvam.ai/api-reference-docs/speech-to-text/faq). Sessions **≤ 60 min** use one `chunk_000.wav` (no VAD split / stitch — typical medical session). Longer audio: one full-file normalize, **VAD on that master**, then overlapping **`chunk_NNN.wav`** slices via ffmpeg **stream copy** (no per-chunk resample).
- **Claim-check completion events** — on success the SQS event is a pointer to `s3://<processed-bucket>/results/<job_id>.json`; on failure it carries `error_message` inline (no results file is written). Schema and consumer sketch in [docs/architecture.md](docs/architecture.md#stage-7--completion-event-claim-check-pointer).
