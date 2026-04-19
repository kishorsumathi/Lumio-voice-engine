"""S3 download/upload helpers."""
import logging
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# Extensions Sarvam rejects when sent as video/mp4 MIME type
_MP4_REMAP = {".mp4": ".m4a"}


def download_audio(bucket: str, key: str, dest_dir: Path) -> Path:
    """
    Download an audio file from S3 to dest_dir.

    If the file has a .mp4 extension it is saved as .m4a instead —
    Sarvam rejects the video/mp4 MIME type but accepts audio/mp4 (.m4a).
    The underlying container format is identical; only the extension changes.
    """
    s3 = boto3.client("s3")
    original_ext = Path(key).suffix.lower()
    remapped_ext = _MP4_REMAP.get(original_ext, original_ext)
    filename = Path(key).stem + remapped_ext
    local_path = dest_dir / filename

    logger.info("Downloading s3://%s/%s → %s", bucket, key, local_path)
    try:
        s3.download_file(bucket, key, str(local_path))
    except ClientError as e:
        logger.error("S3 download failed: %s", e)
        raise

    logger.info("Downloaded %.1f MB", local_path.stat().st_size / 1_048_576)
    return local_path


def get_object_metadata(bucket: str, key: str) -> dict[str, str]:
    """
    Fetch S3 user-defined metadata (x-amz-meta-*) for an object.

    Returns an empty dict on any failure — caller should fall back to env vars.
    boto3 lowercases metadata keys automatically (S3 headers are case-insensitive).
    """
    s3 = boto3.client("s3")
    try:
        resp = s3.head_object(Bucket=bucket, Key=key)
        return dict(resp.get("Metadata") or {})
    except ClientError as e:
        logger.warning("head_object failed for s3://%s/%s: %s", bucket, key, e)
        return {}


def upload_artifact(local_path: Path, bucket: str, key: str) -> None:
    """Upload a local file to S3 (used for storing processed chunks etc.)."""
    s3 = boto3.client("s3")
    try:
        s3.upload_file(str(local_path), bucket, key)
        logger.info("Uploaded %s → s3://%s/%s", local_path.name, bucket, key)
    except ClientError as e:
        logger.error("S3 upload failed: %s", e)
        raise
