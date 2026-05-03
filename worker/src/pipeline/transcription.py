"""
Sarvam Saaras v3 batch transcription + translation (dual-pass).

Each audio chunk is submitted twice to Sarvam in parallel:

  - Job A (`mode=codemix`)   → diarized text in the original language(s).
                                Owns the canonical timeline and speaker IDs.
  - Job B (`mode=translate`) → diarized text translated to English by Sarvam.
                                Provides per-segment English text only.

The two jobs run on the same audio so timestamps are directly comparable.
After both complete we attach English text to each transcription segment by
**timestamp-overlap matching** — for every transcribe segment, all translate
segments whose time window overlaps it are concatenated in chronological
order and stored on `TranscriptSegment.translation`.

This replaces the previous Mayura-text-translation step (and its language-
detection fallback). Sarvam's `translate` mode handles code-mixed audio
(Hinglish) natively, sidestepping the romanized-Hindi failure mode where
Mayura's auto-detect treated Latin-script Hindi as English and passed it
through unchanged.
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
    text: str              # original-language transcription (codemix output)
    translation: str = ""  # English (from the parallel translate-mode pass)
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
def _run_saaras_job(chunk: ChunkInfo, *, mode: str) -> list[TranscriptSegment]:
    """
    Submit one Sarvam batch job for `chunk` with the given Saaras `mode`.

    Returns a list of `TranscriptSegment` whose `text` field carries whatever
    Saaras produced for that mode — original language for `codemix`, English
    for `translate`. The caller decides what to do with the text (use it as
    transcription, or merge it into another segment list as translation).

    Retries up to 3 times with exponential backoff on transient failures.
    A 429 rate-limit response sleeps 60 s before re-raising into the retry.
    """
    from sarvamai.core.api_error import ApiError

    client = _sarvam_client()
    try:
        return _run_saaras_job_inner(client, chunk, mode=mode)
    except ApiError as e:
        if e.status_code == 429:
            logger.warning("Sarvam 429 on chunk %d (mode=%s) — waiting 60s",
                           chunk.index, mode)
            time.sleep(60)
        raise


def _run_saaras_job_inner(client, chunk: ChunkInfo, *, mode: str) -> list[TranscriptSegment]:
    logger.info(
        "Saaras job (mode=%s) chunk %d (%.1f–%.1fs, %.1fmin)",
        mode, chunk.index, chunk.start_time, chunk.end_time, chunk.duration / 60.0,
    )

    throttle()
    job = client.speech_to_text_job.create_job(
        model="saaras:v3",
        mode=mode,                  # "codemix" → original; "translate" → English
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
        raise RuntimeError(
            f"Sarvam batch job failed for chunk {chunk.index} (mode={mode}): {err}"
        )

    with tempfile.TemporaryDirectory() as out_dir:
        job.download_outputs(out_dir)
        import json
        for fname in os.listdir(out_dir):
            if fname.endswith(".json"):
                with open(os.path.join(out_dir, fname)) as f:
                    data = json.load(f)
                segments = _parse_batch_output(data, chunk)
                logger.info(
                    "Chunk %d (mode=%s): %d segments",
                    chunk.index, mode, len(segments),
                )
                return segments

    logger.warning("Chunk %d (mode=%s): no JSON output found", chunk.index, mode)
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


def _zip_translation_into_segments(
    tx_segments: list[TranscriptSegment],
    tr_segments: list[TranscriptSegment],
) -> list[TranscriptSegment]:
    """
    Attach English text from `tr_segments` to `tx_segments` by timestamp overlap.

    For each transcription segment, all translation segments whose time window
    has any overlap with it are picked up and concatenated in chronological
    order. Non-overlapping translation segments are silently dropped (they
    would only happen if Sarvam diarized very differently between the two
    runs — rare on the same audio).

    Mutates the `translation` field of each `tx_segments` entry and returns
    the same list.
    """
    if not tx_segments:
        return tx_segments

    for tx in tx_segments:
        overlapping: list[tuple[float, str]] = []
        for tr in tr_segments:
            overlap_start = max(tx.start_time, tr.start_time)
            overlap_end = min(tx.end_time, tr.end_time)
            if overlap_end > overlap_start:
                overlapping.append((tr.start_time, tr.text))
        overlapping.sort(key=lambda x: x[0])
        tx.translation = " ".join(t for _, t in overlapping).strip()

    return tx_segments


def _process_chunk(chunk: ChunkInfo) -> list[TranscriptSegment]:
    """
    Run transcribe (`codemix`) and translate Saaras jobs for one chunk in
    parallel, then merge the translation text into the transcription segments
    by timestamp overlap.
    """
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_tx = ex.submit(_run_saaras_job, chunk, mode="codemix")
        f_tr = ex.submit(_run_saaras_job, chunk, mode="translate")
        tx_segments = f_tx.result()
        tr_segments = f_tr.result()

    if not tr_segments:
        # Translate pass returned nothing — keep transcription, leave
        # translation empty per segment. This is logged at WARNING by the
        # parser already.
        logger.warning(
            "Chunk %d: translate pass returned 0 segments — translation will be empty",
            chunk.index,
        )
        return tx_segments

    return _zip_translation_into_segments(tx_segments, tr_segments)


def transcribe_all_chunks(chunks: list[ChunkInfo]) -> list[TranscriptSegment]:
    """
    Transcribe + translate all chunks in parallel.

    Up to `SARVAM_MAX_CONCURRENT_CHUNKS` chunks are processed concurrently.
    Each chunk internally fans out to two Sarvam batch jobs (codemix +
    translate). The global RPM throttle in `rate_limit.throttle()` keeps the
    Sarvam API call rate under `SARVAM_RPM_LIMIT` regardless of pool size.

    Returns segments sorted by absolute start_time, with `translation`
    populated from the translate pass.
    """
    all_segments: list[TranscriptSegment] = []

    with ThreadPoolExecutor(max_workers=SARVAM_MAX_CONCURRENT_CHUNKS) as executor:
        future_to_chunk = {
            executor.submit(_process_chunk, chunk): chunk
            for chunk in chunks
        }
        for future in as_completed(future_to_chunk):
            chunk = future_to_chunk[future]
            try:
                segs = future.result()
                all_segments.extend(segs)
            except Exception as e:
                logger.error("Chunk %d transcribe+translate failed: %s",
                             chunk.index, e)
                raise RuntimeError(f"Chunk {chunk.index} failed: {e}") from e

    all_segments.sort(key=lambda s: s.start_time)
    return all_segments
