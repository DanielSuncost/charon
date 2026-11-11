"""Context budget — how much of a model's window the conversation may use.

The window is shared by four things: the system prompt, the tool schemas,
the conversation history and the reply the model is about to write. Planning
history against the whole window only works while the window is much larger
than everything else; on an 8k-32k local model the prompt and the reply
reserve are most of it.

This module is pure arithmetic over estimates. Contract:
docs/contracts/context-budget-and-recovery.md
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, replace
from typing import Any

from charon.providers import Message

# Slack for the chars/4 estimator and per-message framing the estimate ignores.
SAFETY_MARGIN_TOKENS = 256
# The reply is never planned below this, whatever the window.
MIN_REPLY_RESERVE = 1024
# History is never planned below this; a window this tight overflows instead
# and is handled by overflow recovery.
MIN_HISTORY_BUDGET = 512
# A server-reported or derived window below this is treated as a misparse.
MIN_PLAUSIBLE_WINDOW = 1024
# Share of the planning window kept when an overflow names no usable window.
OVERFLOW_SHRINK = 0.8
# No single tool result may take more than this share of the history budget.
RESULT_SHARE = 0.5
# Of a clipped result, this share is taken from the end: exit status, the
# failing assertion and the final answer of a command are at the bottom.
RESULT_TAIL_SHARE = 0.7


def estimate_text_tokens(text: str) -> int:
    return len(text or '') // 4


def estimate_tools_tokens(tools: list[dict] | None) -> int:
    if not tools:
        return 0
    try:
        return len(json.dumps(tools, default=str)) // 4
    except (TypeError, ValueError):
        return 0


def reply_reserve(window: int, max_tokens: int) -> int:
    """Tokens set aside for the reply when planning history.

    A quarter of the window, at least MIN_REPLY_RESERVE, never more than the
    configured output cap: 2048 at 8k, 4096 at 16k, the full cap once a
    quarter of the window exceeds it.
    """
    return max(1, min(max_tokens, max(MIN_REPLY_RESERVE, window // 4)))


@dataclass(frozen=True)
class ContextBudget:
    window: int
    overhead: int        # system prompt + tool schemas
    reply_reserve: int
    history: int         # what conversation history may occupy

    def compaction_trigger(self, threshold: float) -> int:
        """History size at which compaction starts.

        The configured share of the window, or the history budget when that
        is smaller. Large windows keep their existing trigger; small windows
        compact before the reply reserve is eaten.
        """
        return max(1, min(int(self.window * threshold), self.history))

    def compactor_budget(self, threshold: float) -> int:
        """Budget for a compactor that triggers at ``threshold * budget``."""
        if threshold <= 0:
            return self.window
        return max(1, min(self.window, int(self.history / threshold)))

    def request_max_tokens(self, history_tokens: int, max_tokens: int) -> int:
        """Output cap to send with a request carrying ``history_tokens``.

        The configured cap while it fits beside the prompt; otherwise the
        room that is left, never less than the planned reply reserve.
        """
        room = self.window - self.overhead - history_tokens - SAFETY_MARGIN_TOKENS
        return max(1, min(max_tokens, max(self.reply_reserve, room)))

    @property
    def result_cap(self) -> int:
        """Largest single tool result, in tokens, shown to the model."""
        return max(MIN_HISTORY_BUDGET // 2, int(self.history * RESULT_SHARE))


def plan_budget(
    window: int,
    *,
    system_prompt: str = '',
    tools: list[dict] | None = None,
    max_tokens: int,
) -> ContextBudget:
    overhead = estimate_text_tokens(system_prompt) + estimate_tools_tokens(tools)
    reserve = reply_reserve(window, max_tokens)
    history = window - overhead - reserve - SAFETY_MARGIN_TOKENS
    return ContextBudget(
        window=window,
        overhead=overhead,
        reply_reserve=reserve,
        history=max(MIN_HISTORY_BUDGET, history),
    )


# Room kept for the marker so a clipped text never exceeds its limit.
_CLIP_MARKER_ROOM = 160


def clip_text(text: str, max_chars: int, *, tail_share: float = RESULT_TAIL_SHARE) -> str:
    """Keep the head and tail of ``text`` with a marker counting what is gone.

    The result is never longer than ``max_chars``.
    """
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    keep = max(0, max_chars - _CLIP_MARKER_ROOM)
    tail = int(keep * tail_share)
    head = keep - tail
    omitted = len(text) - head - tail
    marker = (
        f'\n[... {omitted} characters omitted to fit the context window; '
        'narrow the command or read a smaller range to see them ...]\n'
    )
    return (text[:head] + marker + (text[-tail:] if tail else ''))[:max_chars]


def clip_oversized_results(messages: list[Message], budget: ContextBudget) -> list[Message]:
    """Return ``messages`` with any oversized tool result clipped for sending.

    Stored messages are never modified: an oversized result is replaced in
    the returned list by a clipped copy.
    """
    max_chars = budget.result_cap * 4
    clipped: list[Message] | None = None
    for index, message in enumerate(messages):
        content: Any = message.content
        if message.role != 'tool_result' or not isinstance(content, str):
            continue
        if len(content) <= max_chars:
            continue
        if clipped is None:
            clipped = list(messages)
        clipped[index] = replace(message, content=clip_text(content, max_chars))
    return messages if clipped is None else clipped


def shrink_window(current: int, reported: int | None) -> int:
    """Planning window to use after the provider refused a prompt as too big.

    A plausible window named by the provider wins when it is smaller than the
    one planned against. Otherwise the plan itself was short (the estimator
    undercounted), so the planning window is cut by a fixed share.
    """
    if reported and MIN_PLAUSIBLE_WINDOW <= reported < current:
        return reported
    return max(MIN_PLAUSIBLE_WINDOW, int(current * OVERFLOW_SHRINK))


# ── Learned windows ─────────────────────────────────────────────────
# Process-wide, in memory. A learned window only ever moves down: every
# entry is evidence that a larger prompt was refused.

_learned: dict[tuple[str, str], int] = {}
_learned_lock = threading.Lock()


def learned_context_window(provider: str, model_id: str) -> int | None:
    with _learned_lock:
        return _learned.get((provider, model_id))


def record_context_window(provider: str, model_id: str, window: int) -> int:
    """Record a refused-prompt window; returns the window now in force."""
    key = (provider, model_id)
    with _learned_lock:
        known = _learned.get(key)
        if known is None or window < known:
            _learned[key] = window
        return _learned[key]


def reset_learned_context_windows() -> None:
    with _learned_lock:
        _learned.clear()


__all__ = [
    'ContextBudget',
    'MIN_HISTORY_BUDGET',
    'MIN_PLAUSIBLE_WINDOW',
    'MIN_REPLY_RESERVE',
    'SAFETY_MARGIN_TOKENS',
    'clip_oversized_results',
    'clip_text',
    'estimate_text_tokens',
    'estimate_tools_tokens',
    'learned_context_window',
    'plan_budget',
    'record_context_window',
    'reply_reserve',
    'reset_learned_context_windows',
    'shrink_window',
]
