#!/usr/bin/env bash
#
# Install / update the Anchor Voice CloudWatch dashboard.
#
# Usage:
#   DASHBOARD_NAME=anchor-voice \
#   AWS_REGION=ap-south-1 \
#   INPUT_QUEUE_NAME=anchor-voice-jobs.fifo \
#   DLQ_NAME=anchor-voice-jobs-dlq.fifo \
#   LAMBDA_NAME=anchor-voice-dispatcher \
#     ./scripts/create_dashboard.sh
#
# Safe to re-run — put-dashboard is idempotent.
# Requires: aws CLI configured, `jq` installed.
set -euo pipefail

DASHBOARD_NAME="${DASHBOARD_NAME:-anchor-voice}"
AWS_REGION="${AWS_REGION:?AWS_REGION is required (e.g. ap-south-1)}"
INPUT_QUEUE_NAME="${INPUT_QUEUE_NAME:?INPUT_QUEUE_NAME is required}"
DLQ_NAME="${DLQ_NAME:?DLQ_NAME is required}"
LAMBDA_NAME="${LAMBDA_NAME:?LAMBDA_NAME is required}"

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
template="${script_dir}/cloudwatch-dashboard.json"

if [[ ! -f "${template}" ]]; then
  echo "error: dashboard template not found: ${template}" >&2
  exit 1
fi

if ! command -v jq >/dev/null 2>&1; then
  echo "error: jq is required but not installed" >&2
  exit 1
fi

# Substitute placeholders.
rendered="$(sed \
  -e "s|__REGION__|${AWS_REGION}|g" \
  -e "s|__INPUT_QUEUE_NAME__|${INPUT_QUEUE_NAME}|g" \
  -e "s|__DLQ_NAME__|${DLQ_NAME}|g" \
  -e "s|__LAMBDA_NAME__|${LAMBDA_NAME}|g" \
  "${template}")"

# Validate JSON before sending.
echo "${rendered}" | jq -e . >/dev/null

echo "Installing dashboard '${DASHBOARD_NAME}' in ${AWS_REGION}..."
aws cloudwatch put-dashboard \
  --region "${AWS_REGION}" \
  --dashboard-name "${DASHBOARD_NAME}" \
  --dashboard-body "${rendered}"

echo
echo "Done. Open:"
echo "  https://${AWS_REGION}.console.aws.amazon.com/cloudwatch/home?region=${AWS_REGION}#dashboards:name=${DASHBOARD_NAME}"
