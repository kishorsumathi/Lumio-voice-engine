"""
Sarvam translation of transcription segments.

Model: mayura:v1 — supports auto source detection + 11 Indian languages + English.
Mode:  formal — appropriate for medical/professional sessions.
Limit: 900 chars per request (under mayura:v1's 1000 char limit).

Batching: segments are packed into batches up to 900 chars / 10 segments using
a ⟦S⟧ separator (Unicode, preserved by Mayura), translated in one API call,
then split back per segment. Reduces ~1260 API calls to ~72 batches.

en-IN passthrough fix: on heavy code-mixed input in ANY Indic script (Devanagari,
Bengali, Gurmukhi, Gujarati, Oriya, Tamil, Telugu, Kannada, Malayalam), Mayura
with source=auto can return the input unchanged. After the first pass we detect
those (Indic script present in output, or output == input) and retry ONLY those
segments with the Mayura source code that matches the detected script.

Parallelism: batches translated concurrently up to MAX_TRANSLATION_WORKERS,
shared 100 RPM rate limiter via rate_limit.throttle().
"""
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from tenacity import retry, stop_after_attempt, wait_exponential, before_sleep_log

from .config import DEFAULT_TARGET_LANGUAGES, get_sarvam_api_key
from .merger import MergedSegment
from .rate_limit import throttle

logger = logging.getLogger(__name__)

_MAX_CHARS_PER_BATCH = 900   # under Mayura's 1000-char limit
_MAX_SEGS_PER_BATCH = 10     # hard cap; ⟦S⟧ separator is preserved so large batches are safe
MAX_TRANSLATION_WORKERS = 10
_SEP = " ⟦S⟧ "  # Unicode angle-bracket marker — preserved by Mayura, never in Indic speech

# Indic script → Mayura v1 source code. Order preserved so Devanagari wins
# when a line mixes Devanagari with other scripts (rare but possible).
_INDIC_SCRIPT_TO_SOURCE: list[tuple[str, re.Pattern[str]]] = [
    ("hi-IN", re.compile(r"[\u0900-\u097F]")),   # Devanagari (Hindi, Marathi, Nepali, Sanskrit)
    ("bn-IN", re.compile(r"[\u0980-\u09FF]")),   # Bengali / Assamese
    ("pa-IN", re.compile(r"[\u0A00-\u0A7F]")),   # Gurmukhi (Punjabi)
    ("gu-IN", re.compile(r"[\u0A80-\u0AFF]")),   # Gujarati
    ("od-IN", re.compile(r"[\u0B00-\u0B7F]")),   # Oriya
    ("ta-IN", re.compile(r"[\u0B80-\u0BFF]")),   # Tamil
    ("te-IN", re.compile(r"[\u0C00-\u0C7F]")),   # Telugu
    ("kn-IN", re.compile(r"[\u0C80-\u0CFF]")),   # Kannada
    ("ml-IN", re.compile(r"[\u0D00-\u0D7F]")),   # Malayalam
]


def _detect_indic_source(text: str) -> str | None:
    """Return the Mayura source code for the first Indic script found in text, or None."""
    for code, pattern in _INDIC_SCRIPT_TO_SOURCE:
        if pattern.search(text):
            return code
    return None

_LANG_NORMALIZE: dict[str, str] = {
    "en": "en-IN", "hi": "hi-IN", "bn": "bn-IN", "gu": "gu-IN",
    "kn": "kn-IN", "ml": "ml-IN", "mr": "mr-IN", "pa": "pa-IN",
    "ta": "ta-IN", "te": "te-IN", "ur": "ur-IN", "or": "od-IN",
    "as": "as-IN", "ne": "ne-IN", "sa": "sa-IN", "sd": "sd-IN",
}


@dataclass
class TranslatedSegment:
    segment_index: int
    translated_text: str


def _sarvam_client():
    from sarvamai import SarvamAI
    return SarvamAI(api_subscription_key=get_sarvam_api_key())


def _build_batches(segments: list[MergedSegment]) -> list[list[MergedSegment]]:
    """
    Pack segments into batches where joined text stays under _MAX_CHARS_PER_BATCH.
    Each segment's text is stripped; empty segments are kept as placeholders.
    """
    batches: list[list[MergedSegment]] = []
    current: list[MergedSegment] = []
    current_len = 0

    for seg in segments:
        text = seg.text.strip()
        addition = len(text) + (len(_SEP) if current else 0)
        if current and (current_len + addition > _MAX_CHARS_PER_BATCH or len(current) >= _MAX_SEGS_PER_BATCH):
            batches.append(current)
            current = [seg]
            current_len = len(text)
        else:
            current.append(seg)
            current_len += addition

    if current:
        batches.append(current)

    return batches


