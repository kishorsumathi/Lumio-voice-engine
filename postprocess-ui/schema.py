"""Pydantic models for pipeline results parsing and LLM structured output."""

from __future__ import annotations

from pydantic import BaseModel, Field


class SpeakerTurn(BaseModel):
    """One contiguous speaker run after merging consecutive same-speaker segments."""

    turn_index: int
    speaker_id: int
    start_time: float
    end_time: float
    transcription: str
    translation: str


class CleanedTurn(BaseModel):
    turn_index: int
    cleaned_transcription: str
    cleaned_translation: str


class GlossaryCorrection(BaseModel):
    heard: str = ""
    corrected: str = ""


class CleanedTurns(BaseModel):
    turns: list[CleanedTurn] = Field(default_factory=list)
    glossary_corrections: list[GlossaryCorrection] = Field(default_factory=list)


class PostprocessTurnOut(BaseModel):
    """Single enriched turn written into the output document."""

    turn_index: int
    speaker_id: int
    start_time: float
    end_time: float
    transcription: str
    translation: str
    cleaned_transcription: str
    cleaned_translation: str


class PostprocessMeta(BaseModel):
    model: str
    turns: list[PostprocessTurnOut]
    glossary_corrections: list[GlossaryCorrection] = Field(default_factory=list)
