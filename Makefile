.PHONY: help install db-up db-down init-db worker-build worker-push run-local lint clean

REGION         ?= ap-south-1
ACCOUNT_ID     := $(shell aws sts get-caller-identity --query Account --output text 2>/dev/null)
ECR_REPO       := $(ACCOUNT_ID).dkr.ecr.$(REGION).amazonaws.com/anchor-voice-worker
DATABASE_URL   ?= postgresql://anchorvoice:anchorvoice@localhost:5432/anchorvoice

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

# ── Quality ────────────────────────────────────────────────────────────────────

lint:
	cd worker && uv run ruff check src/

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name "*.egg-info" -exec rm -rf {} + 2>/dev/null || true
	@echo "Clean"
