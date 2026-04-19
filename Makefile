.PHONY: help install db-up db-down init-db worker-build worker-push run-local lint clean dashboard deploy deploy-image deploy-eventbridge deploy-ui deploy-ui-image ui-build ui-ip send-test

REGION         ?= ap-south-1
ACCOUNT_ID     := $(shell aws sts get-caller-identity --query Account --output text 2>/dev/null)
ECR_REPO       := $(ACCOUNT_ID).dkr.ecr.$(REGION).amazonaws.com/anchor-voice-worker
DATABASE_URL   ?= postgresql://anchorvoice:anchorvoice@localhost:5432/anchorvoice

DASHBOARD_NAME    ?= anchor-voice
INPUT_QUEUE_NAME  ?= anchor-voice-jobs.fifo
DLQ_NAME          ?= anchor-voice-jobs-dlq.fifo
LAMBDA_NAME       ?= anchor-voice-dispatcher

help:
	@echo ""
	@echo "  Local development"
	@echo "    make install        Install worker dependencies"
	@echo "    make db-up          Start local PostgreSQL (Docker)"
	@echo "    make db-down        Stop local PostgreSQL"
	@echo "    make init-db        Create tables (idempotent, no migrations)"
	@echo "    make run-local f=path/to/audio.mp3"
	@echo ""
	@echo "  Docker / ECR"
	@echo "    make worker-build   Build worker Docker image"
	@echo "    make worker-push    Push image to ECR"
	@echo ""
	@echo "  Deployment (idempotent)"
	@echo "    make deploy         Full deploy (SARVAM_API_KEY + RDS_MASTER_PASSWORD in env)"
	@echo "    make deploy-image   Rebuild image + register new task def revision only"
	@echo "    make deploy-eventbridge  Wire S3 uploads to input SQS via EventBridge"
	@echo "    make deploy-ui      Deploy Streamlit UI (ECR + IAM + SG + ECS service, public IP)"
	@echo "    make deploy-ui-image Rolling refresh of UI image only (code changes)"
	@echo "    make ui-build       Build UI Docker image locally"
	@echo "    make ui-ip          Print the UI's current public URL"
	@echo "    make send-test f=s3://bucket/key"
	@echo ""
	@echo "  Observability"
	@echo "    make dashboard      Install/update CloudWatch dashboard"
	@echo ""
	@echo "  Quality"
	@echo "    make lint           Run ruff linter"
	@echo "    make clean          Remove build artefacts"
	@echo ""

# ── Local dev ──────────────────────────────────────────────────────────────────

install:
	cd worker && uv sync

db-up:
	docker run -d --name anchorvoice-pg -e POSTGRES_USER=anchorvoice -e POSTGRES_PASSWORD=anchorvoice -e POSTGRES_DB=anchorvoice -p 5432:5432 postgres:16-alpine
	@echo "PostgreSQL ready on localhost:5432"

db-down:
	docker rm -f anchorvoice-pg 2>/dev/null || true
	@echo "PostgreSQL container removed"

init-db:
	cd worker && DATABASE_URL=$(DATABASE_URL) \
	  uv run python ../scripts/init_db.py

run-local:
	@test -n "$(f)" || (echo "Usage: make run-local f=path/to/audio.mp3 [langs=en,hi]" && exit 1)
	cd worker && DATABASE_URL=$(DATABASE_URL) \
	  uv run python ../scripts/run_local.py $(f) --languages $(or $(langs),en)

# ── Docker / ECR ───────────────────────────────────────────────────────────────

worker-build:
	docker build -t anchor-voice-worker:latest worker/

worker-push: worker-build
	aws ecr get-login-password --region $(REGION) | \
	  docker login --username AWS --password-stdin $(ECR_REPO)
	docker tag anchor-voice-worker:latest $(ECR_REPO):latest
	docker push $(ECR_REPO):latest
	@echo "Pushed $(ECR_REPO):latest"

# ── Deployment ─────────────────────────────────────────────────────────────────

deploy:
	./scripts/deploy.sh

deploy-image:
	./scripts/deploy.sh image

deploy-eventbridge:
	./scripts/deploy.sh eventbridge

deploy-ui:
	./scripts/deploy.sh ui

deploy-ui-image:
	./scripts/deploy.sh ui-image

ui-build:
	docker build --platform linux/amd64 -t anchor-voice-ui:latest ui/

ui-ip:
	@./scripts/deploy.sh ui-ip

send-test:
	@test -n "$(f)" || (echo "Usage: make send-test f=s3://bucket/key" && exit 1)
	./scripts/send_test_job.sh $(f)

# ── Observability ──────────────────────────────────────────────────────────────

dashboard:
	DASHBOARD_NAME=$(DASHBOARD_NAME) \
	  AWS_REGION=$(REGION) \
	  INPUT_QUEUE_NAME=$(INPUT_QUEUE_NAME) \
	  DLQ_NAME=$(DLQ_NAME) \
	  LAMBDA_NAME=$(LAMBDA_NAME) \
	  ./scripts/create_dashboard.sh

# ── Quality ────────────────────────────────────────────────────────────────────

lint:
	cd worker && uv run ruff check src/

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	@echo "Clean"
