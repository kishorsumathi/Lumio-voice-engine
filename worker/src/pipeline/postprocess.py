"""
LLM post-processing pass — normalises transcription and translation.

Runs after the Sarvam transcription + translation stage. Sends merged
segments through Claude in batches and returns normalised text for each
segment. This step is optional: if ANTHROPIC_API_KEY is absent the worker
skips it silently and writes results without normalised fields.

Output fields added per segment:
  normalized_transcript  — ASR-cleaned, script-restored transcription
  normalized_translation — cleaned fluent English translation

Glossary is read from a JSON file at GLOSSARY_FILE_PATH:
  {
    "corrections": [{"heard": "cat distributing", "corrected": "catastrophising"}],
    "terms":       ["sertraline", "sukoon"]
  }
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from .merger import MergedSegment

logger = logging.getLogger(__name__)

# ── Batch budget ──────────────────────────────────────────────────────────────
# 80K chars keeps cleaned-output JSON comfortably under the 16K max_tokens cap.
_BATCH_BUDGET_CHARS = 80_000


# ── Pydantic schemas (structured output contract) ────────────────────────────

class _CleanedSegment(BaseModel):
    turn_index: int
    cleaned_transcription: str
    cleaned_translation: str


class _CleanedBatch(BaseModel):
    turns: list[_CleanedSegment] = Field(default_factory=list)
    glossary_corrections: list[dict] = Field(default_factory=list)


# ── Dataclasses ───────────────────────────────────────────────────────────────

@dataclass
class PostprocessOutput:
    normalized: dict[int, tuple[str, str]]   # segment_index → (transcript, translation)
    glossary_corrections: list[dict]
    model: str


# ── Glossary loading ──────────────────────────────────────────────────────────

def load_glossary(path: str | Path) -> str:
    """
    Read glossary JSON and return the text block the prompt expects.

    JSON format:
      {
        "corrections": [{"heard": "...", "corrected": "..."}],
        "terms":       ["sertraline", "sukoon"]
      }

    Returns empty string if the file is missing, empty, or malformed.
    """
    p = Path(path)
    if not p.exists():
        logger.debug("Glossary file not found: %s — using empty glossary", path)
        return ""
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("Could not read glossary %s: %s — using empty glossary", path, e)
        return ""

    lines: list[str] = []
    for c in data.get("corrections") or []:
        heard = (c.get("heard") or "").strip()
        corrected = (c.get("corrected") or "").strip()
        if heard and corrected:
            lines.append(f"{heard} → {corrected}")
    for t in data.get("terms") or []:
        term = (t or "").strip()
        if term:
            lines.append(term)
    return "\n".join(lines)


# ── Prompt builders ───────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
<role>
You are an expert editor of therapy and psychiatry session transcripts produced by an automatic speech recognition (ASR) system. Each transcript is a one-to-one session between a CLINICIAN (a therapist or psychiatrist) and a PATIENT — never an interview, podcast, or panel discussion.

You repair ASR errors, clean up disfluencies, and reformat speech into readable text — while preserving each speaker's meaning, emotional register, writing scripts, and code-switching pattern exactly. Your output is read by the clinician for case notes and review, so faithfulness and clinical accuracy matter far more than stylistic flair.
</role>

<task>
You will receive a JSON array of contiguous speaker segments from a single therapy/psychiatry session. Each segment has:
  - turn_index (int) — opaque identifier; echo unchanged.
  - speaker_id  (int) — reference only; do NOT mention the speaker inside the cleaned text fields.
  - transcription (str) — English and/or Indian-language content, in whatever scripts the ASR emitted.
  - translation   (str) — pre-existing English translation of the same segment.

For every input segment produce exactly one output segment with the same turn_index, containing:
  - cleaned_transcription — same languages AND same scripts as the source.
  - cleaned_translation   — the English version, repaired and formatted.

Apply these four cleanup passes in this order:

  1. CLINICAL_TERMS
     Fix obviously misheard mental-health, psychiatric, and medical terms.
     Common targets: CBT/DBT/ACT terminology (catastrophising, rumination,
     avoidance, exposure, behavioural activation, schema), symptom vocabulary
     (anhedonia, dissociation, derealisation, panic, intrusive thoughts),
     psychiatric medication names (SSRI, SNRI, sertraline, escitalopram,
     fluoxetine, mirtazapine, lithium, lamotrigine, quetiapine, olanzapine,
     propranolol, clonazepam), and dosage/frequency phrasing.
     Use the <glossary> block when present. Only correct when unambiguous.

  2. MULTILINGUAL + SCRIPT_RESTORATION
     cleaned_transcription must contain ZERO romanised/transliterated
     Indian-language text. Only two things are allowed in Latin script:
     genuine English words and proper nouns conventionally spelled in Latin.
     Every Indian-language word written in Latin by the ASR MUST be converted
     to its native script.

     Examples of required conversions:
       "ab dekho"    → "अब देखो"        (Hindi → Devanagari)
       "theek hai"   → "ठीक है"          (Hindi → Devanagari)
       "naan solren" → "நான் சொல்றேன்"   (Tamil → Tamil script)
       "ami boli"    → "আমি বলি"         (Bengali → Bengali script)

     English loanwords inside Indian-language sentences stay Latin:
       "mood बहुत low है"  ✓
       "मूड बहुत लो है"    ✗

     Indic-script spans already in native script must NOT be romanised.

     cleaned_translation is always fluent English — translate every fragment.

  3. FORMATTING
     a) Sentence boundaries — capital first letter; correct terminal mark
        (. ? !); danda (।) for pure Hindi sentences.
     b) Commas — insert at natural pauses; do not over-comma.
     c) Capitalisation — CBT, DBT, ACT, SSRI, SNRI always uppercase;
        generic drug names lowercase (sertraline); no random emphasis caps.
     d) Interrupted speech — em-dash (—) for mid-sentence breaks; ellipsis
        (…) for trailing meaningful pauses; no punctuation residue after
        filler removal.
     e) Paragraph breaks — \\n\\n only at genuine topic shifts in long
        segments; never after every sentence.
     f) Numbers — digits for clinical quantities ("50 mg"); spelled-out for
        conversational context ("twice a day").
     g) Mixed-script spacing — single space between Latin and Indic words:
        "mood बहुत low है"; no space before danda (।).
     h) Never in output — no markdown, no speaker labels, no [inaudible].

  4. NOISE
     Remove: filler words ("uh", "um", "you know"), back-channels ("hmm",
     "mm-hmm", "haan haan"), stutters, false starts, ASR doublings.
     PRESERVE: emotionally weighted hesitation ("I just— I just can't"),
     genuine tearful repetition, patient self-corrections that change meaning.
</task>

<rules>
HARD RULES:
  - Echo every input turn_index in the output, in the SAME ORDER.
  - One output segment per input segment — no merging, no splitting.
  - NEVER invent content (diagnoses, dates, names, drug doses, family
    history) not present in the source.
  - Do NOT re-attribute content between speakers.
  - cleaned_transcription must contain ZERO romanised Indian-language text.
    Every romanised Indian-language word MUST be in its native script.
  - Do not romanise Indic-script content already in native script.
  - NEVER add speaker labels ("Therapist:", "Patient:", "Speaker N:").
  - cleaned_translation is English ONLY.
  - Empty input (transcription="" and translation="") → return both empty.
  - For each notable clinical mishearing fixed, add {heard, corrected} to
    glossary_corrections.
</rules>

<examples>
Example A — CLINICAL_TERMS
  input:  "She keeps cat distributing about the future and can't sleep."
  output: "She keeps catastrophising about the future and can't sleep."
  glossary: {"heard": "cat distributing", "corrected": "catastrophising"}

Example B — SCRIPT_RESTORATION (romanised Hinglish → Devanagari)
  input transcription:  "haan I I I feel like mood bahut low rehta hai aaj kal aur neend bhi nahi aati uh uh"
  input translation:    "yes I feel like mood is very low these days and sleep also does not come"
  cleaned_transcription: "हाँ, I feel like mood बहुत low रहता है आजकल, और नींद भी नहीं आती।"
  cleaned_translation:   "Yes, I feel like my mood has been very low lately, and I'm not sleeping well either."

Example C — SCRIPT_RESTORATION (real-world sentence)
  input transcription:  "ab dekho these situations tend to get chaotic what you have to keep reinforcing is that although you do understand her concerns validate her"
  input translation:    "Now look, these situations tend to get chaotic. What you have to keep reinforcing is that although you do understand her concerns, validate her."
  cleaned_transcription: "अब देखो, these situations tend to get chaotic. What you have to keep reinforcing is that although you do understand her concerns, validate her."
  cleaned_translation:   "Now look, these situations tend to get chaotic. What you have to keep reinforcing is that although you do understand her concerns, validate her."

Example D — NOISE
  input:  "so um I I I was thinking like you know maybe maybe we should um titrate the dose up to 50 milligrams"
  output: "I was thinking maybe we should titrate the dose up to 50 milligrams."
</examples>

<output_contract>
Return a single JSON object — no markdown fences, no commentary. Shape:
{
  "turns": [
    {"turn_index": <int>, "cleaned_transcription": "<str>", "cleaned_translation": "<str>"},
    ...
  ],
  "glossary_corrections": [{"heard": "<str>", "corrected": "<str>"}, ...]
}
turns length and order MUST match the input array.
glossary_corrections may be [] if no clinical fixes were made.
</output_contract>

<self_check>
Before emitting, verify:
  1. Every input turn_index appears exactly once in the output, in order.
  2. No cleaned_translation contains non-English fragments.
  3. No cleaned text contains speaker labels or markdown.
  4. No content was invented.
  5. cleaned_transcription has ZERO romanised Indian-language text —
     every Latin word is either genuine English or a Latin proper noun.
Then emit JSON only.
</self_check>
"""