def _split_long_text(text: str, max_chars: int) -> list[str]:
    """
    Split text exceeding max_chars into chunks at sentence boundaries.
    Handles Hindi (।), English (. ? !), and mixed Hinglish.
    Falls back to word boundary if no sentence boundary found.
    """
    if len(text) <= max_chars:
        return [text]

    import re
    # Split on sentence-ending punctuation, keeping the delimiter
    sentences = re.split(r'(?<=[.?!।])\s+', text)

    chunks, current = [], ""
    for sentence in sentences:
        if len(sentence) > max_chars:
            # Single sentence too long — split at word boundary
            if current:
                chunks.append(current.strip())
                current = ""
            words = sentence.split()
            part = ""
            for word in words:
                if len(part) + len(word) + 1 > max_chars:
                    chunks.append(part.strip())
                    part = word
                else:
                    part = (part + " " + word).strip()
            if part:
                chunks.append(part)
        elif current and len(current) + len(sentence) + 1 > max_chars:
            chunks.append(current.strip())
            current = sentence
        else:
            current = (current + " " + sentence).strip()

    if current:
        chunks.append(current.strip())

    return [c for c in chunks if c]


@retry(
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=1, min=5, max=60),
    before_sleep=before_sleep_log(logger, logging.WARNING),
    reraise=True,
)
def _translate_batch_text(client, text: str, target_language: str) -> str:
    from sarvamai.core.api_error import ApiError

    throttle()
    try:
        response = client.text.translate(
            input=text,
            source_language_code="auto",
            target_language_code=target_language,
            model="mayura:v1",
            mode="formal",
        )
        return response.translated_text
    except ApiError as e:
        if e.status_code == 429:
            logger.warning("Sarvam 429 rate limit hit — waiting 60s before retry")
            time.sleep(60)
            raise
        if e.status_code == 400 and "exceed" in str(e).lower():
            # Segment too long — split at sentence boundaries, translate each, rejoin
            chunks = _split_long_text(text, 950)
            logger.warning("Text exceeds 1000 chars (%d) — splitting into %d chunks", len(text), len(chunks))
            parts = []
            for chunk in chunks:
                throttle()
                r = client.text.translate(
                    input=chunk,
                    source_language_code="auto",
                    target_language_code=target_language,
                    model="mayura:v1",
                    mode="formal",
                )
                parts.append(r.translated_text)
            return " ".join(parts)
        if "Unable to detect" in str(e):
            throttle()
            response = client.text.translate(
                input=text,
                source_language_code="hi-IN",
                target_language_code=target_language,
                model="mayura:v1",
                mode="formal",
            )
            return response.translated_text
        raise


def _translate_batch(
    client,
    batch: list[MergedSegment],
    target_language: str,
) -> list[TranslatedSegment]:
    """Translate one batch, split result back into per-segment translations."""
    texts = [seg.text.strip() for seg in batch]
    joined = _SEP.join(texts)

    try:
        translated_joined = _translate_batch_text(client, joined, target_language)
        parts = translated_joined.split(_SEP)

        if len(parts) == len(batch):
            return [
                TranslatedSegment(segment_index=seg.segment_index, translated_text=part.strip())
                for seg, part in zip(batch, parts)
            ]

        # Sarvam collapsed or changed the separators — re-translate each segment individually
        logger.warning(
            "Batch split mismatch: expected %d parts, got %d — retrying individually",
            len(batch), len(parts),
        )
        results = []
        for seg in batch:
            text = seg.text.strip()
            if not text:
                results.append(TranslatedSegment(segment_index=seg.segment_index, translated_text=""))
                continue
            try:
                translated = _translate_batch_text(client, text, target_language)
                results.append(TranslatedSegment(segment_index=seg.segment_index, translated_text=translated))
            except Exception as e2:
                logger.error("Individual fallback failed seg=%d: %s", seg.segment_index, e2)
                results.append(TranslatedSegment(segment_index=seg.segment_index, translated_text=""))
        return results
    except Exception as e:
        logger.error("Batch translation failed (lang=%s, segs=%s): %s",
                     target_language, [s.segment_index for s in batch], e)
        return [
            TranslatedSegment(segment_index=seg.segment_index, translated_text="")
            for seg in batch
        ]


