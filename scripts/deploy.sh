#!/usr/bin/env bash
#
# Idempotent end-to-end AWS deployment for anchor-voice.
#
# Every resource is created with "if not exists" semantics — safe to re-run.
# Every shell variable is ALWAYS braced (works identically in bash / zsh —
# avoids the zsh :l modifier trap that bit us during manual deploy).
# Every AWS CLI payload that contains commas, quotes, or nested JSON goes
# through a file:// handoff — no shorthand K=v,K=v.
#
# Usage:
#   export SARVAM_API_KEY='sk_...'
#   export RDS_MASTER_PASSWORD='Strong!Pa55word'
#   export AWS_REGION='ap-south-1'         # optional, default below
#   export ENV='prd'                       # optional, default 'prd'
#   ./scripts/deploy.sh                    # full deploy
#   ./scripts/deploy.sh <phase>            # just one phase (see PHASES below)
#
# Re-running is cheap: existing resources are left alone; only new ones are
# created. Task definition / Lambda code always get a fresh revision.

set -euo pipefail
IFS=$'\n\t'

# ── Required inputs ──────────────────────────────────────────────────────────
: "${SARVAM_API_KEY:?export SARVAM_API_KEY before running}"
: "${RDS_MASTER_PASSWORD:?export RDS_MASTER_PASSWORD before running (strong password, 16+ chars)}"
: "${AWS_REGION:=ap-south-1}"
: "${ENV:=prd}"
: "${APP:=anchor-voice}"

# ── Derived names (industry-standard scheme) ─────────────────────────────────
NS="${APP}-${ENV}"
AWS_ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"

ECR_REPO="${APP}/worker"
S3_BUCKET="${NS}-audio-${AWS_ACCOUNT_ID}-${AWS_REGION}"
DB_NAME="anchorvoice"
DB_USER="anchorvoice"
RDS_ID="${NS}-postgres"
INPUT_QUEUE="${NS}-transcription-jobs.fifo"
DLQ="${NS}-transcription-jobs-dlq.fifo"
EVENTS_QUEUE="${NS}-job-events.fifo"
CLUSTER="${NS}"
TASK_FAMILY="${NS}-worker"
CONTAINER_NAME="${NS}-worker"
LOG_GROUP_WORKER="/ecs/${NS}-worker"
LAMBDA_NAME="${NS}-job-dispatcher"
LOG_GROUP_LAMBDA="/aws/lambda/${LAMBDA_NAME}"
SARVAM_SECRET_NAME="${APP}/${ENV}/sarvam-api-key"
RDS_SECRET_NAME="${APP}/${ENV}/rds-credentials"
EXEC_ROLE="${NS}-ecs-execution-role"
TASK_ROLE="${NS}-worker-task-role"
LAMBDA_ROLE="${NS}-job-dispatcher-role"
DASHBOARD_NAME="${NS}"
IMAGE_URI="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPO}:latest"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
STATE_DIR="${REPO_ROOT}/.deploy-state"
mkdir -p "${STATE_DIR}"

