"""Charon conversation engine — multi-turn agent loop with tool use.

Architecture mirrors pi-agent:
1. User sends message
2. Message added to conversation history
3. History + system prompt sent to LLM (streaming)
4. If LLM returns tool calls → execute tools → add results → loop back to 3
5. If LLM returns text only → turn complete, wait for next user message
6. Lossless compaction when context grows too large (DAG-based, never deletes)

The engine is async and yields events for the UI to consume.
"""
from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import random
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

from charon.providers import (
    Message, ModelInfo, Provider, ToolCall, Usage, get_provider,
)
from charon.tools import ALL_TOOL_DEFS, ToolContext, ToolResult, execute_tool
from charon.tools.tool_catalog import CORE_TOOL_NAMES
from charon.memory.execution_memory import record_tool_event
from charon.infra import config
from charon.infra.performance import TurnTimer, persist_turn_metrics
from charon.conversation.tool_scheduler import (
    ToolExecutionOutcome,
    execution_batches,
    synthetic_tool_result,
)

try:
    from charon.infra.diagnostics import record as _diag
except Exception:  # diagnostics is best-effort and must never block import
    def _diag(*args, **kwargs):
        return None

# Browser visibility settings (graceful fallback if unavailable)
try:
    from charon.providers.browser_settings import (
        needs_session_prompt, set_session_override, set_persistent_default,
        mark_prompted, status_string, should_show_browser,  # noqa: F401 — availability probe: full settings API must import
    )
    _HAS_BROWSER_SETTINGS = True
except ImportError:
    _HAS_BROWSER_SETTINGS = False

# Tools that open a browser
_BROWSER_TOOLS = {'Browser', 'X'}

_DOMAIN_TOOL_NAMES = {
    'research': {
        'Search', 'Web', 'Paper', 'SourceDiscovery', 'Research', 'Browser', 'X',
    },
    'memory': {'Recall', 'Timeline', 'UserModel', 'ProjectKnowledge'},
    'automation': {'Cron', 'Http'},
    'orchestration': {'SpawnShade', 'SpawnBatch', 'SpawnJudgeLoop'},
    'fleet': {'FleetStatus', 'FleetSend', 'FleetHistory', 'FleetOnboard'},
    'skills': {'Skills'},
    'code': {'ExecuteCode'},
}

_DOMAIN_INTENT_PATTERNS = {
    'research': re.compile(
        r'\b(research|web|internet|website|browser|paper|source|citation|'
        r'latest|news|x\.com|twitter|bookmark|libris)\b', re.I,
    ),
    'memory': re.compile(
        r'\b(recall|remember|memory|timeline|user model|project knowledge|preference)\b', re.I,
    ),
    'automation': re.compile(
        r'\b(cron|schedule|recurring|monitor|every (?:hour|day|week)|automation)\b', re.I,
    ),
    'orchestration': re.compile(
        r'\b(spawn|shade|batch|judge loop|delegate|parallel agents?)\b', re.I,
    ),
    'fleet': re.compile(r'\b(fleet|remote agent|onboard node)\b', re.I),
    'skills': re.compile(r'\b(skill|plugin|capability pack)\b', re.I),
    'code': re.compile(r'\b(execute code|python calculation|data analysis)\b', re.I),
}

# Lossless context management (graceful fallback if unavailable)


def _iso_to_epoch(iso_str: str) -> float:
    """Convert an ISO-8601 timestamp to epoch seconds (best-effort)."""
    if not iso_str:
        return 0.0
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return 0.0


def _sanitize_assistant_text(text: str) -> str:
    if not isinstance(text, str) or not text:
        return text
    text = re.sub(r'(?is)<think>.*?</think>', '', text)
    text = re.sub(r'(?im)^\s*</?think>\s*$', '', text)
    text = text.replace('<think>', '').replace('</think>', '')
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def _normalize_thinking_level(value: str | None) -> str:
    # One ladder for the whole codebase (charon.providers.effort): 'max' and
    # 'ultra' are real Codex levels, so they are no longer aliased to 'high'.
    from charon.providers.effort import normalize_thinking_level
    return normalize_thinking_level(value)


def _load_default_thinking_level(state_dir: Path | None) -> str:
    if not state_dir:
        return 'off'
    try:
        data = json.loads((Path(state_dir) / 'onboarding.json').read_text(encoding='utf-8'))
        return _normalize_thinking_level(data.get('reasoning_effort') or data.get('thinking_level'))
    except Exception:
        return 'off'
try:
    from charon.context.context_store import ContextStore
    from charon.context.context_compactor import ContextCompactor, CompactionConfig
    from charon.context.context_assembler import ContextAssembler
    _HAS_LOSSLESS_CONTEXT = True
except ImportError:
    _HAS_LOSSLESS_CONTEXT = False


# ============================================================================
# Events (yielded to callers)
# ============================================================================

@dataclass
class EngineEvent:
    type: str
    data: dict[str, Any] = field(default_factory=dict)


def _evt(etype: str, **data) -> EngineEvent:
    return EngineEvent(type=etype, data=data)


# ============================================================================
# System prompt builder
# ============================================================================

def build_system_prompt(
    *,
    cwd: str,
    agent_name: str = 'Charon',
    tools: list[dict] | None = None,
    project_context: str = '',
    custom_prompt: str = '',
) -> str:
    """Build the system prompt, modeled on pi-agent's approach."""
    date = time.strftime('%Y-%m-%d')

    if custom_prompt:
        prompt = custom_prompt
        prompt += f'\nCurrent date: {date}'
        prompt += f'\nCurrent working directory: {cwd}'
        return prompt

    tool_defs = tools or ALL_TOOL_DEFS
    tool_list = '\n'.join(f"- {t['name']}: {t['description'][:80]}" for t in tool_defs)

    prompt = f"""You are {agent_name}, an expert coding assistant. You help users by reading files, executing commands, editing code, and writing new files.

Available tools:
{tool_list}

Guidelines:
- Use Bash for file operations like ls, grep, find
- Use Read to examine files before editing. You must use this tool instead of cat or sed.
- Use Edit for precise changes. Pass Read's file version as baseHash and group independent hunks in one edits array when practical
- Use Write only for new files or complete rewrites
- When summarizing your actions, output plain text directly - do NOT use cat or bash to display what you did
- Be concise in your responses
- Show file paths clearly when working with files
- When you need the full file, continue with offset until complete
- Always check that required parameters are provided before making tool calls
- Use ToolCatalog to discover and enable specialized capabilities that are not currently exposed
- For x.com workflows, prefer the X tool over generic Browser/Web when possible.
- If the user asks to check x.com bookmarks for anything new, use X action=triage_new_bookmarks.
- If the user asks what new bookmarks have been investigated, use X action=list_investigations with new_only=true.
- If the user asks to deep dive, investigate, or report on a specific bookmarked item, use X action=deep_dive_bookmark or X action=get_investigation depending on whether they want new research or the stored report."""

    if project_context:
        prompt += f'\n\n# Project Context\n\n{project_context}'

    prompt += f'\nCurrent date: {date}'
    prompt += f'\nCurrent working directory: {cwd}'

    return prompt


