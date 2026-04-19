"""DB session + job/segment queries for the Streamlit UI."""
from __future__ import annotations

import uuid
from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import Session, sessionmaker

from config import get_db_url
from models import Job, Segment, Translation

_engine = None
_factory = None


def get_engine():
    global _engine
    if _engine is None:
        _engine = create_engine(
            get_db_url(),
            pool_size=3,
            max_overflow=2,
            pool_pre_ping=True,
        )
    return _engine


def get_session_factory():
    global _factory
    if _factory is None:
        _factory = sessionmaker(bind=get_engine(), expire_on_commit=False)
    return _factory


@contextmanager
def session_scope() -> Generator[Session, None, None]:
    fac = get_session_factory()
    s: Session = fac()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def list_recent_jobs(limit: int = 30) -> list[Job]:
    with session_scope() as s:
        return list(s.scalars(select(Job).order_by(Job.created_at.desc()).limit(limit)))


def get_job(job_id: uuid.UUID) -> Job | None:
    with session_scope() as s:
        return s.get(Job, job_id)


def get_job_by_s3_key(s3_key: str) -> Job | None:
    """Latest job for this exact S3 key (upload path is unique per file)."""
    with session_scope() as s:
        return s.scalars(
            select(Job).where(Job.s3_key == s3_key).order_by(Job.created_at.desc()).limit(1)
        ).first()


def load_job_with_segments(job_id: uuid.UUID) -> tuple[Job | None, list[Segment], dict[str, list[Translation]]]:
    with session_scope() as s:
        job = s.get(Job, job_id)
        if job is None:
            return None, [], {}
        segs = list(
            s.scalars(
                select(Segment)
                .where(Segment.job_id == job_id)
                .order_by(Segment.start_time, Segment.segment_index)
            )
        )
        trans: dict[str, list[Translation]] = {}
        for tr in s.scalars(select(Translation).where(Translation.job_id == job_id)):
            trans.setdefault(tr.target_language, []).append(tr)
        return job, segs, trans


def translations_per_segment(
    trans_by_lang: dict[str, list[Translation]],
) -> dict[uuid.UUID, dict[str, str]]:
    """
    Map segment_id → { target_language: translated_text }.
    Matches RDS: translations.segment_id + target_language + translated_text.
    """
    out: dict[uuid.UUID, dict[str, str]] = {}
    for lang, rows in trans_by_lang.items():
        for tr in rows:
            out.setdefault(tr.segment_id, {})[lang] = tr.translated_text
    return out


def update_speaker_labels_for_job(job_id: uuid.UUID, speaker_id: int, label: str) -> int:
    """Set human-readable label for all segments with this diarization speaker_id."""
    with session_scope() as s:
        res = s.execute(
            update(Segment)
            .where(Segment.job_id == job_id, Segment.speaker_id == speaker_id)
            .values(speaker_label=label.strip()[:100] if label else None)
        )
        return res.rowcount or 0
