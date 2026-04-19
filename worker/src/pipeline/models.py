"""SQLAlchemy ORM models for the transcription pipeline."""
import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Column, DateTime, ForeignKey, Index, Integer,
    Numeric, String, Text, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, relationship


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Job(Base):
    __tablename__ = "jobs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    s3_bucket = Column(String(255), nullable=False)
    s3_key = Column(String(1024), nullable=False)
    original_filename = Column(String(512))
    status = Column(String(50), nullable=False, default="pending")
    # pending | downloading | chunking | transcribing | merging | translating | completed | failed
    audio_duration_seconds = Column(Numeric(10, 2))
    num_chunks = Column(Integer)
    num_speakers = Column(Integer)
    source_language = Column(String(10))
    error_message = Column(Text)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)
    started_at = Column(DateTime(timezone=True))
    completed_at = Column(DateTime(timezone=True))
    extra = Column("metadata", JSONB, default=dict)

    segments = relationship("Segment", back_populates="job", cascade="all, delete-orphan")
    translations = relationship("Translation", back_populates="job", cascade="all, delete-orphan")

    __table_args__ = (
        Index("idx_jobs_status", "status"),
        Index("idx_jobs_s3_key", "s3_key"),
    )


class Segment(Base):
    __tablename__ = "segments"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    job_id = Column(UUID(as_uuid=True), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False)
    chunk_index = Column(Integer, nullable=False)
    segment_index = Column(Integer, nullable=False)
    speaker_id = Column(Integer, nullable=False)
    speaker_label = Column(String(100))
    start_time = Column(Numeric(10, 3), nullable=False)
    end_time = Column(Numeric(10, 3), nullable=False)
    text = Column(Text, nullable=False)
    confidence = Column(Numeric(4, 3))
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)

    job = relationship("Job", back_populates="segments")
    translations = relationship("Translation", back_populates="segment", cascade="all, delete-orphan")

    __table_args__ = (
        Index("idx_segments_job_id", "job_id"),
        Index("idx_segments_speaker", "job_id", "speaker_id"),
    )


class Translation(Base):
    __tablename__ = "translations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    segment_id = Column(UUID(as_uuid=True), ForeignKey("segments.id", ondelete="CASCADE"), nullable=False)
    job_id = Column(UUID(as_uuid=True), ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False)
    target_language = Column(String(10), nullable=False)
    translated_text = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_now)

    segment = relationship("Segment", back_populates="translations")
    job = relationship("Job", back_populates="translations")

    __table_args__ = (
        Index("idx_translations_job", "job_id"),
        Index("idx_translations_segment", "segment_id"),
        UniqueConstraint("segment_id", "target_language", name="uq_translation_segment_lang"),
    )
