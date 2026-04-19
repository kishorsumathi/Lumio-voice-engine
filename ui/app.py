"""
Lumio Voice (pipeline testing build) — upload audio to S3, poll RDS, review diarized transcript,
and edit speaker labels (stored on segment rows, keyed by diarization speaker_id).
"""
from __future__ import annotations

import html
import re
import uuid
from datetime import timedelta
from pathlib import Path

import boto3
import streamlit as st

import config
from db import (
    get_job_by_s3_key,
    list_recent_jobs,
    load_job_with_segments,
    translations_per_segment,
    update_speaker_labels_for_job,
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


@st.cache_resource
def _s3():
    return boto3.client("s3", region_name=config.AWS_REGION)


def _presign_audio(bucket: str, key: str) -> str:
    return _s3().generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=3600,
    )


def _esc(s: str) -> str:
    """Safe text inside HTML snippets."""
    return html.escape(s or "", quote=False)


def _fmt_time(seconds: float) -> str:
    s = float(seconds)
    m, sec = divmod(int(round(s)), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h:d}:{m:02d}:{sec:02d}"
    return f"{m:02d}:{sec:02d}"


def _session_card(j) -> str:
    """One-line label for sidebar (matches jobs table: filename, status, duration)."""
    name = (j.original_filename or "Untitled")[:44]
    if len((j.original_filename or "")) > 44:
        name += "…"
    bits = [name, j.status]
    if j.audio_duration_seconds is not None:
        bits.append(f"{float(j.audio_duration_seconds):.0f}s")
    return " · ".join(bits)


def _default_label(speaker_id: int, stored: str | None) -> str:
    if stored and stored.strip():
        return stored.strip()
    return f"Speaker {speaker_id}"


def _progress_value(status: str) -> float:
    return {
        "pending": 0.05,
        "downloading": 0.12,
        "chunking": 0.22,
        "transcribing": 0.45,
        "merging": 0.62,
        "translating": 0.82,
        "completed": 1.0,
        "failed": 1.0,
    }.get(status, 0.1)


@st.fragment(run_every=timedelta(seconds=2))
def poll_pending():
    """Auto-refresh while waiting for the worker to create/update the job row."""
    pk = st.session_state.get("pending_s3_key")
    if not pk:
        return
    job = get_job_by_s3_key(pk)
    if job is None:
        st.info(
            "**Connecting to the pipeline…**  \n"
            "Your file is on S3. This session will show in **Sessions** once the worker registers the job — "
            "often within seconds, sometimes a few minutes for long files, queue backlog, or cold starts."
        )
        st.progress(0.08)
        return
    # First DB row: sync selection + full rerun so the sidebar list includes this job.
    if st.session_state.get("_pending_linked_job_id") != str(job.id):
        st.session_state._pending_linked_job_id = str(job.id)
        st.session_state.selected_job_id = job.id
        st.rerun()

    st.caption(f"**Pipeline status:** `{job.status}`")
    st.progress(_progress_value(job.status))
    if job.status == "failed":
        st.error(job.error_message or "Job failed")
        st.session_state.pending_s3_key = None
        st.session_state.pending_display_name = None
        st.session_state.pop("_pending_linked_job_id", None)
        st.session_state.selected_job_id = job.id
        return
    if job.status == "completed":
        st.session_state.pending_s3_key = None
        st.session_state.pending_display_name = None
        st.session_state.pop("_pending_linked_job_id", None)
        st.session_state.selected_job_id = job.id
        st.rerun()


