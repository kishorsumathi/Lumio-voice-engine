"""Group speaker segments, batch by context size, call Anthropic LLM, assemble output."""

from __future__ import annotations

import json
import logging
import re
from typing import Callable

from langchain_core.messages import HumanMessage, SystemMessage

from llm import make_chat
from prompt import build_system_prompt, build_user_message
from schema import CleanedTurn, CleanedTurns, GlossaryCorrection, PostprocessMeta, PostprocessTurnOut, SpeakerTurn

logger = logging.getLogger(__name__)

# Serialized-turn payload budget (chars per batch).
#
# We size batches so the cleaned-output JSON always fits comfortably under
# each model's output cap, leaving room for the system prompt + any
# expansion in cleaned text. Hitting the output cap mid-JSON is the only
# failure mode at this layer, so we err small.
#
#   Both models have large context windows; 80K char batch budget keeps
#   cleaned-output JSON well inside the 16K max_tokens output cap.
_BATCH_BUDGET = 80_000
_BATCH_PAYLOAD_CHARS: dict[str, int] = {
    "claude-opus-4-6":   _BATCH_BUDGET,
    "claude-sonnet-4-6": _BATCH_BUDGET,
}
_DEFAULT_BATCH_PAYLOAD_CHARS = _BATCH_BUDGET


def group_turns(segments: list[dict]) -> list[SpeakerTurn]:
    """Merge consecutive same-speaker segments into timeline-order speaker turns."""
    if not segments:
        return []

    def _sk(a: dict) -> float:
        return float(a.get("start_time", 0.0))

    segs = sorted(segments, key=_sk)
    turns: list[SpeakerTurn] = []
    turn_index = 0

    curr_spk = 0
    start_t = 0.0
    end_t = 0.0
    parts_tr: list[str] = []
    parts_en: list[str] = []

    def flush(last_parts_tr: list[str], last_parts_en: list[str]) -> None:
        nonlocal turn_index
        tr_join = " ".join(p for p in last_parts_tr if p).strip()
        en_join = " ".join(p for p in last_parts_en if p).strip()
        turns.append(
            SpeakerTurn(
                turn_index=turn_index,
                speaker_id=curr_spk,
                start_time=start_t,
                end_time=end_t,
                transcription=tr_join,
                translation=en_join,
            )
        )
        turn_index += 1

    for s in segs:
        tr = (s.get("transcription") or "").strip()
        en = (s.get("translation") or "").strip()
        spk = int(s.get("speaker_id", 0))
        st = float(s.get("start_time", 0.0))
        et = float(s.get("end_time", 0.0))

        if not parts_tr and not parts_en:
            curr_spk = spk
            start_t = st
            end_t = et
            parts_tr = [tr]
            parts_en = [en]
            continue

        if spk == curr_spk:
            end_t = et
            parts_tr.append(tr)
            parts_en.append(en)
        else:
            flush(parts_tr, parts_en)
            curr_spk = spk
            start_t = st
            end_t = et
            parts_tr = [tr]
            parts_en = [en]

    flush(parts_tr, parts_en)
    return turns


def _turn_payload_chars(t: SpeakerTurn) -> int:
    dumped = json.dumps(t.model_dump(mode="json"), ensure_ascii=False)
    return len(dumped)


def batch_turns(turns: list[SpeakerTurn], model: str) -> list[list[SpeakerTurn]]:
    limit = _BATCH_PAYLOAD_CHARS.get(model, _DEFAULT_BATCH_PAYLOAD_CHARS)
    batches: list[list[SpeakerTurn]] = []
    cur: list[SpeakerTurn] = []
    cur_size = 0

    for t in turns:
        sz = _turn_payload_chars(t)
        if cur and cur_size + sz > limit:
            batches.append(cur)
            cur = []
            cur_size = 0
        cur.append(t)
        cur_size += sz

    if cur:
        batches.append(cur)
    return batches


def _extract_message_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)
    return str(content)


