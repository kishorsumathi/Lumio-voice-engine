"""
SQS → ECS RunTask dispatcher.

Triggered by SQS FIFO queue. Each message body is JSON:
  {"bucket": "...", "key": "uploads/file.mp3", "size_bytes": 12345678}

The Lambda dispatches an ECS Fargate task, then hands SQS message lifecycle to
the ECS worker, which extends visibility via a heartbeat and deletes the
message on completion (or a permanent failure).

To keep Lambda from auto-deleting the message on function success, the
dispatched record is always returned in `batchItemFailures`. The event-source
mapping must have `FunctionResponseTypes=["ReportBatchItemFailures"]` enabled
and the SQS queue's VisibilityTimeout must be long enough to cover Fargate
cold-start + the worker's first heartbeat (≥ 6 minutes recommended).

If `run_task` itself fails (capacity / bad task def / networking), the record
stays in `batchItemFailures` with the error logged — SQS will redeliver after
visibility expires, and after `maxReceiveCount` will move it to the DLQ.
"""
import json
import logging
import os

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ECS_CLUSTER = os.environ["ECS_CLUSTER"]
ECS_TASK_DEFINITION = os.environ["ECS_TASK_DEFINITION"]
ECS_CONTAINER_NAME = os.environ["ECS_CONTAINER_NAME"]
ECS_SUBNETS = os.environ["ECS_SUBNETS"].split(",")
ECS_SECURITY_GROUPS = os.environ["ECS_SECURITY_GROUPS"].split(",")
ECS_ASSIGN_PUBLIC_IP = os.environ.get("ECS_ASSIGN_PUBLIC_IP", "ENABLED")
SQS_QUEUE_URL = os.environ["SQS_QUEUE_URL"]
TARGET_LANGUAGES = os.environ.get("TARGET_LANGUAGES", "en")

ecs = boto3.client("ecs")


def _dispatch_one(bucket: str, key: str, receipt_handle: str) -> None:
    """Call ECS RunTask; raise if the API reports any failure or returns 0 tasks."""
    resp = ecs.run_task(
        cluster=ECS_CLUSTER,
        taskDefinition=ECS_TASK_DEFINITION,
        launchType="FARGATE",
        networkConfiguration={
            "awsvpcConfiguration": {
                "subnets": ECS_SUBNETS,
                "securityGroups": ECS_SECURITY_GROUPS,
                "assignPublicIp": ECS_ASSIGN_PUBLIC_IP,
            },
        },
        overrides={
            "containerOverrides": [
                {
                    "name": ECS_CONTAINER_NAME,
                    "environment": [
                        {"name": "S3_BUCKET",          "value": bucket},
                        {"name": "S3_KEY",             "value": key},
                        {"name": "TARGET_LANGUAGES",   "value": TARGET_LANGUAGES},
                        {"name": "SQS_QUEUE_URL",      "value": SQS_QUEUE_URL},
                        {"name": "SQS_RECEIPT_HANDLE", "value": receipt_handle},
                    ],
                }
            ]
        },
    )
    failures = resp.get("failures") or []
    tasks = resp.get("tasks") or []
    if failures or not tasks:
        raise RuntimeError(
            f"RunTask reported failures for s3://{bucket}/{key}: "
            f"failures={failures} tasks={len(tasks)}"
        )


def handler(event, context):
    # Every dispatched record is returned as a batch-item failure so the
    # event-source mapping does NOT delete the SQS message — the ECS worker
    # owns the lifecycle. This requires ReportBatchItemFailures on the ESM.
    batch_item_failures: list[dict] = []

    for record in event.get("Records", []):
        message_id = record.get("messageId", "?")
        try:
            body = json.loads(record["body"])
            bucket = body["bucket"]
            key = body["key"]
            receipt_handle = record["receiptHandle"]

            logger.info("Dispatching ECS task for s3://%s/%s (messageId=%s)",
                        bucket, key, message_id)
            _dispatch_one(bucket, key, receipt_handle)
            logger.info("ECS task launched for key=%s (messageId=%s)", key, message_id)
        except Exception as e:
            # Any dispatch failure: log and leave the message for SQS to retry.
            # Downstream DLQ catches poison messages after maxReceiveCount.
            logger.error("Dispatch failed for messageId=%s: %s", message_id, e)

        # Always include the record — Lambda must NOT delete on our behalf.
        batch_item_failures.append({"itemIdentifier": message_id})

    return {"batchItemFailures": batch_item_failures}
