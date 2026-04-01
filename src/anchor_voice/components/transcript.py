import re

import streamlit as st


def render_transcript(utterances: list[dict], speaker_map: dict, highlight_word: str = "") -> list[str]:
    lines = []
    for utt in utterances:
        raw_label = f"Speaker {utt['speaker']}"
        label = speaker_map.get(raw_label, raw_label)
        text = utt["text"]
        if highlight_word:
            pattern = re.compile(re.escape(highlight_word), re.IGNORECASE)
            text = pattern.sub(
                lambda m: f':red-background[{m.group(0)}]',
                text,
            )
        lines.append(f"**{label}:** {text}")
    return lines


def build_download_text(utterances: list[dict], speaker_map: dict) -> str:
    lines = []
    for utt in utterances:
        raw_label = f"Speaker {utt['speaker']}"
        label = speaker_map.get(raw_label, raw_label)
        lines.append(f"{label}: {utt['text']}")
    return "\n\n".join(lines)


def apply_find_replace(utterances: list[dict], find_word: str, replace_word: str) -> list[dict]:
    if not find_word:
        return utterances
    pattern = re.compile(re.escape(find_word), re.IGNORECASE)
    return [{**utt, "text": pattern.sub(replace_word, utt["text"])} for utt in utterances]


def show_speaker_editor(utterances: list[dict], speaker_map: dict, key_prefix: str) -> dict:
    st.subheader("Edit Speaker Labels")
    cols = st.columns(min(len(speaker_map), 4)) if speaker_map else []
    for i, (raw_label, current_name) in enumerate(speaker_map.items()):
        with cols[i % len(cols)]:
            new_name = st.text_input(raw_label, value=current_name, key=f"{key_prefix}_{raw_label}")
            if new_name != current_name:
                speaker_map[raw_label] = new_name
    return speaker_map


def show_transcript_with_download(
    utterances: list[dict],
    speaker_map: dict,
    download_filename: str,
    key_prefix: str,
    utterances_session_key: str = "",
):
    # Find & Replace
    with st.expander("🔍 Find & Replace"):
        col_find, col_replace, col_btn = st.columns([2, 2, 1])
        with col_find:
            find_word = st.text_input("Find", key=f"{key_prefix}_find", placeholder="Word to find")
        with col_replace:
            replace_word = st.text_input("Replace with", key=f"{key_prefix}_replace", placeholder="New word")
        with col_btn:
            st.write("")
            replace_clicked = st.button("Replace All", key=f"{key_prefix}_replace_btn")

        if replace_clicked and find_word and utterances_session_key:
            updated = apply_find_replace(utterances, find_word, replace_word)
            st.session_state[utterances_session_key] = updated
            st.success(f"Replaced all **{find_word}** → **{replace_word}**")
            st.rerun()

    # Transcript with optional highlighting
    st.subheader("Transcript")
    for line in render_transcript(utterances, speaker_map, highlight_word=find_word):
        st.markdown(line)

    download_text = build_download_text(utterances, speaker_map)
    st.download_button(
        label="Download Transcript",
        data=download_text,
        file_name=download_filename,
        mime="text/plain",
        key=f"{key_prefix}_download",
    )