# ── Pretty printing ──────────────────────────────────────────────────────────
log()  { printf '\n\033[1;36m══ %s ══\033[0m\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '\n\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

# Run a command; if it fails, swallow "AlreadyExists"-class errors.
idempotent() {
  local out rc=0
  out="$("$@" 2>&1)" || rc=$?
  if (( rc != 0 )); then
    if echo "${out}" | grep -qE '(AlreadyExists|already exists|BucketAlreadyOwnedByYou|ResourceAlreadyExistsException|EntityAlreadyExists)'; then
      return 0
    fi
    echo "${out}" >&2
    return "${rc}"
  fi
  echo "${out}"
}

PHASE="${1:-all}"

phase_ecr() {
  log "Phase: ECR + image"
  idempotent aws ecr create-repository \
    --region "${AWS_REGION}" \
    --repository-name "${ECR_REPO}" \
    --image-scanning-configuration scanOnPush=true >/dev/null
  ok "ECR repo: ${ECR_REPO}"

  aws ecr get-login-password --region "${AWS_REGION}" | \
    docker login --username AWS --password-stdin \
    "${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com" >/dev/null

  docker build --platform linux/amd64 \
    -t "${ECR_REPO}:latest" \
    "${REPO_ROOT}/worker"
  docker tag  "${ECR_REPO}:latest" "${IMAGE_URI}"
  docker push "${IMAGE_URI}" >/dev/null
  ok "Pushed ${IMAGE_URI}"
}

phase_storage() {
  log "Phase: S3 + Secrets Manager"

  idempotent aws s3api create-bucket \
    --bucket "${S3_BUCKET}" --region "${AWS_REGION}" \
    --create-bucket-configuration "LocationConstraint=${AWS_REGION}" >/dev/null
  aws s3api put-bucket-encryption --bucket "${S3_BUCKET}" --server-side-encryption-configuration \
    '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}' >/dev/null
  aws s3api put-public-access-block --bucket "${S3_BUCKET}" --public-access-block-configuration \
    'BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true' >/dev/null
  ok "S3 bucket: ${S3_BUCKET}"

  # Sarvam secret — update if exists, create otherwise.
  if aws secretsmanager describe-secret --secret-id "${SARVAM_SECRET_NAME}" --region "${AWS_REGION}" >/dev/null 2>&1; then
    aws secretsmanager put-secret-value --region "${AWS_REGION}" \
      --secret-id "${SARVAM_SECRET_NAME}" \
      --secret-string "${SARVAM_API_KEY}" >/dev/null
  else
    aws secretsmanager create-secret --region "${AWS_REGION}" \
      --name "${SARVAM_SECRET_NAME}" \
      --secret-string "${SARVAM_API_KEY}" >/dev/null
  fi
  ok "Secret: ${SARVAM_SECRET_NAME}"

  # RDS secret: placeholder host on first run, replaced by phase_rds.
  if ! aws secretsmanager describe-secret --secret-id "${RDS_SECRET_NAME}" --region "${AWS_REGION}" >/dev/null 2>&1; then
    aws secretsmanager create-secret --region "${AWS_REGION}" \
      --name "${RDS_SECRET_NAME}" \
      --secret-string "{\"username\":\"${DB_USER}\",\"password\":\"${RDS_MASTER_PASSWORD}\",\"host\":\"PLACEHOLDER\",\"port\":5432,\"dbname\":\"${DB_NAME}\"}" >/dev/null
    ok "Secret stub: ${RDS_SECRET_NAME} (host filled in by phase_rds)"
  fi
}

phase_network() {
  log "Phase: VPC lookup + security groups"

  VPC_ID="$(aws ec2 describe-vpcs --region "${AWS_REGION}" \
    --filters Name=isDefault,Values=true --query 'Vpcs[0].VpcId' --output text)"
  [[ "${VPC_ID}" == "None" || -z "${VPC_ID}" ]] && die "No default VPC in ${AWS_REGION}"
  ok "VPC: ${VPC_ID}"

  SUBNETS="$(aws ec2 describe-subnets --region "${AWS_REGION}" \
    --filters Name=vpc-id,Values="${VPC_ID}" Name=default-for-az,Values=true \
    --query 'Subnets[].SubnetId' --output text | tr '\t' ',')"
  [[ -z "${SUBNETS}" ]] && die "No default subnets in VPC ${VPC_ID}"
  ok "Subnets: ${SUBNETS}"

  # ECS SG
  ECS_SG="$(aws ec2 describe-security-groups --region "${AWS_REGION}" \
    --filters Name=vpc-id,Values="${VPC_ID}" Name=group-name,Values="${NS}-ecs" \
    --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || echo 'None')"
  if [[ "${ECS_SG}" == "None" ]]; then
    ECS_SG="$(aws ec2 create-security-group --region "${AWS_REGION}" --vpc-id "${VPC_ID}" \
      --group-name "${NS}-ecs" --description "${NS} ECS tasks" \
      --query GroupId --output text)"
  fi
  ok "ECS SG: ${ECS_SG}"

  # RDS SG
  RDS_SG="$(aws ec2 describe-security-groups --region "${AWS_REGION}" \
    --filters Name=vpc-id,Values="${VPC_ID}" Name=group-name,Values="${NS}-rds" \
    --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || echo 'None')"
  if [[ "${RDS_SG}" == "None" ]]; then
    RDS_SG="$(aws ec2 create-security-group --region "${AWS_REGION}" --vpc-id "${VPC_ID}" \
      --group-name "${NS}-rds" --description "${NS} RDS" \
      --query GroupId --output text)"
  fi
  aws ec2 authorize-security-group-ingress --region "${AWS_REGION}" \
    --group-id "${RDS_SG}" --protocol tcp --port 5432 \
    --source-group "${ECS_SG}" >/dev/null 2>&1 || true
  ok "RDS SG: ${RDS_SG}"

  # Persist for later phases.
  cat > "${STATE_DIR}/network.env" <<EOF
export VPC_ID="${VPC_ID}"
export SUBNETS="${SUBNETS}"
export ECS_SG="${ECS_SG}"
export RDS_SG="${RDS_SG}"
EOF
}

phase_rds() {
  log "Phase: RDS"
  # shellcheck disable=SC1091
  source "${STATE_DIR}/network.env"

  if ! aws rds describe-db-subnet-groups --region "${AWS_REGION}" \
       --db-subnet-group-name "${NS}-db-subnets" >/dev/null 2>&1; then
    # shellcheck disable=SC2086
    aws rds create-db-subnet-group --region "${AWS_REGION}" \
      --db-subnet-group-name "${NS}-db-subnets" \
      --db-subnet-group-description "${NS} db subnets" \
      --subnet-ids $(echo "${SUBNETS}" | tr ',' ' ') >/dev/null
  fi

  if ! aws rds describe-db-instances --region "${AWS_REGION}" \
       --db-instance-identifier "${RDS_ID}" >/dev/null 2>&1; then
    aws rds create-db-instance --region "${AWS_REGION}" \
      --db-instance-identifier "${RDS_ID}" \
      --db-instance-class db.t4g.micro \
      --engine postgres --engine-version 16.3 \
      --master-username "${DB_USER}" \
      --master-user-password "${RDS_MASTER_PASSWORD}" \
      --allocated-storage 20 --storage-type gp3 \
      --db-name "${DB_NAME}" \
      --db-subnet-group-name "${NS}-db-subnets" \
      --vpc-security-group-ids "${RDS_SG}" \
      --backup-retention-period 7 \
      --no-publicly-accessible \
      --storage-encrypted >/dev/null
    warn "RDS creating — ~10 min"
  fi

  aws rds wait db-instance-available --region "${AWS_REGION}" --db-instance-identifier "${RDS_ID}"

  RDS_HOST="$(aws rds describe-db-instances --region "${AWS_REGION}" \
    --db-instance-identifier "${RDS_ID}" \
    --query 'DBInstances[0].Endpoint.Address' --output text)"
  ok "RDS: ${RDS_HOST}"

  aws secretsmanager put-secret-value --region "${AWS_REGION}" \
    --secret-id "${RDS_SECRET_NAME}" \
    --secret-string "{\"username\":\"${DB_USER}\",\"password\":\"${RDS_MASTER_PASSWORD}\",\"host\":\"${RDS_HOST}\",\"port\":5432,\"dbname\":\"${DB_NAME}\"}" >/dev/null
  ok "Secret updated with real RDS host"
}

phase_sqs() {
  log "Phase: SQS queues"

  # DLQ first (input queue's RedrivePolicy needs its ARN).
  if ! aws sqs get-queue-url --region "${AWS_REGION}" --queue-name "${DLQ}" >/dev/null 2>&1; then
    cat > /tmp/deploy-dlq.json <<EOF
{"FifoQueue":"true","ContentBasedDeduplication":"true","MessageRetentionPeriod":"1209600"}
EOF
    aws sqs create-queue --region "${AWS_REGION}" \
      --queue-name "${DLQ}" --attributes file:///tmp/deploy-dlq.json >/dev/null
  fi
  DLQ_URL="$(aws sqs get-queue-url --region "${AWS_REGION}" --queue-name "${DLQ}" --query QueueUrl --output text)"
  DLQ_ARN="$(aws sqs get-queue-attributes --region "${AWS_REGION}" --queue-url "${DLQ_URL}" --attribute-names QueueArn --query Attributes.QueueArn --output text)"
  ok "DLQ: ${DLQ_ARN}"

  # Input queue (uses RedrivePolicy — must go via file:// because of nested JSON).
  if ! aws sqs get-queue-url --region "${AWS_REGION}" --queue-name "${INPUT_QUEUE}" >/dev/null 2>&1; then
    cat > /tmp/deploy-input-q.json <<EOF
{
  "FifoQueue": "true",
  "ContentBasedDeduplication": "true",
  "VisibilityTimeout": "900",
  "MessageRetentionPeriod": "345600",
  "RedrivePolicy": "{\"deadLetterTargetArn\":\"${DLQ_ARN}\",\"maxReceiveCount\":\"3\"}"
}
EOF
    aws sqs create-queue --region "${AWS_REGION}" \
      --queue-name "${INPUT_QUEUE}" --attributes file:///tmp/deploy-input-q.json >/dev/null
  fi
  INPUT_QUEUE_URL="$(aws sqs get-queue-url --region "${AWS_REGION}" --queue-name "${INPUT_QUEUE}" --query QueueUrl --output text)"
  INPUT_QUEUE_ARN="$(aws sqs get-queue-attributes --region "${AWS_REGION}" --queue-url "${INPUT_QUEUE_URL}" --attribute-names QueueArn --query Attributes.QueueArn --output text)"
  ok "Input queue: ${INPUT_QUEUE_ARN}"

  # Events queue.
  if ! aws sqs get-queue-url --region "${AWS_REGION}" --queue-name "${EVENTS_QUEUE}" >/dev/null 2>&1; then
    cat > /tmp/deploy-events-q.json <<EOF
{"FifoQueue":"true","ContentBasedDeduplication":"true","MessageRetentionPeriod":"345600"}
EOF
    aws sqs create-queue --region "${AWS_REGION}" \
      --queue-name "${EVENTS_QUEUE}" --attributes file:///tmp/deploy-events-q.json >/dev/null
  fi
  EVENTS_QUEUE_URL="$(aws sqs get-queue-url --region "${AWS_REGION}" --queue-name "${EVENTS_QUEUE}" --query QueueUrl --output text)"
  EVENTS_QUEUE_ARN="$(aws sqs get-queue-attributes --region "${AWS_REGION}" --queue-url "${EVENTS_QUEUE_URL}" --attribute-names QueueArn --query Attributes.QueueArn --output text)"
  ok "Events queue: ${EVENTS_QUEUE_ARN}"

  cat > "${STATE_DIR}/sqs.env" <<EOF
export DLQ_URL="${DLQ_URL}"
export DLQ_ARN="${DLQ_ARN}"
export INPUT_QUEUE_URL="${INPUT_QUEUE_URL}"
export INPUT_QUEUE_ARN="${INPUT_QUEUE_ARN}"
export EVENTS_QUEUE_URL="${EVENTS_QUEUE_URL}"
export EVENTS_QUEUE_ARN="${EVENTS_QUEUE_ARN}"
EOF
}

phase_logs() {
  log "Phase: CloudWatch log groups"
  for lg in "${LOG_GROUP_WORKER}" "${LOG_GROUP_LAMBDA}"; do
    idempotent aws logs create-log-group --region "${AWS_REGION}" --log-group-name "${lg}" >/dev/null
    aws logs put-retention-policy --region "${AWS_REGION}" --log-group-name "${lg}" --retention-in-days 30 >/dev/null
    ok "Log group: ${lg}"
  done
}

phase_iam() {
  log "Phase: IAM roles"
  # shellcheck disable=SC1091
  source "${STATE_DIR}/sqs.env"

  assume_ecs='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ecs-tasks.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
  assume_lambda='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]}'

  # Execution role
  idempotent aws iam create-role --role-name "${EXEC_ROLE}" \
    --assume-role-policy-document "${assume_ecs}" >/dev/null
  aws iam attach-role-policy --role-name "${EXEC_ROLE}" \
    --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy >/dev/null
  EXEC_ROLE_ARN="$(aws iam get-role --role-name "${EXEC_ROLE}" --query Role.Arn --output text)"
  ok "Exec role: ${EXEC_ROLE_ARN}"

  # Task role
  idempotent aws iam create-role --role-name "${TASK_ROLE}" \
    --assume-role-policy-document "${assume_ecs}" >/dev/null

  cat > /tmp/deploy-task-policy.json <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {"Sid":"ReadAudioFromS3","Effect":"Allow","Action":["s3:GetObject","s3:HeadObject"],"Resource":"arn:aws:s3:::${S3_BUCKET}/*"},
    {"Sid":"ListAudioBucket","Effect":"Allow","Action":["s3:ListBucket"],"Resource":"arn:aws:s3:::${S3_BUCKET}"},
    {"Sid":"ReadSecrets","Effect":"Allow","Action":["secretsmanager:GetSecretValue"],"Resource":["arn:aws:secretsmanager:${AWS_REGION}:${AWS_ACCOUNT_ID}:secret:${SARVAM_SECRET_NAME}-*","arn:aws:secretsmanager:${AWS_REGION}:${AWS_ACCOUNT_ID}:secret:${RDS_SECRET_NAME}-*"]},
    {"Sid":"InputQueueLifecycle","Effect":"Allow","Action":["sqs:ChangeMessageVisibility","sqs:DeleteMessage","sqs:GetQueueAttributes"],"Resource":"${INPUT_QUEUE_ARN}"},
    {"Sid":"PublishCompletionEvents","Effect":"Allow","Action":["sqs:SendMessage"],"Resource":"${EVENTS_QUEUE_ARN}"},
    {"Sid":"WriteLogs","Effect":"Allow","Action":["logs:CreateLogStream","logs:PutLogEvents"],"Resource":"arn:aws:logs:${AWS_REGION}:${AWS_ACCOUNT_ID}:log-group:${LOG_GROUP_WORKER}:*"}
  ]
}
EOF
  python3 -m json.tool /tmp/deploy-task-policy.json >/dev/null
  aws iam put-role-policy --role-name "${TASK_ROLE}" \
    --policy-name inline \
    --policy-document file:///tmp/deploy-task-policy.json
  TASK_ROLE_ARN="$(aws iam get-role --role-name "${TASK_ROLE}" --query Role.Arn --output text)"
  ok "Task role: ${TASK_ROLE_ARN}"

  # Dispatcher role
  idempotent aws iam create-role --role-name "${LAMBDA_ROLE}" \
    --assume-role-policy-document "${assume_lambda}" >/dev/null
  aws iam attach-role-policy --role-name "${LAMBDA_ROLE}" \
    --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole >/dev/null

  cat > /tmp/deploy-lambda-policy.json <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {"Sid":"RunEcsTask","Effect":"Allow","Action":["ecs:RunTask"],"Resource":"*"},
    {"Sid":"PassEcsRoles","Effect":"Allow","Action":["iam:PassRole"],"Resource":["${EXEC_ROLE_ARN}","${TASK_ROLE_ARN}"]},
    {"Sid":"ConsumeInputQueue","Effect":"Allow","Action":["sqs:ReceiveMessage","sqs:DeleteMessage","sqs:GetQueueAttributes","sqs:ChangeMessageVisibility"],"Resource":"${INPUT_QUEUE_ARN}"}
  ]
}
EOF
  python3 -m json.tool /tmp/deploy-lambda-policy.json >/dev/null
  aws iam put-role-policy --role-name "${LAMBDA_ROLE}" \
    --policy-name inline \
    --policy-document file:///tmp/deploy-lambda-policy.json
  LAMBDA_ROLE_ARN="$(aws iam get-role --role-name "${LAMBDA_ROLE}" --query Role.Arn --output text)"
  ok "Dispatcher role: ${LAMBDA_ROLE_ARN}"

  cat > "${STATE_DIR}/iam.env" <<EOF
