"""Charon agent tools — modeled on pi-agent's tool system.

Each tool has:
- name, description, parameters (JSON Schema)
- execute(params, context) -> ToolResult

Tools are defined as Anthropic-style tool dicts for the API,
with a separate execute registry.
"""
from __future__ import annotations

import json
import os
import subprocess
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass
class ToolResult:
    content: str
    is_error: bool = False
    truncated: bool = False
    details: dict[str, Any] | None = None


@dataclass
class ToolContext:
    project_root: Path
    agent_id: str = ''
    state_dir: Path | None = None
    max_output_bytes: int = 50_000
    max_output_lines: int = 2000
    shell_timeout: int = 120
    scope: list[str] | None = None  # shade scope restriction (list of allowed path prefixes)


# -- Truncation helper --------------------------------------------------------

def truncate_output(text: str, max_lines: int = 2000, max_bytes: int = 50_000) -> tuple[str, bool]:
    """Truncate output to fit limits. Returns (text, was_truncated)."""
    lines = text.splitlines(keepends=True)
    truncated = False

    if len(lines) > max_lines:
        lines = lines[-max_lines:]
        truncated = True

    result = ''.join(lines)
    if len(result.encode('utf-8', errors='replace')) > max_bytes:
        while len(result.encode('utf-8', errors='replace')) > max_bytes and lines:
            lines.pop(0)
            result = ''.join(lines)
        truncated = True

    return result, truncated


# -- Read tool ----------------------------------------------------------------

READ_TOOL_DEF = {
    'name': 'Read',
    'description': (
        'Read the contents of a file. Supports text files, PDFs, and images (jpg, png, gif, webp). '
        'PDFs are converted to text automatically. '
        'Output is truncated to 2000 lines or 50KB (whichever is hit first). '
        'Use offset/limit for large files. '
        'When you need the full file, continue with offset until complete.'
    ),
    'input_schema': {
        'type': 'object',
        'properties': {
            'path': {
                'type': 'string',
                'description': 'Path to the file to read (relative or absolute)',
            },
            'offset': {
                'type': 'number',
                'description': 'Line number to start reading from (1-indexed)',
            },
            'limit': {
                'type': 'number',
                'description': 'Maximum number of lines to read',
            },
        },
        'required': ['path'],
    },
}


