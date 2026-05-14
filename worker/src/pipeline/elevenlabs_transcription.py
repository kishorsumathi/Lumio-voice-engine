"""ElevenLabs Scribe v2 transcription adapter."""
from __future__ import annotations

import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from .chunking import ChunkInfo
from .config import (
    ELEVENLABS_KEYTERMS_FROM_GLOSSARY,
    ELEVENLABS_LANGUAGE_CODE,
    ELEVENLABS_MAX_CONCURRENT_CHUNKS,
    ELEVENLABS_MAX_DURATION_S,
    ELEVENLABS_MAX_UPLOAD_BYTES,
    ELEVENLABS_MODEL_ID,
    ELEVENLABS_NO_VERBATIM,
    ELEVENLABS_NUM_SPEAKERS,
    ELEVENLABS_REQUEST_TIMEOUT_S,
    ELEVENLABS_TEMPERATURE,
    GLOSSARY_FILE_PATH,
    get_elevenlabs_api_key,
)
from .transcription import TranscriptSegment

logger = logging.getLogger(__name__)

_MAX_RETRIES = 5
_MAX_SEGMENT_GAP_S = 1.5
_UNSUPPORTED_KEYTERM_CHARS_RE = re.compile(r"[<>{}\[\]\\]")


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _load_keyterms(path: str | Path) -> list[str]:
    if not ELEVENLABS_KEYTERMS_FROM_GLOSSARY:
        return []

    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("Could not load ElevenLabs keyterms from glossary: %s", e)
        return []

    terms: list[str] = []
    for term in data.get("terms") or []:
        if isinstance(term, str):
            terms.append(term)
    for correction in data.get("corrections") or []:
        if isinstance(correction, dict):
            corrected = correction.get("corrected")
            if isinstance(corrected, str):
                terms.append(corrected)

    deduped: list[str] = []
    seen: set[str] = set()
    for term in terms:
        clean = " ".join(term.strip().split())
        if not clean or clean in seen:
            continue
        if len(clean) >= 50 or len(clean.split()) > 5:
            continue
        if _UNSUPPORTED_KEYTERM_CHARS_RE.search(clean):
            continue
        seen.add(clean)
        deduped.append(clean)
        if len(deduped) >= 1000:
            break
    return deduped


