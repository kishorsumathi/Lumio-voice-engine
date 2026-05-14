# Anchor Voice

Event-driven AWS pipeline for transcribing and translating long-form audio — medical sessions, therapy recordings, interviews — using Sarvam Saaras v3 and ElevenLabs Scribe v2 with cross-chunk speaker diarization.

**Lumio Voice** is the product name for the end-user review experience; this repository is **anchor-voice**. The optional Streamlit app is branded **Lumio Voice — pipeline testing (Build)** (see `ui/app.py`).

## Architecture

```
S3 upload → EventBridge → SQS FIFO → Lambda → ECS Fargate
                                                    │
                                        ┌───────────┴───────────────────────┐
                                        │  Pipeline                         │
                                        │  1. Normalize audio               │
                                        │  2. Provider lanes run in parallel│
                                        │       Sarvam Saaras v3       ─────┼──► codemix + translate
                                        │       (VAD chunks)                │
                                        │       ElevenLabs Scribe v2   ─────┼──► diarized STT
                                        │       (full audio when possible)  │
                                        │  3. Per-provider speaker stitching│
                                        │  4. Claude starts per provider    │
                                        │       as soon as that provider    ├──► Anthropic Claude
                                        │       finishes STT + merge        │    (claude-sonnet-4-6)
                                        │  5. Write provider outputs JSON ──┼──► S3 (results/ prefix)
                                        │  6. Publish pointer event ────────┼──► SQS job-events queue
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
- Anthropic API key ([console.anthropic.com](https://console.anthropic.com)) — for the LLM normalisation step (optional; pipeline runs without it)
- ElevenLabs API key ([elevenlabs.io](https://elevenlabs.io)) — for the separate Scribe v2 provider output (optional; Sarvam still runs without it)

## Local Development

```bash
cp .env.example .env
# Fill in: SARVAM_API_KEY, ELEVENLABS_API_KEY, ANTHROPIC_API_KEY
# ElevenLabs and postprocess are enabled by default; unset keys simply skip that path.

cd worker && uv sync && cd ..

