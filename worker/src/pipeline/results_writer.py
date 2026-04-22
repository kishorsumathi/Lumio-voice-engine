"""
Results JSON writer (claim-check pattern).

The worker persists nothing in a database. Every finished job is serialized
to a single JSON object and PUT to S3; the SQS completion event carries only
a small pointer (bucket + key + summary) so the backend reads results with
one S3 GET.

Why claim-check:
  - SQS body limit is 1 MiB; long multi-language transcripts can blow past
    it. Pointers are ~0.5 KiB and never vary with audio length.
  - S3 gives durable, inspectable, replayable results — the backend can
    re-read on redelivery without ambiguity.
  - Any downstream consumer (backend, search indexer, analytics) reads the
    same object; no re-fanning of big payloads over the queue.

Key layout: `<S3_RESULTS_PREFIX>/<job_id>.json` (default `results/<uuid>.json`).
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import boto3
from botocore.exceptions import ClientError

from .config import AWS_REGION, S3_PROCESSED_BUCKET, S3_RESULTS_PREFIX
from .merger import MergedSegment
from .translation import TranslatedSegment

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

_s3_client = None


def _s3():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3", region_name=AWS_REGION)
    return _s3_client


def _json_default(obj: Any) -> Any:
    if isinstance(obj, uuid.UUID):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"Not JSON-serializable: {type(obj).__name__}")


@dataclass
class ResultsLocation:
    """Pointer returned by `write_results` — carried on the SQS event."""
    bucket: str
    key: str
    size_bytes: int
    etag: str


def build_results_document(
    *,
    job_id: uuid.UUID,
    source_bucket: str,
    source_key: str,
    original_filename: str | None,
    audio_duration_seconds: float,
    num_chunks: int,
    num_speakers: int,
    source_language: str | None,
    merged: list[MergedSegment],
    english_translation: list[TranslatedSegment],
    started_at: datetime,
    completed_at: datetime,
) -> dict:
    """
    Assemble the results JSON document (returned as a Python dict, not serialized).

    Schema (v1):
      schema_version, job_id, status
      source { bucket, key, original_filename }
      summary { audio_duration_seconds, num_chunks, num_segments, num_speakers, source_language }
      timing { started_at, completed_at, wall_clock_seconds }
      segments[]: { segment_index, chunk_index, speaker_id, start_time, end_time,
                    transcription, translation, confidence }

    Translation is always English; it is inlined per segment under the key
    `translation`. Segments with a failed translation have `translation: ""`.
    """
    translation_by_index: dict[int, str] = {
        t.segment_index: t.translated_text for t in english_translation
    }

    segments = [
        {
            "segment_index": s.segment_index,
            "chunk_index": s.chunk_index,
            "speaker_id": s.speaker_id,
            "start_time": round(s.start_time, 3),
            "end_time": round(s.end_time, 3),
            "transcription": s.text,
            "translation": translation_by_index.get(s.segment_index, ""),
            "confidence": (
                round(s.confidence, 3) if s.confidence is not None else None
            ),
        }
        for s in merged
    ]

    return {
        "schema_version": SCHEMA_VERSION,
        "job_id": str(job_id),
        "status": "completed",
        "source": {
            "bucket": source_bucket,
            "key": source_key,
            "original_filename": original_filename,
        },
        "summary": {
            "audio_duration_seconds": round(audio_duration_seconds, 2),
            "num_chunks": num_chunks,
            "num_segments": len(merged),
            "num_speakers": num_speakers,
            "source_language": source_language,
        },
        "timing": {
            "started_at": started_at.astimezone(timezone.utc).isoformat(),
            "completed_at": completed_at.astimezone(timezone.utc).isoformat(),
            "wall_clock_seconds": round(
                (completed_at - started_at).total_seconds(), 2
            ),
        },
        "segments": segments,
    }


def write_results(job_id: uuid.UUID, document: dict) -> ResultsLocation:
    """
    PUT the results document to `s3://S3_PROCESSED_BUCKET/<prefix><job_id>.json`.

    Raises if the bucket is not configured or the upload fails — the worker
    treats results persistence as part of a successful job. The SQS pointer
    event is only published after this returns.
    """
    if not S3_PROCESSED_BUCKET:
        raise RuntimeError(
            "S3_PROCESSED_BUCKET is not set — cannot persist results. "
            "Set the env var before starting the worker."
        )

    key = f"{S3_RESULTS_PREFIX}{job_id}.json"
    body = json.dumps(document, default=_json_default, ensure_ascii=False).encode(
        "utf-8"
    )

    # S3 user-metadata for cheap UI listing + pending-upload matching.
    # HeadObject returns these without a body download, so the UI can find
    # "the result for source-key X" with one HEAD per candidate instead of
    # fetching hundreds of KB of transcript. Keys must be <=2 KiB total and
    # S3 forces ASCII; we clip long values and leave optional ones off when
    # they aren't populated.
    source = document.get("source") or {}
    summary = document.get("summary") or {}
    metadata: dict[str, str] = {
        "job-id": str(job_id),
        "source-bucket": str(source.get("bucket") or "")[:256],
        "source-key": str(source.get("key") or "")[:1024],
        "original-filename": str(source.get("original_filename") or "")[:256],
        "audio-duration-seconds": f"{summary.get('audio_duration_seconds', 0):.2f}",
        "num-segments": str(summary.get("num_segments") or 0),
        "num-speakers": str(summary.get("num_speakers") or 0),
        "schema-version": str(document.get("schema_version") or SCHEMA_VERSION),
    }
    # Drop any key whose value ended up empty after the str/clip dance; S3
    # accepts empty values but the UI checks `.get()` truthiness.
    metadata = {k: v for k, v in metadata.items() if v}

    try:
        resp = _s3().put_object(
            Bucket=S3_PROCESSED_BUCKET,
            Key=key,
            Body=body,
            ContentType="application/json; charset=utf-8",
            ServerSideEncryption="AES256",
            Metadata=metadata,
        )
    except ClientError as e:
        logger.error("Failed to PUT results s3://%s/%s: %s",
                     S3_PROCESSED_BUCKET, key, e)
        raise

    etag = (resp.get("ETag") or "").strip('"')
    logger.info(
        "Results written: s3://%s/%s (%d bytes, etag=%s)",
        S3_PROCESSED_BUCKET, key, len(body), etag,
    )
    return ResultsLocation(
        bucket=S3_PROCESSED_BUCKET,
        key=key,
        size_bytes=len(body),
        etag=etag,
    )
