"""
Pipeline orchestrator — entry point for ECS Fargate worker.

Receives a job message (from SQS via ECS task environment variables),
orchestrates the full pipeline, and stores results in RDS PostgreSQL.

Pipeline stages (with status transitions):
  pending → downloading → chunking → transcribing → merging → translating → completed
                                                                          ↓ (on any error)
                                                                        failed
"""
import logging
import os
import sys
import tempfile
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

import boto3
import structlog

from .audio import ensure_audio_only, get_duration
from .chunking import chunk_audio
from .config import DEFAULT_TARGET_LANGUAGES, TRANSLATION_FAILURE_THRESHOLD
from .db import create_tables, health_check
from .events import publish_job_event
from .job_status import create_job, mark_failed, store_results, update_status
from .merger import merge
from .metrics import emit_job_outcome, emit_translation_coverage
from .s3 import download_audio
from .transcription import transcribe_all_chunks
from .translation import translate_segments

# ── Structured logging ────────────────────────────────────────────────────────
structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
)
logging.basicConfig(stream=sys.stdout, level=logging.INFO)
logger = structlog.get_logger(__name__)


# ── SQS visibility heartbeat ──────────────────────────────────────────────────

class _SQSHeartbeat:
    """
    Background thread that keeps the SQS message invisible for the life of
    the job. Extends visibility immediately on start (to cover Fargate
    cold-start / short SQS visibility timeouts) and then every
    `_EXTEND_INTERVAL_S` seconds.
    """
    _EXTEND_INTERVAL_S = int(os.environ.get("SQS_HEARTBEAT_INTERVAL_S", "300"))
    _EXTEND_BY_S = int(os.environ.get("SQS_HEARTBEAT_EXTEND_BY_S", "3600"))

    def __init__(self, queue_url: str, receipt_handle: str):
        self._queue_url = queue_url
        self._receipt_handle = receipt_handle
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._sqs = boto3.client("sqs")

    def start(self):
        # Extend once synchronously before the background loop starts so
        # there is no window where the message can become visible.
        self._extend_once()
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._thread.join(timeout=10)

    def _extend_once(self) -> None:
        try:
            self._sqs.change_message_visibility(
                QueueUrl=self._queue_url,
                ReceiptHandle=self._receipt_handle,
                VisibilityTimeout=self._EXTEND_BY_S,
            )
            logger.info("SQS visibility extended", extend_by_s=self._EXTEND_BY_S)
        except Exception as e:
            logger.warning("SQS heartbeat failed", error=str(e))

    def _run(self):
        while not self._stop.wait(timeout=self._EXTEND_INTERVAL_S):
            self._extend_once()


def _delete_sqs_message(queue_url: str | None, receipt_handle: str | None) -> None:
    if not queue_url or not receipt_handle:
        return
    try:
        boto3.client("sqs").delete_message(
            QueueUrl=queue_url,
            ReceiptHandle=receipt_handle,
        )
        logger.info("SQS message deleted")
    except Exception as e:
        logger.warning("Failed to delete SQS message", error=str(e))


# ── Core pipeline ─────────────────────────────────────────────────────────────

