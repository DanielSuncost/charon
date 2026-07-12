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
import signal
import subprocess
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

try:
    from charon.infra.diagnostics import record as _diag
except Exception:  # diagnostics is best-effort and must never block import
    def _diag(*args, **kwargs):
        return None


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
    frozen: list[str] | None = None  # paths that must NOT be modified (Write/Edit denylist)
    on_tool_output: Callable[[str, str], None] | None = None  # (tool_name, chunk)
    operation_id: str = ''
    operation_domain: str = ''
    work_unit_id: str = ''
    operation_role: str = ''
    runtime_role: str = ''
    parent_agent_id: str = ''
    metadata: dict[str, Any] | None = None


_active_bash_lock = threading.Lock()
_active_bash_proc: subprocess.Popen[str] | None = None
_active_bash_meta: dict[str, Any] = {}
_active_bash_abort = threading.Event()


def _set_active_bash(proc: subprocess.Popen[str] | None, meta: dict[str, Any] | None = None) -> None:
    with _active_bash_lock:
        global _active_bash_proc, _active_bash_meta
        _active_bash_proc = proc
        _active_bash_meta = dict(meta or {})
        if proc is None:
            _active_bash_abort.clear()


def _clear_active_bash(proc: subprocess.Popen[str] | None = None) -> None:
    with _active_bash_lock:
        global _active_bash_proc, _active_bash_meta
        if proc is not None and _active_bash_proc is not proc:
            return
        _active_bash_proc = None
        _active_bash_meta = {}
        _active_bash_abort.clear()