# ============================================================================
# Compaction
# ============================================================================

def estimate_tokens(messages: list[Message]) -> int:
    """Rough token estimate: ~4 chars per token."""
    total_chars = 0
    for msg in messages:
        if isinstance(msg.content, str):
            total_chars += len(msg.content)
        elif isinstance(msg.content, list):
            for block in msg.content:
                if isinstance(block, dict):
                    total_chars += len(json.dumps(block))
        total_chars += len(msg.thinking or '')
        for tc in msg.tool_calls:
            total_chars += len(json.dumps(tc.arguments)) + len(tc.name) + 50
    return total_chars // 4


def should_compact(messages: list[Message], context_window: int, threshold: float = 0.7) -> bool:
    """Check if conversation needs compaction."""
    if context_window <= 0:
        return False
    estimated = estimate_tokens(messages)
    return estimated > (context_window * threshold)


def _extract_file_ops(messages: list[Message]) -> tuple[list[str], list[str], list[str]]:
    """Extract file operations from tool calls in messages.

    Returns (files_read, files_written, files_edited).
    Like pi-agent's file tracking in compaction.
    """
    read = set()
    written = set()
    edited = set()
    commands = []

    for msg in messages:
        # Check tool calls in assistant messages
        for tc in msg.tool_calls:
            args = tc.arguments or {}
            if tc.name == 'Read':
                path = args.get('path', '')
                if path:
                    read.add(path)
            elif tc.name == 'Write':
                path = args.get('path', '')
                if path:
                    written.add(path)
            elif tc.name == 'Edit':
                path = args.get('path', '')
                if path:
                    edited.add(path)
            elif tc.name == 'Bash':
                cmd = args.get('command', '')
                if cmd:
                    commands.append(cmd[:80])

    # Files only read (not modified)
    read_only = read - written - edited

    return sorted(read_only), sorted(written), sorted(edited)


def _format_file_ops(files_read: list[str], files_written: list[str], files_edited: list[str]) -> str:
    """Format file operations as a compact section for the compaction summary."""
    parts = []
    if files_read:
        parts.append(f'Files read: {", ".join(files_read[:15])}')
        if len(files_read) > 15:
            parts.append(f'  ... and {len(files_read) - 15} more')
    if files_edited:
        parts.append(f'Files edited: {", ".join(files_edited[:15])}')
    if files_written:
        parts.append(f'Files created: {", ".join(files_written[:15])}')
    return '\n'.join(parts)


COMPACTION_PROMPT = """Summarize the conversation below into a structured context checkpoint. Another LLM will use this to continue the work.

Use this format:

## Goal
What the user wants to accomplish.

## Progress
### Done
- [x] Completed items

### In Progress
- [ ] Current work

## Key Decisions
- Important choices made and why

## Next Steps
1. What should happen next

## Critical Context
- File paths, error messages, or data needed to continue

Keep each section concise. Preserve exact file paths, function names, and error messages."""


