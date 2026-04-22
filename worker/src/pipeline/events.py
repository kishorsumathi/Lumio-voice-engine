"""
Job-completion event publisher (claim-check pointer on success).

The worker emits exactly one SQS message per job:

  job.completed — carries an S3 pointer (bucket + key + size + etag) to the
                  results JSON the worker just wrote, plus a small summary
                  (duration, segment/speaker counts, source language).

  job.failed    — carries the error message inline; no S3 file is written on
                  failure, so there is no pointer to include.

The queue URL is taken from `JOB_EVENTS_QUEUE_URL`. If unset, publishing is a
no-op (useful for local dev and for tasks that don't yet have the queue
wired up). Publish failures on `job.completed` are logged but never raise —
the results JSON has already been persisted to S3, so the consumer can
reconcile by listing the `results/` prefix if an event is missed.

FIFO queues are supported automatically: if the queue URL ends with `.fifo`,
a `MessageGroupId` (= job_id) and `MessageDeduplicationId` (= job_id +
status) are attached. Deduplication by (job_id, status) means a worker
retry that publishes the same event twice is collapsed by SQS.
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

import boto3

from .config import AWS_REGION, JOB_EVENTS_QUEUE_URL

logger = logging.getLogger(__name__)

_sqs_client = None


def _sqs():
    global _sqs_client
    if _sqs_client is None:
        _sqs_client = boto3.client("sqs", region_name=AWS_REGION)
    return _sqs_client


def _json_default(obj: Any) -> Any:
    """Make UUID, datetime, and Decimal JSON-serializable."""
    if isinstance(obj, uuid.UUID):
        return str(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    raise TypeError(f"Not JSON-serializable: {type(obj).__name__}")


def publish_job_event(event: str, payload: dict) -> None:
    """
    Publish a single `event` (e.g. "job.completed", "job.failed") with `payload`
    to the configured completion queue. No-op if the queue URL isn't set.

    Never raises — failures are logged at WARNING. On `job.completed`, the
    results JSON on S3 is the durable record; a missed event can be recovered
    by listing the `results/` prefix in the processed bucket.
    """
    if not JOB_EVENTS_QUEUE_URL:
        logger.debug("JOB_EVENTS_QUEUE_URL unset — skipping %s publish", event)
        return

    body = {"event": event, **payload}
    message_body = json.dumps(body, default=_json_default, ensure_ascii=False)

    kwargs: dict[str, Any] = {
        "QueueUrl": JOB_EVENTS_QUEUE_URL,
        "MessageBody": message_body,
    }
    if JOB_EVENTS_QUEUE_URL.endswith(".fifo"):
        job_id = str(payload.get("job_id", ""))
        status = str(payload.get("status", event))
        kwargs["MessageGroupId"] = job_id or "default"
        kwargs["MessageDeduplicationId"] = f"{job_id}:{status}"

    try:
        resp = _sqs().send_message(**kwargs)
        logger.info(
            "Published %s event: message_id=%s job_id=%s size_bytes=%d",
            event,
            resp.get("MessageId"),
            payload.get("job_id"),
            len(message_body.encode("utf-8")),
        )
    except Exception as e:
        logger.warning(
            "Failed to publish %s event for job_id=%s: %s",
            event, payload.get("job_id"), e,
        )