def _kill_process_tree(proc: subprocess.Popen[str]) -> None:
    try:
        if os.name != 'nt':
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except Exception:
                proc.kill()
        else:
            subprocess.run(['taskkill', '/F', '/T', '/PID', str(proc.pid)], capture_output=True, text=True, timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def abort_running_bash() -> bool:
    with _active_bash_lock:
        proc = _active_bash_proc
        if not proc or proc.poll() is not None:
            return False
        _active_bash_abort.set()
    _kill_process_tree(proc)
    return True


def get_active_bash_info() -> dict[str, Any]:
    with _active_bash_lock:
        proc = _active_bash_proc
        meta = dict(_active_bash_meta)
        meta['running'] = bool(proc and proc.poll() is None)
        if proc:
            meta['pid'] = proc.pid
        return meta


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


def _managed_processes_path(ctx: ToolContext) -> Path:
    base = ctx.state_dir or (ctx.project_root / '.charon_state')
    base.mkdir(parents=True, exist_ok=True)
    return base / 'managed_processes.json'


def _managed_logs_dir(ctx: ToolContext) -> Path:
    base = ctx.state_dir or (ctx.project_root / '.charon_state')
    d = base / 'process_logs'
    d.mkdir(parents=True, exist_ok=True)
    return d


def _load_managed_processes(ctx: ToolContext) -> dict[str, Any]:
    path = _managed_processes_path(ctx)
    if not path.exists():
        return {'processes': {}}
    try:
        data = json.loads(path.read_text())
        if isinstance(data, dict) and isinstance(data.get('processes'), dict):
            return data
    except Exception as e:
        _diag('tools', 'managed_processes.json unreadable/corrupt; managed-process registry loads as empty', error=e)
    return {'processes': {}}


def _save_managed_processes(ctx: ToolContext, data: dict[str, Any]) -> None:
    path = _managed_processes_path(ctx)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def _is_pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception as e:
        _diag('tools', 'pid liveness probe failed unexpectedly; process reported as not running', error=e, pid=pid)
        return False


def _signal_managed_pid(pid: int, sig: int = signal.SIGTERM) -> bool:
    if pid <= 0:
        return False
    try:
        if os.name != 'nt':
            try:
                os.killpg(pid, sig)
            except Exception:
                os.kill(pid, sig)
        else:
            if sig == signal.SIGKILL:
                subprocess.run(['taskkill', '/F', '/T', '/PID', str(pid)], capture_output=True, text=True, timeout=5)
            else:
                subprocess.run(['taskkill', '/T', '/PID', str(pid)], capture_output=True, text=True, timeout=5)
        return True
    except Exception as e:
        _diag('tools', 'signal send to managed process failed; stop/kill request had no effect', error=e, pid=pid)
        return False


def _refresh_managed_processes(ctx: ToolContext) -> dict[str, Any]:
    data = _load_managed_processes(ctx)
    changed = False
    for _proc_id, entry in list((data.get('processes') or {}).items()):
        pid = int(entry.get('pid', 0) or 0)
        running = _is_pid_running(pid)
        log_path = Path(str(entry.get('log_path') or ''))
        exit_code = entry.get('exit_code')
        if log_path.exists() and exit_code is None:
            try:
                tail = '\n'.join(log_path.read_text(encoding='utf-8', errors='replace').splitlines()[-20:])
                m = re.search(r'__CHARON_EXIT_CODE__=(\d+)', tail)
                if m:
                    entry['exit_code'] = int(m.group(1))
                    exit_code = entry['exit_code']
                    changed = True
            except Exception as e:
                _diag('tools', 'managed-process log tail read failed; exit code detection skipped for this process', error=e)
        if exit_code is not None:
            running = False
        if entry.get('status') == 'running' and not running:
            entry['status'] = 'exited' if exit_code == 0 else 'failed'
            entry['exited_at'] = time.time()
            changed = True
        entry['running'] = running
    if changed:
        _save_managed_processes(ctx, data)
    return data


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
            content='Error: pdftotext not installed. Install poppler-utils: sudo apt install poppler-utils',
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


def detect_foreground_persistent_command(command: str) -> tuple[bool, str]:
    """Detect commands that are likely to run indefinitely in the foreground.

    These commands are a poor fit for the synchronous Bash tool because they
    block the agent until timeout and make the UI look hung.
    """
    cmd = command.strip()
    if not cmd:
        return False, ''

    # A command whose last element is backgrounded with '&' returns to the
    # shell immediately, so it cannot make the UI appear hung — allow it even
    # if it launches a monitor/server/etc. (This is checked before the
    # gui_launch heuristics below, which otherwise flag e.g. `nohup
    # python3 gpu_monitor.py >log 2>&1 &`.)
    if re.search(r'(^|\s)&\s*$', cmd):
        return False, ''

    # Detached/background launches of GUI-style apps are still a poor fit for
    # the synchronous Bash tool: they often appear hung while the shell waits,
    # especially when combined with follow-up sleep/ps/log inspection.
    gui_launch = re.search(
        r'\b(uv\s+run\s+python\d*|python\d*)\s+[^\n|;&]*\b[a-z0-9_.-]*(gui|monitor|server|daemon|watch)\.py\b',
        cmd,
        re.IGNORECASE,
    )
    if re.search(r'\b(nohup|setsid)\b', cmd, re.IGNORECASE) and gui_launch:
        return True, 'detached launch of a long-running Python app via Bash tool'
    if gui_launch and re.search(r'(^|\n|[;&])\s*(sleep\b|ps\b|pgrep\b|pkill\b|grep\b|head\b|tail\b|cat\b)', cmd, re.IGNORECASE):
        return True, 'launching a long-running app and then polling/logging in one Bash call'

    # Explicitly bounded or detached commands are allowed.
    if re.search(r'(^|[;&|()]\s*)(timeout|nohup|setsid|tmux|screen)\b', cmd):
        return False, ''
    if re.search(r'\bdisown\b', cmd):
        return False, ''

    # Search/read commands are always fast — don't block them even if their
    # arguments contain words like "watch", "server", "monitor", etc.
    first_word = cmd.strip().split()[0] if cmd.strip() else ''
    if first_word in ('rg', 'grep', 'egrep', 'fgrep', 'find', 'fd', 'git',
                       'cat', 'head', 'tail', 'wc', 'ls', 'tree', 'stat',
                       'file', 'diff', 'jq', 'sed', 'awk', 'sort', 'uniq',
                       'cut', 'tr', 'xargs', 'ag', 'ack'):
        return False, ''

    persistent_patterns = [
        (r'\bpython\d*\s+[^\n|;&]*\b[a-z0-9_.-]*(gui|monitor|server|daemon|watch)\.py\b', 'foreground Python app'),
        (r'\buv\s+run\s+python\d*\s+[^\n|;&]*\b[a-z0-9_.-]*(gui|monitor|server|daemon|watch)\.py\b', 'foreground Python app via uv'),
        (r'\b(streamlit|gradio|jupyter\s+lab|jupyter\s+notebook|uvicorn|gunicorn|flask\s+run)\b', 'foreground app server'),
        (r'\b(npm|pnpm|yarn|bun)\s+(run\s+)?(dev|start|serve|watch)\b', 'foreground dev server'),
        (r'\b(tail\s+-f|watch\s+|top\b|htop\b|nvtop\b)\b', 'interactive or continuous terminal program'),
    ]
    for pattern, reason in persistent_patterns:
        if re.search(pattern, cmd, re.IGNORECASE):
            return True, reason

    if '| head' in cmd and re.search(r'\b(gui|monitor|server|daemon|watch)\b', cmd, re.IGNORECASE):
        return True, 'piping a likely long-running process into head'

    return False, ''


BASH_TOOL_DEF = {
    'name': 'Bash',
    'description': (
        'Execute a short-lived bash command in the current working directory. '
        'Returns stdout and stderr. Output is truncated to last 2000 lines '
        'or 50KB (whichever is hit first). If truncated, full output is saved '
        'to a temp file. Optionally provide a timeout in seconds. '
        'Do not use this for GUI apps, monitors, servers, watchers, or detached/background jobs — use RunProcess for those.'
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

    is_persistent, persistent_reason = detect_foreground_persistent_command(command)
    if is_persistent:
        return ToolResult(
            content=(
                'Error: this looks like a long-running foreground command, which Charon will not run via the Bash tool because it makes the UI appear hung.\n\n'
                f'Detected: {persistent_reason}.\n\n'
                'Use one of these patterns instead:\n'
                '  1. Bounded smoke test: timeout 3s <command>\n'
                '  2. Managed background run: RunProcess(command=...)\n'
                '  3. Run it in tmux/screen if you want to keep it alive interactively\n\n'
                'For GUI apps, prefer testing imports/startup checks rather than launching the full app in the foreground.'
            ),
            is_error=True,
            details={'command': command, 'persistent_foreground_blocked': True},
        )

    has_sudo = bool(re.search(r'(^|[;&|()]\s*)sudo\b', command))
    sudo_non_interactive = bool(re.search(r'(^|[;&|()]\s*)sudo\s+(?:-n|--non-interactive)\b', command))
    if has_sudo and not sudo_non_interactive:
        return ToolResult(
            content=(
                'Error: interactive sudo is not supported inside the Charon TUI.\n\n'
                'Use one of these secure flows instead:\n'
                '  1. In a normal terminal, refresh sudo credentials with: sudo -v\n'
                '  2. Then rerun the command here as: sudo -n ...\n'
                '  3. For automation, prefer a tightly scoped sudoers NOPASSWD rule for the exact command.\n\n'
                'Charon intentionally refuses to collect or forward your password.'
            ),
            is_error=True,
            details={'command': command, 'needs_interactive_sudo': True},
        )

    popen: subprocess.Popen[str] | None = None
    start_ts = time.time()
    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    chunks_lock = threading.Lock()

    def _emit_chunk(tool_name: str, chunk: str) -> None:
        if ctx.on_tool_output and chunk:
            try:
                ctx.on_tool_output(tool_name, chunk)
            except Exception:
                pass

    def _reader(stream, store: list[str], label: str) -> None:
        try:
            while True:
                chunk = stream.read(1024)
                if not chunk:
                    break
                with chunks_lock:
                    store.append(chunk)
                _emit_chunk('Bash', chunk)
        except Exception as e:
            _diag('tools', 'bash output reader thread failed; command output may be lost or truncated', error=e, stream=label)

    try:
        popen = subprocess.Popen(
            ['bash', '-c', command],
            cwd=str(ctx.project_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            start_new_session=(os.name != 'nt'),
        )
        _set_active_bash(popen, {
            'command': command,
            'cwd': str(ctx.project_root),
            'started_at': start_ts,
            'agent_id': ctx.agent_id,
        })

        timed_out = False
        aborted = False
        t_out = threading.Thread(target=_reader, args=(popen.stdout, stdout_chunks, 'stdout'), daemon=True) if popen.stdout else None
        t_err = threading.Thread(target=_reader, args=(popen.stderr, stderr_chunks, 'stderr'), daemon=True) if popen.stderr else None
        if t_out:
            t_out.start()
        if t_err:
            t_err.start()

        while True:
            rc = popen.poll()
            if rc is not None:
                break
            if _active_bash_abort.is_set():
                aborted = True
                _kill_process_tree(popen)
                break
            if timeout and (time.time() - start_ts) >= float(timeout):
                timed_out = True
                _kill_process_tree(popen)
                break
            time.sleep(0.1)

        try:
            popen.wait(timeout=2)
        except Exception:
            pass
        if t_out:
            t_out.join(timeout=1)
        if t_err:
            t_err.join(timeout=1)
        with chunks_lock:
            stdout = ''.join(stdout_chunks)
            stderr = ''.join(stderr_chunks)
        proc = type('Completed', (), {
            'stdout': stdout,
            'stderr': stderr,
            'returncode': popen.returncode,
        })()
    except Exception as e:
        if popen is not None:
            _clear_active_bash(popen)
        return ToolResult(content=f'Error executing command: {e}', is_error=True)
    finally:
        if popen is not None:
            _clear_active_bash(popen)

    if aborted:
        return ToolResult(
            content='Error: command aborted',
            is_error=True,
            details={'command': command, 'aborted': True},
        )
    if timed_out:
        return ToolResult(
            content=f'Error: command timed out after {timeout}s',
            is_error=True,
            details={'command': command, 'timed_out': True},
        )

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

    if has_sudo and proc.returncode != 0:
        sudo_err = output.lower()
        if (
            'a password is required' in sudo_err
            or 'terminal is required' in sudo_err
            or 'a terminal is required' in sudo_err
            or 'sudo:' in sudo_err
        ):
            output = (
                (output + '\n\n') if output else ''
            ) + (
                'Hint: run `sudo -v` in a normal terminal first, then retry with `sudo -n ...`. '
                'For repeatable automation, prefer a tightly scoped NOPASSWD sudoers rule.'
            )
            details['sudo_auth_failed'] = True

    if proc.returncode != 0:
        return ToolResult(
            content=output or f'Command failed with exit code {proc.returncode}',
            is_error=True,
            truncated=truncated,
            details=details,
        )

    return ToolResult(content=output, truncated=truncated, details=details)


# -- Managed process tools ----------------------------------------------------

RUN_PROCESS_TOOL_DEF = {
    'name': 'RunProcess',
    'description': (
        'Start a long-running background process and track it. '
        'Use this instead of Bash for GUI apps, monitors, servers, or detached jobs. '
        'Returns a process_id you can use with ProcessStatus, ProcessLogs, and StopProcess.'
    ),
    'input_schema': {
        'type': 'object',
        'properties': {
            'command': {'type': 'string', 'description': 'Shell command to run in the background'},
            'cwd': {'type': 'string', 'description': 'Optional working directory'},
            'name': {'type': 'string', 'description': 'Optional human-friendly process name'},
        },
        'required': ['command'],
    },
}

PROCESS_STATUS_TOOL_DEF = {
    'name': 'ProcessStatus',
    'description': 'Show status for one managed process, or list all managed processes if no process_id is given.',
    'input_schema': {
        'type': 'object',
        'properties': {
            'process_id': {'type': 'string', 'description': 'Managed process id'},
        },
        'required': [],
    },
}

PROCESS_LOGS_TOOL_DEF = {
    'name': 'ProcessLogs',
    'description': 'Read recent logs from a managed background process.',
    'input_schema': {
        'type': 'object',
        'properties': {
            'process_id': {'type': 'string', 'description': 'Managed process id'},
            'lines': {'type': 'number', 'description': 'Number of trailing log lines to read (default 80)'},
        },
        'required': ['process_id'],
    },
}

STOP_PROCESS_TOOL_DEF = {
    'name': 'StopProcess',
    'description': 'Stop a managed background process by id.',
    'input_schema': {
        'type': 'object',
        'properties': {
            'process_id': {'type': 'string', 'description': 'Managed process id'},
            'force': {'type': 'boolean', 'description': 'Use SIGKILL / forceful stop if true'},
        },
        'required': ['process_id'],
    },
}


def execute_run_process(params: dict, ctx: ToolContext) -> ToolResult:
    command = str(params.get('command') or '').strip()
    cwd_str = str(params.get('cwd') or '').strip()
    name = str(params.get('name') or '').strip()
    if not command:
        return ToolResult(content='Error: command is required', is_error=True)

    cwd = Path(cwd_str).expanduser() if cwd_str else ctx.project_root
    if not cwd.is_absolute():
        cwd = (ctx.project_root / cwd).resolve()
    if not cwd.exists():
        return ToolResult(content=f'Error: cwd does not exist: {cwd}', is_error=True)

    proc_id = f'proc-{int(time.time() * 1000)}'
    log_path = _managed_logs_dir(ctx) / f'{proc_id}.log'
    logf = open(log_path, 'a', encoding='utf-8')
    wrapped_command = (
        f'{{ {command}; }}; '
        'code=$?; '
        'printf "\n__CHARON_EXIT_CODE__=%s\n" "$code"; '
        'exit "$code"'
    )
    try:
        proc = subprocess.Popen(
            ['bash', '-c', wrapped_command],
            cwd=str(cwd),
            stdout=logf,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=(os.name != 'nt'),
        )
    except Exception as e:
        logf.close()
        return ToolResult(content=f'Error starting process: {e}', is_error=True)
    finally:
        try:
            logf.close()
        except Exception:
            pass

    def _watch_process() -> None:
        try:
            proc.wait()
        except Exception as e:
            _diag('tools', 'managed-process wait failed in watch thread; exit may go unnoticed', error=e, process_id=proc_id)
        try:
            data2 = _refresh_managed_processes(ctx)
            entry2 = (data2.get('processes') or {}).get(proc_id)
            if entry2 and entry2.get('status') == 'running':
                entry2['running'] = False
                entry2['status'] = 'exited' if entry2.get('exit_code', 1) == 0 else 'failed'
                entry2['exited_at'] = time.time()
                _save_managed_processes(ctx, data2)
        except Exception as e:
            _diag('tools', 'managed-process status update failed in watch thread; process may stay marked running', error=e, process_id=proc_id)

    threading.Thread(target=_watch_process, daemon=True).start()

    data = _load_managed_processes(ctx)
    entry = {
        'process_id': proc_id,
        'name': name or command[:60],
        'command': command,
        'wrapped_command': wrapped_command,
        'cwd': str(cwd),
        'pid': proc.pid,
        'status': 'running',
        'running': True,
        'started_at': time.time(),
        'log_path': str(log_path),
        'agent_id': ctx.agent_id,
        'exit_code': None,
    }
    data.setdefault('processes', {})[proc_id] = entry
    _save_managed_processes(ctx, data)
    return ToolResult(
        content=(
            f'Started managed process `{proc_id}`\n'
            f'PID: {proc.pid}\n'
            f'CWD: {cwd}\n'
            f'Log: {log_path}\n'
            f'Command: {command}'
        ),
        details=entry,
    )



def execute_process_status(params: dict, ctx: ToolContext) -> ToolResult:
    proc_id = str(params.get('process_id') or '').strip()
    data = _refresh_managed_processes(ctx)
    procs = data.get('processes', {}) or {}
    if proc_id:
        entry = procs.get(proc_id)
        if not entry:
            return ToolResult(content=f'Error: unknown process_id: {proc_id}', is_error=True)
        lines = [
            f'Process `{proc_id}`',
            f"Name: {entry.get('name', '')}",
            f"Status: {entry.get('status', 'unknown')}",
            f"PID: {entry.get('pid', '')}",
            f"Exit code: {entry.get('exit_code', '')}",
            f"CWD: {entry.get('cwd', '')}",
            f"Log: {entry.get('log_path', '')}",
            f"Command: {entry.get('command', '')}",
        ]
        return ToolResult(content='\n'.join(lines), details=entry)

    if not procs:
        return ToolResult(content='No managed processes.')
    rows = []
    for key, entry in sorted(procs.items(), key=lambda kv: kv[1].get('started_at', 0), reverse=True):
        exit_suffix = f" exit={entry.get('exit_code')}" if entry.get('exit_code') is not None else ''
        rows.append(f"{key}  [{entry.get('status', 'unknown')}]  pid={entry.get('pid', '')}{exit_suffix}  {entry.get('name', '')}")
    return ToolResult(content='\n'.join(rows), details={'count': len(rows)})



def execute_process_logs(params: dict, ctx: ToolContext) -> ToolResult:
    proc_id = str(params.get('process_id') or '').strip()
    lines = int(params.get('lines', 80) or 80)
    data = _refresh_managed_processes(ctx)
    entry = (data.get('processes') or {}).get(proc_id)
    if not entry:
        return ToolResult(content=f'Error: unknown process_id: {proc_id}', is_error=True)
    log_path = Path(str(entry.get('log_path') or ''))
    if not log_path.exists():
        return ToolResult(content=f'No log file yet for `{proc_id}`.', is_error=True)
    try:
        all_lines = log_path.read_text(encoding='utf-8', errors='replace').splitlines()
        text = '\n'.join(all_lines[-max(1, lines):])
        text, truncated = truncate_output(text, ctx.max_output_lines, ctx.max_output_bytes)
        return ToolResult(content=text or '(no log output)', truncated=truncated, details=entry)
    except Exception as e:
        return ToolResult(content=f'Error reading logs: {e}', is_error=True)



def execute_stop_process(params: dict, ctx: ToolContext) -> ToolResult:
    proc_id = str(params.get('process_id') or '').strip()
    force = bool(params.get('force', False))
    data = _refresh_managed_processes(ctx)
    entry = (data.get('processes') or {}).get(proc_id)
    if not entry:
        return ToolResult(content=f'Error: unknown process_id: {proc_id}', is_error=True)
    pid = int(entry.get('pid', 0) or 0)
    if not _is_pid_running(pid):
        entry['status'] = 'exited'
        entry['running'] = False
        _save_managed_processes(ctx, data)
        return ToolResult(content=f'Process `{proc_id}` is already stopped.', details=entry)

    ok = _signal_managed_pid(pid, signal.SIGKILL if force else signal.SIGTERM)
    if not ok:
        return ToolResult(content=f'Error: failed to stop process `{proc_id}`', is_error=True)

    time.sleep(0.2)
    if force is False and _is_pid_running(pid):
        _signal_managed_pid(pid, signal.SIGKILL)
        time.sleep(0.2)
    entry['running'] = _is_pid_running(pid)
    entry['status'] = 'stopped' if not entry['running'] else 'running'
    entry['stopped_at'] = time.time()
    _save_managed_processes(ctx, data)
    return ToolResult(content=f"Stopped process `{proc_id}` ({'force' if force else 'graceful'}).", details=entry)


# -- Tool registry ------------------------------------------------------------

from charon.tools.memory_tools import (
    USER_MODEL_TOOL_DEF, execute_user_model,
    PROJECT_KNOWLEDGE_TOOL_DEF, execute_project_knowledge,
)
from charon.tools.http_tool import HTTP_TOOL_DEF, execute_http
from charon.tools.git_tool import GIT_TOOL_DEF, execute_git
from charon.tools.batch_tool import SPAWN_BATCH_TOOL_DEF, execute_spawn_batch
from charon.tools.search_tool import SEARCH_TOOL_DEF, execute_search
from charon.tools.web_tool import WEB_TOOL_DEF, execute_web
from charon.tools.paper_tool import PAPER_TOOL_DEF, execute_paper
from charon.tools.source_discovery_tool import SOURCE_DISCOVERY_TOOL_DEF, execute_source_discovery
from charon.tools.research_tool import RESEARCH_TOOL_DEF, execute_research
from charon.tools.x_tool import X_TOOL_DEF, execute_x
from charon.tools.cron_tool import CRON_TOOL_DEF, execute_cron
from charon.tools.skills_tool import SKILLS_TOOL_DEF, execute_skills
from charon.tools.execute_code_tool import EXECUTE_CODE_TOOL_DEF, execute_execute_code
from charon.tools.clarify_tool import CLARIFY_TOOL_DEF, execute_clarify

# Optional tools may legitimately be missing (uninstalled extras) — that is an
# ImportError and is only recorded to diagnostics. Any OTHER exception means
# the tool itself is broken; it is recorded here so status/listing surfaces
# (e.g. /tools) can show it instead of the tool silently vanishing.
FAILED_TOOL_IMPORTS: list[dict[str, str]] = []


def _record_tool_import_failure(tool_name: str, exc: BaseException) -> None:
    if isinstance(exc, ImportError):
        _diag('tools', f'optional tool {tool_name} not loaded (missing dependency)', error=exc, tool=tool_name)
        return
    _diag('tools', f'tool {tool_name} failed to import and was dropped from the registry', error=exc, tool=tool_name)
    FAILED_TOOL_IMPORTS.append({'tool': tool_name, 'error': f'{type(exc).__name__}: {exc}'})


# Browser tool — optional, only loads if playwright is installed
# Suppress stdout/stderr during import (browser-use loads ML models noisily)
try:
    import io as _io
    import contextlib as _cl
    with _cl.redirect_stdout(_io.StringIO()), _cl.redirect_stderr(_io.StringIO()):
        from charon.tools.browser_tool import BROWSER_TOOL_DEF, execute_browser
    _HAS_BROWSER = True
except Exception as _e:
    _HAS_BROWSER = False
    _record_tool_import_failure('Browser', _e)
from charon.tools.shade_tool import SHADE_TOOL_DEF, execute_spawn_shade
from charon.tools.judge_loop_tool import JUDGE_LOOP_TOOL_DEF, execute_judge_loop

# Recall tool — optional, only loads if sqlite-vec and sentence-transformers are installed
try:
    from charon.tools.recall_tool import RECALL_TOOL_DEF, execute_recall
    _HAS_RECALL = True
except Exception as _e:
    _HAS_RECALL = False
    _record_tool_import_failure('Recall', _e)

# Timeline tool — episodic + procedural memory; same deps as Recall
try:
    from charon.tools.timeline_tool import TIMELINE_TOOL_DEF, execute_timeline
    _HAS_TIMELINE = True
except Exception as _e:
    _HAS_TIMELINE = False
    _record_tool_import_failure('Timeline', _e)

# Fleet tools — optional, only loads if fleet_registry is available
try:
    from charon.tools.fleet_tool import (
        ALL_FLEET_TOOL_DEFS as _FLEET_DEFS,
        execute_fleet_status, execute_fleet_send, execute_fleet_history, execute_fleet_onboard,
    )
    _HAS_FLEET = True
except Exception as _e:
    _HAS_FLEET = False
    _record_tool_import_failure('Fleet', _e)

ALL_TOOL_DEFS = [
    READ_TOOL_DEF, BASH_TOOL_DEF, EDIT_TOOL_DEF, WRITE_TOOL_DEF,
    RUN_PROCESS_TOOL_DEF, PROCESS_STATUS_TOOL_DEF, PROCESS_LOGS_TOOL_DEF, STOP_PROCESS_TOOL_DEF,
    USER_MODEL_TOOL_DEF, PROJECT_KNOWLEDGE_TOOL_DEF,
    HTTP_TOOL_DEF, GIT_TOOL_DEF,
    SHADE_TOOL_DEF, SPAWN_BATCH_TOOL_DEF, JUDGE_LOOP_TOOL_DEF,
    SEARCH_TOOL_DEF, WEB_TOOL_DEF, PAPER_TOOL_DEF, SOURCE_DISCOVERY_TOOL_DEF, RESEARCH_TOOL_DEF, X_TOOL_DEF,
    CRON_TOOL_DEF, SKILLS_TOOL_DEF, EXECUTE_CODE_TOOL_DEF, CLARIFY_TOOL_DEF,
] + ([BROWSER_TOOL_DEF] if _HAS_BROWSER else []) + ([RECALL_TOOL_DEF] if _HAS_RECALL else []) + ([TIMELINE_TOOL_DEF] if _HAS_TIMELINE else []) + (_FLEET_DEFS if _HAS_FLEET else [])

TOOL_EXECUTORS: dict[str, Callable[[dict, ToolContext], ToolResult]] = {
    'Read': execute_read,
    'Bash': execute_bash,
    'Edit': execute_edit,
    'Write': execute_write,
    'RunProcess': execute_run_process,
    'ProcessStatus': execute_process_status,
    'ProcessLogs': execute_process_logs,
    'StopProcess': execute_stop_process,
    'UserModel': execute_user_model,
    'ProjectKnowledge': execute_project_knowledge,
    'Http': execute_http,
    'Git': execute_git,
    'SpawnShade': execute_spawn_shade,
    'SpawnBatch': execute_spawn_batch,
    'SpawnJudgeLoop': execute_judge_loop,
    'Search': execute_search,
    'Web': execute_web,
    'Paper': execute_paper,
    'SourceDiscovery': execute_source_discovery,
    'Research': execute_research,
    'X': execute_x,
    'Cron': execute_cron,
    'Skills': execute_skills,
    'ExecuteCode': execute_execute_code,
    'Clarify': execute_clarify,
    **(({'Browser': execute_browser} if _HAS_BROWSER else {})),
    **(({'Recall': execute_recall} if _HAS_RECALL else {})),
    **(({'Timeline': execute_timeline} if _HAS_TIMELINE else {})),
    **(({'FleetStatus': execute_fleet_status, 'FleetSend': execute_fleet_send, 'FleetHistory': execute_fleet_history, 'FleetOnboard': execute_fleet_onboard} if _HAS_FLEET else {})),
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
    if not ctx.scope and not ctx.frozen:
        return None  # No restrictions

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
    except Exception as e:
        _diag('tools', 'scope-check path resolution failed; scope/frozen enforcement uses unresolved path', error=e)
    target_str = str(target)

    def _within(entry: str) -> bool:
        p = entry.strip().strip('/')
        if not p:
            return False
        # Match the prefix dir itself or anything beneath it, but require a
        # path-component boundary so "src" does not match a sibling "src-evil/".
        base = str((ctx.project_root / p).resolve())
        return target_str == base or target_str.startswith(base + os.sep)

    # Frozen denylist — blocks modifications regardless of scope.
    if ctx.frozen and name in ('Write', 'Edit'):
        if any(_within(entry) for entry in ctx.frozen):
            frozen_list = ', '.join(ctx.frozen)
            return (
                f'Frozen-path violation: {name} on "{path_param}" targets a frozen path '
                f'[{frozen_list}] that must not be modified.'
            )

    # Scope allowlist gates *modifications* only (Write/Edit). Reads are allowed
    # across the project: an implementer must be able to read its checker/tests
    # (often a frozen file) to understand the target it's optimizing toward —
    # the frozen denylist above still prevents it from modifying them.
    if ctx.scope and name in ('Write', 'Edit'):
        if any(_within(entry) for entry in ctx.scope):
            return None
        scope_list = ', '.join(ctx.scope)
        return (
            f'Scope violation: {name} on "{path_param}" is outside allowed scope [{scope_list}]. '
            f'This shade is restricted to modifying files within its contract scope.'
        )

    return None


def execute_tool(name: str, params: dict, ctx: ToolContext) -> ToolResult:
    """Execute a tool by name. Checks approval, scope, then built-in, then dynamic."""
    # Scope enforcement for shade agents
    scope_error = _check_scope(name, params, ctx)
    if scope_error:
        return ToolResult(content=scope_error, is_error=True)

    # Approval check (skip for shade agents — they have scope enforcement instead)
    if not ctx.scope:  # not a shade
        try:
            from charon.infra.tool_approval import needs_approval, approve_tool_for_session
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
        from charon.tools.dynamic_loader import execute_dynamic_tool
        result = execute_dynamic_tool(name, params, ctx)
        if result is not None:
            return result
    except Exception as e:
        return ToolResult(content=f'Dynamic tool error: {e}', is_error=True)

    return ToolResult(content=f'Unknown tool: {name}', is_error=True)
