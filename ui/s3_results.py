"""
S3-backed results API for the Streamlit UI.

Replaces the old SQLAlchemy / RDS layer. Every completed job is a single JSON
object in `s3://${S3_PROCESSED_BUCKET}/${S3_RESULTS_PREFIX}<job_id>.json`;
the worker attaches `x-amz-meta-*` on the PUT so that listing a few dozen
sessions in the sidebar only costs a cheap ListObjectsV2 + per-object
HeadObject (no body reads).

Conventions expected from the worker (`pipeline.results_writer`):

  Key        : `${S3_RESULTS_PREFIX}<job_id>.json`
  Metadata   : job-id, source-bucket, source-key, original-filename,
               audio-duration-seconds, num-segments, num-speakers,
               schema-version
"""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

AWS_REGION: str = os.getenv("AWS_REGION", "ap-south-1")
S3_BUCKET: str = os.getenv("S3_PROCESSED_BUCKET", "")
S3_RESULTS_PREFIX: str = os.getenv("S3_RESULTS_PREFIX", "results/")

_JOB_ID_RE = re.compile(
    r"(?P<id>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.json$"
)

_s3_client = None


def _s3():
    global _s3_client
    if _s3_client is None:
        _s3_client = boto3.client("s3", region_name=AWS_REGION)
    return _s3_client


@dataclass
class ResultSummary:
    """
    Everything the sidebar needs about one completed job, fetched without
    downloading the segments body. Populated from ListObjectsV2 output + a
    single HeadObject per candidate.
    """
    key: str
    size_bytes: int
    last_modified: datetime
    etag: str
    job_id: str
    original_filename: str | None = None
    audio_duration_seconds: float | None = None
    num_segments: int | None = None
    num_speakers: int | None = None
    source_bucket: str | None = None
    source_key: str | None = None

    @property
    def label(self) -> str:
        name = (self.original_filename or self.job_id or self.key)[:44]
        bits = [name]
        if self.audio_duration_seconds is not None:
            bits.append(f"{self.audio_duration_seconds:.0f}s")
        if self.num_speakers is not None:
            bits.append(f"{self.num_speakers} spk")
        return " · ".join(bits)


def _parse_job_id(key: str) -> str:
    m = _JOB_ID_RE.search(key)
    return m.group("id") if m else key.rsplit("/", 1)[-1].removesuffix(".json")


def _head_summary(key: str, *, size: int, last_modified: datetime, etag: str) -> ResultSummary:
    meta: dict[str, str] = {}
    try:
        resp = _s3().head_object(Bucket=S3_BUCKET, Key=key)
        meta = dict(resp.get("Metadata") or {})
    except ClientError as e:
        logger.warning("HeadObject failed for s3://%s/%s: %s", S3_BUCKET, key, e)

    def _as_float(val: str | None) -> float | None:
        if not val:
            return None
        try:
            return float(val)
        except ValueError:
            return None

    def _as_int(val: str | None) -> int | None:
        if not val:
            return None
        try:
            return int(val)
        except ValueError:
            return None

    return ResultSummary(
        key=key,
        size_bytes=size,
        last_modified=last_modified,
        etag=etag,
        job_id=meta.get("job-id") or _parse_job_id(key),
        original_filename=meta.get("original-filename") or None,
        audio_duration_seconds=_as_float(meta.get("audio-duration-seconds")),
        num_segments=_as_int(meta.get("num-segments")),
        num_speakers=_as_int(meta.get("num-speakers")),
        source_bucket=meta.get("source-bucket") or None,
        source_key=meta.get("source-key") or None,
    )


def list_recent_results(limit: int = 40) -> list[ResultSummary]:
    """
    List the most recently written results JSONs under `${S3_RESULTS_PREFIX}`.

    S3 ListObjectsV2 returns lexicographic order, which is random for UUID
    keys — so we over-fetch, sort by LastModified desc in memory, and HEAD
    the top N. `limit` caps the HEAD fan-out. For a dev tool with < ~1000
    jobs in the bucket this is cheap; past that, add pagination + an index.
    """
    if not S3_BUCKET:
        return []

    paginator = _s3().get_paginator("list_objects_v2")
    scan_cap = max(limit * 5, 200)
    raw: list[tuple[str, int, datetime, str]] = []
    try:
        for page in paginator.paginate(
            Bucket=S3_BUCKET,
            Prefix=S3_RESULTS_PREFIX,
            PaginationConfig={"MaxItems": scan_cap, "PageSize": 100},
        ):
            for obj in page.get("Contents", []) or []:
                key = obj["Key"]
                if not key.endswith(".json"):
                    continue
                raw.append(
                    (
                        key,
                        int(obj.get("Size") or 0),
                        obj["LastModified"],
                        (obj.get("ETag") or "").strip('"'),
                    )
                )
    except ClientError as e:
        logger.error("ListObjectsV2 failed: %s", e)
        raise

    raw.sort(key=lambda t: t[2], reverse=True)
    return [
        _head_summary(key, size=size, last_modified=lm, etag=etag)
        for key, size, lm, etag in raw[:limit]
    ]


def find_result_for_source(source_key: str, *, scan_limit: int = 40) -> ResultSummary | None:
    """
    Resolve a pending upload (`uploads/<uuid>/<name>`) to the results file
    the worker wrote for it. Used by the post-upload "Processing…" poll.

    Scans the newest `scan_limit` results and matches on
    `x-amz-meta-source-key`. HEAD-only, no body reads. Returns None while
    the worker is still processing (or if the user cleared state and the
    upload completed unobserved).
    """
    if not source_key:
        return None
    for summary in list_recent_results(limit=scan_limit):
        if summary.source_key == source_key:
            return summary
    return None


def load_results(key: str) -> dict:
    """Fetch + parse the full results JSON for a selected session."""
    if not S3_BUCKET:
        raise RuntimeError("S3_PROCESSED_BUCKET is not set")
    resp = _s3().get_object(Bucket=S3_BUCKET, Key=key)
    return json.loads(resp["Body"].read().decode("utf-8"))


def presign_audio(bucket: str, key: str, expires: int = 3600) -> str:
    return _s3().generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=expires,
    )