async def compact_messages(
    messages: list[Message],
    provider: Provider,
    model: ModelInfo,
    system_prompt: str,
) -> tuple[list[Message], str]:
    """Compact conversation by summarizing older messages with file tracking.

    Extracts file operations from tool calls (like pi-agent) and generates
    a structured summary with Goal/Progress/Decisions/Next Steps.

    Returns (new_messages, summary).
    """
    if len(messages) < 6:
        return messages, ''

    # Keep last 4 messages (2 turns), summarize the rest
    keep_count = min(4, len(messages) // 2)
    to_summarize = messages[:-keep_count]
    to_keep = messages[-keep_count:]

    # Extract file operations from messages being summarized
    files_read, files_written, files_edited = _extract_file_ops(to_summarize)
    file_ops_text = _format_file_ops(files_read, files_written, files_edited)

    # Build conversation text for summarization
    conversation_text = []
    for msg in to_summarize:
        role = msg.role
        content = msg.content if isinstance(msg.content, str) else json.dumps(msg.content)
        if role == 'tool_result':
            content = content[:500]  # truncate tool results
        conversation_text.append(f'[{role}] {content}')

    prompt = COMPACTION_PROMPT + '\n\n' + '\n'.join(conversation_text)

    # Generate structured summary via LLM
    summary_parts = []
    try:
        async for delta in provider.stream(
            messages=[Message(role='user', content=prompt)],
            model=model,
            system_prompt='You are a conversation summarizer. Output only the structured summary.',
            max_tokens=2048,
        ):
            if delta.type == 'text':
                summary_parts.append(delta.text)
            elif delta.type == 'error':
                summary_parts = [f'[Previous conversation with {len(to_summarize)} messages summarized]']
                break
    except Exception as e:
        _diag('conversation_engine', 'compaction LLM summarization failed; using placeholder summary', error=e)
        summary_parts = [f'[Previous conversation with {len(to_summarize)} messages summarized]']

    summary = ''.join(summary_parts)

    # Append file operations to the summary
    if file_ops_text:
        summary += f'\n\n## Files\n{file_ops_text}'

    # Build new message list with summary + recent messages
    compacted = [
        Message(
            role='user',
            content=f'[Conversation summary from earlier in this session]\n{summary}',
            timestamp=time.time(),
        ),
        Message(
            role='assistant',
            content='Understood. I have the context from our earlier conversation. How can I continue helping you?',
            timestamp=time.time(),
        ),
    ] + to_keep

    return compacted, summary


# ============================================================================
# Conversation engine
# ============================================================================

class ConversationEngine:
    """Multi-turn conversation engine with tool use and streaming.

    Usage:
        engine = ConversationEngine(provider, model, project_root='/my/project')
        async for event in engine.submit('Fix the bug in main.py'):
            handle(event)
    """

    def __init__(
        self,
        provider: Provider | str,
        model: ModelInfo,
        *,
        project_root: str | Path = '.',
        agent_id: str = '',
        agent_name: str = 'Charon',
        system_prompt: str = '',
        project_context: str = '',
        state_dir: str | Path | None = None,
        operation_id: str = '',
        operation_domain: str = '',
        work_unit_id: str = '',
        operation_role: str = '',
        runtime_role: str = '',
        parent_agent_id: str = '',
        max_turns: int = 50,
        max_tool_calls_per_turn: int = 25,
        max_tokens: int = 32768,
        thinking_level: str | None = None,
        auto_compact: bool = True,
        compact_threshold: float = 0.7,
    ):
        if isinstance(provider, str):
            self.provider = get_provider(provider)
            self.provider_name = provider
        else:
            self.provider = provider
            self.provider_name = getattr(model, 'provider', '') or provider.__class__.__name__.lower()
        self.model = model
        self.project_root = Path(project_root).resolve()
        self.agent_id = agent_id
        self.agent_name = agent_name
        self.state_dir = Path(state_dir) if state_dir else None
        self.operation_id = operation_id
        self.operation_domain = operation_domain
        self.work_unit_id = work_unit_id
        self.operation_role = operation_role
        self.runtime_role = runtime_role
        self.parent_agent_id = parent_agent_id
        self.scope: list[str] | None = None  # set for shade agents
        self.frozen: list[str] | None = None  # paths that must not be modified
        self.topology_depth: int = 0  # depth in the delegation tree (0 = root agent)
        self.topology_budget: dict[str, Any] | None = None  # set for shades: governs further spawning
        self.max_turns = max_turns
        self.max_tool_calls_per_turn = max_tool_calls_per_turn
        self.max_tokens = max_tokens
        self.thinking_level = _normalize_thinking_level(thinking_level) if thinking_level is not None else _load_default_thinking_level(self.state_dir)
        self.auto_compact = auto_compact
        self.compact_threshold = compact_threshold

        self.messages: list[Message] = []
        self._aborted = False
        self._awaiting_browser_prompt = False
        self._running = False
        self._turn_cancel_event = threading.Event()
        self._background_writes: set[asyncio.Future] = set()

        # ── Lossless context management ──────────────────────────────
        self._lossless_enabled = False
        self._ctx_db = None
        self._ctx_compactor = None
        self._ctx_assembler = None

        if _HAS_LOSSLESS_CONTEXT and self.state_dir and self.agent_id:
            try:
                from charon.infra.store_adapter import get_db
                self._ctx_db = get_db(self.state_dir)
                ContextStore.ensure_schema(self._ctx_db)
                self._ctx_compactor = ContextCompactor(CompactionConfig(
                    context_threshold=compact_threshold,
                    fresh_tail_count=20,
                ))
                self._ctx_assembler = ContextAssembler(fresh_tail_count=20)
                self._lossless_enabled = True
            except Exception as e:
                _diag('conversation_engine', 'lossless context store init failed; falling back to legacy compaction', error=e)  # Graceful fallback to legacy compaction

        # Load built-in + dynamic tools
        try:
            from charon.tools.dynamic_loader import get_all_tool_defs
            available_tools = get_all_tool_defs(
                state_dir=self.state_dir,
                project_root=self.project_root,
            )
        except Exception as e:
            _diag('conversation_engine', 'dynamic tool loading failed; using built-in tool defs only', error=e)
            available_tools = list(ALL_TOOL_DEFS)
        self._available_tools = list(available_tools)
        self._tool_lock = threading.RLock()
        if config.adaptive_tools():
            initial_names = set(CORE_TOOL_NAMES)
            domain_text = ' '.join((
                self.operation_domain,
                self.operation_role,
                self.runtime_role,
                self.agent_name,
            ))
            for domain, pattern in _DOMAIN_INTENT_PATTERNS.items():
                if pattern.search(domain_text):
                    initial_names.update(_DOMAIN_TOOL_NAMES[domain])
            self.tools = [
                tool for tool in self._available_tools
                if tool.get('name') in initial_names
            ]
        else:
            self.tools = list(self._available_tools)
        self._steering_queue: list[str] = []
        self._follow_up_queue: list[str] = []

        self._project_context = project_context
        self._generated_system_prompt = not bool(system_prompt)
        self._custom_system_prompt_base = system_prompt

        # Build system prompt
        if system_prompt:
            self.system_prompt = system_prompt
        else:
            self.system_prompt = build_system_prompt(
                cwd=str(self.project_root),
                agent_name=agent_name,
                tools=self.tools,
                project_context=project_context,
            )
        self._refresh_generated_system_prompt()

        configure_session = getattr(self.provider, 'configure_session', None)
        if callable(configure_session):
            cache_material = (
                f'{self.agent_id}\0{self.model.model_id}\0{self.project_root}'
            ).encode('utf-8')
            prompt_cache_key = (
                'charon-' + hashlib.sha256(cache_material).hexdigest()[:32]
            )
            configure_session(self.agent_id, prompt_cache_key)

    def update_system_prompt(self, system_prompt: str):
        """Update the system prompt for the next LLM call.

        Called before each task to refresh memory, goals, and coordination
        context. The engine is cached per agent (preserving conversation
        history), but the system prompt is rebuilt per task so memory
        stays fresh.
        """
        self.system_prompt = system_prompt
        self._generated_system_prompt = False
        self._custom_system_prompt_base = system_prompt
        self._refresh_generated_system_prompt()

    def _refresh_generated_system_prompt(self) -> None:
        if not self._generated_system_prompt:
            return
        self.system_prompt = build_system_prompt(
            cwd=str(self.project_root),
            agent_name=self.agent_name,
            tools=self.tools,
            project_context=self._project_context,
        )

    def _enable_tools(self, names: list[str]) -> list[str]:
        """Enable exact tool names and return newly activated names."""
        requested = {str(name).strip().lower() for name in names if str(name).strip()}
        if not requested:
            return []
        with self._tool_lock:
            active = {str(tool.get('name') or '') for tool in self.tools}
            enabled: list[str] = []
            for tool in self._available_tools:
                name = str(tool.get('name') or '')
                if name.lower() in requested and name not in active:
                    active.add(name)
                    enabled.append(name)
            if enabled:
                # Preserve registry order so provider payloads and cache prefixes
                # remain deterministic across runs.
                self.tools = [
                    tool for tool in self._available_tools
                    if str(tool.get('name') or '') in active
                ]
                self._refresh_generated_system_prompt()
            return enabled

    def _activate_tools_for_text(self, text: str) -> list[str]:
        if not config.adaptive_tools():
            return []
        # An explicit caller override such as ``engine.tools = []`` is a hard
        # capability boundary (used by benchmark/sandbox runtimes). Adaptive
        # activation is available only while ToolCatalog remains exposed.
        if not any(tool.get('name') == 'ToolCatalog' for tool in self.tools):
            return []
        names: set[str] = set()
        for domain, pattern in _DOMAIN_INTENT_PATTERNS.items():
            if pattern.search(text):
                names.update(_DOMAIN_TOOL_NAMES[domain])

        # Dynamic tools can opt into intent activation through their name.
        lowered = text.lower()
        for tool in self._available_tools:
            name = str(tool.get('name') or '')
            if name and name.lower() in lowered:
                names.add(name)
        return self._enable_tools(sorted(names))

    def abort(self):
        """Signal the engine to stop after current operation."""
        self._aborted = True
        self._turn_cancel_event.set()

    def steer(self, message: str):
        """Queue a steering message to interrupt the agent mid-run.

        Delivered after the current tool finishes executing. Remaining
        queued tool calls are skipped. The LLM sees the steering message
        on its next turn.
        """
        if message and message.strip():
            self._steering_queue.append(message.strip())
            if self._running:
                self._turn_cancel_event.set()

    def follow_up(self, message: str):
        """Queue a follow-up message for after the agent finishes.

        Delivered only when the agent has no more tool calls or steering
        messages. Use this to chain requests without interrupting.
        """
        if message and message.strip():
            self._follow_up_queue.append(message.strip())

    @property
    def pending_messages(self) -> int:
        """Number of queued steering + follow-up messages."""
        return len(self._steering_queue) + len(self._follow_up_queue)

    def reset(self):
        """Clear conversation history and queues."""
        self.messages = []
        self._aborted = False
        self._running = False
        self._turn_cancel_event.clear()
        self._steering_queue.clear()
        self._follow_up_queue.clear()
        if self._lossless_enabled and self._ctx_db and self.agent_id:
            try:
                ContextStore.clear_context_window(self._ctx_db, self.agent_id)
            except Exception as e:
                _diag('conversation_engine', 'clearing lossless context window failed; stale messages may persist in store', error=e)

    def _persist_message(self, msg: Message) -> None:
        """Persist a message to the lossless context store."""
        if self._lossless_enabled and self._ctx_db and self.agent_id:
            try:
                ContextStore.persist_message(self._ctx_db, self.agent_id, msg)
            except Exception as e:
                _diag('conversation_engine', 'persisting message to lossless context store failed; resume history incomplete', error=e)

    def load_from_store(self) -> int:
        """Load full conversation from lossless context store into engine.

        Restores ALL raw messages (never compacted) so resume picks up
        with full context.  Returns the count of messages loaded, or 0
        if the lossless store is unavailable or empty.
        """
        if not (self._lossless_enabled and self._ctx_db and self.agent_id):
            return 0
        try:
            stored = ContextStore.get_messages_for_agent(
                self._ctx_db, self.agent_id, limit=10000,
            )
            if not stored:
                return 0
            self.messages = [
                Message(
                    role=sm.role,
                    content=sm.content,
                    tool_calls=sm.tool_calls,
                    tool_call_id=sm.tool_call_id,
                    tool_name=sm.tool_name,
                    is_error=sm.is_error,
                    thinking=sm.thinking,
                    timestamp=_iso_to_epoch(sm.created_at),
                )
                for sm in stored
            ]
            return len(self.messages)
        except Exception as e:
            _diag('conversation_engine', 'loading messages from lossless context store failed; resuming without stored history', error=e)
            return 0

    def import_into_store(self, messages: list[Message]) -> int:
        """Import messages into the lossless store (JSONL migration).

        Only imports if the store is empty for this agent.  Returns
        the count of messages imported.
        """
        if not (self._lossless_enabled and self._ctx_db and self.agent_id):
            return 0
        try:
            return ContextStore.import_messages(
                self._ctx_db, self.agent_id, messages,
            )
        except Exception as e:
            _diag('conversation_engine', 'importing messages into lossless context store failed; JSONL migration skipped', error=e)
            return 0

    @property
    def has_lossless_store(self) -> bool:
        """Whether the lossless context store is active."""
        return self._lossless_enabled and self._ctx_db is not None

    @staticmethod
    def _repair_orphaned_tool_calls(messages: list) -> list:
        """Return an API-safe tool-call transcript.

        Compaction or budget trimming can leave either half of a tool exchange:

        * a function call without an output (for example after a crash), or
        * a function output without its call (for example when a parallel tool
          batch was split at a compaction boundary).

        Model APIs require call/output pairs.  Drop outputs whose call is not
        present before them, de-duplicate outputs, and inject a synthetic error
        output for calls that have no result.  Raw messages remain available in
        the lossless store; this only sanitizes the active model context.
        """
        seen_calls: set[str] = set()
        answered: set[str] = set()
        filtered: list = []

        # First remove outputs that cannot legally be sent.  Validate in
        # sequence so a result that precedes its call is treated as orphaned.
        for m in messages:
            role = getattr(m, 'role', None)
            if role == 'assistant':
                filtered.append(m)
                for tc in getattr(m, 'tool_calls', None) or []:
                    tc_id = getattr(tc, 'id', None)
                    if tc_id:
                        seen_calls.add(tc_id)
                continue
            if role == 'tool_result':
                result_id = getattr(m, 'tool_call_id', None)
                if not result_id or result_id not in seen_calls or result_id in answered:
                    continue
                answered.add(result_id)
            filtered.append(m)

        # Then patch calls whose process ended before their tool result was
        # persisted.  Inserting directly after the assistant call is valid for
        # both sequential and parallel tool batches.
        repaired: list = []
        for m in filtered:
            repaired.append(m)
            if getattr(m, 'role', None) == 'assistant' and getattr(m, 'tool_calls', None):
                for tc in m.tool_calls:
                    tc_id = getattr(tc, 'id', None)
                    if tc_id and tc_id not in answered:
                        from charon.providers import Message as _Msg
                        repaired.append(_Msg(
                            role='tool_result',
                            content='(interrupted — tool was not executed)',
                            tool_call_id=tc_id,
                            tool_name=getattr(tc, 'name', 'unknown'),
                            is_error=True,
                        ))
                        answered.add(tc_id)
        return repaired

    def _assemble_context(self) -> list[Message]:
        """Assemble messages from the context store for the LLM call.

        Falls back to self.messages if lossless context is unavailable.
        Also updates the system prompt with recall guidance when summaries
        are present.
        """
        if not (self._lossless_enabled and self._ctx_db and self._ctx_assembler
                and self.agent_id):
            repaired = self._repair_orphaned_tool_calls(self.messages)
            if repaired != self.messages:
                self.messages = repaired  # persist the fix
            return repaired

        try:
            result = self._ctx_assembler.assemble(
                self._ctx_db, self.agent_id,
                token_budget=self.model.context_window,
            )
            # Inject recall guidance into system prompt if summaries present
            if result.system_prompt_addition:
                if result.system_prompt_addition not in self.system_prompt:
                    self._recall_guidance = result.system_prompt_addition
                else:
                    self._recall_guidance = None
            else:
                self._recall_guidance = None

            msgs = result.messages if result.messages else self.messages
            return self._repair_orphaned_tool_calls(msgs)
        except Exception as e:
            _diag('conversation_engine', 'lossless context assembly failed; falling back to in-memory messages', error=e)
            return self._repair_orphaned_tool_calls(self.messages)

    def _get_system_prompt(self) -> str:
        """Get system prompt with optional recall guidance appended."""
        base = self.system_prompt
        guidance = getattr(self, '_recall_guidance', None)
        if guidance:
            return f"{base}\n\n{guidance}"
        return base

    @property
    def tool_context(self) -> ToolContext:
        return ToolContext(
            project_root=self.project_root,
            agent_id=self.agent_id,
            state_dir=self.state_dir,
            scope=self.scope,
            frozen=self.frozen,
            operation_id=self.operation_id,
            operation_domain=self.operation_domain,
            work_unit_id=self.work_unit_id,
            operation_role=self.operation_role,
            runtime_role=self.runtime_role,
            parent_agent_id=self.parent_agent_id,
            cancel_event=self._turn_cancel_event,
            metadata=self._tool_catalog_metadata(),
            topology_depth=self.topology_depth,
            topology_budget=self.topology_budget,
        )

    def _tool_catalog_metadata(self) -> dict[str, Any]:
        with self._tool_lock:
            return {
                'tool_catalog': {
                    'available': list(self._available_tools),
                    'active_names': [
                        str(tool.get('name') or '') for tool in self.tools
                    ],
                    'enable': self._enable_tools,
                },
            }

    def _make_tool_context(self, on_output=None) -> ToolContext:
        return ToolContext(
            project_root=self.project_root,
            agent_id=self.agent_id,
            state_dir=self.state_dir,
            scope=self.scope,
            frozen=self.frozen,
            on_tool_output=on_output,
            operation_id=self.operation_id,
            operation_domain=self.operation_domain,
            work_unit_id=self.work_unit_id,
            operation_role=self.operation_role,
            runtime_role=self.runtime_role,
            parent_agent_id=self.parent_agent_id,
            cancel_event=self._turn_cancel_event,
            metadata=self._tool_catalog_metadata(),
            topology_depth=self.topology_depth,
            topology_budget=self.topology_budget,
        )

    async def _execute_tool_batch(
        self,
        calls: list[ToolCall],
    ) -> AsyncIterator[EngineEvent]:
        """Execute one shared batch (or one exclusive call) with live events.

        The final private event carries outcomes in provider order. Callers must
        persist those results before sending another provider request.
        """
        loop = asyncio.get_running_loop()
        event_queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        semaphore = asyncio.Semaphore(config.max_parallel_tools())
        accumulated: dict[str, list[str]] = {call.id: [] for call in calls}

        async def run_one(call: ToolCall) -> ToolExecutionOutcome:
            async with semaphore:
                if self._aborted:
                    return ToolExecutionOutcome(
                        call=call,
                        result=None,
                        skipped_reason='run aborted before tool started',
                    )
                if self._turn_cancel_event.is_set() and self._steering_queue:
                    return ToolExecutionOutcome(
                        call=call,
                        result=None,
                        skipped_reason='steering message interrupted pending tool',
                    )

                await event_queue.put(('start', call))
                started = time.monotonic()

                def on_output(tool_name: str, chunk: str) -> None:
                    if tool_name != call.name or not chunk:
                        return
                    loop.call_soon_threadsafe(
                        event_queue.put_nowait,
                        ('output', (call, chunk)),
                    )

                tool_ctx = self._make_tool_context(on_output=on_output)
                try:
                    result = await asyncio.to_thread(
                        execute_tool,
                        call.name,
                        call.arguments,
                        tool_ctx,
                    )
                except Exception as exc:
                    result = ToolResult(
                        content=f'Tool execution error: {exc}',
                        is_error=True,
                    )
                outcome = ToolExecutionOutcome(
                    call=call,
                    result=result,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    started=True,
                )
                await event_queue.put(('complete', outcome))
                return outcome

        tasks = [asyncio.create_task(run_one(call)) for call in calls]
        while True:
            if all(task.done() for task in tasks) and event_queue.empty():
                break
            try:
                kind, payload = await asyncio.wait_for(
                    event_queue.get(),
                    timeout=0.05,
                )
            except asyncio.TimeoutError:
                continue

            if kind == 'start':
                call = payload
                yield _evt(
                    'tool_execution_start',
                    tool_call_id=call.id,
                    tool_name=call.name,
                    arguments=call.arguments,
                )
            elif kind == 'output':
                call, chunk = payload
                parts = accumulated.setdefault(call.id, [])
                parts.append(chunk)
                yield _evt(
                    'tool_execution_output',
                    tool_call_id=call.id,
                    tool_name=call.name,
                    content=''.join(parts),
                    chunk=chunk,
                )
            elif kind == 'complete':
                outcome = payload
                result = outcome.result or synthetic_tool_result(
                    outcome.skipped_reason
                )
                yield _evt(
                    'tool_execution_end',
                    tool_call_id=outcome.call.id,
                    tool_name=outcome.call.name,
                    content=result.content,
                    is_error=result.is_error,
                    truncated=result.truncated,
                    duration_ms=outcome.duration_ms,
                )

        outcomes = await asyncio.gather(*tasks)
        yield _evt('_tool_batch_complete', outcomes=outcomes)

    async def _stream_provider_interruptibly(
        self,
        *,
        messages: list[Message],
        system_prompt: str,
        can_interrupt,
    ) -> AsyncIterator[Any]:
        """Pump a provider stream through a queue so abort/steer is prompt.

        Awaiting the next HTTP chunk directly can otherwise hold the engine for
        the provider's full read timeout. Cancelling the producer closes the
        async generator and its response stream.
        """
        queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

        async def produce() -> None:
            try:
                async for delta in self.provider.stream(
                    messages=messages,
                    model=self.model,
                    system_prompt=system_prompt,
                    tools=self.tools if self.tools else None,
                    thinking_level=self.thinking_level,
                    max_tokens=self.max_tokens,
                ):
                    await queue.put(('delta', delta))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await queue.put(('exception', exc))
            finally:
                await queue.put(('done', None))

        producer = asyncio.create_task(produce())
        try:
            while True:
                should_cancel = self._aborted or (
                    self._turn_cancel_event.is_set()
                    and bool(self._steering_queue)
                    and can_interrupt()
                )
                if should_cancel:
                    producer.cancel()
                    await asyncio.gather(producer, return_exceptions=True)
                    return
                try:
                    kind, payload = await asyncio.wait_for(
                        queue.get(), timeout=0.05,
                    )
                except asyncio.TimeoutError:
                    continue
                if kind == 'delta':
                    yield payload
                elif kind == 'exception':
                    raise payload
                else:
                    return
        finally:
            if not producer.done():
                producer.cancel()
                await asyncio.gather(producer, return_exceptions=True)

    async def _interruptible_retry_wait(self, seconds: float) -> bool:
        """Wait for retry backoff; return False when abort/steer interrupts."""
        deadline = time.monotonic() + max(0.0, seconds)
        while time.monotonic() < deadline:
            if self._aborted or self._turn_cancel_event.is_set():
                return False
            await asyncio.sleep(min(0.1, deadline - time.monotonic()))
        return True

    def _record_tool_event_off_path(
        self,
        call: ToolCall,
        result: ToolResult,
        duration_ms: int,
    ) -> None:
        """Queue derived execution-memory bookkeeping without delaying tools."""
        if not (self.state_dir and self.agent_id):
            return
        loop = asyncio.get_running_loop()
        future = loop.run_in_executor(
            None,
            functools.partial(
                record_tool_event,
                self.state_dir,
                session_id=self.agent_id,
                agent_id=self.agent_id,
                provider=self.provider_name,
                tool_name=call.name,
                params=call.arguments,
                result_content=result.content,
                is_error=result.is_error,
                project_root=str(self.project_root),
                duration_ms=duration_ms,
            ),
        )
        self._background_writes.add(future)

        def completed(done: asyncio.Future) -> None:
            self._background_writes.discard(done)
            try:
                done.result()
            except Exception as exc:
                _diag(
                    'conversation_engine',
                    'tool event recording failed; execution memory misses this tool call',
                    error=exc,
                    tool=call.name,
                )

        future.add_done_callback(completed)

    # ── Browser visibility helpers ────────────────────────────────────────────

    def _needs_browser_prompt(self) -> bool:
        """True if we should ask the user about browser visibility this session."""
        if not _HAS_BROWSER_SETTINGS:
            return False
        session_id = self.agent_id or ''
        # Only prompt for interactive sessions (not shade agents)
        if not session_id:
            return False
        return needs_session_prompt(session_id, self.state_dir)

    async def _emit_browser_prompt(self):
        """Emit a browser visibility prompt as an assistant message and return.

        The *next* user message will be caught by _handle_browser_prompt_response().
        We mark that we're waiting for an answer by setting _awaiting_browser_prompt.
        """
        mark_prompted(self.agent_id or '')
        self._awaiting_browser_prompt = True

        text = (
            '🌐 **Browser visibility** — I\'m about to use the browser. '
            'Would you like me to show it, or keep it hidden?\n\n'
            '- **`show`** — open a visible browser window\n'
            '- **`hide`** — run headless (no window)\n\n'
            'Add `--save` to remember your choice permanently. '
            'Or use `/browser status` any time to check the current setting.'
        )
        yield _evt('text_delta', text=text)
        yield _evt('turn_end', stop_reason='browser_visibility_prompt')
        yield _evt('done')

    async def _handle_browser_slash(self, message: str):
        """Handle /browser slash commands without going to the LLM."""
        if not _HAS_BROWSER_SETTINGS:
            yield _evt('text_delta', text='Browser settings module not available.')
            yield _evt('done')
            return

        session_id = self.agent_id or ''
        parts = message.strip().split()
        # parts[0] = '/browser', parts[1] = subcommand, rest = flags
        sub = parts[1].lower() if len(parts) > 1 else 'status'
        save = '--save' in [p.lower() for p in parts[2:]]

        if sub in ('show', 'visible', 'headed', 'on'):
            if save and self.state_dir:
                set_persistent_default(self.state_dir, True)
                msg = '✅ Browser set to **visible** (saved as default).'
            else:
                set_session_override(session_id, True)
                msg = '✅ Browser set to **visible** for this session. Use `--save` to persist.'
            yield _evt('text_delta', text=msg)

        elif sub in ('hide', 'hidden', 'headless', 'off'):
            if save and self.state_dir:
                set_persistent_default(self.state_dir, False)
                msg = '✅ Browser set to **hidden** (saved as default).'
            else:
                set_session_override(session_id, False)
                msg = '✅ Browser set to **hidden** for this session. Use `--save` to persist.'
            yield _evt('text_delta', text=msg)

        elif sub == 'status':
            msg = status_string(session_id, self.state_dir)
            yield _evt('text_delta', text=msg)

        else:
            msg = (
                'Usage:\n'
                '  `/browser show [--save]`   — headed (visible) browser\n'
                '  `/browser hide [--save]`   — headless (hidden) browser\n'
                '  `/browser status`          — show current setting\n'
            )
            yield _evt('text_delta', text=msg)

        yield _evt('turn_end', stop_reason='slash_command')
        yield _evt('done')

    async def submit(self, user_message: str) -> AsyncIterator[EngineEvent]:
        """Run one submission with lifecycle cleanup and hot-path metrics."""
        self._aborted = False
        self._running = True
        self._turn_cancel_event.clear()
        self._activate_tools_for_text(user_message)
        timer = TurnTimer()
        counts = {'turns': 0, 'tool_calls': 0}
        marked: set[str] = set()

        def mark_once(name: str) -> None:
            if name not in marked:
                marked.add(name)
                timer.mark(name)

        try:
            async for event in self._submit_impl(user_message):
                if event.type == 'turn_start':
                    counts['turns'] += 1
                    mark_once('provider_start')
                elif event.type in {'text_delta', 'thinking_delta', 'tool_call', 'error'}:
                    mark_once('first_output')
                    if event.type == 'tool_call':
                        counts['tool_calls'] += 1
                elif event.type == 'tool_execution_start':
                    mark_once('first_tool_start')
                elif event.type == 'tool_execution_end':
                    timer.mark('last_tool_end')
                elif event.type == 'done':
                    timer.mark('completed')
                    provider_metrics = dict(
                        getattr(self.provider, 'last_request_metrics', {}) or {}
                    )
                    timer.set(
                        provider=self.provider_name,
                        model=self.model.model_id,
                        agent_id=self.agent_id,
                        **counts,
                        **provider_metrics,
                    )
                    metrics = timer.snapshot()
                    persist_turn_metrics(self.state_dir, metrics)
                    yield _evt('performance', **metrics)
                yield event
        finally:
            self._running = False
            self._turn_cancel_event.clear()

    async def _submit_impl(self, user_message: str) -> AsyncIterator[EngineEvent]:
        """Submit a user message and run the agent loop.

        Yields events as the agent processes:
        - turn_start: new LLM turn beginning
        - text_delta: streaming text from LLM
        - thinking_delta: streaming thinking from LLM
        - tool_call: LLM wants to call a tool
        - tool_result: tool execution completed
        - turn_end: LLM turn completed (may loop for tool use)
        - compaction: context was compacted
        - error: something went wrong
        - done: agent finished processing
        """
        # ── Slash command: /browser ───────────────────────────────────────────
        stripped = user_message.strip()
        if stripped.lower().startswith('/browser'):
            async for evt in self._handle_browser_slash(stripped):
                yield evt
            return

        # ── Handle response to browser visibility prompt ──────────────────────
        if getattr(self, '_awaiting_browser_prompt', False):
            self._awaiting_browser_prompt = False
            response = stripped.lower().strip('.,!?')
            session_id = self.agent_id or ''
            save = '--save' in stripped.lower()
            if any(w in response for w in ('show', 'visible', 'headed', 'yes')):
                visible = True
            elif any(w in response for w in ('hide', 'hidden', 'headless', 'no')):
                visible = False
            else:
                visible = None  # unclear answer — skip and continue

            if visible is not None and _HAS_BROWSER_SETTINGS:
                if save and self.state_dir:
                    set_persistent_default(self.state_dir, visible)
                else:
                    set_session_override(session_id, visible)
                label = 'visible' if visible else 'hidden'
                save_note = ' (saved as default)' if save and self.state_dir else ' (this session)'
                reply = f'✅ Got it — browser will be **{label}**{save_note}. Now re-send your original request to continue.'
                yield _evt('text_delta', text=reply)
                yield _evt('turn_end', stop_reason='browser_visibility_set')
                yield _evt('done')
                return

        # Add user message
        user_msg = Message(
            role='user',
            content=user_message,
            timestamp=time.time(),
        )
        self.messages.append(user_msg)
        self._persist_message(user_msg)
        yield _evt('message_start', role='user', content=user_message)

        # Agent loop: stream LLM → execute tools → repeat
        turn = 0
        while turn < self.max_turns and not self._aborted:
            turn += 1

            # Check compaction (lossless or legacy)
            if self.auto_compact:
                if self._lossless_enabled and self._ctx_db and self._ctx_compactor:
                    try:
                        compact_result = await self._ctx_compactor.evaluate_and_compact(
                            self._ctx_db, self.agent_id,
                            token_budget=self.model.context_window,
                            provider=self.provider,
                            model=self.model,
                        )
                        if compact_result.action_taken:
                            yield _evt('compaction_end',
                                       summary=f'Lossless compaction: {compact_result.tokens_before} → {compact_result.tokens_after} tokens ({compact_result.level})',
                                       message_count=len(self.messages))
                    except Exception as e:
                        yield _evt('compaction_error', error=str(e))
                elif should_compact(
                    self.messages, self.model.context_window, self.compact_threshold
                ):
                    yield _evt('compaction_start')
                    try:
                        self.messages, summary = await compact_messages(
                            self.messages, self.provider, self.model, self.system_prompt,
                        )
                        yield _evt('compaction_end', summary=summary, message_count=len(self.messages))
                    except Exception as e:
                        yield _evt('compaction_error', error=str(e))

            yield _evt('turn_start', turn=turn)

            # Assemble context (lossless or legacy)
            context_messages = self._assemble_context()
            active_system_prompt = self._get_system_prompt()

            # Stream LLM response
            assistant_text = []
            assistant_thinking = []
            tool_calls: list[ToolCall] = []
            error_msg = None
            stop_reason = 'end_turn'
            usage_data = {}
            error_delta = None

            try:
                async for delta in self._stream_provider_interruptibly(
                    messages=context_messages,
                    system_prompt=active_system_prompt,
                    can_interrupt=lambda calls=tool_calls: not calls,
                ):
                    if self._aborted:
                        break

                    if delta.type == 'text':
                        assistant_text.append(delta.text)
                        yield _evt('text_delta', text=delta.text)
                    elif delta.type == 'thinking':
                        assistant_thinking.append(delta.text)
                        yield _evt('thinking_delta', text=delta.text)
                    elif delta.type == 'tool_call' and delta.tool_call:
                        tool_calls.append(delta.tool_call)
                        yield _evt('tool_call',
                                   tool_call_id=delta.tool_call.id,
                                   tool_name=delta.tool_call.name,
                                   arguments=delta.tool_call.arguments)
                    elif delta.type == 'done':
                        try:
                            info = json.loads(delta.text)
                            usage_data = info.get('usage', {})
                            stop_reason = info.get('stop_reason', 'end_turn')
                        except Exception as exc:
                            _diag('conversation_engine', 'stream done-info parse failed; usage and stop_reason lost for this turn', error=exc)
                    elif delta.type == 'error':
                        error_msg = delta.error
                        error_delta = delta
                        stop_reason = 'error'
                        yield _evt('error', error=delta.error, turn=turn)

                    # A user should be able to interrupt a long prose/reasoning
                    # stream, not only a tool chain. Once a complete tool call
                    # has started arriving we let it finish and use the
                    # existing post-tool steering checkpoint instead.
                    if self._steering_queue and not tool_calls:
                        stop_reason = 'steer'
                        break

                if self._steering_queue and not tool_calls and not error_msg:
                    stop_reason = 'steer'

            except Exception as e:
                error_msg = str(e)
                stop_reason = 'error'
                yield _evt('error', error=str(e), turn=turn)

            # Auto-retry on transient errors (like pi-agent)
            # Only if no text was streamed yet (clean retry)
            if error_msg and not assistant_text and not tool_calls:
                # Built-in providers set retryable explicitly. Keep a narrow
                # compatibility fallback for third-party providers that only
                # populate the legacy error string.
                _retryable = bool(getattr(error_delta, 'retryable', False))
                if (
                    error_delta is None
                    or (
                        getattr(error_delta, 'status_code', None) is None
                        and not getattr(error_delta, 'error_code', None)
                    )
                ):
                    lowered = (error_msg or '').lower()
                    _retryable = any(k in lowered for k in (
                        '502', '503', '429', 'bad gateway', 'overloaded',
                        'rate limit', 'chunked read', 'connection',
                        'service unavailable',
                    ))
                retry_count = getattr(self, '_turn_retry_count', 0)
                if _retryable and retry_count < 2:
                    self._turn_retry_count = retry_count + 1
                    retry_after = getattr(error_delta, 'retry_after_seconds', None)
                    if retry_after is not None:
                        wait = min(30.0, max(0.0, float(retry_after)))
                    else:
                        base = min(8.0, 1.0 * (2 ** retry_count))
                        wait = round(base + random.uniform(0.0, min(0.5, base * 0.25)), 2)
                    yield _evt('retry', attempt=retry_count + 1, max_attempts=2, wait_seconds=wait)
                    if await self._interruptible_retry_wait(wait):
                        continue  # retry this turn
                    stop_reason = 'steer' if self._steering_queue else 'abort'
                self._turn_retry_count = 0
            else:
                self._turn_retry_count = 0

            # Record assistant message
            full_text = _sanitize_assistant_text(''.join(assistant_text))
            full_thinking = ''.join(assistant_thinking)

            assistant_msg = Message(
                role='assistant',
                content=full_text,
                tool_calls=tool_calls,
                thinking=full_thinking,
                usage=Usage(
                    input_tokens=usage_data.get('input_tokens', 0),
                    output_tokens=usage_data.get('output_tokens', 0),
                    total_tokens=usage_data.get('total_tokens', 0),
                ),
                timestamp=time.time(),
            )
            if error_msg:
                assistant_msg.content = full_text or f'Error: {error_msg}'
            self.messages.append(assistant_msg)
            self._persist_message(assistant_msg)

            yield _evt('message_end', role='assistant', content=full_text,
                       tool_call_count=len(tool_calls), stop_reason=stop_reason,
                       usage=usage_data)

            # If error or aborted, stop
            if stop_reason == 'error' or self._aborted:
                yield _evt('turn_end', turn=turn, stop_reason=stop_reason)
                break

            # If no tool calls, agent wants to stop — check follow-up queue
            if not tool_calls:
                yield _evt('turn_end', turn=turn, stop_reason=stop_reason)
                # Steering has priority over deferred follow-ups and must also
                # be delivered when it arrived during a text-only response.
                if self._steering_queue:
                    steer_text = self._steering_queue.pop(0)
                    steer_msg = Message(
                        role='user', content=steer_text, timestamp=time.time(),
                    )
                    self.messages.append(steer_msg)
                    self._persist_message(steer_msg)
                    self._turn_cancel_event.clear()
                    yield _evt('steer_delivered', content=steer_text,
                               remaining=len(self._steering_queue), skipped_tools=0)
                    continue
                # Check follow-up queue
                if self._follow_up_queue:
                    follow_up_text = self._follow_up_queue.pop(0)
                    follow_up_msg = Message(
                        role='user', content=follow_up_text, timestamp=time.time(),
                    )
                    self.messages.append(follow_up_msg)
                    self._persist_message(follow_up_msg)
                    yield _evt('message_start', role='user', content=follow_up_text)
                    yield _evt('follow_up_delivered', content=follow_up_text,
                               remaining=len(self._follow_up_queue))
                    continue  # Loop back for another LLM turn
                break

            # Execute tool calls. Shared/read-only calls run concurrently while
            # exclusive calls form ordering barriers.
            batches, limited_calls = execution_batches(
                tool_calls,
                limit=self.max_tool_calls_per_turn,
            )
            completed_ids: set[str] = set()
            started_count = 0
            steered = False

            for batch in batches:
                if self._aborted:
                    break
                outcomes: list[ToolExecutionOutcome] = []
                async for batch_event in self._execute_tool_batch(batch):
                    if batch_event.type == '_tool_batch_complete':
                        outcomes = batch_event.data.get('outcomes', [])
                    else:
                        if batch_event.type == 'tool_execution_start':
                            started_count += 1
                        yield batch_event

                # Persist completed outputs in provider order, independent of
                # which shared tool happened to finish first.
                for outcome in outcomes:
                    result = outcome.result
                    if result is None:
                        continue
                    tc = outcome.call
                    completed_ids.add(tc.id)
                    tool_msg = Message(
                        role='tool_result',
                        content=result.content,
                        tool_call_id=tc.id,
                        tool_name=tc.name,
                        is_error=result.is_error,
                        timestamp=time.time(),
                    )
                    self.messages.append(tool_msg)
                    self._persist_message(tool_msg)
                    self._record_tool_event_off_path(
                        tc,
                        result,
                        outcome.duration_ms,
                    )

                if self._steering_queue:
                    steered = True
                    break

            # Every emitted call must receive exactly one result, including
            # limit, abort, and steering skips. This keeps all provider payloads
            # protocol-valid without relying on a later repair pass.
            limited_ids = {call.id for call in limited_calls}
            skipped_calls = [
                call for call in tool_calls if call.id not in completed_ids
            ]
            for tc in skipped_calls:
                if tc.id in limited_ids:
                    reason = (
                        f'tool-call limit {self.max_tool_calls_per_turn} reached'
                    )
                elif self._aborted:
                    reason = 'run aborted before tool completed'
                elif self._steering_queue:
                    reason = 'steering message interrupted pending tool'
                else:
                    reason = 'tool was not executed'
                result = synthetic_tool_result(reason)
                tool_msg = Message(
                    role='tool_result',
                    content=result.content,
                    tool_call_id=tc.id,
                    tool_name=tc.name,
                    is_error=True,
                    timestamp=time.time(),
                )
                self.messages.append(tool_msg)
                self._persist_message(tool_msg)
                completed_ids.add(tc.id)
                yield _evt(
                    'tool_execution_end',
                    tool_call_id=tc.id,
                    tool_name=tc.name,
                    content=result.content,
                    is_error=True,
                    truncated=False,
                    synthetic=True,
                    duration_ms=0,
                )

            if self._steering_queue:
                steer_text = self._steering_queue.pop(0)
                steer_msg = Message(
                    role='user', content=steer_text, timestamp=time.time(),
                )
                self.messages.append(steer_msg)
                self._persist_message(steer_msg)
                self._turn_cancel_event.clear()
                yield _evt(
                    'steer_delivered',
                    content=steer_text,
                    remaining=len(self._steering_queue),
                    skipped_tools=max(0, len(tool_calls) - started_count),
                )
                steered = True

            yield _evt('turn_end', turn=turn,
                       stop_reason='steer' if steered else 'tool_use')
            # Loop back to stream next LLM response

        yield _evt('done', total_turns=turn, message_count=len(self.messages),
                    pending_follow_ups=len(self._follow_up_queue))

    async def submit_and_collect(self, user_message: str) -> tuple[str, list[EngineEvent]]:
        """Convenience: submit and collect all events. Returns (final_text, events)."""
        events = []
        final_text_parts = []
        async for event in self.submit(user_message):
            events.append(event)
            if event.type == 'text_delta':
                final_text_parts.append(event.data.get('text', ''))
        return ''.join(final_text_parts), events
