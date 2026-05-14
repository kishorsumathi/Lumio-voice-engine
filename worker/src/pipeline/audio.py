"""Audio utilities: duration detection, format conversion, validation."""
import logging
import subprocess
import json
from pathlib import Path

logger = logging.getLogger(__name__)

# Extensions Sarvam accepts directly
SARVAM_ACCEPTED_EXTS = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".aac", ".webm"}


_FFPROBE_TIMEOUT_S = 30
_FFMPEG_TIMEOUT_S = 1800  # 30 min — covers full-session conversions


def _ffprobe_json(file_path: Path, *, include_format: bool = False) -> dict:
    """Run ffprobe and return the parsed JSON. Raises on probe failure."""
    args = [
        "ffprobe", "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
    ]
    if include_format:
        args.append("-show_format")
    args.append(str(file_path))
    result = subprocess.run(
        args,
        capture_output=True, text=True, check=True,
        timeout=_FFPROBE_TIMEOUT_S,
    )
    return json.loads(result.stdout)


def _ffprobe_streams(file_path: Path) -> list[dict]:
    """Run ffprobe and return the list of streams. Raises on probe failure."""
    return _ffprobe_json(file_path).get("streams", []) or []


def _safe_float(value) -> float:
    try:
        return float(value) if value is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def get_duration(file_path: Path) -> float:
    """
    Return audio duration in seconds using ffprobe.

    Tries the audio stream's ``duration`` first (cheapest, what most clean
    containers expose), then falls back to the format-level ``duration``.
    Browser ``MediaRecorder`` WebM files often ship with no Matroska
    ``Duration`` element at all — the worker's upstream normalization step
    rewrites those to WAV which always has a usable ``duration``, but this
    fallback keeps the helper robust if it is ever called on other formats
    where only ``format.duration`` is populated.
    """
    data = _ffprobe_json(file_path, include_format=True)
    for stream in data.get("streams", []) or []:
        if stream.get("codec_type") == "audio":
            duration = _safe_float(stream.get("duration"))
            if duration > 0:
                logger.info("Duration: %.1fs for %s", duration, file_path.name)
                return duration
    fmt_duration = _safe_float((data.get("format") or {}).get("duration"))
    if fmt_duration > 0:
        logger.info(
            "Duration (format fallback): %.1fs for %s",
            fmt_duration, file_path.name,
        )
        return fmt_duration
    raise RuntimeError(f"Could not determine audio duration for {file_path.name}")


def has_video_stream(file_path: Path) -> bool:
    """
    Return True if the file contains a video stream (not audio-only).

    Only swallows the narrow "no decodable streams / empty output" case that
    legitimately means "no video." Any other failure (ffprobe missing, bad
    path, permission denied, timeout) propagates — silently misclassifying
    those as audio-only and skipping extraction would push video-muxed files
    downstream as audio.
    """
    try:
        streams = _ffprobe_streams(file_path)
    except subprocess.CalledProcessError as e:
        # ffprobe returned non-zero with --v quiet: file is probably unreadable
        # or truncated. Surface the failure rather than guessing.
        raise RuntimeError(
            f"ffprobe failed to inspect {file_path.name}: returncode={e.returncode}"
        ) from e
    return any(s.get("codec_type") == "video" for s in streams)


def ensure_audio_only(file_path: Path, dest_dir: Path) -> Path:
    """
    If the file has a video stream, extract audio-only to .m4a.
    Otherwise return the original path unchanged.
    """
    if not has_video_stream(file_path):
        return file_path

    out_path = dest_dir / (file_path.stem + "_audio.m4a")
    logger.info("Extracting audio from video file %s → %s", file_path.name, out_path.name)
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", str(file_path),
            "-vn",                  # drop video stream
            "-acodec", "copy",      # copy audio codec (fast, lossless)
            str(out_path),
        ],
        check=True, capture_output=True, timeout=_FFMPEG_TIMEOUT_S,
    )
    return out_path


