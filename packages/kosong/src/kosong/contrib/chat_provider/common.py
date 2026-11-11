"""Shared helpers for chat providers that receive reasoning from OpenAI-compatible APIs."""

from __future__ import annotations

from typing import Any, Literal

# Candidate field names for reasoning content in OpenAI-compatible APIs.
#
# - "reasoning_content" — DeepSeek / Kimo / GLM / Doubao / most gateways (the de-facto standard)
# - "reasoning"         — SenseNova / MiMo / some vLLM / some OpenRouter gateways
DEFAULT_REASONING_KEYS: tuple[str, ...] = ("reasoning_content", "reasoning")


type ToolMessageConversion = Literal["extract_text"]


def extract_reasoning(obj: Any, keys: tuple[str, ...] = DEFAULT_REASONING_KEYS) -> str | None:
    """Return the first non-empty reasoning string found among *keys* on *obj*.

    Returns ``None`` when no candidate key carries a non-empty string, so callers
    can distinguish "no reasoning" from "reasoning field present but empty".
    """
    for key in keys:
        value = getattr(obj, key, None)
        if value:
            return value
    return None
