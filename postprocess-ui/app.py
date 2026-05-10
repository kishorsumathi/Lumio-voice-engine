"""
Anchor Voice — Transcript post-processor (Streamlit).

Upload a pipeline `results.json`, run Anthropic Claude over speaker turns,
and download / inspect cleaned transcription + translation.

Loads `ANTHROPIC_API_KEY` and optional `ANTHROPIC_MODEL_ID` from
postprocess-ui/.env via python-dotenv.
"""

from __future__ import annotations

import html
import json
import logging
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

import streamlit as st

from pipeline import run as pipeline_run

logging.basicConfig(level=logging.INFO)

SPEAKER_COLORS = [
    "#2563eb",
    "#dc2626",
    "#16a34a",
    "#ca8a04",
    "#9333ea",
    "#0891b2",
    "#c026d3",
    "#ea580c",
]

MODEL_CHOICES = ("claude-opus-4-6", "claude-sonnet-4-6")


def _speaker_color(speaker_id: int) -> str:
    return SPEAKER_COLORS[speaker_id % len(SPEAKER_COLORS)]


def _esc(s: str) -> str:
    return html.escape(s or "", quote=False)


def _render_turn_grid(pp_turns: list[dict]) -> None:
    st.subheader("Per-turn: original vs cleaned")
    for row in pp_turns:
        sid = int(row.get("speaker_id", 0))
        color = _speaker_color(sid)
        st.markdown(f"##### Turn {row.get('turn_index', 0)} · Speaker {sid}")
        top_l, top_r = st.columns(2)
        bot_l, bot_r = st.columns(2)
        with top_l:
            st.caption("Transcription (original)")
            st.markdown(
                f'<div style="border-left:4px solid {color};padding:10px;background:rgba(0,0,0,0.03);border-radius:4px">'
                f"{_esc(row.get('transcription') or '')}</div>",
                unsafe_allow_html=True,
            )
        with top_r:
            st.caption("Transcription (cleaned)")
            st.markdown(
                f'<div style="border-left:4px solid {color};padding:10px;background:rgba(0,0,0,0.03);border-radius:4px">'
                f"{_esc(row.get('cleaned_transcription') or '')}</div>",
                unsafe_allow_html=True,
            )
        with bot_l:
            st.caption("Translation (original)")
            st.markdown(
                f'<div style="border-left:4px solid {color};padding:10px;background:rgba(0,0,0,0.03);border-radius:4px">'
                f"{_esc(row.get('translation') or '')}</div>",
                unsafe_allow_html=True,
            )
        with bot_r:
            st.caption("Translation (cleaned)")
            st.markdown(
                f'<div style="border-left:4px solid {color};padding:10px;background:rgba(0,0,0,0.03);border-radius:4px">'
                f"{_esc(row.get('cleaned_translation') or '')}</div>",
                unsafe_allow_html=True,
            )


def _render_full_document(pp_turns: list[dict]) -> None:
    st.subheader("Cleaned document (full)")
    left, right = st.columns(2)
    with left:
        st.markdown("##### Cleaned transcription")
        blocks_tr = []
        for row in pp_turns:
            sid = int(row.get("speaker_id", 0))
            txt = row.get("cleaned_transcription") or ""
            blocks_tr.append(
                f'<p style="margin:0 0 6px 0"><strong>Speaker {sid}</strong></p>'
                f'<div style="white-space:pre-wrap;line-height:1.55;margin-bottom:18px">{_esc(txt)}</div>'
            )
        st.markdown(
            f'<div style="font-size:15px">{"".join(blocks_tr)}</div>',
            unsafe_allow_html=True,
        )
    with right:
        st.markdown("##### Cleaned translation")
        blocks_en = []
        for row in pp_turns:
            sid = int(row.get("speaker_id", 0))
            txt = row.get("cleaned_translation") or ""
            blocks_en.append(
                f'<p style="margin:0 0 6px 0"><strong>Speaker {sid}</strong></p>'
                f'<div style="white-space:pre-wrap;line-height:1.55;margin-bottom:18px">{_esc(txt)}</div>'
            )
        st.markdown(
            f'<div style="font-size:15px">{"".join(blocks_en)}</div>',
            unsafe_allow_html=True,
        )

    copy_tr = "\n\n".join(
        f"Speaker {int(r.get('speaker_id', 0))}:\n\n{(r.get('cleaned_transcription') or '').strip()}"
        for r in pp_turns
    )
    copy_en = "\n\n".join(
        f"Speaker {int(r.get('speaker_id', 0))}:\n\n{(r.get('cleaned_translation') or '').strip()}"
        for r in pp_turns
    )
    with st.expander("Copy-friendly plain text"):
        st.code(copy_tr or "(empty)", language=None)
        st.code(copy_en or "(empty)", language=None)


