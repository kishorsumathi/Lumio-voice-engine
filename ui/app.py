"""
Lumio Voice — Streamlit UI (S3-only).

Upload audio to S3 → the worker runs end-to-end (chunk → ASR → diarize →
translate to English) → writes `results/<job_id>.json` to the same bucket
and publishes a pointer on the `job-events` SQS queue for the backend.

This UI never talks to a database. It:

  1. Uploads the audio to `s3://${S3_PROCESSED_BUCKET}/uploads/<uuid>/<name>`
     (EventBridge → Lambda → SQS → ECS picks it up).
  2. Polls the `results/` prefix with HeadObject to find the result the
     worker wrote for that upload (matched via `x-amz-meta-source-key`).
  3. Renders the results JSON: metadata, presigned audio, diarized
     transcription + English translation side-by-side.

Speaker labels are not editable in this build (results files are immutable
claim-check objects). If label editing is needed, add a sidecar like
`results/<job_id>.labels.json` and overlay it here.
"""
from __future__ import annotations

import html
import re
import uuid
from datetime import timedelta
from pathlib import Path

import streamlit as st

import s3_results
from s3_results import (
    S3_BUCKET,
    ResultSummary,
    find_result_for_source,
    list_recent_results,
    load_results,
    presign_audio,
)

SUPPORTED_EXT = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".aac", ".webm", ".mp4"}
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


def _safe_filename(name: str) -> str:
    base = Path(name).name
    out = re.sub(r"[^a-zA-Z0-9._-]+", "_", base).strip("._") or "audio"
    return out[:200]


def _esc(s: str) -> str:
    return html.escape(s or "", quote=False)


def _fmt_time(seconds: float) -> str:
    s = float(seconds)
    m, sec = divmod(int(round(s)), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h:d}:{m:02d}:{sec:02d}"
    return f"{m:02d}:{sec:02d}"


@st.cache_data(ttl=10, show_spinner=False)
def _cached_recent_results(limit: int = 40) -> list[ResultSummary]:
    """
    Short TTL so new sessions show up within ~10s without hammering S3 on
    every rerun (Streamlit reruns the whole script on every widget change).
    """
    return list_recent_results(limit=limit)


@st.cache_data(ttl=300, show_spinner=False)
def _cached_result_doc(key: str, etag: str) -> dict:
    """
    Results files are immutable per key+etag, so cache on (key, etag) and
    invalidate naturally if the worker ever rewrites the object.
    """
    return load_results(key)


def _speaker_color(speaker_id: int) -> str:
    return SPEAKER_COLORS[speaker_id % len(SPEAKER_COLORS)]


@st.fragment(run_every=timedelta(seconds=3))
def poll_pending():
    """Watch for the results JSON the worker will write for our pending upload."""
    pk = st.session_state.get("pending_s3_key")
    if not pk:
        return

    match = find_result_for_source(pk, scan_limit=40)
    if match is None:
        st.info(
            "**Pipeline running…**  \n"
            "Your audio is on S3 and the worker is processing it. This page "
            "will switch to the transcript automatically — usually within "
            "seconds, longer for long files or a cold start."
        )
        st.progress(0.15)
        return

    # Found — pin the session state, clear pending markers, and rerun so the
    # sidebar picks up the new entry and the main panel renders the results.
    st.cache_data.clear()
    st.session_state.selected_result_key = match.key
    st.session_state.pending_s3_key = None
    st.session_state.pending_display_name = None
    st.rerun()


