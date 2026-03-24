"""Lossless context compaction — hierarchical DAG-based summarization.

Replaces Charon's flat ``compact_messages`` with a depth-aware system
that never destroys raw messages.  Summaries form a DAG:

    depth 0 (leaf)       — narrative summaries of raw message chunks
    depth 1 (condensed)  — merged leaf summaries
    depth 2+             — progressively more abstract

Each depth tier uses a different prompt (ported from lossless-claw's
battle-tested prompts) and receives prior-summary continuity context
so summaries don't repeat already-known information.

Compaction always makes progress via three-level escalation:
    normal → aggressive → deterministic truncation fallback
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from typing import Any

from context_store import ContextStore, ContextItem, StoredSummary, _estimate_tokens
from providers import Message, ModelInfo, Provider, StreamDelta


# ── Configuration ───────────────────────────────────────────────────

@dataclass
class CompactionConfig:
    """Tunable compaction parameters."""
    context_threshold: float = 0.75     # fraction of budget that triggers compaction
    fresh_tail_count: int = 20          # recent messages protected from compaction
    leaf_chunk_tokens: int = 20_000     # max source tokens per leaf chunk
    leaf_min_fanout: int = 6            # min messages for a leaf pass
    condensed_min_fanout: int = 4       # min summaries for a condensed pass
    leaf_target_tokens: int = 1200      # target output tokens for leaf summaries
    condensed_target_tokens: int = 2000 # target output tokens for condensed summaries
    incremental_max_depth: int = -1     # -1 = unlimited condensation depth
    max_rounds: int = 10               # max compaction rounds per sweep
    summarizer_timeout: float = 60.0   # seconds before giving up on the LLM


# ── Result types ────────────────────────────────────────────────────

@dataclass
class CompactionResult:
    action_taken: bool
    tokens_before: int
    tokens_after: int
    created_summary_id: str | None = None
    condensed: bool = False
    level: str = 'none'   # 'normal', 'aggressive', 'fallback', 'none'


# ── Depth-aware prompts ─────────────────────────────────────────────
# Ported from lossless-claw's summarize.ts — each depth tier has a
# different prompt calibrated for that level of abstraction.

def _build_leaf_prompt(
    text: str,
    *,
    target_tokens: int,
    aggressive: bool = False,
    previous_summary: str | None = None,
) -> str:
    """Build a leaf (depth 0) summarization prompt."""
    prev = (previous_summary or '').strip() or '(none)'
    if aggressive:
        policy = (
            "Aggressive summary policy:\n"
            "- Keep only durable facts and current task state.\n"
            "- Remove examples, repetition, and low-value narrative details.\n"
            "- Preserve explicit TODOs, blockers, decisions, and constraints."
        )
    else:
        policy = (
            "Normal summary policy:\n"
            "- Preserve key decisions, rationale, constraints, and active tasks.\n"
            "- Keep essential technical details needed to continue work safely.\n"
            "- Remove obvious repetition and conversational filler."
        )

    return (
        "You summarize a SEGMENT of a coding conversation for future model turns.\n"
        "Treat this as incremental memory compaction input, not a full-conversation summary.\n\n"
        f"{policy}\n\n"
        "Output requirements:\n"
        "- Plain text only.\n"
        "- No preamble, headings, or markdown formatting.\n"
        "- Keep it concise while preserving required details.\n"
        "- Track file operations (created, modified, deleted, renamed) with file paths and current status.\n"
        '- If no file operations appear, include exactly: "Files: none".\n'
        '- End with exactly: "Expand for details about: <comma-separated list of what was dropped or compressed>".\n'
        f"- Target length: about {target_tokens} tokens or less.\n\n"
        f"<previous_context>\n{prev}\n</previous_context>\n\n"
        f"<conversation_segment>\n{text}\n</conversation_segment>"
    )


def _build_d1_prompt(
    text: str,
    *,
    target_tokens: int,
    previous_summary: str | None = None,
) -> str:
    """Build a depth-1 condensation prompt."""
    prev = (previous_summary or '').strip()
    if prev:
        prev_block = (
            "It already has this preceding summary as context. Do not repeat information\n"
            "that appears there unchanged. Focus on what is new, changed, or resolved:\n\n"
            f"<previous_context>\n{prev}\n</previous_context>"
        )
    else:
        prev_block = "Focus on what matters for continuation:"

    return (
        "You are compacting leaf-level conversation summaries into a single condensed memory node.\n"
        "You are preparing context for a fresh model instance that will continue this conversation.\n\n"
        f"{prev_block}\n\n"
        "Preserve:\n"
        "- Decisions made and their rationale when rationale matters going forward.\n"
        "- Earlier decisions that were superseded, and what replaced them.\n"
        "- Completed tasks/topics with outcomes.\n"
        "- In-progress items with current state and what remains.\n"
        "- Blockers, open questions, and unresolved tensions.\n"
        "- Specific references (names, paths, URLs, identifiers) needed for continuation.\n\n"
        "Drop low-value detail:\n"
        "- Context that has not changed from previous_context.\n"
        "- Intermediate dead ends where the conclusion is already known.\n"
        "- Transient states that are already resolved.\n"
        "- Tool-internal mechanics and process scaffolding.\n\n"
        "Use plain text. No mandatory structure.\n"
        "Include a timeline with timestamps for significant events.\n"
        "Present information chronologically and mark superseded decisions.\n"
        '"Expand for details about: <comma-separated list of what was dropped or compressed>".\n'
        f"Target length: about {target_tokens} tokens.\n\n"
        f"<conversation_to_condense>\n{text}\n</conversation_to_condense>"
    )


def _build_d2_prompt(text: str, *, target_tokens: int) -> str:
    """Build a depth-2 condensation prompt."""
    return (
        "You are condensing multiple session-level summaries into a higher-level memory node.\n"
        "A future model should understand trajectory, not per-session minutiae.\n\n"
        "Preserve:\n"
        "- Decisions still in effect and their rationale.\n"
        "- Decisions that evolved: what changed and why.\n"
        "- Completed work with outcomes.\n"
        "- Active constraints, limitations, and known issues.\n"
        "- Current state of in-progress work.\n\n"
        "Drop:\n"
        "- Session-local operational detail and process mechanics.\n"
        "- Identifiers that are no longer relevant.\n"
        "- Intermediate states superseded by later outcomes.\n\n"
        "Use plain text. Brief headers are fine if useful.\n"
        "Include a timeline with dates and approximate time of day for key milestones.\n"
        '"Expand for details about: <comma-separated list of what was dropped or compressed>".\n'
        f"Target length: about {target_tokens} tokens.\n\n"
        f"<conversation_to_condense>\n{text}\n</conversation_to_condense>"
    )


def _build_d3plus_prompt(text: str, *, target_tokens: int) -> str:
    """Build a depth 3+ condensation prompt."""
    return (
        "You are creating a high-level memory node from multiple phase-level summaries.\n"
        "This may persist for the rest of the conversation. Keep only durable context.\n\n"
        "Preserve:\n"
        "- Key decisions and rationale.\n"
        "- What was accomplished and current state.\n"
        "- Active constraints and hard limitations.\n"
        "- Important relationships between people, systems, or concepts.\n"
        "- Durable lessons learned.\n\n"
        "Drop:\n"
        "- Operational and process detail.\n"
        "- Method details unless the method itself was the decision.\n"
        "- Specific references unless essential for continuation.\n\n"
        "Use plain text. Be concise.\n"
        "Include a brief timeline with dates for major milestones.\n"
        '"Expand for details about: <comma-separated list of what was dropped or compressed>".\n'
        f"Target length: about {target_tokens} tokens.\n\n"
        f"<conversation_to_condense>\n{text}\n</conversation_to_condense>"
    )


def _build_condensed_prompt(
    text: str,
    *,
    depth: int,
    target_tokens: int,
    previous_summary: str | None = None,
) -> str:
    """Route to the correct depth-specific prompt."""
    if depth <= 1:
        return _build_d1_prompt(text, target_tokens=target_tokens,
                                previous_summary=previous_summary)
    if depth == 2:
        return _build_d2_prompt(text, target_tokens=target_tokens)
    return _build_d3plus_prompt(text, target_tokens=target_tokens)


# ── Target token resolution ─────────────────────────────────────────

def _resolve_target_tokens(
    input_tokens: int,
    *,
    aggressive: bool,
    is_condensed: bool,
    config: CompactionConfig,
) -> int:
    """Compute target summary length based on input size and mode."""
    if is_condensed:
        return max(512, config.condensed_target_tokens)
    if aggressive:
        return max(96, min(640, int(input_tokens * 0.2)))
    return max(192, min(1200, int(input_tokens * 0.35)))


# ── LLM summarization ──────────────────────────────────────────────

_SUMMARIZER_SYSTEM = (
    "You are a context-compaction summarization engine. "
    "Follow user instructions exactly and return plain text summary content only."
)

FALLBACK_MAX_CHARS = 512 * 4


def _deterministic_fallback(text: str, target_tokens: int) -> str:
    """Deterministic truncation when the LLM fails."""
    trimmed = text.strip()
    if not trimmed:
        return '[Empty segment]'
    max_chars = max(256, target_tokens * 4)
    if len(trimmed) <= max_chars:
        return trimmed
    return f"{trimmed[:max_chars]}\n[Truncated for context management]"


async def _call_summarizer(
    provider: Provider,
    model: ModelInfo,
    prompt: str,
    *,
    max_tokens: int,
    temperature: float,
    timeout: float,
) -> str:
    """Call the LLM for summarization with timeout."""
    parts: list[str] = []

    async def _stream():
        async for delta in provider.stream(
            messages=[Message(role='user', content=prompt)],
            model=model,
            system_prompt=_SUMMARIZER_SYSTEM,
            max_tokens=max_tokens,
        ):
            if delta.type == 'text':
                parts.append(delta.text)
            elif delta.type == 'error':
                raise RuntimeError(delta.error or 'Summarizer error')

    try:
        await asyncio.wait_for(_stream(), timeout=timeout)
    except asyncio.TimeoutError:
        return ''
    except Exception:
        return ''

    return ''.join(parts).strip()


async def _summarize_with_escalation(
    provider: Provider,
    model: ModelInfo,
    source_text: str,
    *,
    build_prompt,
    config: CompactionConfig,
    is_condensed: bool = False,
) -> tuple[str, str]:
    """Three-level escalation: normal → aggressive → fallback.

    Returns (summary_text, level).
    """
    input_tokens = max(1, _estimate_tokens(source_text))
    target = _resolve_target_tokens(
        input_tokens, aggressive=False, is_condensed=is_condensed, config=config)

    # Level 1: normal
    prompt = build_prompt(aggressive=False) if not is_condensed else build_prompt()
    summary = await _call_summarizer(
        provider, model, prompt,
        max_tokens=target, temperature=0.2, timeout=config.summarizer_timeout)

    if not summary:
        return _deterministic_fallback(source_text, target), 'fallback'

    if _estimate_tokens(summary) < input_tokens:
        return summary, 'normal'

    # Level 2: aggressive (leaf only — condensed doesn't have an aggressive mode)
    if not is_condensed:
        target_agg = _resolve_target_tokens(
            input_tokens, aggressive=True, is_condensed=False, config=config)
        prompt_agg = build_prompt(aggressive=True)
        summary_agg = await _call_summarizer(
            provider, model, prompt_agg,
            max_tokens=target_agg, temperature=0.1,
            timeout=config.summarizer_timeout)
        if summary_agg and _estimate_tokens(summary_agg) < input_tokens:
            return summary_agg, 'aggressive'

    # Level 3: deterministic fallback
    return _deterministic_fallback(source_text, target), 'fallback'


# ── Format helpers ──────────────────────────────────────────────────

def _format_message_for_summary(msg, *, include_timestamp: bool = True) -> str:
    """Format a StoredMessage for the summarizer input."""
    parts = []
    if include_timestamp and msg.created_at:
        ts = msg.created_at[:19]  # trim to seconds
        parts.append(f"[{ts}]")

    if msg.role == 'tool_result':
        name = msg.tool_name or 'tool'
        content = msg.content[:1000] if msg.content else ''
        error_tag = ' ERROR' if msg.is_error else ''
        parts.append(f"[tool_result: {name}{error_tag}] {content}")
    elif msg.role == 'assistant' and msg.tool_calls:
        calls = []
        for tc in msg.tool_calls:
            args_summary = ', '.join(
                f"{k}={repr(v)[:60]}" for k, v in (tc.arguments or {}).items()
            )
            calls.append(f"{tc.name}({args_summary})")
        text = msg.content[:500] if msg.content else ''
        parts.append(f"[assistant] {text}")
        for c in calls:
            parts.append(f"  → [call: {c}]")
    else:
        parts.append(f"[{msg.role}] {msg.content}")

    return '\n'.join(parts)


def _format_summary_for_condense(summary: StoredSummary) -> str:
    """Format a summary for the condensation input."""
    earliest = summary.earliest_at or summary.created_at
    latest = summary.latest_at or summary.created_at
    header = f"[{earliest[:19]} – {latest[:19]}]"
    return f"{header}\n{summary.content}"


# ── Core compaction engine ──────────────────────────────────────────

class ContextCompactor:
    """Hierarchical compaction engine operating on ContextStore data.

    All methods are stateless — they read/write via db + ContextStore.
    """

    def __init__(self, config: CompactionConfig | None = None):
        self.config = config or CompactionConfig()

    # ── Public API ──────────────────────────────────────────────────

    async def evaluate_and_compact(
        self,
        db,
        agent_id: str,
        *,
        token_budget: int,
        provider: Provider,
        model: ModelInfo,
        force: bool = False,
    ) -> CompactionResult:
        """Evaluate whether compaction is needed and run it if so.

        Called after each turn from the conversation engine.
        """
        tokens_before = ContextStore.get_context_token_count(db, agent_id)
        threshold = int(self.config.context_threshold * token_budget)

        if not force and tokens_before <= threshold:
            return CompactionResult(
                action_taken=False, tokens_before=tokens_before,
                tokens_after=tokens_before)

        return await self._full_sweep(
            db, agent_id,
            token_budget=token_budget, provider=provider, model=model,
            force=force)

    async def compact_leaf(
        self,
        db,
        agent_id: str,
        *,
        provider: Provider,
        model: ModelInfo,
    ) -> CompactionResult:
        """Run a single incremental leaf pass."""
        tokens_before = ContextStore.get_context_token_count(db, agent_id)
        items = ContextStore.get_context_window(db, agent_id)
        chunk = self._select_oldest_leaf_chunk(items)

        if not chunk:
            return CompactionResult(
                action_taken=False, tokens_before=tokens_before,
                tokens_after=tokens_before)

        prior = await self._resolve_prior_summary(db, agent_id, chunk)
        result = await self._leaf_pass(
            db, agent_id, chunk, provider=provider, model=model,
            previous_summary=prior)

        if not result:
            return CompactionResult(
                action_taken=False, tokens_before=tokens_before,
                tokens_after=tokens_before)

        tokens_after = ContextStore.get_context_token_count(db, agent_id)
        return CompactionResult(
            action_taken=True, tokens_before=tokens_before,
            tokens_after=tokens_after, created_summary_id=result[0],
            level=result[1])

    # ── Private: full sweep ─────────────────────────────────────────

    async def _full_sweep(
        self,
        db,
        agent_id: str,
        *,
        token_budget: int,
        provider: Provider,
        model: ModelInfo,
        force: bool,
    ) -> CompactionResult:
        """Phase 1: leaf passes.  Phase 2: condensation passes."""
        tokens_before = ContextStore.get_context_token_count(db, agent_id)
        threshold = int(self.config.context_threshold * token_budget)
        action_taken = False
        condensed = False
        created_id = None
        level = 'none'
        prev_tokens = tokens_before

        # Phase 1: repeated leaf passes
        for _ in range(self.config.max_rounds):
            items = ContextStore.get_context_window(db, agent_id)
            chunk = self._select_oldest_leaf_chunk(items)
            if not chunk:
                break

            prior = await self._resolve_prior_summary(db, agent_id, chunk)
            result = await self._leaf_pass(
                db, agent_id, chunk, provider=provider, model=model,
                previous_summary=prior)
            if not result:
                break

            action_taken = True
            created_id, level = result
            cur_tokens = ContextStore.get_context_token_count(db, agent_id)

            if not force and cur_tokens <= threshold:
                prev_tokens = cur_tokens
                break
            if cur_tokens >= prev_tokens:
                break
            prev_tokens = cur_tokens

        # Phase 2: condensation passes
        max_depth = self.config.incremental_max_depth
        if max_depth < 0:
            max_depth = 100  # effectively unlimited

        for _ in range(self.config.max_rounds):
            if not force and prev_tokens <= threshold:
                break

            items = ContextStore.get_context_window(db, agent_id)
            cond_result = await self._try_condensation(
                db, agent_id, items, provider=provider, model=model,
                max_depth=max_depth)
            if not cond_result:
                break

            action_taken = True
            condensed = True
            created_id, level = cond_result
            cur_tokens = ContextStore.get_context_token_count(db, agent_id)

            if cur_tokens >= prev_tokens:
                break
            prev_tokens = cur_tokens

        tokens_after = ContextStore.get_context_token_count(db, agent_id)
        return CompactionResult(
            action_taken=action_taken, tokens_before=tokens_before,
            tokens_after=tokens_after, created_summary_id=created_id,
            condensed=condensed, level=level)

    # ── Private: leaf pass ──────────────────────────────────────────

    def _select_oldest_leaf_chunk(
        self, items: list[ContextItem],
    ) -> list[ContextItem] | None:
        """Select the oldest contiguous message chunk outside the fresh tail."""
        # Identify message items
        msg_items = [i for i in items if i.item_type == 'message' and i.message_id]
        if len(msg_items) <= self.config.fresh_tail_count:
            return None  # everything is in the fresh tail

        # Protect the fresh tail
        compactable = msg_items[:len(msg_items) - self.config.fresh_tail_count]
        if len(compactable) < self.config.leaf_min_fanout:
            return None

        # Find the first contiguous run of messages in context order
        chunk: list[ContextItem] = []
        chunk_tokens = 0
        started = False

        for item in items:
            if item.item_type != 'message' or item.message_id is None:
                if started:
                    break  # end of contiguous run
                continue

            if item not in compactable:
                if started:
                    break
                continue

            started = True
            chunk.append(item)
            chunk_tokens += item.token_count

            if chunk_tokens >= self.config.leaf_chunk_tokens:
                break

        if len(chunk) < self.config.leaf_min_fanout:
            return None

        return chunk

    async def _resolve_prior_summary(
        self, db, agent_id: str, chunk: list[ContextItem],
    ) -> str | None:
        """Fetch the most recent 1-2 summaries before the chunk for continuity."""
        start_ordinal = min(i.ordinal for i in chunk)
        items = ContextStore.get_context_window(db, agent_id)
        prior_summaries = [
            i for i in items
            if i.ordinal < start_ordinal
            and i.item_type == 'summary'
            and i.summary_id
        ]

        if not prior_summaries:
            return None

        contents: list[str] = []
        for item in prior_summaries[-2:]:
            summary = ContextStore.get_summary(db, item.summary_id)
            if summary and summary.content.strip():
                contents.append(summary.content.strip())

        return '\n\n'.join(contents) if contents else None

    async def _leaf_pass(
        self,
        db,
        agent_id: str,
        chunk: list[ContextItem],
        *,
        provider: Provider,
        model: ModelInfo,
        previous_summary: str | None = None,
    ) -> tuple[str, str] | None:
        """Summarize a chunk of messages into a leaf summary.

        Returns (summary_id, level) or None on failure.
        """
        # Fetch messages
        msg_ids = [i.message_id for i in chunk if i.message_id]
        messages = ContextStore.get_messages_by_ids(db, msg_ids)
        if not messages:
            return None

        # Build source text
        source = '\n\n'.join(
            _format_message_for_summary(m) for m in messages)

        # Build prompt factory
        def build_prompt(aggressive=False):
            target = _resolve_target_tokens(
                _estimate_tokens(source), aggressive=aggressive,
                is_condensed=False, config=self.config)
            return _build_leaf_prompt(
                source, target_tokens=target, aggressive=aggressive,
                previous_summary=previous_summary)

        summary_text, level = await _summarize_with_escalation(
            provider, model, source,
            build_prompt=build_prompt, config=self.config, is_condensed=False)

        if not summary_text:
            return None

        # Persist
        summary_id = ContextStore.insert_summary(
            db,
            agent_id=agent_id,
            kind='leaf',
            depth=0,
            content=summary_text,
            source_message_ids=msg_ids,
            earliest_at=messages[0].created_at if messages else None,
            latest_at=messages[-1].created_at if messages else None,
            model=model.model_id,
        )

        # Replace range in context window
        ordinals = [i.ordinal for i in chunk]
        ContextStore.replace_range_with_summary(
            db, agent_id,
            start_ordinal=min(ordinals),
            end_ordinal=max(ordinals),
            summary_id=summary_id,
            summary_token_count=_estimate_tokens(summary_text),
        )

        return summary_id, level

    # ── Private: condensation ───────────────────────────────────────

    async def _try_condensation(
        self,
        db,
        agent_id: str,
        items: list[ContextItem],
        *,
        provider: Provider,
        model: ModelInfo,
        max_depth: int,
    ) -> tuple[str, str] | None:
        """Find the shallowest eligible depth and run one condensed pass."""
        # Collect summaries outside the fresh tail
        msg_items = [i for i in items if i.item_type == 'message']
        if msg_items:
            tail_start = msg_items[-min(len(msg_items), self.config.fresh_tail_count)].ordinal
        else:
            tail_start = float('inf')

        summary_items = [
            i for i in items
            if i.item_type == 'summary' and i.summary_id and i.ordinal < tail_start
        ]
        if not summary_items:
            return None

        # Group by depth
        depth_groups: dict[int, list[ContextItem]] = {}
        for item in summary_items:
            s = ContextStore.get_summary(db, item.summary_id)
            if not s:
                continue
            if s.depth > max_depth:
                continue
            depth_groups.setdefault(s.depth, []).append(item)

        # Try shallowest first
        for target_depth in sorted(depth_groups.keys()):
            group = depth_groups[target_depth]
            if len(group) < self.config.condensed_min_fanout:
                continue

            # Select a contiguous chunk at this depth
            chunk = self._select_contiguous_summary_chunk(
                db, group, target_depth)
            if not chunk or len(chunk) < self.config.condensed_min_fanout:
                continue

            return await self._condensed_pass(
                db, agent_id, chunk, target_depth,
                provider=provider, model=model)

        return None

    def _select_contiguous_summary_chunk(
        self,
        db,
        candidates: list[ContextItem],
        target_depth: int,
    ) -> list[ContextItem] | None:
        """Select the oldest contiguous run of same-depth summaries."""
        sorted_items = sorted(candidates, key=lambda i: i.ordinal)
        chunk: list[ContextItem] = []
        chunk_tokens = 0

        for item in sorted_items:
            s = ContextStore.get_summary(db, item.summary_id)
            if not s or s.depth != target_depth:
                if chunk:
                    break
                continue

            chunk.append(item)
            chunk_tokens += item.token_count

            if chunk_tokens >= self.config.leaf_chunk_tokens:
                break

        return chunk if chunk else None

    async def _condensed_pass(
        self,
        db,
        agent_id: str,
        chunk: list[ContextItem],
        target_depth: int,
        *,
        provider: Provider,
        model: ModelInfo,
    ) -> tuple[str, str] | None:
        """Merge summaries at target_depth into a depth+1 condensed node."""
        summary_ids = [i.summary_id for i in chunk if i.summary_id]
        summaries = [ContextStore.get_summary(db, sid)
                     for sid in summary_ids]
        summaries = [s for s in summaries if s is not None]
        if not summaries:
            return None

        # Build source text
        source = '\n\n'.join(_format_summary_for_condense(s) for s in summaries)
        output_depth = target_depth + 1

        # Fetch prior summary for continuity (d0/d1 only)
        prior = None
        if target_depth <= 1:
            items = ContextStore.get_context_window(db, agent_id)
            start_ordinal = min(i.ordinal for i in chunk)
            prior_items = [
                i for i in items
                if i.ordinal < start_ordinal and i.item_type == 'summary' and i.summary_id
            ]
            for pi in reversed(prior_items[-2:]):
                ps = ContextStore.get_summary(db, pi.summary_id)
                if ps and ps.depth == target_depth and ps.content.strip():
                    prior = ps.content.strip()
                    break

        target = _resolve_target_tokens(
            _estimate_tokens(source), aggressive=False,
            is_condensed=True, config=self.config)

        def build_prompt(**_kwargs):
            return _build_condensed_prompt(
                source, depth=output_depth, target_tokens=target,
                previous_summary=prior)

        summary_text, level = await _summarize_with_escalation(
            provider, model, source,
            build_prompt=build_prompt, config=self.config, is_condensed=True)

        if not summary_text:
            return None

        # Calculate descendant count
        descendant_count = sum(
            (s.descendant_count + 1) for s in summaries)

        summary_id = ContextStore.insert_summary(
            db,
            agent_id=agent_id,
            kind='condensed',
            depth=output_depth,
            content=summary_text,
            parent_summary_ids=summary_ids,
            earliest_at=min(
                (s.earliest_at or s.created_at for s in summaries),
                default=None),
            latest_at=max(
                (s.latest_at or s.created_at for s in summaries),
                default=None),
            descendant_count=descendant_count,
            model=model.model_id,
        )

        ordinals = [i.ordinal for i in chunk]
        ContextStore.replace_range_with_summary(
            db, agent_id,
            start_ordinal=min(ordinals),
            end_ordinal=max(ordinals),
            summary_id=summary_id,
            summary_token_count=_estimate_tokens(summary_text),
        )

        return summary_id, level
