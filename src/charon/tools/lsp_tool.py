"""Read-only LSP tool; registration/approval are owned by the tool registry."""
from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from charon.ide.lsp import LspError, notify_file_changed, operate

if TYPE_CHECKING:
    from charon.tools import ToolContext, ToolResult


LSP_TOOL_DEF = {
    'name': 'Lsp',
    'description': (
        'Read-only language intelligence from trusted installed language servers. '
        'Actions: status (does not start servers), definition, references, hover, '
        'symbols (document symbols), diagnostics, shutdown (project sessions). '
        'Paths must be existing regular files inside the project. line/column inputs '
        'are one-based Unicode code points; returned LSP positions are zero-based '
        'UTF-16. Missing or stale diagnostics are unavailable, not clean. No installs, '
        'arbitrary commands, code actions, rename, or edits. Server configuration is '
        'trusted runtime metadata, not tool arguments. First use starts a server.'
    ),
    'input_schema': {
        'type': 'object',
        'properties': {
            'action': {'type': 'string', 'enum': ['status', 'definition', 'references', 'hover', 'symbols', 'diagnostics', 'shutdown']},
            'path': {'type': 'string', 'description': 'Project-local file path; required for queries.'},
            'line': {'type': 'integer', 'minimum': 1, 'description': 'One-based line for definition/references/hover.'},
            'column': {'type': 'integer', 'minimum': 1, 'description': 'One-based Unicode code-point column.'},
            'server': {'type': 'string', 'description': 'Optional trusted configured server name.'},
            'timeout': {'type': 'number', 'minimum': 0.1, 'maximum': 30, 'description': 'Total query deadline in seconds (default 10).'},
            'include_declaration': {'type': 'boolean', 'description': 'References: include declaration (default true).'},
        },
        'required': ['action'],
        'additionalProperties': False,
    },
}


def _result(payload: Any, ctx: ToolContext, *, error: bool = False) -> ToolResult:
    # Import only at call time: tools/__init__.py may import this module while
    # constructing its registry.
    from charon.tools import ToolResult

    text = json.dumps(payload, ensure_ascii=False, indent=2)
    max_bytes = max(0, min(int(ctx.max_output_bytes), 50_000))
    max_lines = max(0, min(int(ctx.max_output_lines), 2000))
    lines = text.splitlines(keepends=True)
    limited = ''.join(lines[:max_lines])
    encoded = limited.encode('utf-8', errors='replace')
    truncated = len(lines) > max_lines or len(encoded) > max_bytes
    limited = encoded[:max_bytes].decode('utf-8', errors='ignore')
    return ToolResult(content=limited, is_error=error, truncated=truncated,
                      details={'position_encoding': 'utf-16', 'result_positions': 'zero-based'})


def execute_lsp(params: dict[str, Any], ctx: ToolContext) -> ToolResult:
    try:
        if not isinstance(params, dict):
            raise LspError('LSP parameters must be an object')
        return _result(operate(params, ctx), ctx)
    except (LspError, OSError, ValueError, TypeError) as exc:
        return _result({'error': str(exc)[:2000], 'available': False}, ctx, error=True)


def post_edit_diagnostics(path: str | Path, ctx: ToolContext) -> ToolResult:
    """Invalidate and check an existing session only, with a two-second deadline.

    Optional hook: caller decides whether to surface this best-effort result.
    Missing sessions/publications return an error rather than a clean report.
    """
    try:
        notify_file_changed(path, ctx)
        return _result(operate({'action': 'diagnostics', 'path': str(path), 'timeout': 2},
                               ctx, existing_only=True), ctx)
    except (LspError, OSError, ValueError, TypeError) as exc:
        return _result({'error': str(exc)[:2000], 'available': False}, ctx, error=True)