def convert_to_mono_wav(
    file_path: Path,
    dest_dir: Path,
    sample_rate: int = 16000,
    *,
    output_path: Path | None = None,
) -> Path:
    """
    Convert audio to 16 kHz mono 16-bit PCM WAV (Sarvam-recommended profile).

    Also drops any video stream via ``-vn`` so video-muxed containers
    (``.mp4``, video-bearing ``.webm``) are handled in one pass — the
    pipeline can skip a separate audio-extraction step and normalize
    straight from the original upload.

    Used for silero-vad (default output: ``{stem}_16k_mono.wav`` in dest_dir)
    and for single-chunk Sarvam uploads when ``output_path`` is set.
    """
    out_path = output_path or (dest_dir / (file_path.stem + "_16k_mono.wav"))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    logger.info("Converting to 16kHz mono WAV: %s → %s", file_path.name, out_path.name)
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", str(file_path),
            "-vn",                     # drop any video stream
            "-ar", str(sample_rate),   # resample to target rate
            "-ac", "1",                # mono
            "-acodec", "pcm_s16le",    # 16-bit PCM WAV
            str(out_path),
        ],
        check=True, capture_output=True, timeout=_FFMPEG_TIMEOUT_S,
    )
    return out_path


def convert_to_speech_enhanced_wav(
    file_path: Path,
    dest_dir: Path,
    *,
    slow_down: bool = False,
    output_path: Path | None = None,
) -> Path:
    """
    Convert audio to the same 16 kHz mono PCM WAV contract as
    `convert_to_mono_wav`, with light speech-focused cleanup.

    This is intended for batch transcription, not realtime audio: high/low pass
    filtering removes rumble and ultrasonic noise, EQ boosts consonant clarity,
    dynamic normalisation evens volume, and a limiter prevents clipping.
    """
    out_path = output_path or (dest_dir / (file_path.stem + "_speech_16k_mono.wav"))
    out_path.parent.mkdir(parents=True, exist_ok=True)

    filters = [
        "highpass=f=100",
        "lowpass=f=7600",
        "equalizer=f=2500:t=q:w=1.2:g=4",
        "equalizer=f=4000:t=q:w=1.2:g=4",
        "equalizer=f=6000:t=q:w=1.2:g=2",
        "dynaudnorm=f=100:g=18:p=0.95",
        "alimiter=limit=0.95",
    ]
    if slow_down:
        filters.insert(0, "atempo=0.94")

    logger.info(
        "Converting to speech-enhanced 16kHz mono WAV: %s → %s",
        file_path.name,
        out_path.name,
    )
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", str(file_path),
            "-vn",
            "-ac", "1",
            "-ar", "16000",
            "-sample_fmt", "s16",
            "-af", ",".join(filters),
            str(out_path),
        ],
        check=True, capture_output=True, timeout=_FFMPEG_TIMEOUT_S,
    )
    return out_path


def split_audio_segment(
    source_path: Path,
    dest_path: Path,
    start_ms: int,
    end_ms: int,
) -> Path:
    """
    Extract a time slice [start_ms, end_ms] from ``source_path`` into ``dest_path``.

    ``source_path`` must already be **16 kHz mono 16-bit PCM WAV** (e.g. the full-file
    output of :func:`convert_to_mono_wav`). Uses ffmpeg **stream copy** — no
    per-chunk resample — so chunk files match that profile with minimal CPU.
    """
    start_s = start_ms / 1000.0
    duration_s = (end_ms - start_ms) / 1000.0
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-y",
            "-ss", f"{start_s:.3f}",
            "-i", str(source_path),
            "-t", f"{duration_s:.3f}",
            "-c", "copy",
            str(dest_path),
        ],
        check=True, capture_output=True, timeout=_FFMPEG_TIMEOUT_S,
    )
    return dest_path
