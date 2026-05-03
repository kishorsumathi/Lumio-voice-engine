"""
Local pipeline runner — test the full pipeline without AWS.

Runs transcription + English translation on a local audio file and writes:

  <audio-stem>_results.json      Same schema the worker PUTs to S3 per job.
  <audio-stem>_transcript.txt    Human-readable transcript (speaker, timestamps,
                                 source line, English translation).

Usage:
    python scripts/run_local.py path/to/audio.mp3

Requires:
    - SARVAM_API_KEY env var (or .env with SARVAM_API_KEY=...)
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import tempfile
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "worker" / "src"))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)

from pipeline.audio import convert_to_mono_wav, get_duration  # noqa: E402
from pipeline.chunking import chunk_audio  # noqa: E402
from pipeline.merger import merge  # noqa: E402
from pipeline.results_writer import build_results_document  # noqa: E402
from pipeline.s3 import _MP4_REMAP  # noqa: E402
from pipeline.transcription import transcribe_all_chunks  # noqa: E402


def _segment_timestamp(seg) -> str:
    return (
        f"[{int(seg.start_time // 60):02d}:{int(seg.start_time % 60):02d} – "
        f"{int(seg.end_time // 60):02d}:{int(seg.end_time % 60):02d}]"
    )


def write_transcript_txt(audio_path: Path, merged, output_path: Path) -> None:
    lines = [
        f"Anchor-Voice Transcript — {audio_path.name}",
        "=" * 70,
        "",
    ]
    for seg in merged:
        ts = _segment_timestamp(seg)
        lines.append(f"Speaker {seg.speaker_id}  {ts}")
        lines.append(f"  {seg.text}")
        if seg.translation:
            lines.append(f"  [en] {seg.translation}")
        lines.append("")
    output_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nTranscript saved → {output_path}")


def run(audio_path: Path) -> None:
    print(f"\n{'='*60}")
    print(f"  Anchor-Voice Local Pipeline Runner")
    print(f"  File: {audio_path.name}")
    print(f"{'='*60}\n")

    job_id = uuid.uuid4()
    started_at = datetime.now(timezone.utc)
    print(f"Job ID: {job_id}")

    with tempfile.TemporaryDirectory() as tmp_str:
        work_dir = Path(tmp_str)
        try:
            local_audio = work_dir / audio_path.name
            shutil.copy2(str(audio_path), str(local_audio))

            ext = local_audio.suffix.lower()
            if ext in _MP4_REMAP:
                renamed = local_audio.with_suffix(_MP4_REMAP[ext])
                local_audio.rename(renamed)
                local_audio = renamed

            # Normalize to 16 kHz mono PCM WAV up-front — same contract as
            # the production worker (guarantees a readable RIFF duration
            # header even for browser MediaRecorder WebM, handles video
            # containers via `-vn`, standardizes the rest of the pipeline).
            local_audio = convert_to_mono_wav(local_audio, work_dir)
            duration = get_duration(local_audio)
            if duration <= 0:
                raise RuntimeError(
                    "Normalized audio has non-positive duration — upload may be empty or undecodable"
                )
            print(f"Duration: {duration:.1f}s ({duration/60:.1f} min)")

            chunks = chunk_audio(local_audio, work_dir, already_normalized=True)
            print(f"Chunks: {len(chunks)}")
            for c in chunks:
                print(f"  [{c.index}] {c.start_time:.1f}s–{c.end_time:.1f}s "
                      f"({c.duration/60:.1f}min) [{c.split_reason}]")

            print("\nTranscribing + translating chunks (parallel Saaras passes)...")
            transcript_segs = transcribe_all_chunks(chunks)
            print(f"Transcript segments: {len(transcript_segs)}")

            merged = merge(chunks, transcript_segs)
            print(f"\nMerged segments: {len(merged)}")
            non_empty_translation = sum(1 for s in merged if s.translation)
            print(f"Translation coverage: {non_empty_translation}/{len(merged)} segments")
            print("\n--- Transcript Preview ---")
            for seg in merged[:10]:
                print(f"  [Speaker {seg.speaker_id}] {seg.start_time:.1f}s: {seg.text[:80]}")
                if seg.translation:
                    print(f"      [en] {seg.translation[:80]}")
            if len(merged) > 10:
                print(f"  ... ({len(merged) - 10} more segments)")

            num_speakers = len(set(s.speaker_id for s in merged))
            completed_at = datetime.now(timezone.utc)

            document = build_results_document(
                job_id=job_id,
                source_bucket="local",
                source_key=str(audio_path),
                original_filename=audio_path.name,
                audio_duration_seconds=duration,
                num_chunks=len(chunks),
                num_speakers=num_speakers,
                source_language=None,
                merged=merged,
                started_at=started_at,
                completed_at=completed_at,
            )

            json_path = audio_path.parent / f"{audio_path.stem}_results.json"
            json_path.write_text(
                json.dumps(document, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(f"\nResults JSON saved → {json_path}")

            txt_path = audio_path.parent / f"{audio_path.stem}_transcript.txt"
            write_transcript_txt(audio_path, merged, txt_path)

            print(f"\n✓ Pipeline complete. Job ID: {job_id}")
            print(f"  Speakers: {num_speakers}  Segments: {len(merged)}")

        except Exception as e:
            tb = traceback.format_exc()
            print(f"\n✗ Pipeline failed: {e}")
            print(tb)
            sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Run Anchor-Voice pipeline locally")
    parser.add_argument("audio_file", help="Path to audio file")
    args = parser.parse_args()

    audio_path = Path(args.audio_file)
    if not audio_path.exists():
        print(f"ERROR: File not found: {audio_path}")
        sys.exit(1)

    run(audio_path)


if __name__ == "__main__":
    main()