def _render_result(doc: dict):
    source = doc.get("source") or {}
    summary = doc.get("summary") or {}
    timing = doc.get("timing") or {}
    segments = doc.get("segments") or []

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Job ID", (doc.get("job_id") or "—")[:13] + "…")
    c2.metric("Status", doc.get("status") or "—")
    if summary.get("audio_duration_seconds") is not None:
        c3.metric("Duration (s)", f"{float(summary['audio_duration_seconds']):.1f}")
    if summary.get("num_speakers") is not None:
        c4.metric("Speakers", summary["num_speakers"])
    if summary.get("num_chunks") is not None:
        c5.metric("Chunks", summary["num_chunks"])

    meta_cols = st.columns(3)
    if summary.get("source_language"):
        meta_cols[0].caption(f"Source language: **{summary['source_language']}**")
    meta_cols[1].caption(f"Segments: **{len(segments)}**")
    if timing.get("wall_clock_seconds") is not None:
        meta_cols[2].caption(
            f"Worker wall-clock: **{float(timing['wall_clock_seconds']):.1f}s**"
        )

    bucket = source.get("bucket")
    key = source.get("key")
    if bucket and key:
        st.subheader("Audio")
        try:
            st.audio(presign_audio(bucket, key))
        except Exception as e:
            st.caption(f"Could not presign audio URL: {e}")

    st.subheader("Transcription & translation")
    left, right = st.columns(2)
    with left:
        st.markdown("##### Transcription (source)")
        st.caption("Diarized segments · times in MM:SS")
        for seg in segments:
            color = _speaker_color(int(seg.get("speaker_id", 0)))
            lab = f"Speaker {seg.get('speaker_id', 0)}"
            conf_bit = ""
            if seg.get("confidence") is not None:
                conf_bit = f" · conf {float(seg['confidence']):.2f}"
            st.markdown(
                f'<div style="border-left:4px solid {color};padding:10px 10px 10px 12px;margin-bottom:12px;background:rgba(0,0,0,0.03);border-radius:4px">'
                f"<small>{_fmt_time(seg.get('start_time', 0))} – {_fmt_time(seg.get('end_time', 0))} · "
                f"<strong>{_esc(lab)}</strong>{_esc(conf_bit)}</small><br/>"
                f"{_esc(seg.get('transcription') or '')}</div>",
                unsafe_allow_html=True,
            )

    with right:
        st.markdown("##### Translation (English)")
        st.caption("Inlined per segment in the results JSON")
        for seg in segments:
            color = _speaker_color(int(seg.get("speaker_id", 0)))
            lab = f"Speaker {seg.get('speaker_id', 0)}"
            body = (seg.get("translation") or "").strip() or "—"
            st.markdown(
                f'<div style="border-left:4px solid {color};padding:10px 10px 10px 12px;margin-bottom:12px;background:rgba(0,0,0,0.03);border-radius:4px">'
                f"<small>{_fmt_time(seg.get('start_time', 0))} – {_fmt_time(seg.get('end_time', 0))} · "
                f"<strong>{_esc(lab)}</strong> · en</small><br/>{_esc(body)}</div>",
                unsafe_allow_html=True,
            )

    st.divider()
    ft_left, ft_right = st.columns(2)
    with ft_left:
        st.markdown("##### Full transcription (plain)")
        lines_src = [
            f"Speaker {seg.get('speaker_id', 0)}: {seg.get('transcription') or ''}"
            for seg in segments
        ]
        st.text_area(
            "source",
            value="\n\n".join(lines_src),
            height=360,
            disabled=True,
            label_visibility="collapsed",
        )
    with ft_right:
        st.markdown("##### Full translation (plain)")
        lines_tr = [
            f"Speaker {seg.get('speaker_id', 0)}: {(seg.get('translation') or '').strip()}"
            for seg in segments
        ]
        st.text_area(
            "translation",
            value="\n\n".join(lines_tr),
            height=360,
            disabled=True,
            label_visibility="collapsed",
        )


