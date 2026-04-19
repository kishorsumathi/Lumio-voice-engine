"""Pipeline configuration loaded from environment variables + AWS Secrets Manager."""
import json
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
DEFAULT_TARGET_LANGUAGES: list[str] = [
    x.strip()
    for x in os.getenv("DEFAULT_TARGET_LANGUAGES", "en").split(",")
    if x.strip()
]
# Fraction of segments (0.0–1.0) that may come back with empty translated_text
# per language before the job is marked failed instead of completed. Covers the
# case where a whole batch throws — translation.py stores "" for those segments
# so they must be surfaced at the job level, not silently dropped.
TRANSLATION_FAILURE_THRESHOLD: float = float(
    os.getenv("TRANSLATION_FAILURE_THRESHOLD", "0.05")
)

# ── AWS ───────────────────────────────────────────────────────────────────────
AWS_REGION: str = os.getenv("AWS_REGION", "ap-south-1")
S3_PROCESSED_BUCKET: str = os.getenv("S3_PROCESSED_BUCKET", "")

# Completion-event queue — the worker publishes one SQS message per job
# (status=completed or failed) so downstream services (API backend, frontend,
# notifications) don't have to poll RDS. Leave unset to skip publishing.
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


def _get_secret_json(secret_name: str) -> dict:
    """Fetch a JSON secret from AWS Secrets Manager."""
    return json.loads(_get_secret(secret_name))


_sarvam_api_key: str | None = None
_db_url: str | None = None


def get_sarvam_api_key() -> str:
    global _sarvam_api_key
    if _sarvam_api_key is None:
        secret_name = os.getenv("SARVAM_SECRET_NAME", "anchor-voice/sarvam-api-key")
        # Allow direct env var for local development
        _sarvam_api_key = os.getenv("SARVAM_API_KEY") or _get_secret(secret_name).strip()
    return _sarvam_api_key


def get_db_url() -> str:
    global _db_url
    if _db_url is None:
        # Allow direct env var for local development
        direct = os.getenv("DATABASE_URL")
        if direct:
            _db_url = direct
        else:
            secret_name = os.getenv("RDS_SECRET_NAME", "anchor-voice/rds-credentials")
            creds = _get_secret_json(secret_name)
            host = creds["host"]
            port = creds.get("port", 5432)
            dbname = creds.get("dbname", "anchorvoice")
            user = creds["username"]
            password = creds["password"]
            _db_url = f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{dbname}"
    return _db_url


