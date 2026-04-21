"""
Local pipeline runner — test the full pipeline without AWS.

Usage:
    python scripts/run_local.py path/to/audio.mp3 [--languages en,hi]

Requires:
    - DATABASE_URL env var pointing to a local PostgreSQL instance
    - SARVAM_API_KEY env var
"""
import argparse
import os
import sys
import uuid
from pathlib import Path

# Add worker/src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "worker" / "src"))

import logging
from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)

from pipeline.audio import convert_to_mono_wav, get_duration
from pipeline.chunking import chunk_audio
from pipeline.db import create_tables, health_check
from pipeline.job_status import create_job, mark_failed, store_results, update_status
from pipeline.merger import merge
from pipeline.s3 import _MP4_REMAP
from pipeline.transcription import transcribe_all_chunks
from pipeline.translation import translate_segments

import tempfile
import shutil
import traceback


def _segment_timestamp(seg) -> str:
    return (
        f"[{int(seg.start_time // 60):02d}:{int(seg.start_time % 60):02d} – "
        f"{int(seg.end_time // 60):02d}:{int(seg.end_time % 60):02d}]"
    )


def write_output(audio_path: Path, merged, translations: dict, output_path: Path) -> None:
    lines = []
    lines.append(f"Anchor-Voice Transcript — {audio_path.name}")
    lines.append("=" * 70)
    lines.append("")

    langs = list(translations.keys())

    for seg in merged:
        ts = _segment_timestamp(seg)
        lines.append(f"Speaker {seg.speaker_id}  {ts}")
        lines.append(f"  {seg.text}")
        for lang in langs:
            t = translations[lang][seg.segment_index]
            if t.translated_text:
                lines.append(f"  [{lang}] {t.translated_text}")
        lines.append("")

    output_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"\nTranscript saved → {output_path}")


def write_translation_only_files(audio_path: Path, merged, translations: dict) -> None:
    """
    One .txt per target language: same Speaker + timestamp layout as the combined
    transcript, but only the translated line per segment (local runner only).
    """
    out_dir = audio_path.parent
    stem = audio_path.stem
    for lang, trans_list in translations.items():
        lang_slug = lang.replace("/", "_").replace(" ", "_")
        path = out_dir / f"{stem}_translation_{lang_slug}.txt"
        lines = [
            f"Anchor-Voice Translation ({lang}) — {audio_path.name}",
            "=" * 70,
            "",
        ]
        for seg in merged:
            t = trans_list[seg.segment_index]
            ts = _segment_timestamp(seg)
            lines.append(f"Speaker {seg.speaker_id}  {ts}")
            lines.append(f"  {t.translated_text}")
            lines.append("")
        path.write_text("\n".join(lines), encoding="utf-8")
        print(f"Translation saved → {path}")


def run(audio_path: Path, target_languages: list[str]) -> None:
    print(f"\n{'='*60}")
    print(f"  Anchor-Voice Local Pipeline Runner")
    print(f"  File: {audio_path.name}")
    print(f"  Languages: {target_languages}")
    print(f"{'='*60}\n")

    if not health_check():
        print("ERROR: Cannot connect to database. Check DATABASE_URL.")
        sys.exit(1)

    create_tables()

    # Fake S3 reference for local run
    job_id = create_job("local", str(audio_path), audio_path.name)
    print(f"Job ID: {job_id}")

    with tempfile.TemporaryDirectory() as tmp_str:
        work_dir = Path(tmp_str)
        try:
            # Copy file to work dir (simulates S3 download)
            local_audio = work_dir / audio_path.name
            shutil.copy2(str(audio_path), str(local_audio))

            # Handle .mp4 → .m4a rename
            ext = local_audio.suffix.lower()
            if ext in _MP4_REMAP:
                renamed = local_audio.with_suffix(_MP4_REMAP[ext])
                local_audio.rename(renamed)
                local_audio = renamed

            update_status(job_id, "downloading")
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

            update_status(job_id, "chunking", audio_duration_seconds=round(duration, 2))
            chunks = chunk_audio(local_audio, work_dir, already_normalized=True)
            print(f"Chunks: {len(chunks)}")
            for c in chunks:
                print(f"  [{c.index}] {c.start_time:.1f}s–{c.end_time:.1f}s "
                      f"({c.duration/60:.1f}min) [{c.split_reason}]")

            update_status(job_id, "transcribing", num_chunks=len(chunks))
            print("\nTranscribing chunks...")
            transcript_segs = transcribe_all_chunks(chunks)
            print(f"Transcript segments: {len(transcript_segs)}")

            update_status(job_id, "merging")
            merged = merge(chunks, transcript_segs)
            print(f"\nMerged segments: {len(merged)}")
            print("\n--- Transcript Preview ---")
            for seg in merged[:10]:
                print(f"  [Speaker {seg.speaker_id}] {seg.start_time:.1f}s: {seg.text[:80]}")
            if len(merged) > 10:
                print(f"  ... ({len(merged) - 10} more segments)")

            update_status(job_id, "translating")
            print(f"\nTranslating {len(merged)} segments → {target_languages}...")
            translations = translate_segments(merged, target_languages)
            for lang, segs in translations.items():
                non_empty = sum(1 for t in segs if t.translated_text)
                print(f"  {lang}: {non_empty}/{len(segs)} segments translated")

            segments_for_db = [
                {
                    "chunk_index": s.chunk_index,
                    "segment_index": s.segment_index,
                    "speaker_id": s.speaker_id,
                    "start_time": s.start_time,
                    "end_time": s.end_time,
                    "text": s.text,
                    "confidence": s.confidence,
                }
                for s in merged
            ]
            translations_for_db = {
                lang: [{"segment_index": t.segment_index, "translated_text": t.translated_text}
                       for t in trans_list]
                for lang, trans_list in translations.items()
            }
            num_speakers = len(set(s.speaker_id for s in merged))
            store_results(job_id, segments_for_db, translations_for_db, num_speakers)
            update_status(job_id, "completed", num_speakers=num_speakers)

            output_path = audio_path.parent / (audio_path.stem + "_transcript.txt")
            write_output(audio_path, merged, translations, output_path)
            write_translation_only_files(audio_path, merged, translations)

            print(f"\n✓ Pipeline complete. Job ID: {job_id}")
            print(f"  Speakers: {num_speakers}  Segments: {len(merged)}")

        except Exception as e:
            tb = traceback.format_exc()
            mark_failed(job_id, tb)
            print(f"\n✗ Pipeline failed: {e}")
            print(tb)
            sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Run Anchor-Voice pipeline locally")
    parser.add_argument("audio_file", help="Path to audio file")
    parser.add_argument(
        "--languages", default="en",
        help="Comma-separated target language codes (default: en)"
    )
    args = parser.parse_args()

    audio_path = Path(args.audio_file)
    if not audio_path.exists():
        print(f"ERROR: File not found: {audio_path}")
        sys.exit(1)

    target_languages = [l.strip() for l in args.languages.split(",") if l.strip()]
    run(audio_path, target_languages)


if __name__ == "__main__":
    main()