make run-local f=/path/to/audio.mp3
```

This writes `<stem>_results.json` (same schema the worker PUTs to S3 per job)
and `<stem>_transcript.txt` (human-readable speaker + timestamp layout)
next to the input audio. No AWS, no DB.

To run the post-processing UI locally against an existing results JSON:

```bash
cd postprocess-ui
echo "ANTHROPIC_API_KEY=sk-ant-..." > .env
uv sync
uv run streamlit run app.py
```

## Deploy to AWS

End-to-end deploy is scripted — `scripts/deploy.sh` is idempotent and
phase-addressable.

```bash
export SARVAM_API_KEY='sk_...'
export ANTHROPIC_API_KEY='sk-ant-...'   # optional — stored in Secrets Manager; enables LLM normalisation
export POSTPROCESS_ENABLED='true'       # default true; set false to skip LLM normalisation
export POSTPROCESS_MODEL='claude-sonnet-4-6'  # optional — default claude-sonnet-4-6
export ELEVENLABS_ENABLED='true'        # default true — adds top-level scribe_v2 output
export ELEVENLABS_API_KEY='...'         # optional — stored in Secrets Manager when provided
export ELEVENLABS_NO_VERBATIM='true'    # default true — removes fillers/false starts
export ELEVENLABS_NUM_SPEAKERS=''       # optional; empty lets Scribe infer speakers
export AUDIO_PREPROCESSING_MODE='standard'  # or speech_enhanced
export AWS_REGION='ap-south-1'          # optional (default ap-south-1)
export ENV='prd'                        # optional — names all resources ${APP}-${ENV}-*
make deploy                             # full stack
make deploy-image                       # rebuild image + new task def revision only
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
│   ├── glossary.json           # Clinical terms + ASR corrections fed to LLM normalisation
│   └── src/pipeline/
│       ├── main.py             # Orchestrator + SQS heartbeat (no DB)
│       ├── config.py           # All env vars + Secrets Manager
│       ├── audio.py            # Duration, video→audio extract; 16 kHz mono WAV for Sarvam
│       ├── chunking.py         # VAD-based smart splitting with overlap
│       ├── transcription.py    # Dual Saaras pass per chunk (codemix + translate) + overlap zip
│       ├── elevenlabs_transcription.py # ElevenLabs Scribe v2 adapter
│       ├── merger.py           # Cross-chunk speaker stitching
│       ├── postprocess.py      # LLM normalisation pass (Claude) — clinical terms, script restore
│       ├── results_writer.py   # Serialize + PUT results JSON to S3 (claim-check)
│       ├── events.py           # Publish job.completed (pointer) / job.failed (error) to SQS
│       ├── rate_limit.py       # Shared 100 RPM sliding-window limiter
│       └── metrics.py          # CloudWatch EMF emitter
├── postprocess-ui/             # Standalone Streamlit portal for manual post-processing
│   ├── app.py                  # Upload results JSON, run Claude, review diff, download
│   ├── pipeline.py             # Batching, retries, structured output, truncation handling
│   ├── llm.py                  # ChatAnthropic factory
│   ├── prompt.py               # Full clinical editor system prompt
│   └── schema.py               # Pydantic models
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
│   ├── architecture.md         # Full pipeline architecture + edge case reference
│   └── llm_postprocessing.md   # LLM normalisation design, prompt, validation layers
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
  "summary": { "audio_duration_seconds": 3421.47, "num_chunks": 2, "source_language": null },
  "timing":  { "started_at": "…", "completed_at": "…", "wall_clock_seconds": 1063.52 },
  "sarvam": {
    "provider": "sarvam",
    "model": "saaras:v3",
    "status": "completed",
    "summary": { "num_segments": 287, "num_speakers": 2 },
    "segments": [
      {
        "segment_index": 0, "chunk_index": 0, "speaker_id": 0,
        "start_time": 0.000, "end_time": 8.420,
        "transcription":          "haan I I I feel like mood bahut low hai",
        "translation":            "yes I feel like mood is very low",
        "normalized_transcript":  "हाँ, I feel like mood बहुत low है।",
        "normalized_translation": "Yes, I feel like my mood has been very low.",
        "confidence": 0.942
      }
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
    "segments": ["same shape; translation/normalization produced by Claude"]
  }
}
```

`normalized_transcript` and `normalized_translation` are empty strings when
`POSTPROCESS_ENABLED=false` or `ANTHROPIC_API_KEY` is absent. Provider-specific
`postprocess` keys are `null` in those cases. `summary` contains job-level
facts only; `sarvam.summary` and `scribe_v2.summary` carry model-specific
segment and speaker counts.

Full schema and consumer sketch: [docs/architecture.md](docs/architecture.md#stage-6--results-persistence-s3-claim-check).

## Key Design Decisions

- **No database** — results are stored in S3 (one JSON per job, claim-check); the SQS event carries only a pointer. Backend consumers do one S3 GET per completion event. Missed events are recoverable by listing the `results/` prefix.
- **No pyannote / no diarization model** — Sarvam provides per-chunk diarization; overlap text matching stitches speaker IDs across chunks.
- **No NAT gateway** — ECS runs in public subnets with `assignPublicIp=ENABLED`; saves ~$32/month. No RDS, no private subnets.
- **Two independent STT provider outputs** — Sarvam produces the top-level `sarvam` object, and ElevenLabs Scribe v2 produces the top-level `scribe_v2` object. They are not compared or merged together.
- **Provider-lane parallelism** — Sarvam and Scribe v2 start in parallel after normalization. Sarvam uses VAD chunks; Scribe v2 sends the full normalized audio when it fits the configured 3 GB / 10 hour guard, falling back to chunks only when needed. As soon as one provider finishes transcription and merge, its Claude postprocess starts immediately without waiting for the other provider. Claude concurrency is bounded by `POSTPROCESS_MAX_CONCURRENT_PROVIDERS` (default `2`, one slot per provider lane).
- **Sarvam uses one product, two modes** — transcription uses Saaras v3 `mode=codemix`, translation uses Saaras v3 `mode=translate`. No Mayura, no language-detection sidecar. Both passes run in parallel per chunk; outputs are merged by timestamp overlap.
- **LLM normalisation pass (optional)** — Claude runs a clinical editing pass per provider: fixes misheard medical terms, restores romanised Indian-language text to native script (especially for Scribe v2), fixes formatting, removes ASR noise, and produces English translation for Scribe v2. Controlled via `POSTPROCESS_ENABLED` / `ANTHROPIC_SECRET_NAME`. Failures are logged and skipped — they never block job completion. Glossary of clinical terms and ASR corrections lives in `worker/glossary.json`. Full design in [docs/llm_postprocessing.md](docs/llm_postprocessing.md).
- **100 RPM shared** — single sliding-window rate limiter across all Sarvam calls. The throttle is per-API-call, so the doubled per-chunk concurrency (codemix + translate) doesn't change the absolute Sarvam request rate.
- **Sarvam STT audio** — Every batch upload is **16 kHz, mono, 16-bit WAV (`pcm_s16le`)** per [Sarvam's STT FAQ](https://docs.sarvam.ai/api-reference-docs/speech-to-text/faq). Sessions **≤ 60 min** use one `chunk_000.wav`. Longer audio: one full-file normalize, VAD on that master, then overlapping slices via ffmpeg stream copy.
- **Claim-check completion events** — on success the SQS event is a pointer to `s3://<processed-bucket>/results/<job_id>.json`; on failure it carries `error_message` inline. Schema and consumer sketch in [docs/architecture.md](docs/architecture.md).
