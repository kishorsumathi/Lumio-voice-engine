"""LangChain ChatAnthropic factory for the post-process portal.

Uses the Anthropic API directly with Claude Opus 4.6.
Auth via ``ANTHROPIC_API_KEY`` read from postprocess-ui/.env.
Optional ``ANTHROPIC_MODEL_ID`` overrides the default model.
"""

from __future__ import annotations

import os

from langchain_anthropic import ChatAnthropic

_FALLBACK_MODEL_ID = "claude-opus-4-6"


def make_chat(
    model: str | None = None,
    *,
    temperature: float | None = None,
    max_tokens: int = 16384,
) -> ChatAnthropic:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is not set — add it to postprocess-ui/.env."
        )

    mid = (model or os.environ.get("ANTHROPIC_MODEL_ID") or _FALLBACK_MODEL_ID).strip()

    return ChatAnthropic(
        model=mid,
        anthropic_api_key=api_key,
        temperature=0.0 if temperature is None else temperature,
        max_tokens=max_tokens,
    )
