"""ExecuteCode tool — run Python snippets safely with bounded resources."""
from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from tools import ToolContext, ToolResult, truncate_output


EXECUTE_CODE_TOOL_DEF = {
    'name': 'ExecuteCode',
    'description': (
        'Run a Python snippet in an isolated subprocess with timeout and output caps. '
        'Use for looped/conditional processing when plain tool calls are inefficient.'
    ),
    'input_schema': {
        'type': 'object',
        'properties': {
            'code': {'type': 'string', 'description': 'Python code to run.'},
            'timeout_sec': {'type': 'number', 'description': 'Execution timeout in seconds (default 120).'},
        },
        'required': ['code'],
    },
}


def execute_execute_code(params: dict, ctx: ToolContext) -> ToolResult:
    code = str(params.get('code') or '')
    if not code.strip():
        return ToolResult(content='Error: code is required.', is_error=True)

    timeout = int(params.get('timeout_sec') or 120)
    timeout = max(1, min(timeout, 300))

    workdir = str(ctx.project_root)
    with tempfile.TemporaryDirectory(prefix='charon-exec-') as td:
        script = Path(td) / 'snippet.py'
        script.write_text(code, encoding='utf-8')
        cmd = ['python3', str(script)]
        try:
            proc = subprocess.run(
                cmd,
                cwd=workdir,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return ToolResult(content=f'Error: code execution timed out after {timeout}s.', is_error=True)
        except Exception as e:
            return ToolResult(content=f'Error running code: {e}', is_error=True)

    out = (proc.stdout or '')
    err = (proc.stderr or '')
    merged = out
    if err:
        merged += ('\n' if merged else '') + '[stderr]\n' + err
    merged = merged.strip() or '(no output)'
    merged, truncated = truncate_output(merged, max_lines=ctx.max_output_lines, max_bytes=ctx.max_output_bytes)

    if proc.returncode != 0:
        return ToolResult(
            content=f'Exit code: {proc.returncode}\n{merged}',
            is_error=True,
            truncated=truncated,
            details={'exit_code': proc.returncode},
        )

    return ToolResult(content=merged, truncated=truncated, details={'exit_code': proc.returncode})
