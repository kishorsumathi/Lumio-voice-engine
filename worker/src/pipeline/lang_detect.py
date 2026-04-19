"""
Lingua-based language detection — used **only** as a fallback inside the
`en-IN` passthrough retry path in `translation.py`.

Why a fallback, not a main-path detector:
    - Saaras v3 with `mode=codemix, language_code=unknown` already outputs
      text in native scripts, so the byte-level script regex in
      `_detect_indic_source` decides "is this non-English?" perfectly.
    - What scripts *cannot* tell apart is **which Indic language** uses a
      given script (most painfully, Hindi vs Marathi — both Devanagari).
    - Lingua's statistical model disambiguates those cases well on
      retry-path text (typically the full segment source, 50+ chars).

Contract:
    `detect_source_code(text)` returns a Mayura source code
    (e.g. `"mr-IN"`) when confidence is high enough, else `None` — in which
    case the caller falls back to `_detect_indic_source` (script regex).

Cost/perf:
    - Model built lazily on first call; ~0.5s cold start, cached for process life.
    - Per-call latency ~0.1–0.5 ms for retry-sized inputs.
    - Memory ~10 MB for the 11-language subset.

If `lingua` isn't installed (e.g. local dev without the extra), the module
fails soft — `detect_source_code` returns `None` and the existing script
regex handles everything just like before.
"""
from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)

# Short text is where statistical LID is unreliable. Below this many
# characters we skip lingua and let the script-regex fallback decide.
_MIN_CHARS = 10

# Minimum top-1 confidence to accept lingua's answer. Below this we also
# fall back. Empirically 0.75 keeps Hindi/Marathi separation clean without
# over-rejecting on noisy retry inputs.
_MIN_CONFIDENCE = 0.75

_detector = None
_detector_lock = threading.Lock()
_lang_to_mayura: dict = {}
_import_failed = False


def _build_detector():
    """
    Lazy-init the Lingua detector for the Indian languages + English we care
    about.

    Lingua-py (v2.x) ships models for: BENGALI, GUJARATI, HINDI, MARATHI,
    PUNJABI, TAMIL, TELUGU, URDU, ENGLISH. It does **not** ship Kannada,
    Malayalam, Nepali, Sanskrit, Assamese, or Oriya. Those are intentionally
    not in this list — their Unicode blocks are **unique** (no script-sharing
    ambiguity), so `_detect_indic_source`'s script-range regex already maps
    them to the correct Mayura code with 100% accuracy.

    Where lingua actually earns its keep is **within a shared script** —
    Hindi vs Marathi (both Devanagari) being the headline case — which no
    amount of regex can resolve.

    We use `getattr` so a missing enum member degrades gracefully instead
    of raising at import time.
    """
    global _detector, _lang_to_mayura, _import_failed
    if _detector is not None or _import_failed:
        return _detector

    with _detector_lock:
        if _detector is not None or _import_failed:
            return _detector
        try:
            from lingua import Language, LanguageDetectorBuilder
        except Exception as e:
            logger.warning(
                "lingua not available — falling back to script-regex only: %s", e
            )
            _import_failed = True
            return None

        wanted = [
            ("HINDI",    "hi-IN"),
            ("MARATHI",  "mr-IN"),
            ("BENGALI",  "bn-IN"),
            ("GUJARATI", "gu-IN"),
            ("PUNJABI",  "pa-IN"),
            ("TAMIL",    "ta-IN"),
            ("TELUGU",   "te-IN"),
            ("URDU",     "ur-IN"),
            ("ENGLISH",  "en-IN"),
        ]
        languages: list = []
        mapping: dict = {}
        missing: list[str] = []
        for name, code in wanted:
            lang = getattr(Language, name, None)
            if lang is None:
                missing.append(name)
                continue
            languages.append(lang)
            mapping[lang] = code

        if missing:
            logger.warning(
                "Lingua missing languages (falling back to script regex for these): %s",
                ", ".join(missing),
            )
        if not languages:
            logger.warning("Lingua exposes none of the wanted languages — disabling")
            _import_failed = True
            return None

        _lang_to_mayura = mapping
        _detector = LanguageDetectorBuilder.from_languages(*languages).build()
        logger.info("Lingua language detector built (%d languages)", len(languages))
    return _detector


def detect_source_code(text: str) -> str | None:
    """
    Return a Mayura source code (e.g. `"mr-IN"`) for `text`, or `None` if
    confidence is too low / lingua unavailable / text too short.

    Callers should treat `None` as "use the script-regex fallback".
    Never raises — on any internal error we return `None`.
    """
    if not text:
        return None
    stripped = text.strip()
    if len(stripped) < _MIN_CHARS:
        return None

    detector = _build_detector()
    if detector is None:
        return None

    try:
        confidences = detector.compute_language_confidence_values(stripped)
    except Exception as e:
        logger.debug("Lingua detection error (falling back): %s", e)
        return None
    if not confidences:
        return None

    top = confidences[0]
    score = getattr(top, "value", None)
    language = getattr(top, "language", None)
    lang_name = getattr(language, "name", str(language)) if language else "UNKNOWN"

    # Compact preview of the text for log correlation (never log full segments —
    # could be medical PHI).
    preview = stripped[:40].replace("\n", " ")
    if len(stripped) > 40:
        preview += "…"

    if score is None or language is None or score < _MIN_CONFIDENCE:
        logger.info(
            "lingua: low-confidence fallback — top=%s score=%.2f chars=%d text=%r",
            lang_name, score or 0.0, len(stripped), preview,
        )
        return None

    code = _lang_to_mayura.get(language)
    if code == "en-IN":
        # A script-confirmed Indic segment that lingua scored as English is
        # almost always a lingua error on short / mixed text. Let the
        # caller's script-regex fallback handle it.
        logger.info(
            "lingua: rejected ENGLISH on script-Indic text (falling back) — score=%.2f chars=%d text=%r",
            score, len(stripped), preview,
        )
        return None

    logger.info(
        "lingua: %s (%s) score=%.2f chars=%d text=%r",
        lang_name, code, score, len(stripped), preview,
    )
    return code
