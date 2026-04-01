import json
from datetime import datetime
from pathlib import Path

from .config import LIBRARY_DIR, LIBRARY_INDEX


def load_library() -> list[dict]:
    LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
    if LIBRARY_INDEX.exists():
        return json.loads(LIBRARY_INDEX.read_text())
    return []


def save_to_library(file_bytes: bytes, filename: str, subject: str) -> str:
    LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
    entries = load_library()

    base = Path(filename).stem
    ext = Path(filename).suffix
    stored_name = filename
    counter = 1
    while (LIBRARY_DIR / stored_name).exists():
        stored_name = f"{base}_{counter}{ext}"
        counter += 1

    (LIBRARY_DIR / stored_name).write_bytes(file_bytes)

    entries.append({
        "filename": stored_name,
        "original_name": filename,
        "subject": subject,
        "uploaded_at": datetime.now().isoformat(),
    })
    LIBRARY_INDEX.write_text(json.dumps(entries, indent=2))
    return stored_name


def delete_from_library(filename: str):
    entries = load_library()
    entries = [e for e in entries if e["filename"] != filename]
    LIBRARY_INDEX.write_text(json.dumps(entries, indent=2))
    filepath = LIBRARY_DIR / filename
    if filepath.exists():
        filepath.unlink()