export EXEC_ROLE_ARN="${EXEC_ROLE_ARN}"
export TASK_ROLE_ARN="${TASK_ROLE_ARN}"
export LAMBDA_ROLE_ARN="${LAMBDA_ROLE_ARN}"
EOF

  # AWS eventually-consistent IAM propagation — roles need a few seconds before
  # services will accept them.
  sleep 8
}

phase_ecs() {
  log "Phase: ECS cluster + task definition"
  # shellcheck disable=SC1091
  source "${STATE_DIR}/iam.env"
  # shellcheck disable=SC1091
  source "${STATE_DIR}/sqs.env"

  idempotent aws ecs create-cluster --region "${AWS_REGION}" --cluster-name "${CLUSTER}" >/dev/null
  ok "Cluster: ${CLUSTER}"

  cat > /tmp/deploy-taskdef.json <<EOF
{
  "family": "${TASK_FAMILY}",
  "networkMode": "awsvpc",
  "requiresCompatibilities": ["FARGATE"],
  "cpu": "2048",
  "memory": "8192",
  "executionRoleArn": "${EXEC_ROLE_ARN}",
  "taskRoleArn": "${TASK_ROLE_ARN}",
  "containerDefinitions": [{
    "name": "${CONTAINER_NAME}",
    "image": "${IMAGE_URI}",
    "essential": true,
    "environment": [
      {"name": "AWS_REGION",                   "value": "${AWS_REGION}"},
      {"name": "S3_PROCESSED_BUCKET",          "value": "${S3_BUCKET}"},
      {"name": "SARVAM_SECRET_NAME",           "value": "${SARVAM_SECRET_NAME}"},
      {"name": "RDS_SECRET_NAME",              "value": "${RDS_SECRET_NAME}"},
      {"name": "JOB_EVENTS_QUEUE_URL",         "value": "${EVENTS_QUEUE_URL}"},
      {"name": "DEFAULT_TARGET_LANGUAGES",     "value": "en"},
      {"name": "SARVAM_RPM_LIMIT",             "value": "100"},
      {"name": "TRANSLATION_FAILURE_THRESHOLD","value": "0.05"},
      {"name": "METRICS_NAMESPACE",            "value": "AnchorVoice"}
    ],
    "logConfiguration": {
      "logDriver": "awslogs",
      "options": {
        "awslogs-group":         "${LOG_GROUP_WORKER}",
        "awslogs-region":        "${AWS_REGION}",
        "awslogs-stream-prefix": "worker"
      }
    }
  }]
}
EOF
  python3 -m json.tool /tmp/deploy-taskdef.json >/dev/null
  REV="$(aws ecs register-task-definition --region "${AWS_REGION}" \
    --cli-input-json file:///tmp/deploy-taskdef.json \
    --query 'taskDefinition.revision' --output text)"
  ok "Task definition: ${TASK_FAMILY}:${REV}"
}

