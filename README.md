# Anchor Voice

Event-driven AWS pipeline for transcribing and translating long-form audio — medical sessions, therapy recordings, interviews — using Sarvam Saaras v3 with cross-chunk speaker diarization.

**Lumio Voice** is the product name for the end-user review experience; this repository is **anchor-voice**. The optional Streamlit app is branded **Lumio Voice — pipeline testing (Build)** (see `ui/app.py`).

## Architecture

```
S3 upload → EventBridge → SQS FIFO → Lambda → ECS Fargate
                                                    │
                                        ┌───────────┴────────────────┐
                                        │  Pipeline                  │
                                        │  1. VAD chunking           │
                                        │  2. Transcription ─────────┼──► Sarvam Saaras v3
                                        │  3. Speaker stitching      │
                                        │  4. Batched translation ───┼──► Sarvam Mayura v1
                                        │     + Indic-passthrough    │
                                        │       retry (en-IN)        │
                                        │  5. Store ─────────────────┼──► RDS PostgreSQL
                                        │  6. Publish completion ────┼──► SQS job-events queue
                                        └────────────────────────────┘
```

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
# Fill in: SARVAM_API_KEY, DATABASE_URL

cd worker && uv sync && cd ..
make db-up
make init-db

DATABASE_URL=postgresql://anchorvoice:anchorvoice@localhost:5432/anchorvoice \
  uv run python scripts/run_local.py /path/to/audio.mp3 --languages en
```

## Deploy to AWS

AWS resources are set up manually. The worker runs as an ECS Fargate task triggered by S3 uploads via EventBridge → SQS → Lambda.

```bash
# After creating your ECR repo, RDS, S3 bucket, SQS queue, and ECS cluster manually:

# Store Sarvam API key
aws secretsmanager create-secret \
  --name anchor-voice/sarvam-api-key \
  --secret-string "YOUR_SARVAM_KEY"

# Build and push worker image
make worker-push    # requires ECR repo already created

# Create tables on RDS (idempotent, safe to re-run)
DATABASE_URL='postgresql+psycopg2://USER:PASS@HOST:5432/DBNAME' \
  uv run python scripts/init_db.py
```

See [docs/architecture.md](docs/architecture.md) for the full AWS resource list and wiring.

### Streamlit UI (optional)

A Streamlit app under `ui/` lets you upload audio, monitor job status by polling RDS, review diarized transcription side-by-side with translation, and edit speaker labels in place. It runs as a long-lived ECS Fargate task with a **public IP** (no ALB, no auth by default — intended for small-team / internal use).

```bash
make deploy-ui          # First-time: builds image, creates SG/IAM/task-def/service, prints URL
make deploy-ui-image    # Subsequent code changes: rolling refresh (no AWS resource churn)
make ui-ip              # Print current public URL (IP rotates when the task is replaced)
aws logs tail /ecs/anchor-voice-prd-ui --follow --region ap-south-1
```

What the UI task role has: `s3:PutObject` on `s3://<bucket>/uploads/*` (user uploads), `s3:GetObject` anywhere in the bucket (presigned URLs for audio playback), `secretsmanager:GetSecretValue` on `rds-credentials`, and log-group write perms. Ingress is `0.0.0.0/0:8501`; if you want to lock that down, tighten the SG rule created by `phase_ui` in `scripts/deploy.sh`.

RDS connectivity: `deploy-ui` adds `UI-SG → TCP 5432` on every **active** security group attached to the RDS instance (`anchor-voice-prd-postgres`), not only a group named `anchor-voice-prd-rds` — so manually-created or renamed DB SGs still work. If the UI task and RDS live in **different VPCs**, that rule cannot be added automatically; put the UI in the same VPC as RDS or use VPC peering.

Uploads land at `uploads/{uuid}/{filename}` which EventBridge already routes to the pipeline — no extra wiring needed.

## Project Structure

