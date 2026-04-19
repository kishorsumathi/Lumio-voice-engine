"""Job status transitions — single source of truth for all status updates."""
import logging
import uuid
from datetime import datetime, timezone


from .db import get_session
from .models import Job, Segment, Translation

logger = logging.getLogger(__name__)

VALID_TRANSITIONS: dict[str, list[str]] = {
    "pending":      ["downloading", "failed"],
    "downloading":  ["chunking", "failed"],
    "chunking":     ["transcribing", "failed"],
    "transcribing": ["merging", "failed"],
    "merging":      ["translating", "failed"],
    "translating":  ["completed", "failed"],
    "completed":    [],
    "failed":       [],
}


def create_job(s3_bucket: str, s3_key: str, original_filename: str | None = None) -> uuid.UUID:
    """Insert a new job record and return its ID."""
    with get_session() as session:
        job = Job(
            s3_bucket=s3_bucket,
            s3_key=s3_key,
            original_filename=original_filename,
            status="pending",
            started_at=datetime.now(timezone.utc),
        )
        session.add(job)
        session.flush()
        job_id = job.id
    logger.info("job_id=%s status=pending s3_key=%s", job_id, s3_key)
    return job_id


def update_status(job_id: uuid.UUID, new_status: str, **extra_fields) -> None:
    """Transition job to new_status. Raises ValueError on invalid transition."""
    with get_session() as session:
        job = session.get(Job, job_id)
        if job is None:
            raise RuntimeError(f"Job {job_id} not found")
        current = job.status
        allowed = VALID_TRANSITIONS.get(current, [])
        if new_status not in allowed:
            raise ValueError(
                f"Invalid transition {current!r} → {new_status!r}. Allowed: {allowed}"
            )
        job.status = new_status
        for k, v in extra_fields.items():
            if hasattr(job, k):
                setattr(job, k, v)
        if new_status == "completed":
            job.completed_at = datetime.now(timezone.utc)
    logger.info("job_id=%s status=%s", job_id, new_status)


def mark_failed(job_id: uuid.UUID, error_message: str) -> None:
    """Mark a job as failed with an error message (bypasses normal transition rules)."""
    with get_session() as session:
        job = session.get(Job, job_id)
        if job is None:
            raise RuntimeError(f"Cannot mark failed — job {job_id} not found")
        job.status = "failed"
        job.error_message = error_message[:4000]  # truncate for DB column
        job.completed_at = datetime.now(timezone.utc)
    logger.error("job_id=%s status=failed error=%s", job_id, error_message[:200])


def store_results(
    job_id: uuid.UUID,
    segments: list[dict],
    translations: dict[str, list[dict]],
    num_speakers: int,
    final_status: str | None = None,
) -> None:
    """
    Bulk-insert segments and translations into the DB in a single transaction.

    segments: list of {chunk_index, segment_index, speaker_id, start_time, end_time, text, confidence}
    translations: {language_code: [{segment_index, translated_text}]}

    If `final_status` is given (typically "completed"), the job row is
    transitioned to that status **in the same transaction** as the segment
    and translation inserts. This guarantees consumers never see data without
    the status update or a status update without the data.
    """
    with get_session() as session:
        seg_objects = []
        for seg in segments:
            obj = Segment(
                job_id=job_id,
                chunk_index=seg["chunk_index"],
                segment_index=seg["segment_index"],
                speaker_id=seg["speaker_id"],
                start_time=seg["start_time"],
                end_time=seg["end_time"],
                text=seg["text"],
                confidence=seg.get("confidence"),
            )
            seg_objects.append(obj)

        session.add_all(seg_objects)
        session.flush()

        seg_index_to_id: dict[int, uuid.UUID] = {
            obj.segment_index: obj.id for obj in seg_objects
        }

        trans_objects = []
        for lang, trans_list in translations.items():
            for t in trans_list:
                seg_id = seg_index_to_id.get(t["segment_index"])
                if seg_id is None:
                    continue
                trans_objects.append(Translation(
                    segment_id=seg_id,
                    job_id=job_id,
                    target_language=lang,
                    translated_text=t["translated_text"],
                ))
        if trans_objects:
            session.add_all(trans_objects)

        job = session.get(Job, job_id)
        if job is None:
            raise RuntimeError(f"store_results: job {job_id} not found")
        job.num_speakers = num_speakers

        if final_status is not None:
            allowed = VALID_TRANSITIONS.get(job.status, [])
            if final_status not in allowed:
                raise ValueError(
                    f"Invalid transition {job.status!r} → {final_status!r}. "
                    f"Allowed: {allowed}"
                )
            job.status = final_status
            if final_status == "completed":
                job.completed_at = datetime.now(timezone.utc)

    logger.info(
        "job_id=%s stored %d segments, %d translations, status=%s",
        job_id, len(seg_objects), len(trans_objects) if trans_objects else 0,
        final_status or "(unchanged)",
    )