phase_lambda() {
  log "Phase: Lambda dispatcher"
  # shellcheck disable=SC1091
  source "${STATE_DIR}/network.env"
  # shellcheck disable=SC1091
  source "${STATE_DIR}/sqs.env"
  # shellcheck disable=SC1091
  source "${STATE_DIR}/iam.env"

  (cd "${REPO_ROOT}/lambda" && zip -q -r /tmp/deploy-dispatcher.zip handler.py)

  cat > /tmp/deploy-lambda-env.json <<EOF
{
  "Variables": {
    "ECS_CLUSTER":         "${CLUSTER}",
    "ECS_TASK_DEFINITION": "${TASK_FAMILY}",
    "ECS_CONTAINER_NAME":  "${CONTAINER_NAME}",
    "ECS_SUBNETS":         "${SUBNETS}",
    "ECS_SECURITY_GROUPS": "${ECS_SG}",
    "ECS_ASSIGN_PUBLIC_IP":"ENABLED",
    "SQS_QUEUE_URL":       "${INPUT_QUEUE_URL}",
    "TARGET_LANGUAGES":    "en"
  }
}
EOF
  python3 -m json.tool /tmp/deploy-lambda-env.json >/dev/null

  if aws lambda get-function --region "${AWS_REGION}" --function-name "${LAMBDA_NAME}" >/dev/null 2>&1; then
    aws lambda update-function-code --region "${AWS_REGION}" \
      --function-name "${LAMBDA_NAME}" \
      --zip-file fileb:///tmp/deploy-dispatcher.zip >/dev/null
    aws lambda wait function-updated --region "${AWS_REGION}" --function-name "${LAMBDA_NAME}"
    aws lambda update-function-configuration --region "${AWS_REGION}" \
      --function-name "${LAMBDA_NAME}" \
      --environment file:///tmp/deploy-lambda-env.json >/dev/null
    aws lambda wait function-updated --region "${AWS_REGION}" --function-name "${LAMBDA_NAME}"
  else
    aws lambda create-function --region "${AWS_REGION}" \
      --function-name "${LAMBDA_NAME}" \
      --runtime python3.12 \
      --role "${LAMBDA_ROLE_ARN}" \
      --handler handler.handler \
      --timeout 30 --memory-size 256 \
      --zip-file fileb:///tmp/deploy-dispatcher.zip \
      --environment file:///tmp/deploy-lambda-env.json >/dev/null
    aws lambda wait function-active --region "${AWS_REGION}" --function-name "${LAMBDA_NAME}"
  fi
  ok "Lambda: ${LAMBDA_NAME}"

  # Event source mapping — create once. If one exists for this queue, skip.
  EXISTING_ESM="$(aws lambda list-event-source-mappings --region "${AWS_REGION}" \
    --function-name "${LAMBDA_NAME}" \
    --query "EventSourceMappings[?EventSourceArn=='${INPUT_QUEUE_ARN}'].UUID" \
    --output text)"
  if [[ -z "${EXISTING_ESM}" || "${EXISTING_ESM}" == "None" ]]; then
    aws lambda create-event-source-mapping --region "${AWS_REGION}" \
      --function-name "${LAMBDA_NAME}" \
      --event-source-arn "${INPUT_QUEUE_ARN}" \
      --batch-size 1 \
      --function-response-types ReportBatchItemFailures >/dev/null
    ok "Event source mapping created"
  else
    ok "Event source mapping already present (${EXISTING_ESM})"
  fi
}