def _retry_after_seconds(exc: Exception) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None) or getattr(exc, "headers", None)
    if not headers:
        return None
    value = None
    try:
        value = headers.get("Retry-After")
    except Exception:
        return None
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def _status_code(exc: Exception) -> int | None:
    for attr in ("status_code", "status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    value = getattr(response, "status_code", None)
    return value if isinstance(value, int) else None


def _is_retryable(exc: Exception) -> bool:
    status = _status_code(exc)
    return status == 429 or (status is not None and 500 <= status <= 599)


def _validate_chunk(chunk: ChunkInfo) -> None:
    size = chunk.path.stat().st_size
    if size >= ELEVENLABS_MAX_UPLOAD_BYTES:
        raise RuntimeError(
            f"ElevenLabs chunk {chunk.index} is {size} bytes, exceeding "
            f"limit {ELEVENLABS_MAX_UPLOAD_BYTES}. Reduce MAX_CHUNK_DURATION_S."
        )
    if chunk.duration > ELEVENLABS_MAX_DURATION_S:
        raise RuntimeError(
            f"ElevenLabs chunk {chunk.index} is {chunk.duration:.1f}s, exceeding "
            f"limit {ELEVENLABS_MAX_DURATION_S:.1f}s. Reduce MAX_CHUNK_DURATION_S."
        )


def _client():
    from elevenlabs.client import ElevenLabs

    return ElevenLabs(api_key=get_elevenlabs_api_key())


def _convert_with_retries(client, chunk: ChunkInfo, keyterms: list[str]):
    params = {
        "model_id": ELEVENLABS_MODEL_ID,
        "diarize": True,
        "timestamps_granularity": "word",
        "file_format": "other",
        "tag_audio_events": True,
        "no_verbatim": ELEVENLABS_NO_VERBATIM,
        "temperature": ELEVENLABS_TEMPERATURE,
    }
    if ELEVENLABS_NUM_SPEAKERS is not None:
        params["num_speakers"] = ELEVENLABS_NUM_SPEAKERS
    if ELEVENLABS_LANGUAGE_CODE:
        params["language_code"] = ELEVENLABS_LANGUAGE_CODE
    if keyterms:
        params["keyterms"] = keyterms

    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            with chunk.path.open("rb") as audio_file:
                return client.speech_to_text.convert(
                    file=audio_file,
                    request_options={
                        "timeout_in_seconds": ELEVENLABS_REQUEST_TIMEOUT_S,
                    },
                    **params,
                )
        except Exception as e:
            if attempt == _MAX_RETRIES or not _is_retryable(e):
                raise
            wait_s = _retry_after_seconds(e)
            if wait_s is None:
                wait_s = min(60.0, 2.0 ** attempt)
            logger.warning(
                "ElevenLabs Scribe v2 retryable failure on chunk %d "
                "(attempt %d/%d, status=%s); sleeping %.1fs",
                chunk.index,
                attempt,
                _MAX_RETRIES,
                _status_code(e),
                wait_s,
            )
            time.sleep(wait_s)


def _parse_words_response(response: Any, chunk: ChunkInfo) -> list[TranscriptSegment]:
    words = _get(response, "words", []) or []
    segments: list[TranscriptSegment] = []
    cur_speaker: str | None = None
    cur_parts: list[str] = []
    cur_start: float | None = None
    cur_end: float | None = None

    def flush() -> None:
        nonlocal cur_speaker, cur_parts, cur_start, cur_end
        text = "".join(cur_parts).strip()
        if text and cur_start is not None and cur_end is not None:
            segments.append(
                TranscriptSegment(
                    chunk_index=chunk.index,
                    speaker_id=cur_speaker or "speaker_0",
                    start_time=round(chunk.start_time + cur_start, 3),
                    end_time=round(chunk.start_time + max(cur_end, cur_start), 3),
                    text=text,
                )
            )
        cur_speaker = None
        cur_parts = []
        cur_start = None
        cur_end = None

    for word in words:
        typ = _get(word, "type", "word")
        if typ == "audio_event":
            continue

        text = str(_get(word, "text", "") or "")
        if not text:
            continue

        speaker = str(_get(word, "speaker_id", "speaker_0") or "speaker_0")
        start = _get(word, "start", None)
        end = _get(word, "end", None)
        try:
            start_f = float(start) if start is not None else cur_end
            end_f = float(end) if end is not None else start_f
        except (TypeError, ValueError):
            start_f = cur_end
            end_f = cur_end

        speaker_changed = cur_speaker is not None and speaker != cur_speaker
        gap_too_large = (
            cur_end is not None
            and start_f is not None
            and start_f - cur_end > _MAX_SEGMENT_GAP_S
        )
        if speaker_changed or gap_too_large:
            flush()

        if cur_speaker is None:
            cur_speaker = speaker
        if cur_start is None and start_f is not None:
            cur_start = start_f

        cur_parts.append(text)
        if end_f is not None:
            cur_end = end_f

    flush()

    if not segments:
        text = str(_get(response, "text", "") or "").strip()
        if text:
            segments.append(
                TranscriptSegment(
                    chunk_index=chunk.index,
                    speaker_id="speaker_0",
                    start_time=round(chunk.start_time, 3),
                    end_time=round(chunk.start_time + chunk.duration, 3),
                    text=text,
                )
            )
    return segments


def _process_chunk(chunk: ChunkInfo, keyterms: list[str]) -> list[TranscriptSegment]:
    _validate_chunk(chunk)
    logger.info(
        "ElevenLabs Scribe v2 job chunk %d (%.1f-%.1fs, %.1fmin)",
        chunk.index,
        chunk.start_time,
        chunk.end_time,
        chunk.duration / 60.0,
    )
    response = _convert_with_retries(_client(), chunk, keyterms)
    segments = _parse_words_response(response, chunk)
    logger.info("ElevenLabs chunk %d: %d segments", chunk.index, len(segments))
    return segments


def transcribe_all_chunks_elevenlabs(chunks: list[ChunkInfo]) -> list[TranscriptSegment]:
    """Transcribe all chunks with ElevenLabs Scribe v2."""
    keyterms = _load_keyterms(GLOSSARY_FILE_PATH)
    if keyterms:
        logger.info("ElevenLabs keyterms loaded", extra={"count": len(keyterms)})

    all_segments: list[TranscriptSegment] = []
    with ThreadPoolExecutor(max_workers=ELEVENLABS_MAX_CONCURRENT_CHUNKS) as executor:
        future_to_chunk = {
            executor.submit(_process_chunk, chunk, keyterms): chunk
            for chunk in chunks
        }
        for future in as_completed(future_to_chunk):
            chunk = future_to_chunk[future]
            try:
                all_segments.extend(future.result())
            except Exception as e:
                logger.error("ElevenLabs chunk %d failed: %s", chunk.index, e)
                raise RuntimeError(f"ElevenLabs chunk {chunk.index} failed: {e}") from e

    all_segments.sort(key=lambda s: s.start_time)
    return all_segments
