"""
Job-completion event publisher.

The worker emits exactly one SQS message per job — either `job.completed` or
`job.failed` — containing everything a consumer needs to decide whether to
refresh its UI / fan-out notifications / kick off post-processing, without
hitting the database first.

The queue URL is taken from `JOB_EVENTS_QUEUE_URL`. If unset, publishing is
a no-op (useful for local dev and for tasks that don't yet have the queue
wired up). Publish failures are logged but never raise — the job row in RDS
is the source of truth; the event is a notification, not the record.

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

    Never raises — failures are logged at WARNING. The RDS row is the
    authoritative record; consumers that miss an event can reconcile by
    polling `jobs` periodically.
    """
    if not JOB_EVENTS_QUEUE_URL:
        logger.debug("JOB_EVENTS_QUEUE_URL unset — skipping %s publish", event)
        return

    body = {"event": event, **payload}
    message_body = json.dumps(body, default=_json_default)

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
            "Published %s event: message_id=%s job_id=%s",
            event, resp.get("MessageId"), payload.get("job_id"),
        )
    except Exception as e:
        logger.warning(
            "Failed to publish %s event for job_id=%s: %s",
            event, payload.get("job_id"), e,
        )
