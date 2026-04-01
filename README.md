# Lumio

Audio transcription application with speaker diarization, real-time live transcription, and a shared audio library. Built with Streamlit and powered by Deepgram's speech-to-text API.

## Features

- **Upload & Transcribe** — Upload audio files (WAV, MP3, M4A, FLAC, etc.) of any length and get speaker-diarized transcriptions. Edit speaker labels (e.g., "Speaker 0" → "Kishor") and changes reflect across the entire transcript. Download transcripts as `.txt`.

- **Live Transcription** — Real-time speech-to-text directly from your browser microphone. Audio streams via WebSocket to Deepgram with a live waveform visualizer. Text appears as you speak.

- **Audio Library** — Save uploaded audio files with a subject/description to a shared library. Browse, play, and transcribe any file from the library.

- **Model Selection** — Choose between transcription models (Deepgram Nova-3, Nova-2) from the sidebar. Additional models (OpenAI Whisper) are listed as coming soon.

## Project Structure

```
Lumio-voice/
├── app.py                                  # Streamlit entry point
├── src/
│   └── anchor_voice/
│       ├── config.py                       # Settings, API keys, model registry
│       ├── transcription.py                # Deepgram client, transcribe, diarization
│       ├── library.py                      # Audio library CRUD (file storage + JSON index)
│       └── components/
│           ├── transcript.py               # Speaker editor, transcript renderer, download
│           └── live_transcription.py       # Browser-side JS for real-time mic streaming
├── data/
│   └── audio_library/                      # Stored audio files + index.json (gitignored)
├── .streamlit/
│   └── config.toml                         # Streamlit config (5GB upload limit)
├── .env                                    # DEEPGRAM_API_KEY (not committed)
├── pyproject.toml                          # Project metadata & dependencies (uv)
└── uv.lock                                # Locked dependencies
```

## Setup

### Prerequisites

- Python 3.13+
- [uv](https://docs.astral.sh/uv/) package manager
- [Deepgram API key](https://console.deepgram.com/)

### Installation

```bash
# Clone the repository
git clone <repo-url>
cd anchor-voice

# Install dependencies
uv sync

# Configure environment
cp .env.example .env
# Edit .env and add your Deepgram API key
```

### Environment Variables

Create a `.env` file in the project root:

```
DEEPGRAM_API_KEY=your_deepgram_api_key_here
```

### Run

```bash
uv run streamlit run app.py
```

The app will open at `http://localhost:8501`.

## Architecture

### Two Transcription Paths

| Feature | Upload / Library | Live Transcription |
|---|---|---|
| **API** | Deepgram REST (`POST /v1/listen`) | Deepgram WebSocket (`wss://api.deepgram.com/v1/listen`) |
| **Runs on** | Python (server-side) | JavaScript (browser-side) |
| **Diarization** | Yes (full speaker separation) | No (single-speaker captioning) |
| **Speaker editing** | Yes | No |
| **Audio source** | Uploaded file | Browser microphone |

### Why Browser-Side for Live?

Streamlit reruns the entire Python script on every UI interaction. A Python-based WebSocket would get killed on each rerun. The live transcription runs entirely in JavaScript inside an embedded HTML component (`st.components.v1.html`), keeping the WebSocket connection alive independently of Streamlit's lifecycle.

### Audio Library Storage

Files are stored on disk in `data/audio_library/` with metadata tracked in `index.json`. This is a simple file-based approach — no database required.

## Supported Audio Formats

WAV, MP3, M4A, FLAC, OGG, WMA, AAC, WebM, MP4

## Adding New Models

Edit `MODELS` in `src/anchor_voice/config.py`:

```python
MODELS = {
    "Deepgram Nova-3": {"provider": "deepgram", "model": "nova-3", "available": True},
    "Your New Model": {"provider": "provider", "model": "model-id", "available": True},
}
```

Set `"available": False` to list a model as "Coming Soon" (disables transcription buttons).