def main() -> None:
    st.set_page_config(page_title="Anchor Voice — transcript post-process", layout="wide")
    st.title("Transcript post-processing")
    st.caption(
        "Upload a pipeline results JSON → Claude cleans clinical terms, "
        "multilingual drift, formatting, and filler noise."
    )

    with st.sidebar:
        model = st.selectbox("Model", MODEL_CHOICES, index=0)
        glossary = st.text_area(
            "Optional glossary (one line each; `wrong → right` or just `term`)",
            height=120,
            placeholder="cat distributing → catastrophising",
        )

    up = st.file_uploader("Pipeline results JSON", type=["json"])

    run_clicked = st.button("Run post-processing", type="primary", disabled=up is None)

    if up is not None:
        upload_key = (up.name, len(up.getvalue()))
        if st.session_state.get("_pp_upload_key") != upload_key:
            st.session_state["_pp_upload_key"] = upload_key
            st.session_state.pop("cleaned_doc", None)
            st.session_state.pop("cleaned_filename", None)

        try:
            raw = json.loads(up.getvalue().decode("utf-8"))
        except Exception as e:
            st.error(f"Invalid JSON: {e}")
            return

        summary = raw.get("summary") or {}
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Job ID", str(raw.get("job_id") or "—")[:14] + "…")
        c2.metric("Segments", len(raw.get("segments") or []))
        if summary.get("num_speakers") is not None:
            c3.metric("Speakers", summary["num_speakers"])
        if summary.get("audio_duration_seconds") is not None:
            c4.metric("Duration (s)", f'{float(summary["audio_duration_seconds"]):.1f}')

        if run_clicked:
            prog = st.progress(0.0, text="Starting…")
            try:

                def _on_progress(done: int, total: int) -> None:
                    prog.progress(done / max(total, 1), text=f"Batch {done}/{total}")

                with st.spinner(f"Calling Claude ({model})…"):
                    cleaned = pipeline_run(
                        raw,
                        model=model,
                        glossary=glossary,
                        on_progress=_on_progress,
                    )
            except Exception as e:
                prog.empty()
                st.error(str(e))
                return

            prog.progress(1.0, text="Done")
            st.session_state["cleaned_doc"] = cleaned
            st.session_state["cleaned_filename"] = f"cleaned_{raw.get('job_id') or 'results'}.json"

        cleaned_doc = st.session_state.get("cleaned_doc")
        if cleaned_doc:
            pp = cleaned_doc.get("postprocess") or {}
            pp_turns = pp.get("turns") or []

            _render_turn_grid(pp_turns)
            _render_full_document(pp_turns)

            gloss = pp.get("glossary_corrections") or []
            if gloss:
                st.subheader("Glossary corrections")
                st.dataframe(gloss, use_container_width=True)

            with st.expander("Raw cleaned JSON"):
                st.json(cleaned_doc)

            blob = json.dumps(cleaned_doc, ensure_ascii=False, indent=2)
            st.download_button(
                label="Download cleaned JSON",
                data=blob,
                file_name=st.session_state.get("cleaned_filename", "cleaned_results.json"),
                mime="application/json",
                type="primary",
            )


if __name__ == "__main__":
    main()
