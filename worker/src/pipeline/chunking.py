"""
VAD-based smart audio chunking with overlap for cross-chunk speaker stitching.

Strategy:
  1. Run silero-vad on the full WAV to detect speech/silence segments.
  2. Compute an adaptive silence threshold from the audio's own distribution
     (40th percentile of observed silence durations).
  3. Use a greedy algorithm to split at the longest silence gap inside a
     ±5-minute search window around each TARGET_CHUNK_DURATION_S boundary.
  4. Each chunk (except the first) starts OVERLAP_DURATION_S before the split
     boundary so the overlap region can be used for speaker ID stitching.
"""
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .audio import convert_to_mono_wav, split_audio_segment, get_duration
from .config import (
    SARVAM_MAX_SINGLE_DURATION_S,
    MAX_CHUNK_DURATION_S,
    TARGET_CHUNK_DURATION_S,
    MIN_SILENCE_DURATION_S,
    SILENCE_SEARCH_WINDOW_S,
    OVERLAP_DURATION_S,
)

logger = logging.getLogger(__name__)


@dataclass
class SilenceSegment:
    start: float
    end: float

    @property
    def duration(self) -> float:
        return self.end - self.start

    @property
    def midpoint(self) -> float:
        return (self.start + self.end) / 2.0


@dataclass
class ChunkInfo:
    path: Path
    index: int
    start_time: float       # actual start of audio file (may include overlap prefix)
    end_time: float         # end of this chunk's unique content
    content_start: float    # where this chunk's unique content begins (= prev chunk's end_time)
    duration: float         # duration of the audio file (including overlap)
    split_reason: str       # "silence_gap" | "fallback_closest" | "forced_boundary" | "single" | "remainder"


def _run_silero_vad(wav_path: Path) -> list[SilenceSegment]:
    # Audio is loaded via soundfile (already a dep) rather than torchaudio.load.
    # torchaudio ≥ 2.9 delegates decoding to a separate torchcodec package which
    # is not in our image and isn't worth the bloat — soundfile reads WAV/FLAC
    # natively via libsndfile, which ffmpeg has already normalised to 16kHz mono
    # upstream (convert_to_mono_wav).
    import numpy as np
    import soundfile as sf
    import torch

    model, utils = torch.hub.load(
        repo_or_dir="snakers4/silero-vad",
        model="silero_vad",
        force_reload=False,
        onnx=False,
        trust_repo=True,
    )
    get_speech_ts, *_ = utils

    audio_np, sr = sf.read(str(wav_path), dtype="float32", always_2d=False)
    # Defensive: collapse any stereo to mono (ffmpeg already produces mono).
    if audio_np.ndim > 1:
        audio_np = audio_np.mean(axis=1)
    if sr != 16000:
        # Extremely rare path — ffmpeg already resampled. Keep a fallback using
        # torchaudio.functional so we don't silently drift when the upstream
        # contract changes.
        import torchaudio.functional as F
        waveform = F.resample(
            torch.from_numpy(audio_np), orig_freq=sr, new_freq=16000
        )
    else:
        waveform = torch.from_numpy(np.ascontiguousarray(audio_np))

    speech_timestamps = get_speech_ts(
        waveform,
        model,
        sampling_rate=16000,
        threshold=0.5,
        min_speech_duration_ms=250,
        min_silence_duration_ms=int(MIN_SILENCE_DURATION_S * 1000),
    )

    total_duration = len(waveform) / 16000.0
    return _speech_to_silence(speech_timestamps, total_duration, sr=16000)


def _speech_to_silence(
    speech_timestamps: list[dict],
    total_duration: float,
    sr: int = 16000,
) -> list[SilenceSegment]:
    silences: list[SilenceSegment] = []
    prev_end = 0.0
    for ts in speech_timestamps:
        start = ts["start"] / sr
        if start > prev_end + 0.01:
            silences.append(SilenceSegment(start=prev_end, end=start))
        prev_end = ts["end"] / sr
    if prev_end < total_duration - 0.01:
        silences.append(SilenceSegment(start=prev_end, end=total_duration))
    return silences


def _compute_adaptive_threshold(silences: list[SilenceSegment]) -> float:
    if not silences:
        return MIN_SILENCE_DURATION_S
    durations = [s.duration for s in silences]
    threshold = float(np.percentile(durations, 40))
    clamped = max(0.3, min(1.5, threshold))
    logger.debug(
        "Silence durations — min=%.2fs, p40=%.2fs, max=%.2fs → threshold=%.2fs",
        min(durations), threshold, max(durations), clamped,
    )
    return clamped


def _find_best_split(
    candidates: list[SilenceSegment],
    target: float,
    window: float,
    chunk_start: float,
    max_end: float,
) -> tuple[float, str]:
    window_lo = max(chunk_start, target - window)
    window_hi = min(max_end, target + window)

    in_window = [c for c in candidates if window_lo <= c.midpoint <= window_hi]
    if in_window:
        best = max(in_window, key=lambda s: s.duration)
        return best.midpoint, "silence_gap"

    safe_zone = [c for c in candidates if chunk_start < c.midpoint <= max_end]
    if safe_zone:
        best = min(safe_zone, key=lambda s: abs(s.midpoint - target))
        return best.midpoint, "fallback_closest"

    logger.warning(
        "No silence found near %.1fs target or in safe zone — forcing boundary cut", target
    )
    return max_end - 60.0, "forced_boundary"


