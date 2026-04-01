import streamlit as st
import streamlit.components.v1 as components

from src.anchor_voice.config import AUDIO_TYPES, DEEPGRAM_API_KEY, LIBRARY_DIR, MODELS
from src.anchor_voice.transcription import transcribe_audio
from src.anchor_voice.library import load_library, save_to_library, delete_from_library
from src.anchor_voice.components.transcript import (
    show_speaker_editor,
    show_transcript_with_download,
)
from src.anchor_voice.components.live_transcription import get_live_html


# ── Page config ──
st.set_page_config(page_title="Lumio", page_icon="🎙️", layout="wide")
st.title("Lumio - Transcription")

# ── Sidebar: Model Selection & Keyterms ──
with st.sidebar:
    st.header("Settings")
    selected_model = st.selectbox(
        "Transcription Model",
        options=list(MODELS.keys()),
        index=0,
    )
    model_info = MODELS[selected_model]
    if not model_info["available"]:
        st.warning(f"⚠️ {selected_model} is under development and not yet available for transcription.")
    else:
        st.success(f"✅ {selected_model}")

    st.markdown("---")
    st.subheader("Keyterm Prompting")
    st.caption("Boost recognition of important words — names, product terms, jargon. Up to 100 terms.")
    keyterms_input = st.text_area(
        "Keyterms (one per line)",
        placeholder="plaud\nsukoon",
        key="keyterms_input",
    )
    keyterms = [t.strip() for t in keyterms_input.split("\n") if t.strip()] if keyterms_input else []
    if keyterms:
        st.info(f"{len(keyterms)} keyterm(s) active")

# ── Tabs ──
tab_upload, tab_live, tab_library = st.tabs(["📁 Upload Audio", "🎤 Live Transcription", "📚 Audio Library"])

# ════════════════════════════════════════════
# TAB 1: Upload & Transcribe
# ════════════════════════════════════════════
with tab_upload:
    uploaded_file = st.file_uploader("Upload an audio file", type=AUDIO_TYPES)

    if uploaded_file is not None:
        st.audio(uploaded_file)

        col_transcribe, col_save = st.columns(2)

        with col_transcribe:
            if st.button("Transcribe", key="transcribe_btn", disabled=not model_info["available"]):
                with st.spinner("Transcribing... (this may take a while for long files)"):
                    try:
                        audio_bytes = uploaded_file.read()
                        st.session_state["upload_audio_bytes"] = audio_bytes
                        utterances = transcribe_audio(audio_bytes, selected_model, keyterms=keyterms)
                        st.session_state["upload_utterances"] = utterances
                        speakers = sorted(set(u["speaker"] for u in utterances))
                        st.session_state["upload_speaker_map"] = {f"Speaker {s}": f"Speaker {s}" for s in speakers}
                    except Exception as e:
                        st.error(f"Transcription failed: {e}")

        with col_save:
            with st.expander("💾 Save to Audio Library"):
                subject = st.text_input(
                    "Subject / Description",
                    placeholder="e.g. Daily catchup call, Plaud recording",
                    key="upload_subject",
                )
                if st.button("Save to Library", key="save_to_lib_btn"):
                    if not subject.strip():
                        st.warning("Please enter a subject.")
                    else:
                        audio_bytes = uploaded_file.read()
                        if not audio_bytes:
                            audio_bytes = st.session_state.get("upload_audio_bytes", b"")
                            if not audio_bytes:
                                uploaded_file.seek(0)
                                audio_bytes = uploaded_file.read()
                        stored = save_to_library(audio_bytes, uploaded_file.name, subject.strip())
                        st.success(f"Saved as **{stored}** to library!")

    # ── Display results ──
    if "upload_utterances" in st.session_state:
        utterances = st.session_state["upload_utterances"]
        speaker_map = st.session_state["upload_speaker_map"]
        speaker_map = show_speaker_editor(utterances, speaker_map, "upload_speaker_edit")
        st.session_state["upload_speaker_map"] = speaker_map
        show_transcript_with_download(
            utterances, speaker_map, "transcript.txt", "upload",
            utterances_session_key="upload_utterances",
        )

# ════════════════════════════════════════════
# TAB 2: Live Transcription
# ════════════════════════════════════════════
with tab_live:
    if not model_info["available"]:
        st.warning(f"⚠️ {selected_model} is not available for live transcription yet.")
    else:
        dg_model = MODELS[selected_model]["model"]
        live_html = get_live_html(DEEPGRAM_API_KEY, dg_model)
        components.html(live_html, height=450, scrolling=True)

# ════════════════════════════════════════════
# TAB 3: Audio Library
# ════════════════════════════════════════════
with tab_library:
    st.subheader("Shared Audio Library")
    st.caption("Browse, play, and transcribe pre-recorded audio files.")

    entries = load_library()

    if not entries:
        st.info("No audio files in the library yet. Upload a file and save it to the library from the Upload tab.")
    else:
        for idx, entry in enumerate(entries):
            with st.container():
                col_info, col_play, col_action = st.columns([3, 2, 2])

                with col_info:
                    st.markdown(f"**{entry['original_name']}**")
                    st.caption(f"📋 {entry['subject']}  •  🕐 {entry['uploaded_at'][:10]}")

                with col_play:
                    filepath = LIBRARY_DIR / entry["filename"]
                    if filepath.exists():
                        st.audio(str(filepath))

                with col_action:
                    btn_col1, btn_col2 = st.columns(2)
                    with btn_col2:
                        if st.button("🗑️", key=f"lib_delete_{idx}", help="Delete from library"):
                            delete_from_library(entry["filename"])
                            st.rerun()
                    with btn_col1:
                        pass
                    if st.button("Transcribe", key=f"lib_transcribe_{idx}", disabled=not model_info["available"]):
                        with st.spinner(f"Transcribing {entry['original_name']}..."):
                            try:
                                audio_bytes = filepath.read_bytes()
                                utterances = transcribe_audio(audio_bytes, selected_model, keyterms=keyterms)
                                speakers = sorted(set(u["speaker"] for u in utterances))
                                speaker_map = {f"Speaker {s}": f"Speaker {s}" for s in speakers}
                                st.session_state[f"lib_utterances_{idx}"] = utterances
                                st.session_state[f"lib_speaker_map_{idx}"] = speaker_map
                            except Exception as e:
                                st.error(f"Transcription failed: {e}")

                # Show transcript if available
                if f"lib_utterances_{idx}" in st.session_state:
                    utterances = st.session_state[f"lib_utterances_{idx}"]
                    speaker_map = st.session_state[f"lib_speaker_map_{idx}"]
                    speaker_map = show_speaker_editor(utterances, speaker_map, f"lib_speaker_{idx}")
                    st.session_state[f"lib_speaker_map_{idx}"] = speaker_map
                    show_transcript_with_download(
                        utterances, speaker_map, f"{entry['original_name']}_transcript.txt", f"lib_{idx}",
                        utterances_session_key=f"lib_utterances_{idx}",
                    )

                st.markdown("---")
