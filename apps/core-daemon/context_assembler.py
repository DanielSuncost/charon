"""Context assembler — builds the message array for each LLM call.

Reads from the context_window table and assembles:

    [summary₁, summary₂, ..., summaryₙ, message₁, ..., messageₘ]
     ├── budget-constrained, oldest dropped first ──┤ ├── fresh tail ──┤

Summaries are presented as user messages with XML wrappers so the model
can reason about their age, scope, and how to drill deeper.

When summaries are present, a system prompt fragment is generated with
depth-aware recall guidance (ported from lossless-claw).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from context_store import (
    ContextItem, ContextStore, StoredMessage, StoredSummary,
    _estimate_tokens,
)
from providers import Message, ToolCall


# ── Result types ────────────────────────────────────────────────────

@dataclass
class AssembleResult:
    """Result of context assembly."""
    messages: list[Message]
    estimated_tokens: int
    system_prompt_addition: str | None = None
    stats: dict = field(default_factory=dict)


# ── XML summary formatting ─────────────────────────────────────────

def _format_summary_xml(summary: StoredSummary) -> str:
    """Format a summary as an XML-wrapped user message."""
    attrs = [
        f'id="{summary.summary_id}"',
        f'kind="{summary.kind}"',
        f'depth="{summary.depth}"',
        f'descendant_count="{summary.descendant_count}"',
    ]
    if summary.earliest_at:
        attrs.append(f'earliest_at="{summary.earliest_at[:19]}"')
    if summary.latest_at:
        attrs.append(f'latest_at="{summary.latest_at[:19]}"')

    lines = [f'<summary {" ".join(attrs)}>']

    # Parent refs for condensed summaries
    if summary.kind == 'condensed' and summary.parent_summary_ids:
        lines.append('  <parents>')
        for pid in summary.parent_summary_ids:
            lines.append(f'    <summary_ref id="{pid}" />')
        lines.append('  </parents>')

    lines.append('  <content>')
    lines.append(summary.content)
    lines.append('  </content>')
    lines.append('</summary>')
    return '\n'.join(lines)


# ── System prompt guidance ──────────────────────────────────────────

def _build_recall_guidance(
    summary_count: int,
    max_depth: int,
    condensed_count: int,
) -> str | None:
    """Build system prompt addition for recall guidance.

    Only emitted when summaries are present in context.
    Depth-aware: minimal for shallow, full for deep compaction.
    """
    if summary_count == 0:
        return None

    heavily_compacted = max_depth >= 2 or condensed_count >= 2

    sections = [
        "## Context Recall",
        "",
        "Summaries above are compressed context — maps to details, not the details themselves.",
        "",
        "**Recall priority:** Use the Search tool to find specific details from compacted history.",
        "",
        "**Summaries include \"Expand for details about:\" footers** listing compressed specifics.",
        "When you need those specifics, search for them rather than guessing.",
    ]

    if heavily_compacted:
        sections.extend([
            "",
            "**⚠ Deeply compacted context — search before asserting specifics.**",
            "",
            "**Uncertainty checklist (run before answering):**",
            "- Am I making exact factual claims from a condensed summary?",
            "- Could compaction have omitted a crucial detail?",
            "- Would this answer fail if the user asks for proof?",
            "",
            "If yes to any → search first.",
            "",
            "**Do not guess** exact commands, SHAs, file paths, timestamps, "
            "config values, or causal claims from condensed summaries. "
            "Search first or state uncertainty.",
        ])
    else:
        sections.extend([
            "",
            "**For precision questions** (exact commands, SHAs, paths, "
            "timestamps, config values): search before answering.",
            "Do not guess from summaries — search first or state uncertainty.",
        ])

    return '\n'.join(sections)


# ── Message reconstruction ──────────────────────────────────────────

def _stored_to_message(stored: StoredMessage) -> Message:
    """Reconstruct a providers.Message from a StoredMessage."""
    return Message(
        role=stored.role,
        content=stored.content,
        tool_calls=stored.tool_calls,
        tool_call_id=stored.tool_call_id,
        tool_name=stored.tool_name,
        is_error=stored.is_error,
        thinking=stored.thinking,
        timestamp=0.0,
    )


def _summary_to_message(summary: StoredSummary) -> Message:
    """Wrap a summary as a user message with XML."""
    content = _format_summary_xml(summary)
    return Message(role='user', content=content, timestamp=0.0)


# ── Resolved item (internal) ───────────────────────────────────────

@dataclass
class _ResolvedItem:
    ordinal: int
    message: Message
    tokens: int
    is_message: bool
    summary_depth: int = 0
    summary_kind: str = ''


# ── Assembler ───────────────────────────────────────────────────────

class ContextAssembler:
    """Builds the model context from the context_window table.

    Stateless — all data comes from db + ContextStore.
    """

    def __init__(self, fresh_tail_count: int = 20):
        self.fresh_tail_count = fresh_tail_count

    def assemble(
        self,
        db,
        agent_id: str,
        *,
        token_budget: int,
    ) -> AssembleResult:
        """Build messages for the model, fitting within token_budget.

        1. Fetch all context items
        2. Resolve each to a Message (summary → XML user msg, message → reconstructed)
        3. Split into evictable prefix + protected fresh tail
        4. Drop oldest evictable items until under budget
        5. Generate recall guidance when summaries are present
        """
        items = ContextStore.get_context_window(db, agent_id)
        if not items:
            return AssembleResult(messages=[], estimated_tokens=0,
                                 stats={'raw': 0, 'summaries': 0, 'total': 0})

        # Resolve all items
        resolved: list[_ResolvedItem] = []
        summary_count = 0
        max_depth = 0
        condensed_count = 0

        for item in items:
            r = self._resolve_item(db, item)
            if r:
                resolved.append(r)
                if not r.is_message:
                    summary_count += 1
                    max_depth = max(max_depth, r.summary_depth)
                    if r.summary_kind == 'condensed':
                        condensed_count += 1

        raw_count = sum(1 for r in resolved if r.is_message)

        # Split: evictable prefix + protected fresh tail
        tail_start = max(0, len(resolved) - self.fresh_tail_count)
        fresh_tail = resolved[tail_start:]
        evictable = resolved[:tail_start]

        # Compute fresh tail cost (always included)
        tail_tokens = sum(r.tokens for r in fresh_tail)

        # Fill remaining budget from evictable, dropping oldest
        remaining = max(0, token_budget - tail_tokens)
        selected: list[_ResolvedItem] = []
        evictable_total = sum(r.tokens for r in evictable)

        if evictable_total <= remaining:
            selected = evictable
        else:
            # Keep newest evictable items that fit
            kept: list[_ResolvedItem] = []
            accum = 0
            for item in reversed(evictable):
                if accum + item.tokens <= remaining:
                    kept.append(item)
                    accum += item.tokens
                else:
                    break
            kept.reverse()
            selected = kept

        # Combine and extract messages
        all_items = selected + fresh_tail
        messages = [r.message for r in all_items]
        estimated_tokens = sum(r.tokens for r in all_items)

        # Build recall guidance
        guidance = _build_recall_guidance(
            summary_count, max_depth, condensed_count)

        return AssembleResult(
            messages=messages,
            estimated_tokens=estimated_tokens,
            system_prompt_addition=guidance,
            stats={
                'raw': raw_count,
                'summaries': summary_count,
                'total': len(resolved),
                'max_depth': max_depth,
            },
        )

    def _resolve_item(
        self, db, item: ContextItem,
    ) -> _ResolvedItem | None:
        """Resolve a context item to a Message."""
        if item.item_type == 'message' and item.message_id:
            stored = ContextStore.get_message(db, item.message_id)
            if not stored:
                return None
            return _ResolvedItem(
                ordinal=item.ordinal,
                message=_stored_to_message(stored),
                tokens=stored.token_count or _estimate_tokens(stored.content),
                is_message=True,
            )

        if item.item_type == 'summary' and item.summary_id:
            summary = ContextStore.get_summary(db, item.summary_id)
            if not summary:
                return None
            msg = _summary_to_message(summary)
            tokens = _estimate_tokens(msg.content if isinstance(msg.content, str) else '')
            return _ResolvedItem(
                ordinal=item.ordinal,
                message=msg,
                tokens=tokens,
                is_message=False,
                summary_depth=summary.depth,
                summary_kind=summary.kind,
            )

        return None