def chunk_audio(
    source_path: Path,
    dest_dir: Path,
    target_duration: int = TARGET_CHUNK_DURATION_S,
    max_duration: int = MAX_CHUNK_DURATION_S,
    overlap_duration: int = OVERLAP_DURATION_S,
    *,
    already_normalized: bool = False,
) -> list[ChunkInfo]:
    """
    Split source_path into overlapping chunks for Sarvam transcription.

    Each chunk (except the first) starts overlap_duration seconds before
    its content_start so the overlap region can be used for speaker stitching.

    When ``already_normalized`` is True, ``source_path`` is assumed to be a
    16 kHz mono 16-bit PCM WAV produced by :func:`convert_to_mono_wav`, and
    we skip re-encoding — the short path copies the file to ``chunk_000.wav``
    and the long path uses it directly as the VAD master.
    """
    total_duration = get_duration(source_path)
    logger.info(
        "Chunking %s — total=%.1fs, target=%ds, max=%ds, overlap=%ds, already_normalized=%s",
        source_path.name, total_duration, target_duration, max_duration, overlap_duration,
        already_normalized,
    )

    # ── Fast path: fits within Sarvam's 60-min limit → no split needed ──
    if total_duration <= SARVAM_MAX_SINGLE_DURATION_S:
        chunk_path = dest_dir / "chunk_000.wav"
        if already_normalized:
            # Source is already the canonical 16 kHz mono PCM WAV — just
            # materialize it as chunk_000.wav (cheap copy within tempdir).
            if source_path.resolve() != chunk_path.resolve():
                shutil.copy2(source_path, chunk_path)
        else:
            convert_to_mono_wav(source_path, dest_dir, output_path=chunk_path)
        logger.info("Single chunk (no split needed), 16 kHz mono WAV for Sarvam")
        return [ChunkInfo(
            path=chunk_path,
            index=0,
            start_time=0.0,
            end_time=total_duration,
            content_start=0.0,
            duration=total_duration,
            split_reason="single",
        )]

    # ── Mono WAV master for VAD ──────────────────────────────────────────
    if already_normalized:
        wav_path = source_path
        wav_is_owned = False
    else:
        logger.info("Converting to mono WAV for VAD analysis")
        wav_path = convert_to_mono_wav(source_path, dest_dir)
        wav_is_owned = True

    logger.info("Running silero-vad")
    all_silences = _run_silero_vad(wav_path)
    logger.info("Found %d silence segments", len(all_silences))

    threshold = _compute_adaptive_threshold(all_silences)
    candidates = [s for s in all_silences if s.duration >= threshold]
    logger.info("Split candidates (≥ %.2fs): %d", threshold, len(candidates))

    # ── Determine split boundaries ────────────────────────────────────────
    boundaries: list[tuple[float, str]] = []   # (end_time, reason)
    chunk_start = 0.0

    while chunk_start < total_duration:
        remaining = total_duration - chunk_start
        if remaining <= max_duration:
            boundaries.append((total_duration, "remainder"))
            break

        target_end = chunk_start + target_duration
        max_end = chunk_start + max_duration - 60.0

        end_time, reason = _find_best_split(
            candidates=candidates,
            target=target_end,
            window=float(SILENCE_SEARCH_WINDOW_S),
            chunk_start=chunk_start,
            max_end=max_end,
        )
        boundaries.append((end_time, reason))
        chunk_start = end_time

    # ── Extract chunk audio files with overlap prefix ─────────────────────
    chunks: list[ChunkInfo] = []
    prev_end = 0.0

    for idx, (end_time, reason) in enumerate(boundaries):
        content_start = prev_end
        # Add overlap prefix for all chunks after the first
        audio_start = max(0.0, content_start - overlap_duration) if idx > 0 else 0.0

        start_ms = int(audio_start * 1000)
        end_ms = int(end_time * 1000)
        chunk_path = dest_dir / f"chunk_{idx:03d}.wav"

        # Same 16 kHz mono master as VAD: one normalize, N ffmpeg stream-copies
        split_audio_segment(wav_path, chunk_path, start_ms, end_ms)

        logger.info(
            "Chunk %d: file=%.1fs–%.1fs, content=%.1fs–%.1fs (%.1fmin) [%s]",
            idx, audio_start, end_time, content_start, end_time,
            (end_time - content_start) / 60.0, reason,
        )

        chunks.append(ChunkInfo(
            path=chunk_path,
            index=idx,
            start_time=audio_start,
            end_time=end_time,
            content_start=content_start,
            duration=end_time - audio_start,
            split_reason=reason,
        ))
        prev_end = end_time

    if wav_is_owned:
        try:
            wav_path.unlink()
        except OSError:
            pass

    logger.info(
        "Chunking complete: %d chunks from %.1f min audio",
        len(chunks), total_duration / 60.0,
    )
    return chunks