def _en_passthrough_retry_source(source: str, translated: str) -> str | None:
    """
    If the en-IN output looks untranslated, return the Mayura source code to retry with.
    Covers any Indic script (Hindi, Bengali, Punjabi, Gujarati, Oriya, Tamil, Telugu,
    Kannada, Malayalam). Returns None when the output looks fine.
    """
    src = (source or "").strip()
    out = (translated or "").strip()
    if not src or not out:
        return None

    # Case 1: the English output still contains Indic script → retry with that script's source.
    code_from_output = _detect_indic_source(out)
    if code_from_output:
        return code_from_output

    # Case 2: the output is byte-for-byte the source (and long enough to be meaningful)
    # AND the source contains Indic script — pure-English repeats don't need a retry.
    if len(src) >= 15 and src == out:
        return _detect_indic_source(src)

    return None


def _retry_en_passthroughs(
    client,
    segments: list[MergedSegment],
    translated_list: list[TranslatedSegment],
) -> list[TranslatedSegment]:
    """
    Re-translate en-IN segments that look untranslated, using the source code
    matching the detected Indic script. Single-segment calls (no batching) to
    keep the retry surgical and avoid re-introducing the auto-detection passthrough.
    """
    seg_by_index: dict[int, MergedSegment] = {s.segment_index: s for s in segments}

    retry_plan: list[tuple[TranslatedSegment, str]] = []
    for t in translated_list:
        src_code = _en_passthrough_retry_source(seg_by_index[t.segment_index].text, t.translated_text)
        if src_code:
            retry_plan.append((t, src_code))

    if not retry_plan:
        return translated_list

    by_code: dict[str, int] = {}
    for _, c in retry_plan:
        by_code[c] = by_code.get(c, 0) + 1
    logger.info(
        "en-IN passthrough retry: %d/%d segments (by source: %s)",
        len(retry_plan), len(translated_list), by_code,
    )

    def _translate_one(text: str, src_code: str) -> str:
        throttle()
        response = client.text.translate(
            input=text,
            source_language_code=src_code,
            target_language_code="en-IN",
            model="mayura:v1",
            mode="formal",
        )
        return (response.translated_text or "").strip()

    def _one(item: tuple[TranslatedSegment, str]) -> TranslatedSegment:
        ts, src_code = item
        src = seg_by_index[ts.segment_index].text.strip()
        try:
            if len(src) > 950:
                pieces = _split_long_text(src, 950)
                fixed_parts = [_translate_one(p, src_code) for p in pieces]
                fixed = " ".join(p for p in fixed_parts if p).strip()
            else:
                fixed = _translate_one(src, src_code)
            if fixed and not _detect_indic_source(fixed) and fixed != src:
                return TranslatedSegment(segment_index=ts.segment_index, translated_text=fixed)
            return ts
        except Exception as e:
            logger.warning(
                "en-IN retry failed seg=%d (source=%s): %s",
                ts.segment_index, src_code, e,
            )
            return ts

    fixed_by_index: dict[int, TranslatedSegment] = {}
    with ThreadPoolExecutor(max_workers=MAX_TRANSLATION_WORKERS) as ex:
        for res in ex.map(_one, retry_plan):
            fixed_by_index[res.segment_index] = res

    repaired = [fixed_by_index.get(t.segment_index, t) for t in translated_list]
    changed = sum(
        1 for orig, new in zip(translated_list, repaired)
        if orig.translated_text != new.translated_text
    )
    logger.info("en-IN passthrough retry: %d segments repaired", changed)
    return repaired


def translate_segments(
    segments: list[MergedSegment],
    target_languages: list[str] | None = None,
) -> dict[str, list[TranslatedSegment]]:
    """
    Translate all segments into each target language using batched parallel calls.
    Returns: {language_code: [TranslatedSegment, ...]} in original segment order.
    """
    raw_langs = target_languages or DEFAULT_TARGET_LANGUAGES
    langs = [
        _LANG_NORMALIZE.get(lang.strip(), lang.strip())
        for lang in raw_langs
        if lang.strip()
    ]
    if not langs:
        return {}

    client = _sarvam_client()
    batches = _build_batches(segments)
    results: dict[str, list[TranslatedSegment]] = {}

    for lang in langs:
        logger.info(
            "Translating %d segments in %d batches → %s (workers=%d)",
            len(segments), len(batches), lang, MAX_TRANSLATION_WORKERS,
        )

        seg_results: dict[int, TranslatedSegment] = {}

        with ThreadPoolExecutor(max_workers=MAX_TRANSLATION_WORKERS) as executor:
            futures = {
                executor.submit(_translate_batch, client, batch, lang): batch
                for batch in batches
            }
            for future in as_completed(futures):
                for ts in future.result():
                    seg_results[ts.segment_index] = ts

        ordered = [seg_results[seg.segment_index] for seg in segments]
        if lang == "en-IN":
            ordered = _retry_en_passthroughs(client, segments, ordered)
        results[lang] = ordered
        logger.info("Translation complete: %s — %d segments", lang, len(results[lang]))

    return results
