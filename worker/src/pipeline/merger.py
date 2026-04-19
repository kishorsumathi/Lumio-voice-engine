"""
Cross-chunk speaker stitching via overlap text matching.

Each chunk (except the first) starts OVERLAP_DURATION_S before its content_start.
The overlap region is transcribed by both adjacent chunks with Sarvam's built-in
diarization. Speaker IDs in the overlap are matched via rapidfuzz token_set_ratio
to build a globally consistent speaker map without any external diarization model.
"""
import logging
from dataclasses import dataclass

from rapidfuzz.fuzz import token_set_ratio

from .chunking import ChunkInfo
from .transcription import TranscriptSegment

logger = logging.getLogger(__name__)

STITCH_MIN_SCORE = 65   # rapidfuzz score below this = don't remap, keep local ID


@dataclass
class MergedSegment:
    chunk_index: int
    segment_index: int
    speaker_id: int        # 0-based global integer
    start_time: float
    end_time: float
    text: str
    confidence: float | None


def _group_text_by_speaker(segments: list[TranscriptSegment]) -> dict[str, str]:
    """Concatenate all text per speaker_id."""
    result: dict[str, str] = {}
    for seg in segments:
        result[seg.speaker_id] = result.get(seg.speaker_id, "") + " " + seg.text
    return {k: v.strip() for k, v in result.items()}


def _build_remap(
    prev_speakers: dict[str, str],
    curr_speakers: dict[str, str],
    prev_global_map: dict[str, str],
) -> dict[str, str]:
    """
    Match curr chunk's local speaker IDs to prev chunk's global IDs using
    text similarity in the overlap region. Returns {curr_local_id: global_id}.
    """
    if not prev_speakers or not curr_speakers:
        logger.warning("Empty overlap — keeping chunk-local speaker IDs")
        return {s: s for s in curr_speakers}

    scores: dict[tuple[str, str], float] = {}
    for curr_id, curr_text in curr_speakers.items():
        for prev_id, prev_text in prev_speakers.items():
            if curr_text and prev_text:
                scores[(curr_id, prev_id)] = token_set_ratio(curr_text, prev_text)

    # Greedy 1-to-1 assignment, highest score first
    used_curr: set[str] = set()
    used_prev: set[str] = set()
    remap: dict[str, str] = {}

    for (curr_id, prev_id), score in sorted(scores.items(), key=lambda x: -x[1]):
        if curr_id in used_curr or prev_id in used_prev:
            continue
        if score >= STITCH_MIN_SCORE:
            global_id = prev_global_map.get(prev_id, prev_id)
            remap[curr_id] = global_id
            used_curr.add(curr_id)
            used_prev.add(prev_id)
            logger.info(
                "Stitch: chunk_local[%s] → global[%s] (score=%.0f)",
                curr_id, global_id, score,
            )
        else:
            logger.warning(
                "Low confidence match %s↔%s score=%.0f — keeping local ID",
                curr_id, prev_id, score,
            )

    for curr_id in curr_speakers:
        if curr_id not in remap:
            remap[curr_id] = curr_id

    return remap


def _normalize_to_int(segments: list[MergedSegment]) -> list[MergedSegment]:
    """Remap string speaker IDs to 0-based integers in order of first appearance."""
    mapping: dict[str, int] = {}
    result = []
    for seg in segments:
        label = seg.speaker_id  # type: ignore[assignment]  — still string here
        if label not in mapping:
            mapping[label] = len(mapping)
        result.append(MergedSegment(
            chunk_index=seg.chunk_index,
            segment_index=seg.segment_index,
            speaker_id=mapping[label],
            start_time=seg.start_time,
            end_time=seg.end_time,
            text=seg.text,
            confidence=seg.confidence,
        ))
    return result


def _merge_consecutive(segments: list[MergedSegment]) -> list[MergedSegment]:
    """Merge back-to-back segments from the same speaker into one."""
    if not segments:
        return []
    merged = [segments[0]]
    for seg in segments[1:]:
        prev = merged[-1]
        if seg.speaker_id == prev.speaker_id:
            merged[-1] = MergedSegment(
                chunk_index=prev.chunk_index,
                segment_index=prev.segment_index,
                speaker_id=prev.speaker_id,
                start_time=prev.start_time,
                end_time=seg.end_time,
                text=prev.text + " " + seg.text,
                confidence=prev.confidence,
            )
        else:
            merged.append(seg)
    return merged


def merge(
    chunks: list[ChunkInfo],
    transcript_segments: list[TranscriptSegment],
) -> list[MergedSegment]:
    """
    Stitch speaker IDs across chunk boundaries using overlap text matching,
    deduplicate overlap regions, then produce globally consistent 0-based
    integer speaker IDs.
    """
    by_chunk: dict[int, list[TranscriptSegment]] = {}
    for seg in transcript_segments:
        by_chunk.setdefault(seg.chunk_index, []).append(seg)

    # Build speaker remap per chunk
    speaker_maps: dict[int, dict[str, str]] = {}
    chunk0_ids = set(s.speaker_id for s in by_chunk.get(0, []))
    speaker_maps[0] = {s: s for s in chunk0_ids}

    for i in range(1, len(chunks)):
        curr_chunk = chunks[i]
        overlap_start = curr_chunk.start_time      # audio file start (with overlap)
        overlap_end = curr_chunk.content_start     # = prev chunk's end_time

        prev_overlap_segs = [
            s for s in by_chunk.get(i - 1, [])
            if s.start_time >= overlap_start
        ]
        curr_overlap_segs = [
            s for s in by_chunk.get(i, [])
            if s.end_time <= overlap_end
        ]

        prev_speakers = _group_text_by_speaker(prev_overlap_segs)
        curr_speakers = _group_text_by_speaker(curr_overlap_segs)

        logger.info(
            "Boundary %d→%d: overlap=%.1f–%.1fs  prev_spk=%s  curr_spk=%s",
            i - 1, i, overlap_start, overlap_end,
            list(prev_speakers.keys()), list(curr_speakers.keys()),
        )

        speaker_maps[i] = _build_remap(
            prev_speakers, curr_speakers, speaker_maps[i - 1]
        )

    # Apply remap + discard overlap duplicates (keep content_start onwards per chunk)
    raw: list[MergedSegment] = []
    for i, chunk in enumerate(chunks):
        remap = speaker_maps.get(i, {})
        for seg in by_chunk.get(i, []):
            if seg.start_time < chunk.content_start:
                continue  # overlap region — already covered by previous chunk
            global_id = remap.get(seg.speaker_id, seg.speaker_id)
            raw.append(MergedSegment(
                chunk_index=seg.chunk_index,
                segment_index=0,
                speaker_id=global_id,   # type: ignore[arg-type]
                start_time=seg.start_time,
                end_time=seg.end_time,
                text=seg.text,
                confidence=seg.confidence,
            ))

    raw.sort(key=lambda s: s.start_time)
    normalized = _normalize_to_int(raw)
    final = _merge_consecutive(normalized)

    for idx, seg in enumerate(final):
        seg.segment_index = idx

    num_speakers = len(set(s.speaker_id for s in final))
    logger.info("Merge complete: %d segments, %d speakers", len(final), num_speakers)
    return final
