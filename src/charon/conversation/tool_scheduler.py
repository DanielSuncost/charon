"""Concurrency policy and batching for model-emitted tool calls."""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from typing import Any, Literal

from charon.providers import ToolCall
from charon.tools import ToolResult


ConcurrencyMode = Literal['shared', 'exclusive']


_SHARED_TOOLS = {
    'Read',
    'Search',
    'Web',
    'Paper',
    'SourceDiscovery',
    'Recall',
    'Timeline',
    'ProcessStatus',
    'ProcessLogs',
    'FleetStatus',
    'FleetHistory',
}

_READ_ONLY_GIT = {
    'status', 'diff', 'show', 'log', 'branch', 'tag', 'rev-parse', 'ls-files',
    'grep', 'blame', 'remote', 'describe', 'shortlog', 'for-each-ref',
}


def _git_subcommand(arguments: dict[str, Any]) -> str:
    action = str(arguments.get('action') or '').strip().lower()
    if action:
        return action
    command = str(arguments.get('command') or '').strip()
    try:
        parts = shlex.split(command)
    except ValueError:
        parts = command.split()
    if parts and parts[0] == 'git':
        parts = parts[1:]
    while parts and parts[0].startswith('-'):
        parts = parts[1:]
    return parts[0].lower() if parts else ''


def tool_concurrency_mode(
    name: str,
    arguments: dict[str, Any] | None = None,
) -> ConcurrencyMode:
    """Return the safest useful concurrency mode for one prepared call.

    Unknown/dynamic tools default to exclusive. Argument-sensitive tools only
    become shared when their operation is unambiguously read-only.
    """
    args = arguments or {}
    if name in _SHARED_TOOLS:
        return 'shared'
    if name == 'Http':
        method = str(args.get('method') or 'GET').strip().upper()
        return 'shared' if method in {'GET', 'HEAD', 'OPTIONS'} else 'exclusive'
    if name == 'Git':
        return 'shared' if _git_subcommand(args) in _READ_ONLY_GIT else 'exclusive'
    if name in {'UserModel', 'ProjectKnowledge'}:
        action = str(args.get('action') or 'get').strip().lower()
        return 'shared' if action in {'get', 'list', 'search', 'show'} else 'exclusive'
    return 'exclusive'


def tool_is_interruptible(name: str) -> bool:
    """Whether a cooperative cancellation event is meaningful to this tool."""
    return name in {'Bash', 'Read', 'Web', 'Paper', 'SourceDiscovery', 'Http'}


def execution_batches(
    calls: list[ToolCall],
    *,
    limit: int,
) -> tuple[list[list[ToolCall]], list[ToolCall]]:
    """Partition calls into shared groups separated by exclusive barriers."""
    accepted = calls[:max(0, limit)]
    skipped = calls[len(accepted):]
    batches: list[list[ToolCall]] = []
    shared: list[ToolCall] = []

    def flush_shared() -> None:
        if shared:
            batches.append(list(shared))
            shared.clear()

    for call in accepted:
        if tool_concurrency_mode(call.name, call.arguments) == 'shared':
            shared.append(call)
        else:
            flush_shared()
            batches.append([call])
    flush_shared()
    return batches, skipped


@dataclass
class ToolExecutionOutcome:
    call: ToolCall
    result: ToolResult | None
    duration_ms: int = 0
    started: bool = False
    skipped_reason: str = ''


def synthetic_tool_result(reason: str) -> ToolResult:
    clean = re.sub(r'\s+', ' ', reason).strip() or 'tool was not executed'
    return ToolResult(
        content=f'(skipped — {clean})',
        is_error=True,
        details={'__synthetic': True, 'reason': clean},
    )


__all__ = [
    'ConcurrencyMode',
    'ToolExecutionOutcome',
    'execution_batches',
    'synthetic_tool_result',
    'tool_concurrency_mode',
    'tool_is_interruptible',
]
