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
# Fraction of *substantial* source segments (those with text length ≥
# TRANSLATION_MIN_SUBSTANTIAL_CHARS) that may come back with empty
# `translation` after the single-best-match overlap zip before the job is
# marked failed instead of completed. Brief backchannels ("Hmm", "Skirt",
# "Okay") are deliberately excluded from this rate because the zip
# correctly leaves them with empty translation when no translate-pass
# segment maps to them — they're noise, not failures. A non-trivial
# segment with empty translation is the real signal of trouble (model
# drift between codemix and translate modes, or a wholly failed translate
# pass on this audio).
TRANSLATION_FAILURE_THRESHOLD: float = float(
    os.getenv("TRANSLATION_FAILURE_THRESHOLD", "0.60")
)

# Minimum text length (characters, post-strip) for a transcription segment
# to count toward the translation-coverage failure rate. Anything shorter
# is treated as a backchannel/interjection that the translate pass is
# allowed to omit without penalty.
TRANSLATION_MIN_SUBSTANTIAL_CHARS: int = int(
    os.getenv("TRANSLATION_MIN_SUBSTANTIAL_CHARS", "30")
)

# ── Audio preprocessing ───────────────────────────────────────────────────────
# `standard` keeps the existing 16 kHz mono PCM WAV conversion. `speech_enhanced`
# applies light filtering/EQ/dynamic normalisation before transcription.
AUDIO_PREPROCESSING_MODE: str = os.getenv("AUDIO_PREPROCESSING_MODE", "standard").lower()
AUDIO_SLOW_DOWN: bool = os.getenv("AUDIO_SLOW_DOWN", "false").lower() == "true"

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


# ── LLM post-processing ───────────────────────────────────────────────────────
# Set POSTPROCESS_ENABLED=false to skip the normalisation step entirely.
POSTPROCESS_ENABLED: bool = os.getenv("POSTPROCESS_ENABLED", "true").lower() == "true"
POSTPROCESS_MODEL: str = os.getenv("POSTPROCESS_MODEL", "claude-sonnet-4-6")
POSTPROCESS_MAX_CONCURRENT_PROVIDERS: int = max(
    1,
    int(os.getenv("POSTPROCESS_MAX_CONCURRENT_PROVIDERS", "2")),
)

# Path to glossary JSON (see postprocess.py for format). Defaults to
# glossary.json in the working directory (/app inside the container).
GLOSSARY_FILE_PATH: str = os.getenv("GLOSSARY_FILE_PATH", "/app/glossary.json")

# Direct env var for local dev; in production set ANTHROPIC_SECRET_NAME so
# the key is fetched from Secrets Manager (not stored in task env vars).
ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
ANTHROPIC_SECRET_NAME: str = os.getenv("ANTHROPIC_SECRET_NAME", "anchor-voice/anthropic-api-key")

# ── ElevenLabs Scribe v2 ──────────────────────────────────────────────────────
ELEVENLABS_ENABLED: bool = os.getenv("ELEVENLABS_ENABLED", "true").lower() == "true"
ELEVENLABS_MODEL_ID: str = os.getenv("ELEVENLABS_MODEL_ID", "scribe_v2")
ELEVENLABS_API_KEY: str = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_SECRET_NAME: str = os.getenv("ELEVENLABS_SECRET_NAME", "anchor-voice/elevenlabs-api-key")
ELEVENLABS_MAX_CONCURRENT_CHUNKS: int = max(
    1,
    int(os.getenv("ELEVENLABS_MAX_CONCURRENT_CHUNKS", "2")),
)
ELEVENLABS_LANGUAGE_CODE: str = os.getenv("ELEVENLABS_LANGUAGE_CODE", "")
# Scribe v2: no_verbatim=True removes filler words, false starts, and
# non-speech sounds. This is the desired product default.
ELEVENLABS_NO_VERBATIM: bool = os.getenv("ELEVENLABS_NO_VERBATIM", "true").lower() == "true"
ELEVENLABS_NUM_SPEAKERS: int | None = (
    int(os.getenv("ELEVENLABS_NUM_SPEAKERS", ""))
    if os.getenv("ELEVENLABS_NUM_SPEAKERS", "").strip()
    else None
)
ELEVENLABS_TEMPERATURE: float = float(os.getenv("ELEVENLABS_TEMPERATURE", "0.0"))
ELEVENLABS_REQUEST_TIMEOUT_S: int = int(os.getenv("ELEVENLABS_REQUEST_TIMEOUT_S", "1800"))
ELEVENLABS_KEYTERMS_FROM_GLOSSARY: bool = (
    os.getenv("ELEVENLABS_KEYTERMS_FROM_GLOSSARY", "true").lower() == "true"
)
ELEVENLABS_MAX_UPLOAD_BYTES: int = int(
    os.getenv("ELEVENLABS_MAX_UPLOAD_BYTES", str(3_000_000_000))
)
ELEVENLABS_MAX_DURATION_S: float = float(
    os.getenv("ELEVENLABS_MAX_DURATION_S", str(10 * 60 * 60))
)


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
_anthropic_api_key: str | None = None
_elevenlabs_api_key: str | None = None


def get_anthropic_api_key() -> str:
    global _anthropic_api_key
    if _anthropic_api_key is None:
        _anthropic_api_key = ANTHROPIC_API_KEY or _get_secret(ANTHROPIC_SECRET_NAME).strip()
    return _anthropic_api_key


def get_elevenlabs_api_key() -> str:
    global _elevenlabs_api_key
    if _elevenlabs_api_key is None:
        _elevenlabs_api_key = ELEVENLABS_API_KEY or _get_secret(ELEVENLABS_SECRET_NAME).strip()
    return _elevenlabs_api_key


def get_sarvam_api_key() -> str:
    global _sarvam_api_key
    if _sarvam_api_key is None:
        secret_name = os.getenv("SARVAM_SECRET_NAME", "anchor-voice/sarvam-api-key")
        # Allow direct env var for local development
        _sarvam_api_key = os.getenv("SARVAM_API_KEY") or _get_secret(secret_name).strip()
    return _sarvam_api_key
