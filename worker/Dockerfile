# ── Stage 1: Build deps ──────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ make curl git \
    && rm -rf /var/lib/apt/lists/*

# Pin uv by version tag so image builds are reproducible.
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv

# Copy manifest + lockfile + source. Lockfile is authoritative in the image.
# CPU-only torch is already pinned via [tool.uv.sources] in pyproject.toml.
COPY pyproject.toml uv.lock ./
COPY src/ ./src/

# UV_PROJECT_ENVIRONMENT forces uv sync to install into /opt/venv instead of
# its default (a .venv adjacent to pyproject.toml), so the runtime stage can
# COPY /opt/venv cleanly.
ENV UV_PROJECT_ENVIRONMENT=/opt/venv
RUN uv venv /opt/venv && \
    uv sync --frozen --no-dev

# Pre-download silero-vad weights so the runtime stage never hits torch.hub
# (no network egress, no git dependency at runtime).
RUN /opt/venv/bin/python -c "import torch; \
torch.hub.load('snakers4/silero-vad', 'silero_vad', \
               force_reload=False, onnx=False, trust_repo=True)"


# ── Stage 2: Runtime ─────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

# ffmpeg provides ffprobe (duration) + ffmpeg (format conversion);
# libsndfile1 is required by soundfile for audio I/O.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg libsndfile1 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 10001 app \
    && useradd  --system --uid 10001 --gid 10001 --home /home/app --create-home app

# Copy the resolved venv from builder (owned by app for hub-cache writes).
COPY --from=builder --chown=app:app /opt/venv /opt/venv
# Silero weights are cached under /home/app/.cache/torch to keep them
# writable for the non-root user (torch may refresh).
COPY --from=builder --chown=app:app /root/.cache/torch /home/app/.cache/torch

WORKDIR /app
COPY --chown=app:app src/ ./src/
# Bundled at build time; override at runtime with GLOSSARY_FILE_PATH env var
# pointing to a volume-mounted or Secrets Manager-fetched file.
COPY --chown=app:app glossary.json ./glossary.json

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH="/app/src" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HOME=/home/app \
    XDG_CACHE_HOME=/home/app/.cache \
    TORCH_HOME=/home/app/.cache/torch

USER app

# ECS injects at runtime:
#   S3_BUCKET, S3_KEY,
#   SQS_QUEUE_URL, SQS_RECEIPT_HANDLE,
#   AWS_REGION, SARVAM_SECRET_NAME, ANTHROPIC_SECRET_NAME,
#   S3_PROCESSED_BUCKET, S3_RESULTS_PREFIX, JOB_EVENTS_QUEUE_URL,
#   POSTPROCESS_ENABLED, POSTPROCESS_MODEL (optional)

ENTRYPOINT ["python", "-m", "pipeline.main"]
