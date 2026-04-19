"""
Sarvam Saaras v3 batch transcription.

Each audio chunk is submitted as an independent Sarvam batch job.
Multiple chunks are processed in parallel (up to SARVAM_MAX_CONCURRENT_CHUNKS).

The start_time_offset for each chunk is added to all returned timestamps
so every segment carries an absolute position in the full audio.
"""
import logging
import os
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from tenacity import retry, stop_after_attempt, wait_exponential, before_sleep_log

from .chunking import ChunkInfo
from .config import (
    SARVAM_BATCH_TIMEOUT_S,
    SARVAM_BATCH_POLL_INTERVAL_S,
    SARVAM_MAX_CONCURRENT_CHUNKS,
    get_sarvam_api_key,
)
from .rate_limit import throttle

logger = logging.getLogger(__name__)


@dataclass
class TranscriptSegment:
    chunk_index: int
    speaker_id: str        # Sarvam's raw speaker ID (will be remapped later)
    start_time: float      # absolute seconds in full audio
    end_time: float        # absolute seconds in full audio
    text: str
    confidence: float | None = None


def _sarvam_client():
    from sarvamai import SarvamAI
    return SarvamAI(api_subscription_key=get_sarvam_api_key())


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=2, min=10, max=60),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
def transcribe_chunk(chunk: ChunkInfo) -> list[TranscriptSegment]:
    """
    Transcribe a single audio chunk via Sarvam batch API.

    Retries up to 3 times with exponential backoff on transient failures.
    429 rate limit errors wait 60s before retry.
    chunk.start_time is added to all timestamps for absolute positioning.
    """
    from sarvamai.core.api_error import ApiError

    client = _sarvam_client()
    try:
        return _transcribe_chunk_inner(client, chunk)
    except ApiError as e:
        if e.status_code == 429:
            logger.warning("Sarvam 429 on chunk %d — waiting 60s", chunk.index)
            time.sleep(60)
        raise


def _transcribe_chunk_inner(client, chunk: ChunkInfo) -> list[TranscriptSegment]:

    logger.info(
        "Transcribing chunk %d (%.1f–%.1fs, %.1fmin)",
        chunk.index, chunk.start_time, chunk.end_time, chunk.duration / 60.0,
    )

    throttle()
    job = client.speech_to_text_job.create_job(
        model="saaras:v3",
        mode="codemix",             # handles mixed-language audio (Hinglish, etc.)
        language_code="unknown",    # auto-detect all 22 Indian languages + English
        with_diarization=True,
    )
    throttle()
    job.upload_files([str(chunk.path)])
    throttle()
    job.start()
    job.wait_until_complete(
        poll_interval=SARVAM_BATCH_POLL_INTERVAL_S,
        timeout=SARVAM_BATCH_TIMEOUT_S,
    )

    throttle()
    file_results = job.get_file_results()
    if file_results["failed"]:
        err = file_results["failed"][0].get("error_message", "unknown error")
        raise RuntimeError(f"Sarvam batch job failed for chunk {chunk.index}: {err}")

    with tempfile.TemporaryDirectory() as out_dir:
        job.download_outputs(out_dir)
        import json
        for fname in os.listdir(out_dir):
            if fname.endswith(".json"):
                with open(os.path.join(out_dir, fname)) as f:
                    data = json.load(f)
                segments = _parse_batch_output(data, chunk)
                logger.info(
                    "Chunk %d: %d segments transcribed", chunk.index, len(segments)
                )
                return segments

    logger.warning("Chunk %d: no JSON output found", chunk.index)
    return []


def _parse_batch_output(data: dict, chunk: ChunkInfo) -> list[TranscriptSegment]:
    """
    Parse Sarvam batch JSON output into TranscriptSegment list.

    Timestamps from Sarvam are relative to the chunk start.
    We add chunk.start_time to make them absolute.
    """
    offset = chunk.start_time
    segments: list[TranscriptSegment] = []

    diarized = data.get("diarized_transcript") if isinstance(data, dict) else None
    if diarized:
        entries = (
            diarized.get("entries", diarized)
            if isinstance(diarized, dict)
            else diarized
        )
        if entries:
            entries = sorted(
                entries,
                key=lambda e: (e.get("start_time_seconds", 0) if isinstance(e, dict)
                               else getattr(e, "start_time_seconds", 0)),
            )
        for entry in (entries or []):
            if isinstance(entry, dict):
                speaker_id = str(entry.get("speaker_id", "0"))
                text = entry.get("transcript", "").strip()
                start = float(entry.get("start_time_seconds", 0.0)) + offset
                end = float(entry.get("end_time_seconds", start + 1.0)) + offset
            else:
                speaker_id = str(getattr(entry, "speaker_id", "0"))
                text = getattr(entry, "transcript", "").strip()
                start = float(getattr(entry, "start_time_seconds", 0.0)) + offset
                end = float(getattr(entry, "end_time_seconds", start + 1.0)) + offset

            if text:
                segments.append(TranscriptSegment(
                    chunk_index=chunk.index,
                    speaker_id=speaker_id,
                    start_time=round(start, 3),
                    end_time=round(end, 3),
                    text=text,
                ))
        if segments:
            return segments

    # Fallback: plain transcript, no timestamps available
    transcript = data.get("transcript", "") if isinstance(data, dict) else ""
    if transcript and transcript.strip():
        # Estimate timestamps proportionally across the chunk duration
        logger.warning(
            "Chunk %d: no diarized transcript — using plain transcript with estimated timestamps",
            chunk.index,
        )
        segments.append(TranscriptSegment(
            chunk_index=chunk.index,
            speaker_id="0",
            start_time=round(offset, 3),
            end_time=round(offset + chunk.duration, 3),
            text=transcript.strip(),
        ))
    return segments


def transcribe_all_chunks(chunks: list[ChunkInfo]) -> list[TranscriptSegment]:
    """
    Transcribe all chunks in parallel (up to SARVAM_MAX_CONCURRENT_CHUNKS).
    Returns segments sorted by absolute start_time.
    """
    all_segments: list[TranscriptSegment] = []

    with ThreadPoolExecutor(max_workers=SARVAM_MAX_CONCURRENT_CHUNKS) as executor:
        future_to_chunk = {
            executor.submit(transcribe_chunk, chunk): chunk
            for chunk in chunks
        }
        for future in as_completed(future_to_chunk):
            chunk = future_to_chunk[future]
            try:
                segs = future.result()
                all_segments.extend(segs)
            except Exception as e:
                logger.error("Chunk %d transcription failed: %s", chunk.index, e)
                raise RuntimeError(f"Chunk {chunk.index} failed: {e}") from e

    all_segments.sort(key=lambda s: s.start_time)
    return all_segments
