"""On-demand discovery and activation of Charon's larger tool registry."""
from __future__ import annotations

import re
from typing import Any


CORE_TOOL_NAMES = frozenset({
    'Read', 'Bash', 'Edit', 'Write', 'Git', 'Http', 'RunProcess',
    'Clarify', 'ToolCatalog',
})

# Tool-loading rule: prefix stability versus deferral.
#
# Tools are rendered at the start of the prompt, so any change to the tool
# list mid-session invalidates every prefix the serving side has reused.
# What that costs differs in kind by provider, not just in degree:
#   - Anthropic (explicit cache_control): ~1.15x the current context in
#     tokens, a 1.25x write where a 0.1x read would have been.
#   - local llama.cpp / LM Studio (KV-cache reuse): a cold re-read of the
#     whole context. Measured 2026-11-11 on a 27B model: 137.1 s to first
#     token for 16k tokens cold against 1.3 s when only a turn was appended,
#     so ~4-5 min near a 32k window. That is past the OpenAI-compatible
#     provider's 300 s read timeout, so the turn fails outright.
# Deferring the follow-up tools saves ~186 tokens per call (measured with
# scripts/measure_request_overhead.py). That only pays where the provider
# reuses nothing and re-reads the whole prompt every call anyway.
#
# So, by provider (``reuses_prompt_prefix``, default True):
#   - reuses a prefix (nearly all: Anthropic, OpenAI, Codex, DeepSeek,
#     local llama.cpp servers): tools deferred only to save tokens are
#     loaded from the start, and the list stays fixed.
#   - shown to reuse nothing (opt out with reuses_prompt_prefix = False):
#     those tools are deferred until evidence they are needed.
#
# This covers every tool deferred only for tokens (FOLLOWUP_TOOL_NAMES). It
# does not cover the domain tools behind intent patterns and ToolCatalog:
# loading all of them up front is the 58-tool registry, ~12,300 schema
# tokens of mostly irrelevant tools that also crowd the model's tool choice.
# Those loads stay intent-driven on every provider, and each one costs a
# single prefix invalidation (a cold re-read on a local server), cheapest
# early.

# Managed-process follow-ups. Useless until a process exists; deferred only
# on providers that reuse no prefix, until RunProcess runs or a message talks about
# processes, servers or logs (which covers processes left by an earlier
# session).
PROCESS_FOLLOWUP_TOOL_NAMES = ('ProcessStatus', 'ProcessLogs', 'StopProcess')

# Every tool deferred only to save tokens, governed by the rule above.
FOLLOWUP_TOOL_NAMES = frozenset(PROCESS_FOLLOWUP_TOOL_NAMES)


TOOL_CATALOG_DEF = {
    'name': 'ToolCatalog',
    'description': (
        'Discover or enable specialized tools that are not in the current '
        'compact tool set. Search by capability before claiming a tool is unavailable.'
    ),
    'input_schema': {
        'type': 'object',
        'properties': {
            'action': {
                'type': 'string',
                'enum': ['search', 'enable', 'list'],
                'description': 'Search available tools, enable matches, or list enabled tools',
            },
            'query': {
                'type': 'string',
                'description': 'Capability words, such as research, browser, memory, or fleet',
            },
            'names': {
                'type': 'array',
                'items': {'type': 'string'},
                'description': 'Exact tool names to enable',
            },
        },
        'required': ['action'],
    },
}


def _matches(definitions: list[dict], query: str) -> list[dict]:
    words = [word for word in re.findall(r'[a-z0-9]+', query.lower()) if len(word) > 1]
    if not words:
        return list(definitions)
    scored: list[tuple[int, str, dict]] = []
    for definition in definitions:
        name = str(definition.get('name') or '')
        haystack = f'{name} {definition.get("description", "")}'.lower()
        score = sum(3 if word in name.lower() else 1 for word in words if word in haystack)
        if score:
            scored.append((score, name, definition))
    scored.sort(key=lambda item: (-item[0], item[1].lower()))
    return [item[2] for item in scored]


def execute_tool_catalog(params: dict, ctx: Any):
    # Imported lazily to avoid a circular import during built-in registration.
    from charon.tools import ToolResult

    catalog = (ctx.metadata or {}).get('tool_catalog') if ctx else None
    if not catalog:
        return ToolResult(content='Tool catalog is unavailable in this runtime.', is_error=True)

    definitions = list(catalog.get('available') or [])
    active_names = set(catalog.get('active_names') or [])
    action = str(params.get('action') or '').lower()
    query = str(params.get('query') or '')

    if action == 'list':
        names = sorted(active_names)
        return ToolResult(content='Enabled tools: ' + ', '.join(names))

    matches = _matches(definitions, query)
    if action == 'search':
        if not matches:
            return ToolResult(content=f'No tools matched {query!r}.')
        rows = [
            f'- {item.get("name")}: {str(item.get("description") or "")[:180]}'
            for item in matches[:12]
        ]
        return ToolResult(content='Matching tools:\n' + '\n'.join(rows))

    if action == 'enable':
        requested = [str(name) for name in params.get('names') or []]
        if not requested:
            requested = [str(item.get('name')) for item in matches[:12]]
        enable = catalog.get('enable')
        if not callable(enable):
            return ToolResult(content='Tool activation callback is unavailable.', is_error=True)
        enabled = list(enable(requested) or [])
        if not enabled:
            return ToolResult(content='No matching tools were enabled.', is_error=True)
        return ToolResult(
            content=(
                'Enabled for the next model turn: ' + ', '.join(enabled)
            ),
            details={'enabled': enabled},
        )

    return ToolResult(content='Error: action must be search, enable, or list', is_error=True)


__all__ = ['CORE_TOOL_NAMES', 'FOLLOWUP_TOOL_NAMES', 'PROCESS_FOLLOWUP_TOOL_NAMES', 'TOOL_CATALOG_DEF', 'execute_tool_catalog']
