from deepgram import DeepgramClient

from .config import DEEPGRAM_API_KEY, MODELS


def get_client():
    return DeepgramClient(api_key=DEEPGRAM_API_KEY)


def transcribe_audio(audio_bytes: bytes, model_key: str, keyterms: list[str] | None = None) -> list[dict]:
    model_info = MODELS[model_key]
    if not model_info["available"]:
        raise ValueError(f"{model_key} is not yet available.")

    client = get_client()
    kwargs = dict(
        request=audio_bytes,
        model=model_info["model"],
        language="en-IN",
        smart_format=True,
        punctuate=True,
        diarize=True,
        utterances=True,
    )
    if keyterms:
        kwargs["keyterm"] = keyterms

    response = client.listen.v1.media.transcribe_file(**kwargs)
    words = response.results.channels[0].alternatives[0].words or []
    return group_words_by_speaker(words)


def group_words_by_speaker(words) -> list[dict]:
    if not words:
        return []
    utterances = []
    current_speaker = None
    current_words = []
    for w in words:
        speaker = getattr(w, "speaker", None)
        if speaker is None:
            speaker = w.get("speaker", 0) if isinstance(w, dict) else 0
        if speaker != current_speaker:
            if current_words:
                utterances.append({"speaker": current_speaker, "text": " ".join(current_words)})
            current_speaker = speaker
            current_words = [w.word if hasattr(w, "word") else w.get("word", "")]
        else:
            current_words.append(w.word if hasattr(w, "word") else w.get("word", ""))
    if current_words:
        utterances.append({"speaker": current_speaker, "text": " ".join(current_words)})
    return utterances
