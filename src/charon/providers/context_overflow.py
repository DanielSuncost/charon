"""Recognise a provider refusing a prompt because it does not fit the window.

Every provider words this differently and none of them share an error code,
so detection is by message. The wordings covered are the published error
texts of OpenAI-compatible servers (OpenAI, OpenRouter, vLLM, llama.cpp,
LM Studio), Anthropic and Gemini.

Contract: docs/contracts/context-budget-and-recovery.md
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Statuses that mean something else even when the body mentions tokens:
# authentication, missing model, and rate limits counted in tokens per minute.
_NOT_OVERFLOW_STATUS = frozenset({401, 403, 404, 429})

_VERDICT = re.compile(
    r"context[_ -]?length[_ -]?exceeded"
    r"|exceed[_ -]?context[_ -]?size"
    r"|maximum context length"
    r"|context (?:length|window|size)"
    r"|prompt is too long"
    r"|input is too long"
    r"|too many (?:input |prompt )?tokens"
    r"|input token count"
    r"|exceeds? the (?:maximum|max|model's) (?:number of )?(?:input |prompt )?tokens",
    re.IGNORECASE,
)

# Tokens-per-minute refusals mention limits and requested tokens too.
_RATE_LIMIT = re.compile(r"rate limit|per min|tokens per|\bTPM\b|quota", re.IGNORECASE)

_NUMBER = r"(\d[\d,]*)"

_WINDOW_PATTERNS = tuple(re.compile(p, re.IGNORECASE) for p in (
    r"n_ctx\W{1,4}" + _NUMBER,
    r"maximum context length (?:is|of) " + _NUMBER,
    r"context (?:length|window|size)(?: is| of)?(?: only)? " + _NUMBER,
    r">\s*" + _NUMBER + r" maximum",
    r"maximum number of tokens allowed \(?" + _NUMBER,
    r"maximum (?:of|is) " + _NUMBER + r" tokens",
))

_PROMPT_PATTERNS = tuple(re.compile(p, re.IGNORECASE) for p in (
    r"n_prompt_tokens\W{1,4}" + _NUMBER,
    r"prompt is too long: " + _NUMBER,
    r"input token count \(?" + _NUMBER,
    r"\(" + _NUMBER + r" in (?:the |your )?(?:messages|prompt)",
    r"requested (?:about )?" + _NUMBER + r" tokens",
))


@dataclass(frozen=True)
class ContextOverflow:
    """A prompt refused for size. Either number may be absent."""
    window: int | None = None
    prompt_tokens: int | None = None


def _first_number(patterns, text: str) -> int | None:
    for pattern in patterns:
        match = pattern.search(text)
        if match:
            try:
                value = int(match.group(1).replace(',', ''))
            except ValueError:
                continue
            if value > 0:
                return value
    return None


def detect_context_overflow(
    text: str | None,
    status_code: int | None = None,
) -> ContextOverflow | None:
    """Return the overflow described by an error message, or None."""
    if not text:
        return None
    if status_code in _NOT_OVERFLOW_STATUS:
        return None
    if not _VERDICT.search(text):
        return None
    if status_code is None and _RATE_LIMIT.search(text):
        return None
    return ContextOverflow(
        window=_first_number(_WINDOW_PATTERNS, text),
        prompt_tokens=_first_number(_PROMPT_PATTERNS, text),
    )


__all__ = ['ContextOverflow', 'detect_context_overflow']