phase_eventbridge() {
  log "Phase: EventBridge (S3 upload → input SQS)"
  # Prefer the state file (fresh run), else re-derive from AWS so this phase
  # can be run standalone on an already-deployed stack.
  if [[ -f "${STATE_DIR}/sqs.env" ]]; then
    # shellcheck disable=SC1091
    source "${STATE_DIR}/sqs.env"
  else
    warn "No ${STATE_DIR}/sqs.env — querying AWS for queue URL/ARN"
    INPUT_QUEUE_URL="$(aws sqs get-queue-url --region "${AWS_REGION}" \
      --queue-name "${INPUT_QUEUE}" --query QueueUrl --output text)"
    INPUT_QUEUE_ARN="$(aws sqs get-queue-attributes --region "${AWS_REGION}" \
      --queue-url "${INPUT_QUEUE_URL}" --attribute-names QueueArn \
      --query Attributes.QueueArn --output text)"
  fi

  : "${UPLOAD_PREFIX:=uploads/}"
  local RULE_NAME="${NS}-s3-upload"

  # 1. Enable EventBridge notifications on the bucket (idempotent — last-write-wins).
  aws s3api put-bucket-notification-configuration \
    --region "${AWS_REGION}" \
    --bucket "${S3_BUCKET}" \
    --notification-configuration '{"EventBridgeConfiguration":{}}' >/dev/null
  ok "S3 EventBridge notifications enabled on ${S3_BUCKET}"

  # 2. Rule: Object Created for this bucket + key prefix.
  cat > /tmp/deploy-eb-pattern.json <<EOF
{
  "source": ["aws.s3"],
  "detail-type": ["Object Created"],
  "detail": {
    "bucket": { "name": ["${S3_BUCKET}"] },
    "object": { "key": [{ "prefix": "${UPLOAD_PREFIX}" }] }
  }
}
EOF
  python3 -m json.tool /tmp/deploy-eb-pattern.json >/dev/null

  aws events put-rule --region "${AWS_REGION}" \
    --name "${RULE_NAME}" \
    --event-pattern file:///tmp/deploy-eb-pattern.json \
    --state ENABLED \
    --description "Dispatch worker on new s3://${S3_BUCKET}/${UPLOAD_PREFIX}* uploads" >/dev/null
  local RULE_ARN
  RULE_ARN="$(aws events describe-rule --region "${AWS_REGION}" --name "${RULE_NAME}" --query Arn --output text)"
  ok "EventBridge rule: ${RULE_ARN}"

  # 3. SQS queue policy: allow EventBridge (scoped to this rule) to SendMessage.
  cat > /tmp/deploy-queue-policy.json <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "AllowEventBridgeSendMessage",
      "Effect": "Allow",
      "Principal": {"Service": "events.amazonaws.com"},
      "Action": "sqs:SendMessage",
      "Resource": "${INPUT_QUEUE_ARN}",
      "Condition": {
        "ArnEquals": {"aws:SourceArn": "${RULE_ARN}"}
      }
    }
  ]
}
EOF
  python3 -m json.tool /tmp/deploy-queue-policy.json >/dev/null

  # set-queue-attributes wants the Policy as a JSON-encoded string — build via python
  # to avoid fragile shell escaping.
  python3 -c 'import json; p=open("/tmp/deploy-queue-policy.json").read(); print(json.dumps({"Policy": p}))' \
    > /tmp/deploy-queue-attrs.json
  aws sqs set-queue-attributes --region "${AWS_REGION}" \
    --queue-url "${INPUT_QUEUE_URL}" \
    --attributes file:///tmp/deploy-queue-attrs.json >/dev/null
  ok "Input queue policy grants events.amazonaws.com:SendMessage"

  # 4. Target: reshape S3 event → {bucket,key,size_bytes} (matches lambda/handler.py)
  # and push to the FIFO queue with a MessageGroupId matching send_test_job.sh.
  # ContentBasedDeduplication=true on the queue gives us dedup for free.
  cat > /tmp/deploy-eb-targets.json <<EOF