def _read_pdf(target: Path, params: dict, ctx: ToolContext) -> ToolResult:
    """Extract text from a PDF file using pdftotext (poppler-utils)."""
    import shutil
    import subprocess

    pdftotext = shutil.which('pdftotext')
    if not pdftotext:
        return ToolResult(
            content=f'Error: pdftotext not installed. Install poppler-utils: sudo apt install poppler-utils',
            is_error=True,
        )

    try:
        proc = subprocess.run(
            [pdftotext, '-layout', str(target), '-'],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode != 0:
            return ToolResult(content=f'Error: pdftotext failed: {proc.stderr}', is_error=True)

        text = proc.stdout
    except subprocess.TimeoutExpired:
        return ToolResult(content='Error: PDF extraction timed out', is_error=True)
    except Exception as e:
        return ToolResult(content=f'Error reading PDF: {e}', is_error=True)

    lines = text.splitlines(keepends=True)
    total_lines = len(lines)

    offset = int(params.get('offset', 1)) - 1
    limit = int(params.get('limit', 0)) or ctx.max_output_lines
    offset = max(0, offset)
    selected = lines[offset:offset + limit]
    result_text = ''.join(selected)
    result_text, truncated = truncate_output(result_text, ctx.max_output_lines, ctx.max_output_bytes)

    meta = f'\n[PDF: {target.name}, {total_lines} lines total]'
    if truncated or offset > 0 or (offset + limit) < total_lines:
        shown_end = min(offset + len(selected), total_lines)
        meta += f' [Showing lines {offset+1}-{shown_end}]'
        if (offset + limit) < total_lines:
            meta += f' Use offset={shown_end + 1} to continue.'

    return ToolResult(content=result_text + meta, truncated=truncated)


def execute_read(params: dict, ctx: ToolContext) -> ToolResult:
    path_str = params.get('path', '')
    if not path_str:
        return ToolResult(content='Error: path is required', is_error=True)

    target = Path(path_str)
    if not target.is_absolute():
        target = ctx.project_root / target

    if not target.exists():
        return ToolResult(content=f'Error: file not found: {path_str}', is_error=True)
    if not target.is_file():
        return ToolResult(content=f'Error: not a file: {path_str}', is_error=True)

    # PDF support: use pdftotext if available
    if target.suffix.lower() == '.pdf':
        return _read_pdf(target, params, ctx)

    try:
        text = target.read_text(encoding='utf-8', errors='replace')
    except Exception as e:
        return ToolResult(content=f'Error reading file: {e}', is_error=True)

    lines = text.splitlines(keepends=True)
    total_lines = len(lines)

    offset = int(params.get('offset', 1)) - 1  # convert to 0-indexed
    limit = int(params.get('limit', 0)) or ctx.max_output_lines

    offset = max(0, offset)
    selected = lines[offset:offset + limit]
    result_text = ''.join(selected)
    result_text, truncated = truncate_output(result_text, ctx.max_output_lines, ctx.max_output_bytes)

    # Add metadata if truncated or offset
    meta = ''
    if truncated or offset > 0 or (offset + limit) < total_lines:
        shown_start = offset + 1
        shown_end = min(offset + len(selected), total_lines)
        meta = f'\n[Showing lines {shown_start}-{shown_end} of {total_lines}]'
        if truncated:
            meta += f' (truncated to {ctx.max_output_bytes // 1000}KB limit)'
        meta += f'. Use offset={shown_end + 1} to continue.'

    return ToolResult(content=result_text + meta, truncated=truncated)


# -- Write tool ---------------------------------------------------------------

WRITE_TOOL_DEF = {
    'name': 'Write',
    'description': (
        "Write content to a file. Creates the file if it doesn't exist, "
        "overwrites if it does. Automatically creates parent directories."
    ),
    'input_schema': {
        'type': 'object',
        'properties': {
            'path': {
                'type': 'string',
                'description': 'Path to the file to write (relative or absolute)',
            },
            'content': {
                'type': 'string',
                'description': 'Content to write to the file',
            },
        },
        'required': ['path', 'content'],
    },
}


def execute_write(params: dict, ctx: ToolContext) -> ToolResult:
    path_str = params.get('path', '')
    content = params.get('content', '')

    if not path_str:
        return ToolResult(content='Error: path is required', is_error=True)

    target = Path(path_str)
    if not target.is_absolute():
        target = ctx.project_root / target

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding='utf-8')
        byte_count = len(content.encode('utf-8'))
        return ToolResult(
            content=f'Successfully wrote {byte_count} bytes to {path_str}',
            details={'path': str(target), 'bytes': byte_count},
        )
    except Exception as e:
        return ToolResult(content=f'Error writing file: {e}', is_error=True)


# -- Edit tool ----------------------------------------------------------------

EDIT_TOOL_DEF = {
    'name': 'Edit',
    'description': (
        'Edit a file by replacing exact text. The oldText must match exactly '
        '(including whitespace). Use this for precise, surgical edits.'
    ),
    'input_schema': {
        'type': 'object',
        'properties': {
            'path': {
                'type': 'string',
                'description': 'Path to the file to edit (relative or absolute)',
            },
            'oldText': {
                'type': 'string',
                'description': 'Exact text to find and replace (must match exactly)',
            },
            'newText': {
                'type': 'string',
                'description': 'New text to replace the old text with',
            },
        },
        'required': ['path', 'oldText', 'newText'],
    },
}