def main():
    st.set_page_config(page_title="Lumio Voice — pipeline testing (Build)", layout="wide")
    if not config.S3_BUCKET:
        st.error("Set **S3_PROCESSED_BUCKET** (or run in ECS with task env).")
        st.stop()

    st.title("Lumio Voice")
    st.caption("Pipeline testing · Build — upload audio → S3 → review diarized transcript and speaker labels.")

    if "selected_job_id" not in st.session_state:
        st.session_state.selected_job_id = None
    if "pending_s3_key" not in st.session_state:
        st.session_state.pending_s3_key = None
    if st.session_state.pop("_toast_upload_ok", False):
        try:
            st.toast("Audio submitted — transcription will appear here when ready.", icon="✅")
        except Exception:
            pass

    # Reset sidebar selectbox on the run *after* upload — cannot assign
    # `sidebar_session_select` after the widget is instantiated (same run).
    if st.session_state.pop("_reset_sidebar_select", False):
        st.session_state["sidebar_session_select"] = ""

    # ── Sidebar: sessions (click = load audio + transcripts in main) ───────
    with st.sidebar:
        st.markdown("### Sessions")
        pk_wait = st.session_state.get("pending_s3_key")
        if pk_wait:
            fn = st.session_state.get("pending_display_name") or Path(pk_wait).name
            st.success(
                f"**Submitted:** `{fn}`  \n"
                "Pipeline starting — this session usually appears below within **seconds to a few minutes**, "
                "depending on file length, queue, and worker load."
            )
        try:
            jobs = list_recent_jobs(40)
        except Exception as e:
            st.error(f"Database: {e}")
            jobs = []
        job_by_id = {str(j.id): j for j in jobs}
        opt_ids = [""] + [str(j.id) for j in jobs]

        def _fmt_sid(sid: str) -> str:
            if not sid:
                return "➕ New upload…"
            j = job_by_id.get(sid)
            return _session_card(j) if j else sid

        ix = 0
        if st.session_state.selected_job_id and str(st.session_state.selected_job_id) in opt_ids:
            ix = opt_ids.index(str(st.session_state.selected_job_id))

        picked = st.selectbox(
            "Pick a session",
            options=opt_ids,
            index=ix,
            format_func=_fmt_sid,
            label_visibility="collapsed",
            key="sidebar_session_select",
        )
        if picked:
            st.session_state.selected_job_id = uuid.UUID(picked)
            st.session_state.pending_s3_key = None
            st.session_state.pending_display_name = None
            st.session_state.pop("_pending_linked_job_id", None)
        else:
            st.session_state.selected_job_id = None

        with st.expander("Upload audio", expanded=not picked):
            st.caption("Files go to `uploads/{uuid}/…` — EventBridge starts the worker.")

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
                        _s3().put_object(
                            Bucket=config.S3_BUCKET,
                            Key=key,
                            Body=raw,
                            ContentType=up.type or "application/octet-stream",
                        )
                        st.session_state.pending_s3_key = key
                        st.session_state.pending_display_name = fname
                        st.session_state.selected_job_id = None
                        st.session_state["_reset_sidebar_select"] = True
                        st.session_state["_toast_upload_ok"] = True
                        st.rerun()
                    except Exception as e:
                        st.error(f"S3 upload failed: {e}")

    job_id = st.session_state.selected_job_id

    if job_id is None and st.session_state.pending_s3_key:
        st.markdown("### Processing your upload")
        st.markdown(
            "Your audio is on S3 and a **transcription job** will show in **Sessions** once registered — "
            "often quickly, or a few minutes for long audio or a busy queue. Live status updates are below."
        )
        poll_pending()
        return

    if st.session_state.pending_s3_key:
        poll_pending()

    if job_id is None:
        st.info("Upload a file or pick a session from the sidebar.")
        return

    job, segments, trans_by_lang = load_job_with_segments(job_id)
    if job is None:
        st.warning("Job not found.")
        return

    # ── Metadata (RDS `jobs` row) ────────────────────────────────────────────
    trans_by_seg = translations_per_segment(trans_by_lang)
    lang_codes = sorted(trans_by_lang.keys())

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Job ID", str(job.id)[:13] + "…")
    c2.metric("Status", job.status)
    if job.audio_duration_seconds is not None:
        c3.metric("Duration (s)", f"{float(job.audio_duration_seconds):.1f}")
    if job.num_speakers is not None:
        c4.metric("Speakers", job.num_speakers)
    if job.num_chunks is not None:
        c5.metric("Chunks", job.num_chunks)
    meta_cols = st.columns(3)
    if job.source_language:
        meta_cols[0].caption(f"Source language (jobs): **{job.source_language}**")
    meta_cols[1].caption(f"Segments in DB: **{len(segments)}**")
    if lang_codes:
        meta_cols[2].caption(f"Translation languages: **{', '.join(lang_codes)}**")
    else:
        meta_cols[2].caption("Translation languages: **—**")

    if job.status not in ("completed", "failed"):
        st.warning("Job still running — select another session or wait.")
        return

    if job.status == "failed":
        st.error(job.error_message or "Failed")
        return

    # ── Audio (`jobs.s3_bucket` + `jobs.s3_key`) ────────────────────────────
    st.subheader("Audio")
    try:
        url = _presign_audio(job.s3_bucket, job.s3_key)
        st.audio(url)
    except Exception as e:
        st.caption(f"Could not presign audio URL: {e}")

    # Unique speaker ids in order of first appearance (`segments.speaker_id` / `speaker_label`)
    seen: list[int] = []
    for s in segments:
        if s.speaker_id not in seen:
            seen.append(s.speaker_id)

    with st.expander("Speaker labels (edit `segments.speaker_label` per diarization id)", expanded=False):
        st.caption("Updates all rows sharing the same `speaker_id` for this job.")
        cols = st.columns(min(len(seen), 4) or 1)
        new_labels: dict[int, str] = {}
        for i, sid in enumerate(seen):
            first = next(x for x in segments if x.speaker_id == sid)
            default = _default_label(sid, first.speaker_label)
            with cols[i % len(cols)]:
                new_labels[sid] = st.text_input(
                    f"Speaker {sid}",
                    value=default,
                    key=f"inp_{job_id}_{sid}",
                )
        if st.button("Save speaker labels"):
            try:
                for sid, lab in new_labels.items():
                    update_speaker_labels_for_job(job_id, sid, lab)
                st.success("Saved.")
                st.rerun()
            except Exception as e:
                st.error(str(e))

    # ── Diarized: transcription (segments.text) | translation (translations.translated_text) ──
    st.subheader("Transcription & translation")
    pick_lang = lang_codes[0] if lang_codes else None
    if len(lang_codes) > 1:
        pick_lang = st.selectbox("Translation column language", lang_codes, index=0, key=f"lang_pick_{job_id}")

    left, right = st.columns(2)
    with left:
        st.markdown("##### Transcription (source)")
        st.caption("`segments.text` · times from `start_time` / `end_time`")
        for seg in segments:
            color = SPEAKER_COLORS[seg.speaker_id % len(SPEAKER_COLORS)]
            lab = _default_label(seg.speaker_id, seg.speaker_label)
            conf = ""
            if seg.confidence is not None:
                conf = f" · conf {float(seg.confidence):.2f}"
            st.markdown(
                f'<div style="border-left:4px solid {color};padding:10px 10px 10px 12px;margin-bottom:12px;background:rgba(0,0,0,0.03);border-radius:4px">'
                f"<small>{_fmt_time(seg.start_time)} – {_fmt_time(seg.end_time)} · "
                f"<strong>{_esc(lab)}</strong>{_esc(conf)}</small><br/>{_esc(seg.text)}</div>",
                unsafe_allow_html=True,
            )

    with right:
        st.markdown("##### Translation")
        st.caption("`translations.translated_text` per `target_language`")
        if not lang_codes:
            st.info("No rows in `translations` for this job (pipeline may have skipped translation).")
        else:
            for seg in segments:
                color = SPEAKER_COLORS[seg.speaker_id % len(SPEAKER_COLORS)]
                lab = _default_label(seg.speaker_id, seg.speaker_label)
                tmap = trans_by_seg.get(seg.id, {})
                body = (tmap.get(pick_lang) or "").strip() or "—"
                st.markdown(
                    f'<div style="border-left:4px solid {color};padding:10px 10px 10px 12px;margin-bottom:12px;background:rgba(0,0,0,0.03);border-radius:4px">'
                    f"<small>{_fmt_time(seg.start_time)} – {_fmt_time(seg.end_time)} · "
                    f"<strong>{_esc(lab)}</strong> · {_esc(pick_lang)}</small><br/>{_esc(body)}</div>",
                    unsafe_allow_html=True,
                )

    st.divider()
    ft_left, ft_right = st.columns(2)
    with ft_left:
        st.markdown("##### Full transcription (plain)")
        lines_src = []
        for seg in segments:
            lab = _default_label(seg.speaker_id, seg.speaker_label)
            lines_src.append(f"{lab}: {seg.text}")
        st.text_area(
            "source",
            value="\n\n".join(lines_src),
            height=360,
            disabled=True,
            label_visibility="collapsed",
        )
    with ft_right:
        st.markdown("##### Full translation (plain)")
        if not lang_codes:
            st.text_area("translation", value="", height=360, disabled=True, label_visibility="collapsed")
        else:
            lines_tr = []
            for seg in segments:
                lab = _default_label(seg.speaker_id, seg.speaker_label)
                tmap = trans_by_seg.get(seg.id, {})
                txt = (tmap.get(pick_lang) or "").strip()
                lines_tr.append(f"{lab}: {txt}")
            st.text_area(
                "translation",
                value="\n\n".join(lines_tr),
                height=360,
                disabled=True,
                label_visibility="collapsed",
            )


main()
