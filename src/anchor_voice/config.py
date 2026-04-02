import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY", "")

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DATA_DIR = BASE_DIR / "data"
LIBRARY_DIR = DATA_DIR / "audio_library"
LIBRARY_INDEX = LIBRARY_DIR / "index.json"

AUDIO_TYPES = ["wav", "mp3", "m4a", "flac", "ogg", "wma", "aac", "webm", "mp4"]

MODELS = {
    "Deepgram Nova-3": {"provider": "deepgram", "model": "nova-3", "available": True},
    "Deepgram Nova-3 Medical": {"provider": "deepgram", "model": "nova-3-medical", "available": True},
    "Deepgram Nova-2": {"provider": "deepgram", "model": "nova-2", "available": True},
    "OpenAI Whisper Large v3 (Coming Soon)": {"provider": "openai", "model": "whisper-large-v3", "available": False},
    "OpenAI Whisper Large v3 Turbo (Coming Soon)": {"provider": "openai", "model": "whisper-large-v3-turbo", "available": False},
}