def execute_edit(params: dict, ctx: ToolContext) -> ToolResult:
    path_str = params.get('path', '')
    old_text = params.get('oldText', '')
    new_text = params.get('newText', '')

    if not path_str:
        return ToolResult(content='Error: path is required', is_error=True)
    if not old_text:
        return ToolResult(content='Error: oldText is required', is_error=True)

    target = Path(path_str)
    if not target.is_absolute():
        target = ctx.project_root / target

    if not target.exists():
        return ToolResult(content=f'Error: file not found: {path_str}', is_error=True)

    try:
        content = target.read_text(encoding='utf-8')
    except Exception as e:
        return ToolResult(content=f'Error reading file: {e}', is_error=True)

    count = content.count(old_text)
    if count == 0:
        # Show helpful context
        preview = old_text[:100].replace('\n', '\\n')
        return ToolResult(
            content=f'Error: oldText not found in {path_str}. Text to find: "{preview}..."',
            is_error=True,
        )
    if count > 1:
        return ToolResult(
            content=f'Error: oldText found {count} times in {path_str}. Must match exactly once.',
            is_error=True,
        )

    new_content = content.replace(old_text, new_text, 1)
    try:
        target.write_text(new_content, encoding='utf-8')
        return ToolResult(content=f'Successfully edited {path_str}')
    except Exception as e:
        return ToolResult(content=f'Error writing file: {e}', is_error=True)


# -- Bash tool ----------------------------------------------------------------

BASH_TOOL_DEF = {
    'name': 'Bash',
    'description': (
        'Execute a bash command in the current working directory. '
        'Returns stdout and stderr. Output is truncated to last 2000 lines '
        'or 50KB (whichever is hit first). If truncated, full output is saved '
        'to a temp file. Optionally provide a timeout in seconds.'
    ),
    'input_schema': {
        'type': 'object',
        'properties': {
            'command': {
                'type': 'string',
                'description': 'Bash command to execute',
            },
            'timeout': {
                'type': 'number',
                'description': 'Timeout in seconds (optional, no default timeout)',
            },
        },
        'required': ['command'],
    },
}


def execute_bash(params: dict, ctx: ToolContext) -> ToolResult:
    command = params.get('command', '')
    timeout = params.get('timeout') or ctx.shell_timeout

    if not command:
        return ToolResult(content='Error: command is required', is_error=True)

    try:
        proc = subprocess.run(
            ['bash', '-c', command],
            cwd=str(ctx.project_root),
            capture_output=True,
            text=True,
            timeout=int(timeout),
        )
    except subprocess.TimeoutExpired:
        return ToolResult(
            content=f'Error: command timed out after {timeout}s',
            is_error=True,
        )
    except Exception as e:
        return ToolResult(content=f'Error executing command: {e}', is_error=True)

    output = ''
    if proc.stdout:
        output += proc.stdout
    if proc.stderr:
        if output:
            output += '\n'
        output += proc.stderr

    output, truncated = truncate_output(output, ctx.max_output_lines, ctx.max_output_bytes)

    if truncated:
        # Save full output to temp file
        import tempfile
        tmp = tempfile.NamedTemporaryFile(
            mode='w', suffix='.log', prefix='pi-bash-',
            delete=False, dir='/tmp',
        )
        full_output = (proc.stdout or '') + (proc.stderr or '')
        tmp.write(full_output)
        tmp.close()
        output += f'\n\n[Output truncated. Full output: {tmp.name}]'

    details = {
        'command': command,
        'exit_code': proc.returncode,
        'truncated': truncated,
    }

    if proc.returncode != 0:
        return ToolResult(
            content=output or f'Command failed with exit code {proc.returncode}',
            is_error=True,
            truncated=truncated,
            details=details,
        )

    return ToolResult(content=output, truncated=truncated, details=details)


# -- Tool registry ------------------------------------------------------------