[
  {
    "Id": "input-queue",
    "Arn": "${INPUT_QUEUE_ARN}",
    "SqsParameters": {"MessageGroupId": "default"},
    "InputTransformer": {
      "InputPathsMap": {
        "bucket": "\$.detail.bucket.name",
        "key":    "\$.detail.object.key",
        "size":   "\$.detail.object.size"
      },
      "InputTemplate": "{\"bucket\":<bucket>,\"key\":<key>,\"size_bytes\":<size>}"
    }
  }
]
EOF
  python3 -m json.tool /tmp/deploy-eb-targets.json >/dev/null

  aws events put-targets --region "${AWS_REGION}" \
    --rule "${RULE_NAME}" \
    --targets file:///tmp/deploy-eb-targets.json >/dev/null
  ok "EventBridge target set → ${INPUT_QUEUE_ARN} (group=default, prefix=${UPLOAD_PREFIX})"
}

phase_dashboard() {
  log "Phase: CloudWatch dashboard"
  DASHBOARD_NAME="${DASHBOARD_NAME}" \
  AWS_REGION="${AWS_REGION}" \
  INPUT_QUEUE_NAME="${INPUT_QUEUE}" \
  DLQ_NAME="${DLQ}" \
  LAMBDA_NAME="${LAMBDA_NAME}" \
    "${SCRIPT_DIR}/create_dashboard.sh"
}