_THINK_BLOCK_RE = re.compile(r"<think[^>]*>[\s\S]*?</think>", re.IGNORECASE)
_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)\s*```", re.IGNORECASE)


def _strip_reasoning_prelude(text: str) -> str:
    """Remove `<think>...</think>` blocks some reasoning-style models emit before JSON."""
    return _THINK_BLOCK_RE.sub("", text or "").strip()


def _parse_json_object(text: str) -> dict:
    """Extract the JSON object from model output.

    Handles the realistic mess we see from chat models in practice:
      - `<think>...</think>` reasoning preludes
      - markdown fences (```json ... ```)
      - prose before / after the JSON
      - multiple top-level objects (we pick the LAST balanced one, which is
        what the model actually 'committed' to as its final answer).
    """
    s = _strip_reasoning_prelude(text or "")

    fences = _FENCE_RE.findall(s)
    if fences:
        for body in reversed(fences):
            try:
                obj = json.loads(body.strip())
                if isinstance(obj, dict):
                    return obj
            except json.JSONDecodeError:
                continue

    candidates: list[tuple[int, int]] = []
    depth = 0
    start = -1
    in_str = False
    esc = False
    for i, ch in enumerate(s):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth == 0:
                continue
            depth -= 1
            if depth == 0 and start != -1:
                candidates.append((start, i + 1))
                start = -1

    for a, b in reversed(candidates):
        try:
            obj = json.loads(s[a:b])
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue

    raise ValueError("No JSON object found in model response")


def _is_truncated(resp) -> bool:
    """Return True if the API stopped because it hit the output token limit."""
    meta = getattr(resp, "response_metadata", {}) or {}
    # LangChain surfaces this as finish_reason (OpenAI-style) or stop_reason (Anthropic-style)
    reason = meta.get("stop_reason") or meta.get("finish_reason") or ""
    return str(reason).lower() == "max_tokens"


def _invoke_cleaned_turns(llm, messages: list) -> CleanedTurns:
    """Structured output via Anthropic (json_schema, then tools), else raw JSON parse."""

    for label, factory in (
        ("json_schema", lambda: llm.with_structured_output(CleanedTurns, method="json_schema")),
        ("tool_calling", lambda: llm.with_structured_output(CleanedTurns)),
    ):
        try:
            structured = factory()
            out = structured.invoke(messages)
            if isinstance(out, CleanedTurns):
                return out
            if isinstance(out, dict):
                return CleanedTurns.model_validate(out)
        except Exception as e:
            logger.debug("Structured output (%s) failed; trying next path: %s", label, e)

    resp = llm.invoke(messages)

    if _is_truncated(resp):
        raise _OutputTruncatedError("Model hit max_tokens limit — output was truncated")

    text = _extract_message_text(resp.content)
    if not text or not text.strip():
        rc = getattr(resp, "additional_kwargs", {}) or {}
        text = rc.get("refusal") or ""

    if not text or not text.strip():
        logger.error(
            "Empty response from model. additional_kwargs_keys=%s response_metadata_keys=%s",
            list(getattr(resp, "additional_kwargs", {}) or {}),
            list(getattr(resp, "response_metadata", {}) or {}),
        )
        raise ValueError("Model returned empty content")

    try:
        obj = _parse_json_object(text)
    except ValueError:
        snippet = text if len(text) <= 1500 else text[:750] + "\n…\n" + text[-750:]
        logger.error("Could not parse JSON from model output. Raw response:\n%s", snippet)
        raise

    return CleanedTurns.model_validate(obj)


class _OutputTruncatedError(Exception):
    """Raised when the model hit max_tokens — signals the caller to split the batch."""


def _align_to_batch(batch: list[SpeakerTurn], parsed: CleanedTurns) -> CleanedTurns:
    """Ensure one CleanedTurn per batch row with matching turn_index."""
    by_idx = {c.turn_index: c for c in parsed.turns}
    fixed: list[CleanedTurn] = []
    for b in batch:
        ct = by_idx.get(b.turn_index)
        if ct is None:
            fixed.append(
                CleanedTurn(
                    turn_index=b.turn_index,
                    cleaned_transcription=b.transcription,
                    cleaned_translation=b.translation,
                )
            )
        else:
            fixed.append(ct)
    return CleanedTurns(turns=fixed, glossary_corrections=parsed.glossary_corrections)


def _fallback_identity(batch: list[SpeakerTurn]) -> CleanedTurns:
    return CleanedTurns(
        turns=[
            CleanedTurn(
                turn_index=b.turn_index,
                cleaned_transcription=b.transcription,
                cleaned_translation=b.translation,
            )
            for b in batch
        ],
        glossary_corrections=[],
    )


def clean_batch(llm, batch: list[SpeakerTurn], glossary: str, _depth: int = 0) -> CleanedTurns:
    """Clean one batch of turns. Splits in half and recurses if output is truncated."""

    # Hard stop: a single turn that still truncates is uncleanable — pass through.
    if len(batch) == 1 and _depth > 0:
        logger.warning(
            "Single turn %s still truncates — passing through uncleaned",
            batch[0].turn_index,
        )
        return _fallback_identity(batch)

    system = build_system_prompt(glossary)
    payload = [t.model_dump(mode="json") for t in batch]
    payload_json = json.dumps(payload, ensure_ascii=False)
    expected = [b.turn_index for b in batch]
    user_body = build_user_message(payload_json, expected_indices=expected)

    reminders = [
        (
            "Your previous reply did not match the <output_contract>. "
            "Return a single JSON object with keys `turns` and "
            f"`glossary_corrections`. `turns` must contain exactly {len(batch)} "
            f"items with turn_index values {expected} in that order. "
            "No markdown, no commentary."
        ),
        "Final attempt — emit valid JSON matching <output_contract> only.",
    ]

    messages: list = [
        SystemMessage(content=system),
        HumanMessage(content=user_body),
    ]

    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            if attempt > 0:
                messages.append(HumanMessage(content=reminders[attempt - 1]))
            parsed = _invoke_cleaned_turns(llm, messages)
            aligned = _align_to_batch(batch, parsed)
            return aligned
        except _OutputTruncatedError:
            # Output token limit hit — split the batch and recurse, don't retry.
            mid = len(batch) // 2
            logger.warning(
                "Output truncated for batch of %d turns (depth=%d) — splitting at %d",
                len(batch), _depth, mid,
            )
            left = clean_batch(llm, batch[:mid], glossary, _depth + 1)
            right = clean_batch(llm, batch[mid:], glossary, _depth + 1)
            merged_turns = left.turns + right.turns
            merged_glossary: list[GlossaryCorrection] = list(left.glossary_corrections)
            _merge_glossary(merged_glossary, right.glossary_corrections)
            return CleanedTurns(turns=merged_turns, glossary_corrections=merged_glossary)
        except Exception as e:
            last_exc = e
            logger.warning("clean_batch attempt %s failed: %s", attempt + 1, e)

    logger.error("All retries failed for batch starting turn_index=%s: %s", batch[0].turn_index, last_exc)
    return _fallback_identity(batch)


def _merge_glossary(acc: list[GlossaryCorrection], new: list[GlossaryCorrection]) -> None:
    seen: set[tuple[str, str]] = {(g.heard, g.corrected) for g in acc}
    for g in new:
        key = (g.heard, g.corrected)
        if key not in seen:
            seen.add(key)
            acc.append(g)


def assemble(
    raw: dict,
    turns: list[SpeakerTurn],
    cleaned_flat: list[CleanedTurn],
    glossary_all: list[GlossaryCorrection],
    *,
    model: str,
) -> dict:
    by_idx = {c.turn_index: c for c in cleaned_flat}
    pp_turns: list[PostprocessTurnOut] = []
    for t in turns:
        c = by_idx.get(t.turn_index)
        if c is None:
            c = CleanedTurn(
                turn_index=t.turn_index,
                cleaned_transcription=t.transcription,
                cleaned_translation=t.translation,
            )
        pp_turns.append(
            PostprocessTurnOut(
                turn_index=t.turn_index,
                speaker_id=t.speaker_id,
                start_time=t.start_time,
                end_time=t.end_time,
                transcription=t.transcription,
                translation=t.translation,
                cleaned_transcription=c.cleaned_transcription,
                cleaned_translation=c.cleaned_translation,
            )
        )

    meta = PostprocessMeta(model=model, turns=pp_turns, glossary_corrections=glossary_all)
    out = dict(raw)
    out["postprocess"] = meta.model_dump(mode="json")
    return out


def run(
    raw: dict,
    *,
    model: str,
    glossary: str,
    on_progress: Callable[[int, int], None] | None = None,
) -> dict:
    segments = raw.get("segments")
    if not isinstance(segments, list):
        raise ValueError("results JSON must contain a list field 'segments'")

    turns = group_turns(segments)
    batches = batch_turns(turns, model=model)
    llm = make_chat(model)

    cleaned_flat: list[CleanedTurn] = []
    glossary_acc: list[GlossaryCorrection] = []

    for i, batch in enumerate(batches):
        result = clean_batch(llm, batch, glossary)
        cleaned_flat.extend(result.turns)
        _merge_glossary(glossary_acc, result.glossary_corrections)
        if on_progress:
            on_progress(i + 1, len(batches))

    return assemble(raw, turns, cleaned_flat, glossary_acc, model=model)