def process_job(
    s3_bucket: str,
    s3_key: str,
    target_languages: list[str] | None = None,
    receipt_handle: str | None = None,
    queue_url: str | None = None,
) -> uuid.UUID:
    """
    Run the full transcription pipeline for one audio file.

    Returns the job UUID once complete (or raises on failure).
    """
    original_filename = Path(s3_key).name
    job_id = create_job(s3_bucket, s3_key, original_filename)
    log = logger.bind(job_id=str(job_id), s3_key=s3_key)

    wall_start = time.monotonic()
    duration = 0.0
    num_chunks = 0
    num_segments = 0
    num_speakers = 0

    # Start SQS heartbeat if we have the handles
    heartbeat = None
    if receipt_handle and queue_url:
        heartbeat = _SQSHeartbeat(queue_url, receipt_handle)
        heartbeat.start()

    with tempfile.TemporaryDirectory() as tmp_str:
        work_dir = Path(tmp_str)
        try:
            # ── 1. Download ──────────────────────────────────────────────
            update_status(job_id, "downloading")
            log.info("Downloading audio")
            audio_path = download_audio(s3_bucket, s3_key, work_dir)

            # ── 2. Validate + prepare ────────────────────────────────────
            audio_path = ensure_audio_only(audio_path, work_dir)
            duration = get_duration(audio_path)
            log.info("Audio ready", duration_s=round(duration, 1), filename=audio_path.name)

            # ── 3. Chunk ─────────────────────────────────────────────────
            update_status(job_id, "chunking", audio_duration_seconds=round(duration, 2))
            log.info("Chunking audio")
            chunks = chunk_audio(audio_path, work_dir)
            num_chunks = len(chunks)
            log.info("Chunks created", count=num_chunks)

            # ── 4. Transcribe chunks (parallel) ───────────────────────────
            update_status(job_id, "transcribing", num_chunks=num_chunks)
            log.info("Transcribing chunks", num_chunks=num_chunks)
            transcript_segments = transcribe_all_chunks(chunks)
            log.info("Transcription complete", num_segments=len(transcript_segments))

            # ── 5. Merge + stitch speakers across chunks ──────────────────
            update_status(job_id, "merging")
            merged = merge(chunks, transcript_segments)
            num_segments = len(merged)
            log.info("Merge complete", num_merged=num_segments)

            # ── 6. Translate ──────────────────────────────────────────────
            langs = target_languages or DEFAULT_TARGET_LANGUAGES
            update_status(job_id, "translating")
            log.info("Translating", languages=langs)
            translations = translate_segments(merged, langs)

            # Surface silent translation batch failures. translate_segments
            # stores "" for segments whose batch raised — we compare against
            # the non-empty source to get the real failure rate per language.
            nonempty_src = sum(1 for s in merged if s.text.strip())
            for lang, trans_list in translations.items():
                empty = sum(
                    1 for t in trans_list
                    if not t.translated_text.strip()
                    and merged[t.segment_index].text.strip()
                )
                fail_rate = (empty / nonempty_src) if nonempty_src else 0.0
                log.info(
                    "Translation coverage",
                    language=lang,
                    empty_segments=empty,
                    nonempty_source=nonempty_src,
                    failure_rate=round(fail_rate, 4),
                )
                emit_translation_coverage(
                    language=lang,
                    empty_segments=empty,
                    nonempty_source=nonempty_src,
                )
                if fail_rate > TRANSLATION_FAILURE_THRESHOLD:
                    raise RuntimeError(
                        f"Translation failure rate {fail_rate:.1%} for {lang} "
                        f"exceeds threshold {TRANSLATION_FAILURE_THRESHOLD:.1%} "
                        f"({empty}/{nonempty_src} segments empty)"
                    )

            # ── 7. Persist to RDS ─────────────────────────────────────────
            segments_for_db = [
                {
                    "chunk_index": s.chunk_index,
                    "segment_index": s.segment_index,
                    "speaker_id": s.speaker_id,
                    "start_time": s.start_time,
                    "end_time": s.end_time,
                    "text": s.text,
                    "confidence": s.confidence,
                }
                for s in merged
            ]
            translations_for_db = {
                lang: [
                    {"segment_index": t.segment_index, "translated_text": t.translated_text}
                    for t in trans_list
                ]
                for lang, trans_list in translations.items()
            }
            num_speakers = len(set(s.speaker_id for s in merged))
            num_segments = len(merged)
            store_results(
                job_id,
                segments_for_db,
                translations_for_db,
                num_speakers,
                final_status="completed",
            )

            log.info("Pipeline complete")
            _delete_sqs_message(queue_url, receipt_handle)

            emit_job_outcome(
                status="completed",
                wall_clock_s=time.monotonic() - wall_start,
                audio_duration_s=duration,
                num_segments=num_segments,
                num_speakers=num_speakers,
                num_chunks=num_chunks,
            )

            # Best-effort notify. RDS has already committed; if this fails the
            # consumer can still reconcile from the jobs table.
            publish_job_event(
                "job.completed",
                {
                    "job_id": str(job_id),
                    "status": "completed",
                    "s3_bucket": s3_bucket,
                    "s3_key": s3_key,
                    "original_filename": original_filename,
                    "audio_duration_seconds": round(duration, 2),
                    "num_chunks": num_chunks,
                    "num_segments": num_segments,
                    "num_speakers": num_speakers,
                    "target_languages": langs,
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                },
            )

        except Exception as exc:
            tb = traceback.format_exc()
            log.error("Pipeline failed", error=str(exc), traceback=tb)
            try:
                mark_failed(job_id, tb)
            except Exception as mark_exc:
                log.error("mark_failed itself failed — job row may be stale",
                          error=str(mark_exc))
            emit_job_outcome(
                status="failed",
                wall_clock_s=time.monotonic() - wall_start,
                audio_duration_s=duration,
                num_segments=num_segments,
                num_speakers=num_speakers,
                num_chunks=num_chunks,
            )
            publish_job_event(
                "job.failed",
                {
                    "job_id": str(job_id),
                    "status": "failed",
                    "s3_bucket": s3_bucket,
                    "s3_key": s3_key,
                    "original_filename": original_filename,
                    "error_message": str(exc)[:1000],
                    "failed_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            # Do NOT delete the SQS message on failure — let SQS's visibility
            # timeout + maxReceiveCount drive redelivery / DLQ. The job row is
            # marked `failed` so re-runs will create a fresh row.
            raise
        finally:
            if heartbeat:
                heartbeat.stop()

    return job_id


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    """
    Entry point for ECS Fargate task.

    The Lambda that launches this task injects job parameters as environment
    variables: S3_BUCKET, S3_KEY, TARGET_LANGUAGES, SQS_QUEUE_URL,
    SQS_RECEIPT_HANDLE.
    """
    s3_bucket = os.environ.get("S3_BUCKET")
    s3_key = os.environ.get("S3_KEY")

    if not s3_bucket or not s3_key:
        logger.error("Missing required env vars: S3_BUCKET, S3_KEY")
        sys.exit(1)

    target_languages_raw = os.environ.get("TARGET_LANGUAGES", "")
    target_languages = [x.strip() for x in target_languages_raw.split(",") if x.strip()] or None

    receipt_handle = os.environ.get("SQS_RECEIPT_HANDLE")
    queue_url = os.environ.get("SQS_QUEUE_URL")

    logger.info(
        "Worker starting",
        s3_bucket=s3_bucket,
        s3_key=s3_key,
        target_languages=target_languages,
    )

    if not health_check():
        logger.error("Database health check failed — aborting")
        sys.exit(1)

    # Self-bootstrap the schema. Idempotent (SQLAlchemy create_all is a no-op
    # if every table already exists) and cheap (~100ms). Keeps us out of the
    # "someone forgot to init the DB" failure mode. Does NOT run ALTERs on
    # existing tables — additive schema changes only.
    try:
        create_tables()
    except Exception as e:
        logger.error("Schema bootstrap failed — aborting", error=str(e))
        sys.exit(1)

    try:
        job_id = process_job(
            s3_bucket=s3_bucket,
            s3_key=s3_key,
            target_languages=target_languages,
            receipt_handle=receipt_handle,
            queue_url=queue_url,
        )
        logger.info("Worker finished", job_id=str(job_id))
    except Exception as e:
        logger.error("Worker exited with error", error=str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