phase_summary() {
  log "Done"
  cat <<EOF

  Region:          ${AWS_REGION}
  Environment:     ${ENV}
  Image:           ${IMAGE_URI}
  S3 bucket:       ${S3_BUCKET}
  Input queue:     ${INPUT_QUEUE}
  DLQ:             ${DLQ}
  Events queue:    ${EVENTS_QUEUE}
  Cluster:         ${CLUSTER}
  Task definition: ${TASK_FAMILY}
  Lambda:          ${LAMBDA_NAME}

  Trigger by upload (auto):
    aws s3 cp YOUR_FILE.mp3 s3://${S3_BUCKET}/uploads/  --region ${AWS_REGION}

  Or send a test job by hand:
    ./scripts/send_test_job.sh s3://${S3_BUCKET}/uploads/YOUR_FILE.mp3

  Tail worker logs:
    aws logs tail ${LOG_GROUP_WORKER} --region ${AWS_REGION} --follow

  Dashboard:
    https://${AWS_REGION}.console.aws.amazon.com/cloudwatch/home?region=${AWS_REGION}#dashboards:name=${DASHBOARD_NAME}

EOF
}

# ── Dispatcher ───────────────────────────────────────────────────────────────
case "${PHASE}" in
  ecr)       phase_ecr ;;
  storage)   phase_storage ;;
  network)   phase_network ;;
  rds)       phase_rds ;;
  sqs)       phase_sqs ;;
  logs)      phase_logs ;;
  iam)       phase_iam ;;
  ecs)       phase_ecs ;;
  lambda)    phase_lambda ;;
  eventbridge) phase_eventbridge ;;
  dashboard) phase_dashboard ;;
  image)     phase_ecr ; phase_ecs ;;  # rebuild + register new task def revision
  all)
    phase_network
    phase_storage
    phase_ecr
    phase_rds
    phase_sqs
    phase_logs
    phase_iam
    phase_ecs
    phase_lambda
    phase_eventbridge
    phase_dashboard
    phase_summary
    ;;
  *) die "Unknown phase: ${PHASE}" ;;
esac
