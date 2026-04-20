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


def _ffprobe_streams(file_path: Path) -> list[dict]:
    """Run ffprobe and return the list of streams. Raises on probe failure."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "quiet",
            "-print_format", "json",
            "-show_streams",
            str(file_path),
        ],
        capture_output=True, text=True, check=True,
        timeout=_FFPROBE_TIMEOUT_S,
    )
    data = json.loads(result.stdout)
    return data.get("streams", []) or []


def get_duration(file_path: Path) -> float:
    """Return audio duration in seconds using ffprobe."""
    streams = _ffprobe_streams(file_path)
    for stream in streams:
        if stream.get("codec_type") == "audio":
            duration = float(stream.get("duration", 0))
            if duration > 0:
                logger.info("Duration: %.1fs for %s", duration, file_path.name)
                return duration
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

    Used for silero-vad (default output: ``{stem}_16k_mono.wav`` in dest_dir)
    and for single-chunk Sarvam uploads when ``output_path`` is set.
    """
    out_path = output_path or (dest_dir / (file_path.stem + "_16k_mono.wav"))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    logger.info("Converting to 16kHz mono WAV: %s → %s", file_path.name, out_path.name)
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", str(file_path),
            "-ar", str(sample_rate),   # resample to target rate
            "-ac", "1",                # mono
            "-acodec", "pcm_s16le",    # 16-bit PCM WAV
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