def main():
    st.set_page_config(page_title="Lumio Voice — pipeline testing (Build)", layout="wide")
    if not S3_BUCKET:
        st.error("Set **S3_PROCESSED_BUCKET** (or run in ECS with task env).")
        st.stop()

    st.title("Lumio Voice")
    st.caption(
        "Pipeline testing · Build — upload audio → S3 → review diarized "
        "transcript + English translation."
    )

    st.session_state.setdefault("selected_result_key", None)
    st.session_state.setdefault("pending_s3_key", None)
    st.session_state.setdefault("pending_display_name", None)

    if st.session_state.pop("_toast_upload_ok", False):
        try:
            st.toast("Audio submitted — transcript will appear here when ready.", icon="✅")
        except Exception:
            pass

    if st.session_state.pop("_reset_sidebar_select", False):
        st.session_state["sidebar_session_select"] = ""

    # ── Sidebar ────────────────────────────────────────────────────────────
    with st.sidebar:
        st.markdown("### Sessions")
        if st.session_state.get("pending_s3_key"):
            fn = st.session_state.get("pending_display_name") or Path(
                st.session_state["pending_s3_key"]
            ).name
            st.success(
                f"**Submitted:** `{fn}`  \n"
                "Pipeline running — the finished session will appear below."
            )

        col_refresh, _ = st.columns([1, 3])
        if col_refresh.button("↻", help="Reload sessions from S3"):
            st.cache_data.clear()

        try:
            sessions = _cached_recent_results(limit=40)
        except Exception as e:
            st.error(f"S3 list failed: {e}")
            sessions = []

        by_key = {s.key: s for s in sessions}
        opt_keys = [""] + [s.key for s in sessions]

        def _fmt_key(k: str) -> str:
            if not k:
                return "➕ New upload…"
            s = by_key.get(k)
            return s.label if s else k

        ix = 0
        if (
            st.session_state.selected_result_key
            and st.session_state.selected_result_key in opt_keys
        ):
            ix = opt_keys.index(st.session_state.selected_result_key)

        picked = st.selectbox(
            "Pick a session",
            options=opt_keys,
            index=ix,
            format_func=_fmt_key,
            label_visibility="collapsed",
            key="sidebar_session_select",
        )
        if picked:
            st.session_state.selected_result_key = picked
            st.session_state.pending_s3_key = None
            st.session_state.pending_display_name = None
        else:
            st.session_state.selected_result_key = None

        with st.expander("Upload audio", expanded=not picked):
            st.caption(
                "Files go to `uploads/{uuid}/…` — EventBridge triggers the "
                "worker, which writes `results/<job_id>.json` when done."
            )

            up = st.file_uploader(
                "File",
                type=sorted({x.lstrip(".") for x in SUPPORTED_EXT}),
                label_visibility="collapsed",
            )

            if up is not None:
                raw = up.getvalue()
                fname = _safe_filename(up.name)
                uid = uuid.uuid4()
                key = f"uploads/{uid}/{fname}"
                if st.button("Send to pipeline", type="primary", use_container_width=True):
                    try:
                        s3_results._s3().put_object(
                            Bucket=S3_BUCKET,
                            Key=key,
                            Body=raw,
                            ContentType=up.type or "application/octet-stream",
                        )
                        st.session_state.pending_s3_key = key
                        st.session_state.pending_display_name = fname
                        st.session_state.selected_result_key = None
                        st.session_state["_reset_sidebar_select"] = True
                        st.session_state["_toast_upload_ok"] = True
                        st.rerun()
                    except Exception as e:
                        st.error(f"S3 upload failed: {e}")

    # ── Main panel ─────────────────────────────────────────────────────────
    selected_key = st.session_state.selected_result_key

    if selected_key is None and st.session_state.pending_s3_key:
        st.markdown("### Processing your upload")
        st.markdown(
            "Your audio is on S3 and the **worker** is running end-to-end. "
            "This view auto-refreshes and switches to the transcript the "
            "moment the results file appears."
        )
        poll_pending()
        return

    if st.session_state.pending_s3_key:
        poll_pending()

    if selected_key is None:
        st.info("Upload a file or pick a session from the sidebar.")
        return

    summary = by_key.get(selected_key)
    etag = summary.etag if summary else ""
    try:
        doc = _cached_result_doc(selected_key, etag)
    except Exception as e:
        st.error(f"Could not load results for `{selected_key}`: {e}")
        return

    _render_result(doc)


main()