from tools.memory_tools import (
    USER_MODEL_TOOL_DEF, execute_user_model,
    PROJECT_KNOWLEDGE_TOOL_DEF, execute_project_knowledge,
)
from tools.http_tool import HTTP_TOOL_DEF, execute_http
from tools.git_tool import GIT_TOOL_DEF, execute_git
from tools.batch_tool import SPAWN_BATCH_TOOL_DEF, execute_spawn_batch
from tools.search_tool import SEARCH_TOOL_DEF, execute_search
from tools.web_tool import WEB_TOOL_DEF, execute_web

# Browser tool — optional, only loads if playwright is installed
# Suppress stdout/stderr during import (browser-use loads ML models noisily)
try:
    import io as _io, contextlib as _cl
    with _cl.redirect_stdout(_io.StringIO()), _cl.redirect_stderr(_io.StringIO()):
        from tools.browser_tool import BROWSER_TOOL_DEF, execute_browser
    _HAS_BROWSER = True
except (ImportError, Exception):
    _HAS_BROWSER = False
from tools.shade_tool import SHADE_TOOL_DEF, execute_spawn_shade

# Recall tool — optional, only loads if sqlite-vec and sentence-transformers are installed
try:
    from tools.recall_tool import RECALL_TOOL_DEF, execute_recall
    _HAS_RECALL = True
except (ImportError, Exception):
    _HAS_RECALL = False

ALL_TOOL_DEFS = [
    READ_TOOL_DEF, BASH_TOOL_DEF, EDIT_TOOL_DEF, WRITE_TOOL_DEF,
    USER_MODEL_TOOL_DEF, PROJECT_KNOWLEDGE_TOOL_DEF,
    HTTP_TOOL_DEF, GIT_TOOL_DEF,
    SHADE_TOOL_DEF, SPAWN_BATCH_TOOL_DEF, SEARCH_TOOL_DEF, WEB_TOOL_DEF,
] + ([BROWSER_TOOL_DEF] if _HAS_BROWSER else []) + ([RECALL_TOOL_DEF] if _HAS_RECALL else [])

TOOL_EXECUTORS: dict[str, Callable[[dict, ToolContext], ToolResult]] = {
    'Read': execute_read,
    'Bash': execute_bash,
    'Edit': execute_edit,
    'Write': execute_write,
    'UserModel': execute_user_model,
    'ProjectKnowledge': execute_project_knowledge,
    'Http': execute_http,
    'Git': execute_git,
    'SpawnShade': execute_spawn_shade,
    'SpawnBatch': execute_spawn_batch,
    'Search': execute_search,
    'Web': execute_web,
    **(({'Browser': execute_browser} if _HAS_BROWSER else {})),
    **(({'Recall': execute_recall} if _HAS_RECALL else {})),
}


# ── Interactive approval ──────────────────────────────────────────────

import threading

_approval_lock = threading.Lock()
_approval_pending: dict | None = None
_approval_event = threading.Event()
_approval_result: bool = False
_approval_callback = None  # set by the backend to emit events to the TUI


def set_approval_callback(callback):
    """Set the function that sends approval requests to the TUI.
    
    Called by the backend on startup. The callback receives:
    (tool_name, params_summary, risk, reason) and should emit
    an approval_request event to the frontend.
    """
    global _approval_callback
    _approval_callback = callback


def respond_to_approval(approved: bool):
    """Called by the backend when the user responds to an approval prompt."""
    global _approval_result
    _approval_result = approved
    _approval_event.set()


def _request_interactive_approval(tool_name: str, params: dict, risk: str, reason: str, session_id: str) -> bool:
    """Request approval from the user via the TUI.
    
    Blocks until the user responds (y/n) or times out after 60 seconds.
    """
    global _approval_pending, _approval_result

    if not _approval_callback:
        # No TUI connected — auto-approve (CLI mode)
        return True

    # Build a concise params summary
    summary_parts = []
    if 'command' in params:
        summary_parts.append(f'command: {str(params["command"])[:80]}')
    if 'url' in params:
        summary_parts.append(f'url: {str(params["url"])[:80]}')
    if 'path' in params:
        summary_parts.append(f'path: {str(params["path"])[:80]}')
    if 'action' in params:
        summary_parts.append(f'action: {params["action"]}')
    params_summary = ', '.join(summary_parts) if summary_parts else str(params)[:100]

    _approval_event.clear()
    _approval_result = False

    # Send request to TUI
    _approval_callback(tool_name, params_summary, risk, reason)

    # Block until user responds or timeout
    responded = _approval_event.wait(timeout=60)
    if not responded:
        return False  # timeout = deny

    return _approval_result


