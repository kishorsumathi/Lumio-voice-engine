"""
Pipeline orchestrator — entry point for ECS Fargate worker.

Receives a job message (from SQS via ECS task environment variables),
orchestrates the full pipeline, writes the results JSON to S3, and
publishes a pointer event to the completion queue. Nothing is persisted
in a database — the backend consumes from the completion queue and reads
the results object from S3.

Pipeline stages (structured-log state transitions, not persisted):
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
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import boto3
import structlog

from .audio import convert_to_mono_wav, convert_to_speech_enhanced_wav, get_duration
from .chunking import chunk_audio
from .config import (
    AUDIO_PREPROCESSING_MODE,
    AUDIO_SLOW_DOWN,
    ELEVENLABS_ENABLED,
    ELEVENLABS_MODEL_ID,
    GLOSSARY_FILE_PATH,
    POSTPROCESS_ENABLED,
    POSTPROCESS_MAX_CONCURRENT_PROVIDERS,
    POSTPROCESS_MODEL,
    TRANSLATION_FAILURE_THRESHOLD,
    TRANSLATION_MIN_SUBSTANTIAL_CHARS,
    get_anthropic_api_key,
)
from .elevenlabs_transcription import (
    can_transcribe_full_audio_elevenlabs,
    transcribe_audio_elevenlabs,
)
from .postprocess import run_postprocess
from .events import publish_job_event
from .merger import merge
from .metrics import emit_job_outcome, emit_translation_coverage
from .results_writer import build_provider_output, build_results_document, write_results
from .s3 import download_audio
from .transcription import transcribe_all_chunks

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


# ── Provider execution helpers ────────────────────────────────────────────────

def _normalize_audio_for_pipeline(audio_path: Path, work_dir: Path, log) -> Path:
    if AUDIO_PREPROCESSING_MODE == "speech_enhanced":
        log.info("Normalizing audio with speech enhancement", slow_down=AUDIO_SLOW_DOWN)
        return convert_to_speech_enhanced_wav(
            audio_path,
            work_dir,
            slow_down=AUDIO_SLOW_DOWN,
        )
    if AUDIO_PREPROCESSING_MODE != "standard":
        raise RuntimeError(
            "AUDIO_PREPROCESSING_MODE must be 'standard' or 'speech_enhanced' "
            f"(got {AUDIO_PREPROCESSING_MODE!r})"
        )
    log.info("Normalizing audio to 16 kHz mono WAV")
    return convert_to_mono_wav(audio_path, work_dir)


def _check_sarvam_translation_coverage(merged, log) -> None:
    substantial = [
        s for s in merged
        if len(s.text.strip()) >= TRANSLATION_MIN_SUBSTANTIAL_CHARS
    ]
    nonempty_src = len(substantial)
    empty = sum(1 for s in substantial if not s.translation.strip())
    fail_rate = (empty / nonempty_src) if nonempty_src else 0.0

    total_text = sum(1 for s in merged if s.text.strip())
    total_empty = sum(
        1 for s in merged
        if not s.translation.strip() and s.text.strip()
    )
    log.info(
        "Translation coverage",
        provider="sarvam",
        language="en-IN",
        empty_segments=empty,
        nonempty_source=nonempty_src,
        failure_rate=round(fail_rate, 4),
        total_text_segments=total_text,
        total_empty_translations=total_empty,
        min_substantial_chars=TRANSLATION_MIN_SUBSTANTIAL_CHARS,
    )
    emit_translation_coverage(
        language="en-IN",
        empty_segments=empty,
        nonempty_source=nonempty_src,
    )
    if fail_rate > TRANSLATION_FAILURE_THRESHOLD:
        raise RuntimeError(
            f"Translation failure rate {fail_rate:.1%} for en-IN "
            f"exceeds threshold {TRANSLATION_FAILURE_THRESHOLD:.1%} "
            f"({empty}/{nonempty_src} substantial segments empty)"
        )


def _run_sarvam_provider(chunks, log) -> dict:
    start = time.monotonic()
    log.info("provider=sarvam status=transcribing", num_chunks=len(chunks))
    transcript_segments = transcribe_all_chunks(chunks)
    log.info("provider=sarvam transcription complete", num_segments=len(transcript_segments))
    merged = merge(chunks, transcript_segments)
    _check_sarvam_translation_coverage(merged, log)
    return {
        "provider": "sarvam",
        "model": "saaras:v3",
        "merged": merged,
        "metadata": {
            "elapsed_seconds": round(time.monotonic() - start, 2),
            "num_segments": len(merged),
            "num_speakers": len(set(s.speaker_id for s in merged)),
        },
    }


def _run_scribe_provider(chunks, audio_path: Path, audio_duration: float, log) -> dict:
    start = time.monotonic()
    log.info("provider=scribe_v2 status=transcribing", num_chunks=len(chunks))
    result = transcribe_audio_elevenlabs(audio_path, audio_duration, chunks)
    log.info(
        "provider=scribe_v2 transcription complete",
        input_mode=result.input_mode,
        num_segments=len(result.segments),
    )
    merged = merge(result.chunks, result.segments)
    return {
        "provider": "elevenlabs",
        "model": ELEVENLABS_MODEL_ID,
        "merged": merged,
        "metadata": {
            "elapsed_seconds": round(time.monotonic() - start, 2),
            "input_mode": result.input_mode,
            "num_segments": len(merged),
            "num_speakers": len(set(s.speaker_id for s in merged)),
        },
    }


def _run_provider_postprocess(name: str, provider: dict, api_key: str, log) -> dict:
    source_provider = "scribe_v2" if name == "scribe_v2" else "sarvam"
    pp_result = run_postprocess(
        provider["merged"],
        api_key=api_key,
        model=POSTPROCESS_MODEL,
        glossary_path=GLOSSARY_FILE_PATH,
        source_provider=source_provider,
    )
    log.info(
        "Postprocessing complete",
        provider=name,
        normalized_segments=len(pp_result.normalized),
        glossary_corrections=len(pp_result.glossary_corrections),
    )
    return {
        "normalized": pp_result.normalized,
        "postprocess": {
            "model": pp_result.model,
            "glossary_corrections": pp_result.glossary_corrections,
            "translation_source": "claude" if name == "scribe_v2" else "provider_then_claude",
            "script_restoration": name == "scribe_v2",
        },
    }


def _run_provider_pipeline(
    name: str,
    chunks,
    log,
    *,
    audio_path: Path,
    audio_duration: float,
    anthropic_api_key: str,
    postprocess_gate: threading.Semaphore,
) -> dict:
    if name == "sarvam":
        provider = _run_sarvam_provider(chunks, log)
    elif name == "scribe_v2":
        provider = _run_scribe_provider(chunks, audio_path, audio_duration, log)
    else:
        raise ValueError(f"Unknown provider: {name}")

    if POSTPROCESS_ENABLED and anthropic_api_key:
        with postprocess_gate:
            try:
                provider.update(
                    _run_provider_postprocess(
                        name,
                        provider,
                        anthropic_api_key,
                        log,
                    )
                )
            except Exception as e:
                log.warning(
                    "Postprocessing failed — provider results will have empty normalized fields",
                    provider=name,
                    error=str(e),
                )
    return provider


# ── Core pipeline ─────────────────────────────────────────────────────────────

def process_job(
    s3_bucket: str,
    s3_key: str,
    receipt_handle: str | None = None,
    queue_url: str | None = None,
) -> uuid.UUID:
    """
    Run the full transcription pipeline for one audio file.

    Returns the job UUID once complete (or raises on failure). Correlation-
    only — the UUID is never persisted to a database; it's used as the S3
    results key and as the SQS FIFO MessageGroupId / dedup key.
    """
    original_filename = Path(s3_key).name
    job_id = uuid.uuid4()
    started_at = datetime.now(timezone.utc)
    log = logger.bind(job_id=str(job_id), s3_key=s3_key)
    log.info("Job accepted", status="pending", original_filename=original_filename)

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
            log.info("status=downloading")
            audio_path = download_audio(s3_bucket, s3_key, work_dir)

            # ── 2. Normalize to 16 kHz mono PCM WAV up-front ─────────────
            # One canonical format for VAD and both STT providers. The
            # resulting WAV has a usable RIFF duration header and drops any
            # video stream via ffmpeg.
            audio_path = _normalize_audio_for_pipeline(audio_path, work_dir, log)
            duration = get_duration(audio_path)
            if duration <= 0:
                raise RuntimeError(
                    "Normalized audio has non-positive duration — upload may be empty or undecodable"
                )
            log.info("Audio ready", duration_s=round(duration, 1), filename=audio_path.name)

            # ── 3. Provider setup ─────────────────────────────────────────
            anthropic_api_key = ""
            if POSTPROCESS_ENABLED:
                try:
                    anthropic_api_key = get_anthropic_api_key()
                except Exception as e:
                    log.warning("Could not retrieve Anthropic API key — skipping postprocess", error=str(e))
            if anthropic_api_key:
                log.info(
                    "provider-lane postprocessing enabled",
                    model=POSTPROCESS_MODEL,
                    max_concurrent=POSTPROCESS_MAX_CONCURRENT_PROVIDERS,
                )
            elif POSTPROCESS_ENABLED:
                log.info("ANTHROPIC_API_KEY not set — skipping postprocess")

            providers: dict[str, dict] = {}
            provider_errors: dict[str, str] = {}
            postprocess_gate = threading.Semaphore(
                max(1, POSTPROCESS_MAX_CONCURRENT_PROVIDERS)
            )

            # ── 4. Scribe full-audio lane + Sarvam chunking ───────────────
            # Scribe v2 can accept much larger inputs than Sarvam, so start it
            # immediately on the normalized full audio when it fits. Sarvam
            # still gets VAD chunks; Scribe only falls back to those chunks
            # when the full audio exceeds the configured Scribe request limits.
            with ThreadPoolExecutor(max_workers=2) as executor:
                future_to_provider = {}
                scribe_started = False
                if (
                    ELEVENLABS_ENABLED
                    and can_transcribe_full_audio_elevenlabs(audio_path, duration)
                ):
                    log.info("provider=scribe_v2 launching full-audio transcription")
                    future_to_provider[
                        executor.submit(
                            _run_provider_pipeline,
                            "scribe_v2",
                            [],
                            log,
                            audio_path=audio_path,
                            audio_duration=duration,
                            anthropic_api_key=anthropic_api_key,
                            postprocess_gate=postprocess_gate,
                        )
                    ] = "scribe_v2"
                    scribe_started = True

                log.info("status=chunking", audio_duration_seconds=round(duration, 2))
                chunks = chunk_audio(audio_path, work_dir, already_normalized=True)
                num_chunks = len(chunks)
                log.info("Chunks created", count=num_chunks)

                log.info(
                    "status=transcribing",
                    num_chunks=num_chunks,
                    elevenlabs_enabled=ELEVENLABS_ENABLED,
                )
                future_to_provider[
                    executor.submit(
                        _run_provider_pipeline,
                        "sarvam",
                        chunks,
                        log,
                        audio_path=audio_path,
                        audio_duration=duration,
                        anthropic_api_key=anthropic_api_key,
                        postprocess_gate=postprocess_gate,
                    )
                ] = "sarvam"
                if ELEVENLABS_ENABLED and not scribe_started:
                    log.info("provider=scribe_v2 launching chunked transcription")
                    future_to_provider[
                        executor.submit(
                            _run_provider_pipeline,
                            "scribe_v2",
                            chunks,
                            log,
                            audio_path=audio_path,
                            audio_duration=duration,
                            anthropic_api_key=anthropic_api_key,
                            postprocess_gate=postprocess_gate,
                        )
                    ] = "scribe_v2"

                for future in as_completed(future_to_provider):
                    provider_name = future_to_provider[future]
                    try:
                        providers[provider_name] = future.result()
                    except Exception as e:
                        provider_errors[provider_name] = str(e)
                        log.warning(
                            "Provider failed",
                            provider=provider_name,
                            error=str(e),
                        )
                        if provider_name == "sarvam":
                            raise

            sarvam_provider = providers["sarvam"]
            merged = sarvam_provider["merged"]
            num_segments = len(merged)
            num_speakers = len(set(s.speaker_id for s in merged))

            # ── 5. Assemble + persist results JSON to S3 ──────────────────
            completed_at = datetime.now(timezone.utc)

            provider_outputs = {
                "sarvam": build_provider_output(
                    provider="sarvam",
                    model=sarvam_provider["model"],
                    merged=sarvam_provider["merged"],
                    normalized=sarvam_provider.get("normalized"),
                    postprocess_meta=sarvam_provider.get("postprocess"),
                    metadata={
                        **sarvam_provider.get("metadata", {}),
                        "preprocessing_mode": AUDIO_PREPROCESSING_MODE,
                    },
                )
            }
            if ELEVENLABS_ENABLED:
                if "scribe_v2" in providers:
                    scribe_provider = providers["scribe_v2"]
                    provider_outputs["scribe_v2"] = build_provider_output(
                        provider="elevenlabs",
                        model=scribe_provider["model"],
                        merged=scribe_provider["merged"],
                        normalized=scribe_provider.get("normalized"),
                        postprocess_meta=scribe_provider.get("postprocess"),
                        metadata={
                            **scribe_provider.get("metadata", {}),
                            "preprocessing_mode": AUDIO_PREPROCESSING_MODE,
                        },
                        fill_translation_from_normalized=True,
                    )
                else:
                    provider_outputs["scribe_v2"] = build_provider_output(
                        provider="elevenlabs",
                        model=ELEVENLABS_MODEL_ID,
                        status="failed",
                        error=provider_errors.get("scribe_v2", "unknown error"),
                        metadata={"preprocessing_mode": AUDIO_PREPROCESSING_MODE},
                    )

            document = build_results_document(
                job_id=job_id,
                source_bucket=s3_bucket,
                source_key=s3_key,
                original_filename=original_filename,
                audio_duration_seconds=duration,
                num_chunks=num_chunks,
                source_language=None,  # Sarvam auto-detects; we don't surface it per-job yet
                started_at=started_at,
                completed_at=completed_at,
                provider_outputs=provider_outputs,
            )
            results_location = write_results(job_id, document)

            log.info(
                "Results persisted",
                status="completed",
                results_bucket=results_location.bucket,
                results_key=results_location.key,
                results_size_bytes=results_location.size_bytes,
            )
            _delete_sqs_message(queue_url, receipt_handle)

            emit_job_outcome(
                status="completed",
                wall_clock_s=time.monotonic() - wall_start,
                audio_duration_s=duration,
                num_segments=num_segments,
                num_speakers=num_speakers,
                num_chunks=num_chunks,
            )

            # Best-effort notify. The S3 results object is the durable record;
            # if publish fails the consumer can reconcile by listing the
            # results/ prefix in the processed bucket.
            publish_job_event(
                "job.completed",
                {
                    "job_id": str(job_id),
                    "status": "completed",
                    "source": {
                        "bucket": s3_bucket,
                        "key": s3_key,
                        "original_filename": original_filename,
                    },
                    "results": {
                        "bucket": results_location.bucket,
                        "key": results_location.key,
                        "size_bytes": results_location.size_bytes,
                        "etag": results_location.etag,
                    },
                    "summary": {
                        "audio_duration_seconds": round(duration, 2),
                        "num_chunks": num_chunks,
                        "num_segments": num_segments,
                        "num_speakers": num_speakers,
                    },
                    "completed_at": completed_at.isoformat(),
                },
            )

        except Exception as exc:
            tb = traceback.format_exc()
            log.error("Pipeline failed", error=str(exc), traceback=tb)
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
                    "source": {
                        "bucket": s3_bucket,
                        "key": s3_key,
                        "original_filename": original_filename,
                    },
                    "error_message": str(exc)[:1000],
                    "failed_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            # Do NOT delete the SQS message on failure — let SQS's visibility
            # timeout + maxReceiveCount drive redelivery / DLQ.
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
    variables: S3_BUCKET, S3_KEY, SQS_QUEUE_URL, SQS_RECEIPT_HANDLE.
    `S3_PROCESSED_BUCKET` must be set on the task definition — the worker
    writes the results JSON there. Translation is always English (produced
    by Sarvam's translate-mode pass alongside transcription); there is no
    target-language env var.
    """
    s3_bucket = os.environ.get("S3_BUCKET")
    s3_key = os.environ.get("S3_KEY")

    if not s3_bucket or not s3_key:
        logger.error("Missing required env vars: S3_BUCKET, S3_KEY")
        sys.exit(1)

    if not os.environ.get("S3_PROCESSED_BUCKET"):
        logger.error(
            "Missing required env var: S3_PROCESSED_BUCKET — cannot persist results"
        )
        sys.exit(1)

    receipt_handle = os.environ.get("SQS_RECEIPT_HANDLE")
    queue_url = os.environ.get("SQS_QUEUE_URL")

    logger.info("Worker starting", s3_bucket=s3_bucket, s3_key=s3_key)

    try:
        job_id = process_job(
            s3_bucket=s3_bucket,
            s3_key=s3_key,
            receipt_handle=receipt_handle,
            queue_url=queue_url,
        )
        logger.info("Worker finished", job_id=str(job_id))
    except Exception as e:
        logger.error("Worker exited with error", error=str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
