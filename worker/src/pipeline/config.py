"""Pipeline configuration loaded from environment variables + AWS Secrets Manager."""
import logging
import os

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# ── Chunking ──────────────────────────────────────────────────────────────────
SARVAM_MAX_SINGLE_DURATION_S: int = 3600                                        # 60 min — Sarvam hard limit per request
MAX_CHUNK_DURATION_S: int = int(os.getenv("MAX_CHUNK_DURATION_S", "2700"))      # 45 min — max when splitting
TARGET_CHUNK_DURATION_S: int = int(os.getenv("TARGET_CHUNK_DURATION_S", "2400"))  # 40 min
MIN_SILENCE_DURATION_S: float = float(os.getenv("MIN_SILENCE_DURATION_S", "0.3"))
SILENCE_SEARCH_WINDOW_S: int = int(os.getenv("SILENCE_SEARCH_WINDOW_S", "300"))  # 5 min

# ── Sarvam ────────────────────────────────────────────────────────────────────
SARVAM_RPM_LIMIT: int = int(os.getenv("SARVAM_RPM_LIMIT", "100"))           # requests per minute
SARVAM_BATCH_TIMEOUT_S: int = int(os.getenv("SARVAM_BATCH_TIMEOUT_S", "1800"))
SARVAM_BATCH_POLL_INTERVAL_S: int = int(os.getenv("SARVAM_BATCH_POLL_INTERVAL_S", "10"))
SARVAM_MAX_CONCURRENT_CHUNKS: int = int(os.getenv("SARVAM_MAX_CONCURRENT_CHUNKS", "10"))

# ── Translation ───────────────────────────────────────────────────────────────
# Translation is always English-only and produced by Sarvam Saaras `mode=translate`
# running in parallel with the transcription pass. There is no per-job target
# language; the only knob is the failure threshold below.
#
# Fraction of non-empty source segments (0.0–1.0) that may come back with empty
# `translation` after the timestamp-overlap zip before the job is marked failed
# instead of completed. An empty `translation` means the translate-mode Saaras
# pass produced no segment whose timestamps overlapped the corresponding
# transcription segment — a sign of either model drift between modes or a
# wholly failed translate pass on this audio.
TRANSLATION_FAILURE_THRESHOLD: float = float(
    os.getenv("TRANSLATION_FAILURE_THRESHOLD", "0.05")
)

# ── AWS ───────────────────────────────────────────────────────────────────────
AWS_REGION: str = os.getenv("AWS_REGION", "ap-south-1")

# Bucket where the worker writes the final results JSON. The SQS completion
# event carries a pointer (bucket + key) into this bucket; the backend reads
# from here. Typically the same bucket as uploads, using a `results/` prefix.
S3_PROCESSED_BUCKET: str = os.getenv("S3_PROCESSED_BUCKET", "")
S3_RESULTS_PREFIX: str = os.getenv("S3_RESULTS_PREFIX", "results/")

# Completion-event queue — the worker publishes exactly one SQS message per
# job (status=completed or failed) containing an S3 pointer to the results
# JSON (completed) or the error message (failed). Backend consumes from here.
JOB_EVENTS_QUEUE_URL: str = os.getenv("JOB_EVENTS_QUEUE_URL", "")

# ── Overlap stitching ─────────────────────────────────────────────────────────
OVERLAP_DURATION_S: int = int(os.getenv("OVERLAP_DURATION_S", "120"))  # 2 min

# ── Supported audio extensions ────────────────────────────────────────────────
SUPPORTED_EXTENSIONS: set[str] = {
    ".wav", ".mp3", ".m4a", ".flac", ".ogg", ".aac", ".webm"
}
# .mp4 is handled separately — converted to .m4a before Sarvam upload


def _get_secret(secret_name: str) -> str:
    """Fetch a plain-string secret from AWS Secrets Manager."""
    client = boto3.client("secretsmanager", region_name=AWS_REGION)
    try:
        response = client.get_secret_value(SecretId=secret_name)
        return response.get("SecretString", "")
    except ClientError as e:
        logger.error("Failed to fetch secret %s: %s", secret_name, e)
        raise


_sarvam_api_key: str | None = None


def get_sarvam_api_key() -> str:
    global _sarvam_api_key
    if _sarvam_api_key is None:
        secret_name = os.getenv("SARVAM_SECRET_NAME", "anchor-voice/sarvam-api-key")
        # Allow direct env var for local development
        _sarvam_api_key = os.getenv("SARVAM_API_KEY") or _get_secret(secret_name).strip()
    return _sarvam_api_key
