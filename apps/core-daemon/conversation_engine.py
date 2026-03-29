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
import json
import queue
import re
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

from providers import (
    AssistantResponse, Message, ModelInfo, Provider, StreamDelta,
    ToolCall, Usage, get_provider,
)
from tools import ALL_TOOL_DEFS, ToolContext, ToolResult, execute_tool
from execution_memory import record_tool_event

# Browser visibility settings (graceful fallback if unavailable)
try:
    from browser_settings import (
        needs_session_prompt, set_session_override, set_persistent_default,
        mark_prompted, status_string, should_show_browser,
    )
    _HAS_BROWSER_SETTINGS = True
except ImportError:
    _HAS_BROWSER_SETTINGS = False

# Tools that open a browser
_BROWSER_TOOLS = {'Browser', 'X'}

# Lossless context management (graceful fallback if unavailable)


def _sanitize_assistant_text(text: str) -> str:
    if not isinstance(text, str) or not text:
        return text
    text = re.sub(r'(?is)<think>.*?</think>', '', text)
    text = re.sub(r'(?im)^\s*</?think>\s*$', '', text)
    text = text.replace('<think>', '').replace('</think>', '')
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()
try:
    from context_store import ContextStore
    from context_compactor import ContextCompactor, CompactionConfig
    from context_assembler import ContextAssembler
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
- Use Edit for precise changes (oldText must match exactly)
- Use Write only for new files or complete rewrites
- When summarizing your actions, output plain text directly - do NOT use cat or bash to display what you did
- Be concise in your responses
- Show file paths clearly when working with files
- When you need the full file, continue with offset until complete
- Always check that required parameters are provided before making tool calls
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
    except Exception:
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
        max_turns: int = 50,
        max_tool_calls_per_turn: int = 25,
        max_tokens: int = 32768,
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
        self.scope: list[str] | None = None  # set for shade agents
        self.max_turns = max_turns
        self.max_tool_calls_per_turn = max_tool_calls_per_turn
        self.max_tokens = max_tokens
        self.auto_compact = auto_compact
        self.compact_threshold = compact_threshold

        self.messages: list[Message] = []
        self._aborted = False
        self._awaiting_browser_prompt = False

        # ── Lossless context management ──────────────────────────────
        self._lossless_enabled = False
        self._ctx_db = None
        self._ctx_compactor = None
        self._ctx_assembler = None

        if _HAS_LOSSLESS_CONTEXT and self.state_dir and self.agent_id:
            try:
                from store_adapter import get_db
                self._ctx_db = get_db(self.state_dir)
                ContextStore.ensure_schema(self._ctx_db)
                self._ctx_compactor = ContextCompactor(CompactionConfig(
                    context_threshold=compact_threshold,
                    fresh_tail_count=20,
                ))
                self._ctx_assembler = ContextAssembler(fresh_tail_count=20)
                self._lossless_enabled = True
            except Exception:
                pass  # Graceful fallback to legacy compaction

        # Load built-in + dynamic tools
        try:
            from tools.dynamic_loader import get_all_tool_defs
            self.tools = get_all_tool_defs(
                state_dir=self.state_dir,
                project_root=self.project_root,
            )
        except Exception:
            self.tools = ALL_TOOL_DEFS
        self._steering_queue: list[str] = []
        self._follow_up_queue: list[str] = []

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

    def update_system_prompt(self, system_prompt: str):
        """Update the system prompt for the next LLM call.

        Called before each task to refresh memory, goals, and coordination
        context. The engine is cached per agent (preserving conversation
        history), but the system prompt is rebuilt per task so memory
        stays fresh.
        """
        self.system_prompt = system_prompt

    def abort(self):
        """Signal the engine to stop after current operation."""
        self._aborted = True

    def steer(self, message: str):
        """Queue a steering message to interrupt the agent mid-run.

        Delivered after the current tool finishes executing. Remaining
        queued tool calls are skipped. The LLM sees the steering message
        on its next turn.
        """
        if message and message.strip():
            self._steering_queue.append(message.strip())

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
        self._steering_queue.clear()
        self._follow_up_queue.clear()
        if self._lossless_enabled and self._ctx_db and self.agent_id:
            try:
                ContextStore.clear_context_window(self._ctx_db, self.agent_id)
            except Exception:
                pass

    def _persist_message(self, msg: Message) -> None:
        """Persist a message to the lossless context store."""
        if self._lossless_enabled and self._ctx_db and self.agent_id:
            try:
                ContextStore.persist_message(self._ctx_db, self.agent_id, msg)
            except Exception:
                pass

    def _assemble_context(self) -> list[Message]:
        """Assemble messages from the context store for the LLM call.

        Falls back to self.messages if lossless context is unavailable.
        Also updates the system prompt with recall guidance when summaries
        are present.
        """
        if not (self._lossless_enabled and self._ctx_db and self._ctx_assembler
                and self.agent_id):
            return self.messages

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

            return result.messages if result.messages else self.messages
        except Exception:
            return self.messages

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
        )

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
        self._aborted = False

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

            try:
                async for delta in self.provider.stream(
                    messages=context_messages,
                    model=self.model,
                    system_prompt=active_system_prompt,
                    tools=self.tools if self.tools else None,
                    max_tokens=self.max_tokens,
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
                        except Exception:
                            pass
                    elif delta.type == 'error':
                        error_msg = delta.error
                        stop_reason = 'error'
                        yield _evt('error', error=delta.error, turn=turn)

            except Exception as e:
                error_msg = str(e)
                stop_reason = 'error'
                yield _evt('error', error=str(e), turn=turn)

            # Auto-retry on transient errors (like pi-agent)
            # Only if no text was streamed yet (clean retry)
            if error_msg and not assistant_text and not tool_calls:
                _retryable = any(k in (error_msg or '') for k in (
                    '502', '503', '429', 'Bad Gateway', 'overloaded',
                    'rate limit', 'chunked read', 'connection',
                    'Token refreshed', 'service unavailable',
                ))
                retry_count = getattr(self, '_turn_retry_count', 0)
                if _retryable and retry_count < 2:
                    self._turn_retry_count = retry_count + 1
                    wait = (retry_count + 1) * 3
                    yield _evt('retry', attempt=retry_count + 1, max_attempts=2, wait_seconds=wait)
                    await asyncio.sleep(wait)
                    continue  # retry this turn
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

            # Execute tool calls
            tool_count = 0
            steered = False
            for tc in tool_calls:
                if self._aborted or tool_count >= self.max_tool_calls_per_turn:
                    break
                tool_count += 1

                yield _evt('tool_execution_start',
                           tool_call_id=tc.id, tool_name=tc.name,
                           arguments=tc.arguments)

                output_q: queue.Queue[str] = queue.Queue()
                tool_ctx = ToolContext(
                    project_root=self.project_root,
                    agent_id=self.agent_id,
                    state_dir=self.state_dir,
                    scope=self.scope,
                    on_tool_output=lambda tool_name, chunk: output_q.put(chunk) if tool_name == tc.name else None,
                )

                _tool_t0 = time.time()
                result_box: dict[str, Any] = {}
                err_box: dict[str, Exception] = {}

                def _run_tool() -> None:
                    try:
                        result_box['result'] = execute_tool(tc.name, tc.arguments, tool_ctx)
                    except Exception as e:
                        err_box['error'] = e

                tool_thread = threading.Thread(target=_run_tool, daemon=True)
                tool_thread.start()
                streamed_tool_parts: list[str] = []
                while tool_thread.is_alive() or not output_q.empty():
                    while True:
                        try:
                            chunk = output_q.get_nowait()
                        except queue.Empty:
                            break
                        streamed_tool_parts.append(chunk)
                        yield _evt('tool_execution_output',
                                   tool_call_id=tc.id, tool_name=tc.name,
                                   content=''.join(streamed_tool_parts), chunk=chunk)
                    if tool_thread.is_alive():
                        await asyncio.sleep(0.05)
                tool_thread.join(timeout=0.1)
                if 'error' in err_box:
                    raise err_box['error']
                result = result_box['result']
                _tool_dt = int((time.time() - _tool_t0) * 1000)

                # Record tool result as message
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
                if self.state_dir and self.agent_id:
                    try:
                        record_tool_event(
                            self.state_dir,
                            session_id=self.agent_id,
                            agent_id=self.agent_id,
                            provider=self.provider_name,
                            tool_name=tc.name,
                            params=tc.arguments,
                            result_content=result.content,
                            is_error=result.is_error,
                            project_root=str(self.project_root),
                            duration_ms=_tool_dt,
                        )
                    except Exception:
                        pass

                yield _evt('tool_execution_end',
                           tool_call_id=tc.id, tool_name=tc.name,
                           content=result.content, is_error=result.is_error,
                           truncated=result.truncated)

                # Check steering queue after each tool — interrupt if present
                if self._steering_queue:
                    steer_text = self._steering_queue.pop(0)
                    steer_msg = Message(
                        role='user', content=steer_text, timestamp=time.time(),
                    )
                    self.messages.append(steer_msg)
                    self._persist_message(steer_msg)
                    yield _evt('steer_delivered', content=steer_text,
                               remaining=len(self._steering_queue),
                               skipped_tools=len(tool_calls) - tool_count)
                    steered = True
                    break  # Skip remaining tool calls

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
