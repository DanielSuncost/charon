"""Recall tool — semantic memory search for Charon agents.

Gives agents access to the semantic memory engine for hybrid
vector + keyword search across all past conversations and memories.
This supplements (does not replace) the existing Search tool.

Search tool = FTS5 keyword search over raw conversation JSONL
Recall tool = hybrid vector + FTS5 search over indexed memories
"""
from __future__ import annotations

from pathlib import Path

from charon.tools import ToolContext, ToolResult


RECALL_TOOL_DEF = {
    'name': 'Recall',
    'description': (
        'Semantic memory search — find relevant memories, facts, and past conversation '
        'context using meaning-based search (not just keywords). '
        'Use this when you need to remember something the user said, a preference they '
        'expressed, a fact from a past session, or context about the project. '
        'Returns ranked results with relevance scores and version history for updated facts.'
    ),
    'input_schema': {
        'type': 'object',
        'properties': {
            'query': {
                'type': 'string',
                'description': 'What to search for — describe the memory or fact you need.',
            },
            'include_profile': {
                'type': 'boolean',
                'description': 'Include the user profile summary (static facts + recent context). Default: false.',
            },
            'limit': {
                'type': 'number',
                'description': 'Max results (default: 10).',
            },
        },
        'required': ['query'],
    },
}


def _get_engine(state_dir: Path):
    """Lazy-load the memory engine. Returns None if deps are missing."""
    try:
        from charon.memory.memory_engine import MemoryEngine
        return MemoryEngine(state_dir)
    except ImportError:
        return None
    except Exception:
        return None


def execute_recall(params: dict, ctx: ToolContext) -> ToolResult:
    """Execute a semantic memory recall."""
    query = str(params.get('query', '')).strip()
    if not query:
        return ToolResult(content='Error: query is required.', is_error=True)

    if not ctx.state_dir:
        return ToolResult(content='Error: state_dir not available.', is_error=True)

    engine = _get_engine(ctx.state_dir)
    if engine is None:
        # Fall back to the existing Search tool if memory engine isn't available
        return ToolResult(
            content='Semantic memory not available (missing sqlite-vec or sentence-transformers). '
                    'Use the Search tool for keyword-based search instead.',
            is_error=True,
        )

    include_profile = bool(params.get('include_profile', False))
    limit = int(params.get('limit') or 10)

    try:
        result = engine.recall(
            query,
            limit=limit,
            include_profile=include_profile,
        )
    except Exception as e:
        return ToolResult(content=f'Recall error: {e}', is_error=True)
    finally:
        engine.close()

    if not result.memories and not result.profile_static and not result.profile_dynamic:
        return ToolResult(content=f'No memories found for: {query}')

    lines = []

    if include_profile:
        if result.profile_static:
            lines.append('## User Profile (Stable Facts)')
            for fact in result.profile_static[:15]:
                lines.append(f'- {fact}')
        if result.profile_dynamic:
            lines.append('\n## Recent Context')
            for fact in result.profile_dynamic[:10]:
                lines.append(f'- {fact}')
        if lines:
            lines.append('')

    if result.memories:
        lines.append(f'## Relevant Memories ({len(result.memories)} results, {result.timing_ms:.0f}ms)')
        for i, sm in enumerate(result.memories):
            mem = sm.memory
            parts = [f'\n**{i+1}.** (score: {sm.score:.3f})']
            if mem.event_date:
                parts.append(f' [{mem.event_date}]')
            if mem.is_static:
                parts.append(' (permanent)')
            if not mem.is_latest:
                parts.append(' (SUPERSEDED)')
            lines.append(''.join(parts))
            lines.append(f'  {mem.content}')

            # Show version chain for knowledge updates
            if sm.version_chain:
                lines.append('  Previous versions:')
                for old in sm.version_chain:
                    old_date = f' [{old.event_date}]' if old.event_date else ''
                    lines.append(f'  - (v{old.version}{old_date}) {old.content}')

    return ToolResult(content='\n'.join(lines))