def _check_scope(name: str, params: dict, ctx: ToolContext) -> str | None:
    """Check if a tool call is within the shade's allowed scope.

    Returns an error message if blocked, None if allowed.
    Only enforced when ctx.scope is set (shade agents).
    """
    if not ctx.scope:
        return None  # No scope restriction

    # Tools that access paths
    path_param = None
    if name in ('Read', 'Write', 'Edit'):
        path_param = params.get('path', '')
    elif name == 'Bash':
        # Can't reliably scope bash commands — allow but log
        # (shade system prompt already tells it to stay in scope)
        return None
    elif name in ('Git',):
        # Git operates on the whole repo — allow
        return None

    if not path_param:
        return None

    # Resolve the path
    target = Path(path_param)
    if not target.is_absolute():
        target = ctx.project_root / target
    try:
        target = target.resolve()
    except Exception:
        pass
    target_str = str(target)

    # Check if the path falls within any allowed scope prefix
    project_root = str(ctx.project_root.resolve())
    for scope_entry in ctx.scope:
        scope_path = scope_entry.strip().strip('/')
        if not scope_path:
            continue
        # Scope can be relative to project root
        allowed = str((ctx.project_root / scope_path).resolve())
        if target_str.startswith(allowed):
            return None
        # Also check if target is the scope dir itself
        if target_str == allowed:
            return None

    scope_list = ', '.join(ctx.scope)
    return (
        f'Scope violation: {name} on "{path_param}" is outside allowed scope [{scope_list}]. '
        f'This shade is restricted to files within its contract scope.'
    )


def execute_tool(name: str, params: dict, ctx: ToolContext) -> ToolResult:
    """Execute a tool by name. Checks approval, scope, then built-in, then dynamic."""
    # Scope enforcement for shade agents
    scope_error = _check_scope(name, params, ctx)
    if scope_error:
        return ToolResult(content=scope_error, is_error=True)

    # Approval check (skip for shade agents — they have scope enforcement instead)
    if not ctx.scope:  # not a shade
        try:
            from tool_approval import needs_approval, approve_tool_for_session
            session_id = ctx.agent_id or 'default'
            needs, risk, reason = needs_approval(name, params, session_id=session_id)
            if needs and session_id != 'default':
                needs2, _, _ = needs_approval(name, params, session_id='default')
                if not needs2:
                    needs = False
            if needs:
                # Ask for interactive approval via the pending approval mechanism
                approved = _request_interactive_approval(name, params, risk, reason, session_id)
                if not approved:
                    return ToolResult(
                        content=f'Blocked: {reason} (user denied)',
                        is_error=True,
                    )
                # User approved — remember for this session
                approve_tool_for_session(session_id, name)
                approve_tool_for_session('default', name)
        except ImportError:
            pass

    executor = TOOL_EXECUTORS.get(name)
    if executor:
        try:
            return executor(params, ctx)
        except Exception as e:
            return ToolResult(content=f'Tool execution error: {e}', is_error=True)

    # Try dynamic tools
    try:
        from tools.dynamic_loader import execute_dynamic_tool
        result = execute_dynamic_tool(name, params, ctx)
        if result is not None:
            return result
    except Exception as e:
        return ToolResult(content=f'Dynamic tool error: {e}', is_error=True)

    return ToolResult(content=f'Unknown tool: {name}', is_error=True)