def _build_system_prompt(glossary_text: str) -> str:
    gloss = (glossary_text or "").strip()
    if not gloss:
        glossary_xml = (
            "<glossary>\n"
            "(no domain glossary provided — infer corrections from session context only)\n"
            "</glossary>"
        )
    else:
        glossary_xml = (
            "<glossary>\n"
            "Apply these substitutions where they fit the surrounding context.\n"
            "Each line is either `wrong → right` or just `term` (authoritative spelling).\n\n"
            f"{gloss}\n"
            "</glossary>"
        )
    return _SYSTEM_PROMPT + "\n" + glossary_xml


def _build_user_message(payload_json: str, expected_indices: list[int]) -> str:
    return (
        "<input_batch>\n"
        f"Number of segments: {len(expected_indices)}\n"
        f"Expected turn_index sequence: {expected_indices}\n"
        "Below is the JSON array of input segments from a therapy / psychiatry "
        "session. Process each one and return the JSON object specified by "
        "<output_contract>. Output JSON only — no fences, no commentary.\n\n"
        f"{payload_json}\n"
        "</input_batch>"
    )


# ── JSON parse helpers ────────────────────────────────────────────────────────

_THINK_RE = re.compile(r"<think[^>]*>[\s\S]*?</think>", re.IGNORECASE)
_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)\s*```", re.IGNORECASE)


def _extract_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            elif isinstance(block, str):
                parts.append(block)
        return "".join(parts)
    return str(content)


def _parse_json_object(text: str) -> dict:
    s = _THINK_RE.sub("", text or "").strip()
    for body in reversed(_FENCE_RE.findall(s)):
        try:
            obj = json.loads(body.strip())
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    depth, start, in_str, esc = 0, -1, False, False
    candidates: list[tuple[int, int]] = []
    for i, ch in enumerate(s):
        if in_str:
            esc = not esc and ch == "\\"
            if not esc and ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth > 0:
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


# ── Truncation sentinel ───────────────────────────────────────────────────────

class _TruncatedError(Exception):
    pass


def _is_truncated(resp) -> bool:
    meta = getattr(resp, "response_metadata", {}) or {}
    reason = meta.get("stop_reason") or meta.get("finish_reason") or ""
    return str(reason).lower() == "max_tokens"


# ── Core invoke ───────────────────────────────────────────────────────────────

def _invoke(llm: ChatAnthropic, messages: list) -> _CleanedBatch:
    for method in ("json_schema", None):
        try:
            kwargs = {"method": method} if method else {}
            structured = llm.with_structured_output(_CleanedBatch, **kwargs)
            out = structured.invoke(messages)
            if isinstance(out, _CleanedBatch):
                return out
            if isinstance(out, dict):
                return _CleanedBatch.model_validate(out)
        except Exception as e:
            logger.debug("Structured output (%s) failed: %s", method or "tool_calling", e)

    resp = llm.invoke(messages)
    if _is_truncated(resp):
        raise _TruncatedError("max_tokens hit — output truncated")
    text = _extract_text(resp.content)
    if not text.strip():
        raise ValueError("Model returned empty content")
    obj = _parse_json_object(text)
    return _CleanedBatch.model_validate(obj)


# ── Alignment (fills any gaps Claude skipped) ─────────────────────────────────

def _align(batch_segs: list[dict], parsed: _CleanedBatch) -> _CleanedBatch:
    by_idx = {c.turn_index: c for c in parsed.turns}
    fixed = []
    for s in batch_segs:
        ct = by_idx.get(s["turn_index"])
        if ct is None:
            fixed.append(_CleanedSegment(
                turn_index=s["turn_index"],
                cleaned_transcription=s["transcription"],
                cleaned_translation=s["translation"],
            ))
        else:
            fixed.append(ct)
    return _CleanedBatch(turns=fixed, glossary_corrections=parsed.glossary_corrections)


def _identity(batch_segs: list[dict]) -> _CleanedBatch:
    return _CleanedBatch(
        turns=[
            _CleanedSegment(
                turn_index=s["turn_index"],
                cleaned_transcription=s["transcription"],
                cleaned_translation=s["translation"],
            )
            for s in batch_segs
        ],
        glossary_corrections=[],
    )


# ── Batch execution with retries + split-on-truncation ───────────────────────

def _clean_batch(
    llm: ChatAnthropic,
    batch_segs: list[dict],
    system: str,
    _depth: int = 0,
) -> _CleanedBatch:
    if len(batch_segs) == 1 and _depth > 0:
        logger.warning(
            "Single segment %s still truncates — passing through uncleaned",
            batch_segs[0]["turn_index"],
        )
        return _identity(batch_segs)

    payload_json = json.dumps(batch_segs, ensure_ascii=False)
    expected = [s["turn_index"] for s in batch_segs]
    user_body = _build_user_message(payload_json, expected)

    reminders = [
        (
            "Your previous reply did not match the <output_contract>. "
            "Return a single JSON object with keys `turns` and `glossary_corrections`. "
            f"`turns` must contain exactly {len(batch_segs)} items with "
            f"turn_index values {expected} in that order. No markdown, no commentary."
        ),
        "Final attempt — emit valid JSON matching <output_contract> only.",
    ]

    messages: list = [SystemMessage(content=system), HumanMessage(content=user_body)]
    last_exc: Exception | None = None

    for attempt in range(3):
        try:
            if attempt > 0:
                messages.append(HumanMessage(content=reminders[attempt - 1]))
            parsed = _invoke(llm, messages)
            return _align(batch_segs, parsed)
        except _TruncatedError:
            mid = len(batch_segs) // 2
            logger.warning(
                "Output truncated for batch of %d segments (depth=%d) — splitting at %d",
                len(batch_segs), _depth, mid,
            )
            left = _clean_batch(llm, batch_segs[:mid], system, _depth + 1)
            right = _clean_batch(llm, batch_segs[mid:], system, _depth + 1)
            merged_corrections: list[dict] = list(left.glossary_corrections)
            seen = {(c.get("heard"), c.get("corrected")) for c in merged_corrections}
            for c in right.glossary_corrections:
                key = (c.get("heard"), c.get("corrected"))
                if key not in seen:
                    seen.add(key)
                    merged_corrections.append(c)
            return _CleanedBatch(
                turns=left.turns + right.turns,
                glossary_corrections=merged_corrections,
            )
        except Exception as e:
            last_exc = e
            logger.warning("clean_batch attempt %d failed: %s", attempt + 1, e)

    logger.error(
        "All retries failed for batch starting turn_index=%s: %s",
        batch_segs[0]["turn_index"], last_exc,
    )
    return _identity(batch_segs)


# ── Public entry point ────────────────────────────────────────────────────────

def run_postprocess(
    merged: list[MergedSegment],
    *,
    api_key: str,
    model: str,
    glossary_path: str,
) -> PostprocessOutput:
    """
    Run LLM normalisation over all merged segments.

    Returns a PostprocessOutput with:
      normalized: dict mapping segment_index → (normalized_transcript, normalized_translation)
      glossary_corrections: deduplicated list of {heard, corrected} dicts
      model: the model ID used
    """
    glossary_text = load_glossary(glossary_path)
    system = _build_system_prompt(glossary_text)

    llm = ChatAnthropic(
        model=model,
        anthropic_api_key=api_key,
        temperature=0.0,
        max_tokens=16384,
    )

    # Each MergedSegment becomes one turn (1:1 mapping, no grouping).
    # turn_index = segment_index so alignment is trivial.
    all_segs = [
        {
            "turn_index": s.segment_index,
            "speaker_id": s.speaker_id,
            "transcription": s.text,
            "translation": s.translation,
        }
        for s in merged
    ]

    # Split into batches by character budget
    batches: list[list[dict]] = []
    cur: list[dict] = []
    cur_chars = 0
    for seg in all_segs:
        sz = len(json.dumps(seg, ensure_ascii=False))
        if cur and cur_chars + sz > _BATCH_BUDGET_CHARS:
            batches.append(cur)
            cur, cur_chars = [], 0
        cur.append(seg)
        cur_chars += sz
    if cur:
        batches.append(cur)

    logger.info("Postprocessing %d segments in %d batches", len(merged), len(batches))

    normalized: dict[int, tuple[str, str]] = {}
    all_corrections: list[dict] = []
    seen_corrections: set[tuple[str, str]] = set()

    for i, batch in enumerate(batches):
        result = _clean_batch(llm, batch, system)
        for ct in result.turns:
            normalized[ct.turn_index] = (ct.cleaned_transcription, ct.cleaned_translation)
        for c in result.glossary_corrections:
            key = (c.get("heard", ""), c.get("corrected", ""))
            if key not in seen_corrections:
                seen_corrections.add(key)
                all_corrections.append(c)
        logger.info("Postprocess batch %d/%d done", i + 1, len(batches))

    return PostprocessOutput(
        normalized=normalized,
        glossary_corrections=all_corrections,
        model=model,
    )