```
├── lambda/
│   └── handler.py           # SQS → ECS RunTask dispatcher (deploy manually)
├── worker/                  # ECS Fargate container
│   ├── Dockerfile
│   ├── pyproject.toml
│   └── src/pipeline/
│       ├── main.py          # Orchestrator + SQS heartbeat
│       ├── config.py        # All env vars + Secrets Manager
│       ├── audio.py         # Duration detection, format conversion
│       ├── chunking.py      # VAD-based smart splitting with overlap
│       ├── transcription.py # Sarvam batch API, parallel chunks
│       ├── merger.py        # Cross-chunk speaker stitching
│       ├── translation.py   # Batched parallel translation + en-IN Indic-passthrough retry
│       ├── lang_detect.py   # Lingua-based source LID (Hindi vs Marathi) — retry-path fallback only
│       ├── events.py        # Publish job.completed / job.failed to SQS events queue
│       ├── rate_limit.py    # Shared 100 RPM sliding-window limiter
│       ├── models.py        # SQLAlchemy ORM
│       ├── db.py            # Session factory
│       └── job_status.py    # Status state machine + DB writes
├── ui/                      # Streamlit review app (ECS Fargate, public IP)
│   ├── app.py               # Upload, poll RDS, diarized view, speaker-label editor
│   ├── config.py            # DATABASE_URL resolution (env or Secrets Manager)
│   ├── db.py                # Session factory + query helpers
│   ├── models.py            # SQLAlchemy ORM (mirrors worker schema)
│   ├── Dockerfile
│   └── requirements.txt
├── scripts/
│   ├── init_db.py           # Idempotent schema bootstrap (SQLAlchemy create_all)
│   └── run_local.py         # Local pipeline runner (no AWS needed)
├── docs/
│   └── architecture.md      # Full architecture + edge case reference
└── Makefile
```

## Database Schema

Three tables: `jobs`, `segments`, `translations`.

```sql
SELECT id, status, audio_duration_seconds, num_speakers, started_at
FROM jobs ORDER BY started_at DESC;

SELECT speaker_id, start_time, end_time, text
FROM segments WHERE job_id = '...' ORDER BY start_time;

SELECT s.text, t.translated_text
FROM segments s JOIN translations t ON t.segment_id = s.id
WHERE t.target_language = 'en-IN' AND s.job_id = '...';
```

## Key Design Decisions

- **No pyannote / no diarization model** — Sarvam provides per-chunk diarization; overlap text matching stitches speaker IDs across chunks
- **No NAT gateway** — ECS in public subnets saves ~$32/month; RDS in isolated subnets
- **Sarvam-only** — transcription, diarization, and translation all via Sarvam APIs
- **100 RPM shared** — single sliding-window rate limiter across all Sarvam calls
- **Sessions ≤ 60 min** — single chunk, no splitting or stitching needed (covers most medical sessions)
- **Translation batching** — up to 10 segments / 900 chars per Mayura call (delimiter `⟦S⟧`); ~10× fewer API calls than per-segment translation
- **Indic-passthrough repair with Lingua LID** — for `en-IN` targets, segments where Mayura auto-detect returns the Hinglish input unchanged are re-translated with an explicit source code. Byte-level script regex is the entry gate (*should we retry?*); [Lingua](https://github.com/pemistahl/lingua-py) — restricted to the 9 Indian languages it ships models for + English, 0.75 confidence floor — picks the actual Mayura source code from the original text, which disambiguates **Hindi vs Marathi** (both Devanagari) that pure Unicode ranges can't. Kannada, Malayalam, Odia and every other script with a unique Unicode block go through the regex fallback (100% accurate, no lingua needed). See [docs/architecture.md](docs/architecture.md#en-in-indic-passthrough-retry-pass).
- **Completion events via SQS** — on finish (success or failure) the worker publishes one self-contained SQS message (`job.completed` / `job.failed`) to `JOB_EVENTS_QUEUE_URL`. Your API backend / frontend consumes it instead of polling RDS. Schema + consumer sketch in [docs/architecture.md](docs/architecture.md#stage-7--completion-event-fan-out-notification).
