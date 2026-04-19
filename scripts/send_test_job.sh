#!/usr/bin/env bash
#
# Submit a job by sending an SQS message to the input queue.
#
# - Auto-derives bucket/key from an s3://... URI (or pass --bucket / --key).
# - Uses an explicit MessageDeduplicationId so FIFO dedup never silently drops
#   a retry.
# - Shell vars are always braced (zsh-safe).
#
# Usage:
#   ./scripts/send_test_job.sh s3://anchor-voice-prd-audio-.../uploads/rec02.m4a
# or
#   AWS_REGION=... ENV=prd APP=anchor-voice \
#     ./scripts/send_test_job.sh --bucket <b> --key <k>

set -euo pipefail

: "${AWS_REGION:=ap-south-1}"
: "${ENV:=prd}"
: "${APP:=anchor-voice}"
NS="${APP}-${ENV}"
INPUT_QUEUE="${NS}-transcription-jobs.fifo"
LOG_GROUP_WORKER="/ecs/${NS}-worker"

bucket=""
key=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --bucket) bucket="$2"; shift 2 ;;
    --key)    key="$2";    shift 2 ;;
    s3://*)
      # s3://<bucket>/<key>
      path="${1#s3://}"
      bucket="${path%%/*}"
      key="${path#*/}"
      shift
      ;;
    *) echo "unknown arg: $1" >&2; exit 1 ;;
  esac
done

[[ -z "${bucket}" || -z "${key}" || "${bucket}" == "${key}" ]] && {
  echo "Usage: $0 s3://<bucket>/<key>   OR   $0 --bucket <b> --key <k>" >&2
  exit 1
}

INPUT_QUEUE_URL="$(aws sqs get-queue-url --region "${AWS_REGION}" \
  --queue-name "${INPUT_QUEUE}" --query QueueUrl --output text)"

dedup_id="$(date +%s)-${RANDOM}"
body="{\"bucket\":\"${bucket}\",\"key\":\"${key}\",\"ts\":\"$(date -u +%FT%TZ)\"}"

MESSAGE_ID="$(aws sqs send-message --region "${AWS_REGION}" \
  --queue-url "${INPUT_QUEUE_URL}" \
  --message-group-id default \
  --message-deduplication-id "${dedup_id}" \
  --message-body "${body}" \
  --query MessageId --output text)"

echo "Submitted."
echo "  MessageId:       ${MESSAGE_ID}"
echo "  DedupId:         ${dedup_id}"
echo "  Body:            ${body}"
echo
echo "Tail logs:"
echo "  aws logs tail ${LOG_GROUP_WORKER} --region ${AWS_REGION} --follow"
