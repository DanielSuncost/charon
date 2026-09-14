"""Charon agent tools — modeled on pi-agent's tool system.

Each tool has:
- name, description, parameters (JSON Schema)
- execute(params, context) -> ToolResult

Tools are defined as Anthropic-style tool dicts for the API,
with a separate execute registry.
"""
from __future__ import annotations

import ast
import difflib
import importlib.util
import json
import os
import signal
import subprocess
import re
import threading
import time
import uuid
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
    cancel_event: threading.Event | None = None
    topology_depth: int = 0  # depth in the delegation tree (0 = root agent)
    topology_budget: dict[str, Any] | None = None  # set for shades: governs further spawning


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
        'Read the contents of a file. Supports text files, PDFs, office documents '
        '(xlsx, docx, pptx), and images (jpg, png, gif, webp). '
        'PDFs and office documents are converted to text automatically. '
        'Output is truncated to 2000 lines or 50KB (whichever is hit first). '
        'Use offset/limit or ranges for large files; auto mode returns a symbol '
        'outline for very large source files. Every text read returns a content '
        'version that can be passed to Edit as baseHash.'
    ),
    'input_schema': {
        'type': 'object',
        'properties': {
            'path': {
                'type': 'string',
                'description': 'Path to the file to read (relative or absolute)',
            },
            'offset': {
                'type': 'integer',
                'description': 'Line number to start reading from (1-indexed)',
            },
            'limit': {
                'type': 'integer',
                'description': 'Maximum number of lines to read',
            },
            'ranges': {
                'type': 'array',
                'description': 'Optional non-contiguous inclusive line ranges',
                'items': {
                    'type': 'object',
                    'properties': {
                        'start': {'type': 'integer', 'minimum': 1},
                        'end': {'type': 'integer', 'minimum': 1},
                    },
                    'required': ['start', 'end'],
                },
            },
            'mode': {
                'type': 'string',
                'enum': ['auto', 'full', 'outline'],
                'description': 'auto (default), full text, or compact symbol outline',
            },
            'lineNumbers': {
                'type': 'boolean',
                'description': 'Prefix returned text lines with 1-indexed line numbers',
            },
        },
        'required': ['path'],
    },
}


_LARGE_TEXT_BYTES = 2 * 1024 * 1024
_AUTO_OUTLINE_BYTES = 256 * 1024


def _source_outline(text: str, suffix: str) -> str:
    """Return a compact source/document outline with line locations."""
    entries: list[tuple[int, str]] = []
    if suffix == '.py':
        try:
            tree = ast.parse(text)
            for node in ast.walk(tree):
                if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    kind = 'class' if isinstance(node, ast.ClassDef) else 'def'
                    entries.append((node.lineno, f'{kind} {node.name}'))
        except SyntaxError:
            pass
    if not entries:
        patterns = (
            re.compile(r'^\s*(?:export\s+)?(?:async\s+)?(?:class|def|function|interface|type|enum|struct|trait)\s+([\w$]+)'),
            re.compile(r'^\s{0,3}(#{1,6})\s+(.+)$'),
        )
        for line_no, line in enumerate(text.splitlines(), 1):
            for pattern in patterns:
                match = pattern.match(line)
                if match:
                    label = match.group(2) if len(match.groups()) > 1 else line.strip()
                    entries.append((line_no, label.strip()))
                    break
    entries.sort(key=lambda item: item[0])
    if not entries:
        return '(No symbols or headings detected.)'
    return '\n'.join(f'{line_no:>6}  {label}' for line_no, label in entries[:1000])


def _stream_source_outline(target: Path, ctx: ToolContext) -> tuple[str, int]:
    """Outline a large file without retaining its complete contents."""
    symbol = re.compile(
        r'^\s*(?:export\s+)?(?:async\s+)?'
        r'(?:class|def|function|interface|type|enum|struct|trait)\s+([\w$]+)'
    )
    heading = re.compile(r'^\s{0,3}(#{1,6})\s+(.+)$')
    entries: list[str] = []
    total_lines = 0
    with target.open('r', encoding='utf-8', errors='replace') as handle:
        for line_no, line in enumerate(handle, 1):
            total_lines = line_no
            if ctx.cancel_event and ctx.cancel_event.is_set():
                raise InterruptedError('Read cancelled')
            match = symbol.match(line)
            label = match.group(1) if match else ''
            if not label:
                match = heading.match(line)
                label = match.group(2).strip() if match else ''
            if label and len(entries) < 1000:
                entries.append(f'{line_no:>6}  {label}')
    return ('\n'.join(entries) or '(No symbols or headings detected.)'), total_lines


def _normalized_ranges(params: dict) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    for item in params.get('ranges') or []:
        try:
            start = max(1, int(item.get('start', 1)))
            end = max(start, int(item.get('end', start)))
            ranges.append((start, end))
        except (AttributeError, TypeError, ValueError):
            continue
    return ranges


def _number_lines(lines: list[str], start_numbers: list[int]) -> str:
    return ''.join(
        f'{line_no:>6}\t{line}'
        for line_no, line in zip(start_numbers, lines, strict=True)
    )


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


def _read_office(target: Path, params: dict, ctx: ToolContext) -> ToolResult:
    """Extract text from XLSX/DOCX/PPTX (and reject legacy .xls/.doc/.ppt)."""
    from charon.tools.office_read import OfficeExtractError, extract_office_text

    try:
        text = extract_office_text(target)
    except OfficeExtractError as e:
        return ToolResult(content=f'Error: {e}', is_error=True)

    lines = text.splitlines(keepends=True)
    total_lines = len(lines)

    offset = max(0, int(params.get('offset', 1)) - 1)
    limit = int(params.get('limit', 0)) or ctx.max_output_lines
    selected = lines[offset:offset + limit]
    result_text = ''.join(selected)
    result_text, truncated = truncate_output(result_text, ctx.max_output_lines, ctx.max_output_bytes)

    meta = f'\n[{target.suffix.lstrip(".").upper()}: {target.name}, {total_lines} lines total]'
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

    # Office documents: XLSX/XLSM/DOCX/PPTX (legacy .xls/.doc/.ppt get a
    # conversion hint instead of mojibake from read_text)
    from charon.tools.office_read import LEGACY_SUFFIXES, OFFICE_SUFFIXES
    if target.suffix.lower() in OFFICE_SUFFIXES | set(LEGACY_SUFFIXES):
        return _read_office(target, params, ctx)

    from charon.tools.file_snapshots import file_version

    try:
        version = file_version(target)
        size = target.stat().st_size
    except Exception as e:
        return ToolResult(content=f'Error reading file: {e}', is_error=True)

    mode = str(params.get('mode') or 'auto').lower()
    ranges = _normalized_ranges(params)
    explicit_window = bool(ranges or 'offset' in params or 'limit' in params)
    if mode == 'auto' and size >= _AUTO_OUTLINE_BYTES and not explicit_window:
        mode = 'outline'

    try:
        if mode == 'outline':
            if size >= _LARGE_TEXT_BYTES:
                result_text, outline_lines = _stream_source_outline(target, ctx)
            else:
                text = target.read_text(encoding='utf-8', errors='replace')
                outline_lines = len(text.splitlines())
                result_text = _source_outline(text, target.suffix.lower())
            meta = f'\n[Outline of {target.name}; {outline_lines} lines]'
            return ToolResult(
                content=f'{result_text}{meta}\n[File version: {version}]',
                details={'path': str(target), 'version': version, 'mode': 'outline'},
            )

        offset = max(0, int(params.get('offset', 1)) - 1)
        limit = max(1, int(params.get('limit', 0)) or ctx.max_output_lines)
        selected: list[str] = []
        selected_numbers: list[int] = []

        if size < _LARGE_TEXT_BYTES:
            text = target.read_text(encoding='utf-8', errors='replace')
            lines = text.splitlines(keepends=True)
            total_lines = len(lines)
            if ranges:
                for start, end in ranges:
                    for index in range(start - 1, min(end, total_lines)):
                        selected.append(lines[index])
                        selected_numbers.append(index + 1)
            else:
                selected = lines[offset:offset + limit]
                selected_numbers = list(range(offset + 1, offset + 1 + len(selected)))
        else:
            total_lines = 0
            with target.open('r', encoding='utf-8', errors='replace') as handle:
                for line_no, line in enumerate(handle, 1):
                    total_lines = line_no
                    if ctx.cancel_event and ctx.cancel_event.is_set():
                        return ToolResult(content='Read cancelled.', is_error=True)
                    wanted = (
                        any(start <= line_no <= end for start, end in ranges)
                        if ranges
                        else offset < line_no <= offset + limit
                    )
                    if wanted:
                        selected.append(line)
                        selected_numbers.append(line_no)

        result_text = (
            _number_lines(selected, selected_numbers)
            if params.get('lineNumbers', False)
            else ''.join(selected)
        )
        result_text, truncated = truncate_output(
            result_text, ctx.max_output_lines, ctx.max_output_bytes,
        )
        if ranges:
            range_label = ', '.join(f'{start}-{end}' for start, end in ranges)
            meta = f'\n[Showing requested ranges {range_label} of {total_lines} lines]'
        else:
            shown_start = offset + 1
            shown_end = min(offset + len(selected), total_lines)
            meta = ''
            if truncated or offset > 0 or (offset + limit) < total_lines:
                meta = f'\n[Showing lines {shown_start}-{shown_end} of {total_lines}]'
                if truncated:
                    meta += f' (truncated to {ctx.max_output_bytes // 1000}KB limit)'
                if shown_end < total_lines:
                    meta += f'. Use offset={shown_end + 1} to continue.'
        return ToolResult(
            content=f'{result_text}{meta}\n[File version: {version}]',
            truncated=truncated,
            details={
                'path': str(target),
                'version': version,
                'total_lines': total_lines,
                'line_numbers': selected_numbers,
            },
        )
    except Exception as e:
        return ToolResult(content=f'Error reading file: {e}', is_error=True)


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
        from charon.tools.file_snapshots import atomic_write_text, file_version
        atomic_write_text(target, content)
        byte_count = len(content.encode('utf-8'))
        version = file_version(target)
        return ToolResult(
            content=(
                f'Successfully wrote {byte_count} bytes to {path_str}\n'
                f'[File version: {version}]'
            ),
            details={'path': str(target), 'bytes': byte_count, 'version': version},
        )
    except Exception as e:
        return ToolResult(content=f'Error writing file: {e}', is_error=True)


# -- Edit tool ----------------------------------------------------------------

EDIT_TOOL_DEF = {
    'name': 'Edit',
    'description': (
        'Atomically edit one or more exact text blocks or inclusive line ranges. '
        'Pass Read\'s baseHash to reject stale edits. All hunks are validated '
        'before any content is written.'
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
            'baseHash': {
                'type': 'string',
                'description': 'Optional file version returned by Read',
            },
            'edits': {
                'type': 'array',
                'minItems': 1,
                'description': 'Multiple atomic edits against the same original file',
                'items': {
                    'type': 'object',
                    'properties': {
                        'oldText': {'type': 'string'},
                        'newText': {'type': 'string'},
                        'startLine': {'type': 'integer', 'minimum': 1},
                        'endLine': {'type': 'integer', 'minimum': 1},
                    },
                    'required': ['newText'],
                    'anyOf': [
                        {'required': ['oldText']},
                        {'required': ['startLine', 'endLine']},
                    ],
                },
            },
        },
        'required': ['path'],
        'anyOf': [
            {'required': ['oldText', 'newText']},
            {'required': ['edits']},
        ],
    },
}


def _near_edit_match(content: str, old_text: str) -> str:
    """Locate the closest same-sized line window for actionable failures."""
    old_lines = old_text.splitlines() or [old_text]
    lines = content.splitlines()
    width = max(1, len(old_lines))
    best_ratio = 0.0
    best_line = 0
    best_text = ''
    for index in range(max(1, len(lines) - width + 1)):
        candidate = '\n'.join(lines[index:index + width])
        ratio = difflib.SequenceMatcher(None, old_text, candidate).ratio()
        if ratio > best_ratio:
            best_ratio = ratio
            best_line = index + 1
            best_text = candidate
    if best_ratio < 0.35 or not best_text:
        return ''
    preview = best_text[:300].replace('\n', '\\n')
    return f' Closest match ({best_ratio:.0%}) starts at line {best_line}: "{preview}".'


def execute_edit(params: dict, ctx: ToolContext) -> ToolResult:
    path_str = params.get('path', '')

    if not path_str:
        return ToolResult(content='Error: path is required', is_error=True)

    raw_edits = params.get('edits')
    if raw_edits is None:
        if not params.get('oldText'):
            return ToolResult(content='Error: oldText is required', is_error=True)
        if 'newText' not in params:
            return ToolResult(content='Error: newText is required', is_error=True)
        raw_edits = [{
            'oldText': params.get('oldText'),
            'newText': params.get('newText', ''),
        }]
    if not isinstance(raw_edits, list) or not raw_edits:
        return ToolResult(content='Error: edits must be a non-empty array', is_error=True)

    target = Path(path_str)
    if not target.is_absolute():
        target = ctx.project_root / target

    if not target.exists():
        return ToolResult(content=f'Error: file not found: {path_str}', is_error=True)

    try:
        from charon.tools.file_snapshots import atomic_write_text, file_version
        initial_version = file_version(target)
        content = target.read_text(encoding='utf-8')
    except Exception as e:
        return ToolResult(content=f'Error reading file: {e}', is_error=True)

    base_hash = str(params.get('baseHash') or '')
    if base_hash and base_hash != initial_version:
        return ToolResult(
            content=(
                f'Error: stale edit for {path_str}. Expected version '
                f'{base_hash}, current version is {initial_version}. Read the file again.'
            ),
            is_error=True,
            details={'expected_version': base_hash, 'current_version': initial_version},
        )

    line_starts = [0]
    for line in content.splitlines(keepends=True):
        line_starts.append(line_starts[-1] + len(line))
    line_count = len(line_starts) - 1
    spans: list[tuple[int, int, str, int]] = []
    for edit_index, edit in enumerate(raw_edits, 1):
        if not isinstance(edit, dict) or 'newText' not in edit:
            return ToolResult(
                content=f'Error: edit {edit_index} requires newText',
                is_error=True,
            )
        replacement = str(edit.get('newText', ''))
        old_text = edit.get('oldText')
        if isinstance(old_text, str) and old_text:
            count = content.count(old_text)
            if count == 0:
                preview = old_text[:100].replace('\n', '\\n')
                return ToolResult(
                    content=(
                        f'Error: oldText not found in {path_str} for edit {edit_index}. '
                        f'Text to find: "{preview}...".'
                        f'{_near_edit_match(content, old_text)}'
                    ),
                    is_error=True,
                )
            if count > 1:
                return ToolResult(
                    content=(
                        f'Error: oldText found {count} times in {path_str} '
                        f'for edit {edit_index}. Must match exactly once.'
                    ),
                    is_error=True,
                )
            start = content.index(old_text)
            end = start + len(old_text)
        else:
            try:
                start_line = int(edit['startLine'])
                end_line = int(edit['endLine'])
            except (KeyError, TypeError, ValueError):
                return ToolResult(
                    content=f'Error: edit {edit_index} requires oldText or startLine/endLine',
                    is_error=True,
                )
            if not (1 <= start_line <= end_line <= line_count):
                return ToolResult(
                    content=(
                        f'Error: edit {edit_index} line range {start_line}-{end_line} '
                        f'is outside 1-{line_count}'
                    ),
                    is_error=True,
                )
            start = line_starts[start_line - 1]
            end = line_starts[end_line]
        spans.append((start, end, replacement, edit_index))

    ordered = sorted(spans, key=lambda span: (span[0], span[1]))
    for previous, current in zip(ordered, ordered[1:], strict=False):
        if current[0] < previous[1]:
            return ToolResult(
                content=(
                    f'Error: edits {previous[3]} and {current[3]} overlap; '
                    'no changes were written.'
                ),
                is_error=True,
            )

    new_content = content
    for start, end, replacement, _ in reversed(ordered):
        new_content = new_content[:start] + replacement + new_content[end:]

    try:
        if file_version(target) != initial_version:
            return ToolResult(
                content=f'Error: {path_str} changed while preparing the edit. Read it again.',
                is_error=True,
            )
        atomic_write_text(target, new_content)
        final_version = file_version(target)
        return ToolResult(
            content=(
                f'Successfully edited {path_str} ({len(ordered)} atomic edit(s))\n'
                f'[File version: {final_version}]'
            ),
            details={
                'path': str(target),
                'edits': len(ordered),
                'previous_version': initial_version,
                'version': final_version,
            },
        )
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
            if _active_bash_abort.is_set() or (
                ctx.cancel_event is not None and ctx.cancel_event.is_set()
            ):
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
from charon.tools.pykernel_tool import PYKERNEL_TOOL_DEF, execute_pykernel
from charon.tools.refine_tool import REFINE_TOOL_DEF, execute_refine
from charon.tools.clarify_tool import CLARIFY_TOOL_DEF, execute_clarify

# Optional tools may legitimately be missing (uninstalled extras) — that is an
# ImportError and is only recorded to diagnostics. Any OTHER exception means
# the tool itself is broken; it is recorded here so status/listing surfaces
# (e.g. /tools) can show it instead of the tool silently vanishing.
FAILED_TOOL_IMPORTS: list[dict[str, str]] = []


def _optional_dependency_available(module_name: str) -> bool:
    """Return whether an optional dependency can actually be imported."""
    try:
        return importlib.util.find_spec(module_name) is not None
    except (ImportError, ValueError):
        return False


def _record_tool_import_failure(tool_name: str, exc: BaseException) -> None:
    if isinstance(exc, ImportError):
        _diag('tools', f'optional tool {tool_name} not loaded (missing dependency)', error=exc, tool=tool_name)
        return
    _diag('tools', f'tool {tool_name} failed to import and was dropped from the registry', error=exc, tool=tool_name)
    FAILED_TOOL_IMPORTS.append({'tool': tool_name, 'error': f'{type(exc).__name__}: {exc}'})


# Browser tool — optional, only loads if playwright is installed
# Suppress stdout/stderr during import (browser-use loads ML models noisily)
try:
    if not _optional_dependency_available('playwright'):
        raise ImportError("Browser requires the 'browser' extra (playwright).")
    import io as _io
    import contextlib as _cl
    with _cl.redirect_stdout(_io.StringIO()), _cl.redirect_stderr(_io.StringIO()):
        from charon.tools.browser_tool import BROWSER_TOOL_DEF, execute_browser
    _HAS_BROWSER = True
except Exception as _e:
    _HAS_BROWSER = False
    _record_tool_import_failure('Browser', _e)
from charon.tools.shade_tool import SHADE_TOOL_DEF, execute_spawn_shade, LIST_RETAINED_SHADES_TOOL_DEF, execute_list_retained_shades
from charon.tools.judge_loop_tool import JUDGE_LOOP_TOOL_DEF, execute_judge_loop
from charon.tools.tool_catalog import TOOL_CATALOG_DEF, execute_tool_catalog

# Recall tool — optional, only loads if sqlite-vec and sentence-transformers are installed
try:
    if not all(_optional_dependency_available(name) for name in ('sentence_transformers', 'sqlite_vec')):
        raise ImportError("Recall requires the 'memory' extra (sentence-transformers and sqlite-vec).")
    from charon.tools.recall_tool import RECALL_TOOL_DEF, execute_recall
    _HAS_RECALL = True
except Exception as _e:
    _HAS_RECALL = False
    _record_tool_import_failure('Recall', _e)

# Timeline tool — episodic + procedural memory; same deps as Recall
try:
    if not all(_optional_dependency_available(name) for name in ('sentence_transformers', 'sqlite_vec')):
        raise ImportError("Timeline requires the 'memory' extra (sentence-transformers and sqlite-vec).")
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

# Overseer tools — optional, only loads if charon.workspace + the tool contract are available
try:
    from charon.tools.overseer_tool import (
        OVERSEER_TOOL_DEFS as _OVERSEER_DEFS,
        OVERSEER_TOOL_EXECUTORS as _OVERSEER_EXECUTORS,
    )
    _HAS_OVERSEER = True
except Exception as _e:
    _HAS_OVERSEER = False
    _record_tool_import_failure('Overseer', _e)

# System map skill (skills/system-map) as a tool — optional, only loads if charon.workspace is available
try:
    from charon.tools.system_map_tool import (
        SYSTEM_MAP_TOOL_DEFS as _SYSTEM_MAP_DEFS,
        SYSTEM_MAP_TOOL_EXECUTORS as _SYSTEM_MAP_EXECUTORS,
    )
    _HAS_SYSTEM_MAP = True
except Exception as _e:
    _HAS_SYSTEM_MAP = False
    _record_tool_import_failure('SystemMap', _e)

ALL_TOOL_DEFS = [
    READ_TOOL_DEF, BASH_TOOL_DEF, EDIT_TOOL_DEF, WRITE_TOOL_DEF, TOOL_CATALOG_DEF,
    RUN_PROCESS_TOOL_DEF, PROCESS_STATUS_TOOL_DEF, PROCESS_LOGS_TOOL_DEF, STOP_PROCESS_TOOL_DEF,
    USER_MODEL_TOOL_DEF, PROJECT_KNOWLEDGE_TOOL_DEF,
    HTTP_TOOL_DEF, GIT_TOOL_DEF,
    SHADE_TOOL_DEF, LIST_RETAINED_SHADES_TOOL_DEF, SPAWN_BATCH_TOOL_DEF, JUDGE_LOOP_TOOL_DEF,
    SEARCH_TOOL_DEF, WEB_TOOL_DEF, PAPER_TOOL_DEF, SOURCE_DISCOVERY_TOOL_DEF, RESEARCH_TOOL_DEF, X_TOOL_DEF,
    CRON_TOOL_DEF, SKILLS_TOOL_DEF, EXECUTE_CODE_TOOL_DEF, CLARIFY_TOOL_DEF,
    PYKERNEL_TOOL_DEF, REFINE_TOOL_DEF,
] + ([BROWSER_TOOL_DEF] if _HAS_BROWSER else []) + ([RECALL_TOOL_DEF] if _HAS_RECALL else []) + ([TIMELINE_TOOL_DEF] if _HAS_TIMELINE else []) + (_FLEET_DEFS if _HAS_FLEET else []) + (_OVERSEER_DEFS if _HAS_OVERSEER else []) + (_SYSTEM_MAP_DEFS if _HAS_SYSTEM_MAP else [])

TOOL_EXECUTORS: dict[str, Callable[[dict, ToolContext], ToolResult]] = {
    'Read': execute_read,
    'Bash': execute_bash,
    'Edit': execute_edit,
    'Write': execute_write,
    'ToolCatalog': execute_tool_catalog,
    'RunProcess': execute_run_process,
    'ProcessStatus': execute_process_status,
    'ProcessLogs': execute_process_logs,
    'StopProcess': execute_stop_process,
    'UserModel': execute_user_model,
    'ProjectKnowledge': execute_project_knowledge,
    'Http': execute_http,
    'Git': execute_git,
    'SpawnShade': execute_spawn_shade,
    'ListRetainedShades': execute_list_retained_shades,
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
    'PyKernel': execute_pykernel,
    'Refine': execute_refine,
    'Clarify': execute_clarify,
    **(({'Browser': execute_browser} if _HAS_BROWSER else {})),
    **(({'Recall': execute_recall} if _HAS_RECALL else {})),
    **(({'Timeline': execute_timeline} if _HAS_TIMELINE else {})),
    **(({'FleetStatus': execute_fleet_status, 'FleetSend': execute_fleet_send, 'FleetHistory': execute_fleet_history, 'FleetOnboard': execute_fleet_onboard} if _HAS_FLEET else {})),
    **((_OVERSEER_EXECUTORS if _HAS_OVERSEER else {})),
    **((_SYSTEM_MAP_EXECUTORS if _HAS_SYSTEM_MAP else {})),
}


# ── Interactive approval ──────────────────────────────────────────────

APPROVAL_TIMEOUT_SECONDS = 60.0


@dataclass
class _PendingApproval:
    event: threading.Event
    approved: bool | None = None


_approval_lock = threading.Lock()
_approval_pending: dict[str, _PendingApproval] = {}
_approval_callback: Callable[[dict[str, Any]], None] | None = None


def set_approval_callback(callback: Callable[[dict[str, Any]], None] | None) -> None:
    """Set the backend event sink for interactive approval lifecycle events.

    The callback receives complete ``approval_request`` and
    ``approval_resolved`` event dictionaries. Request/response correlation is
    always performed with ``approval_id``.
    """
    global _approval_callback
    with _approval_lock:
        _approval_callback = callback


def respond_to_approval(
    approval_id: str | bool,
    approved: bool | None = None,
) -> bool:
    """Resolve exactly one pending approval request.

    The one-argument boolean form is retained for compatibility with an older
    TUI, but is accepted only when exactly one request is pending. It can never
    cross-release concurrent requests.
    """
    if approved is None and isinstance(approval_id, bool):
        approved = approval_id
        with _approval_lock:
            if len(_approval_pending) != 1:
                return False
            target_id = next(iter(_approval_pending))
    else:
        target_id = str(approval_id or '').strip()
        approved = bool(approved)

    if not target_id:
        return False

    with _approval_lock:
        pending = _approval_pending.get(target_id)
        if pending is None:
            return False
        pending.approved = bool(approved)
        pending.event.set()
    return True


def _approval_event_sink() -> Callable[[dict[str, Any]], None] | None:
    with _approval_lock:
        return _approval_callback


def _emit_approval_lifecycle(event: dict[str, Any]) -> bool:
    callback = _approval_event_sink()
    if callback is None:
        return False
    callback(event)
    return True


def _request_interactive_approval(
    tool_name: str,
    params: dict,
    risk: str,
    reason: str,
    ctx: ToolContext,
    *,
    timeout_seconds: float = APPROVAL_TIMEOUT_SECONDS,
) -> tuple[bool, str]:
    """Request a targeted approval and wait only for that request's response.

    Returns ``(approved, resolution)`` where resolution is ``approved``,
    ``denied``, ``timeout``, or ``unavailable``.
    """
    # Without an interactive response channel, a gated action must fail closed.
    if _approval_event_sink() is None:
        return False, 'unavailable'

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

    approval_id = f'approval-{uuid.uuid4().hex}'
    pending = _PendingApproval(event=threading.Event())
    with _approval_lock:
        _approval_pending[approval_id] = pending

    request_event = {
        'type': 'approval_request',
        'approval_id': approval_id,
        'tool': tool_name,
        'params': params_summary,
        'risk': risk,
        'reason': reason,
        'session_id': ctx.agent_id or 'default',
        'agent_id': ctx.agent_id or '',
        'operation_id': ctx.operation_id or '',
        'operation_domain': ctx.operation_domain or '',
        'work_unit_id': ctx.work_unit_id or '',
        'operation_role': ctx.operation_role or '',
        'runtime_role': ctx.runtime_role or '',
        'timeout_seconds': max(0.0, float(timeout_seconds)),
    }

    try:
        if not _emit_approval_lifecycle(request_event):
            with _approval_lock:
                _approval_pending.pop(approval_id, None)
            return False, 'unavailable'
    except Exception as exc:
        with _approval_lock:
            _approval_pending.pop(approval_id, None)
        _diag('tools', 'approval request callback failed; gated tool call denied', error=exc, tool=tool_name)
        return False, 'unavailable'

    responded = pending.event.wait(timeout=max(0.0, float(timeout_seconds)))
    with _approval_lock:
        current = _approval_pending.pop(approval_id, None)
        approved = bool(current.approved) if current is not None else False

    resolution = 'approved' if responded and approved else 'denied' if responded else 'timeout'
    try:
        _emit_approval_lifecycle({
            'type': 'approval_resolved',
            'approval_id': approval_id,
            'approved': approved if responded else False,
            'resolution': resolution,
            'tool': tool_name,
            'session_id': ctx.agent_id or 'default',
            'agent_id': ctx.agent_id or '',
            'operation_id': ctx.operation_id or '',
            'work_unit_id': ctx.work_unit_id or '',
        })
    except Exception as exc:
        _diag('tools', 'approval resolution callback failed; TUI may retain a stale prompt', error=exc, approval_id=approval_id)

    return approved if responded else False, resolution


def _path_within(target: Path, entry: str, root: Path) -> bool:
    """True when `target` is `entry` or sits beneath it. `entry` may be relative."""
    e = entry.strip()
    if not e:
        return False
    base_path = Path(e).expanduser()
    if not base_path.is_absolute():
        base_path = root / e.strip('/')
    try:
        base = str(base_path.resolve())
    except Exception:
        base = str(base_path)
    t = str(target)
    return t == base or t.startswith(base + os.sep)


def _check_shell_scope(command: str, ctx: ToolContext) -> str | None:
    """Apply the shade's scope and frozen lists to a shell command.

    Bash used to bypass scope entirely on the grounds that shell cannot be
    analysed reliably. It cannot be analysed *perfectly*, but the common write
    forms are readable, and a command that genuinely cannot be read is refused
    rather than waved through — an unreadable command is the case most likely
    to be an escape, not the case most likely to be benign.
    """
    from charon.tools.shell_scope import analyze_write_targets

    analysis = analyze_write_targets(command, ctx.project_root)

    if not analysis.is_analyzable:
        return (
            f'Scope violation: this shade is restricted to '
            f'[{", ".join(ctx.scope or ctx.frozen or [])}], and the command cannot be '
            f'checked against that restriction ({analysis.unanalyzable}). '
            f'Rewrite it so its write targets are literal paths, or use Write/Edit.'
        )

    for target in analysis.targets:
        if ctx.frozen and any(_path_within(target, e, ctx.project_root) for e in ctx.frozen):
            return (
                f'Frozen-path violation: the command writes "{target}", which is inside '
                f'a frozen path [{", ".join(ctx.frozen)}] that must not be modified.'
            )
        if ctx.scope and not any(_path_within(target, e, ctx.project_root) for e in ctx.scope):
            return (
                f'Scope violation: the command writes "{target}", outside allowed scope '
                f'[{", ".join(ctx.scope)}]. This shade is restricted to modifying files '
                f'within its contract scope.'
            )
    return None


# git actions that only read; everything else can move files the shade does not own.
_GIT_READ_ONLY_ACTIONS = {'status', 'diff', 'log', 'branch'}


def _check_git_scope(params: dict, ctx: ToolContext) -> str | None:
    """Apply scope to the Git tool.

    Git was previously allowed wholesale because "git operates on the whole
    repo" — which is the reason to gate it, not to exempt it. Reads stay open;
    actions that rewrite the working tree or stage paths the shade does not own
    are refused while a scope is in force.
    """
    action = str(params.get('action', '')).strip().lower()
    if action in _GIT_READ_ONLY_ACTIONS or not action:
        return None
    if not ctx.scope:
        return None

    if action == 'add':
        files = params.get('files') or ['.']
        for f in files:
            target = Path(f)
            if not target.is_absolute():
                target = ctx.project_root / f
            try:
                target = target.resolve()
            except Exception:
                pass
            if not any(_path_within(target, e, ctx.project_root) for e in ctx.scope):
                return (
                    f'Scope violation: git add "{f}" stages a path outside allowed scope '
                    f'[{", ".join(ctx.scope)}]. Stage only files within the contract scope.'
                )
        return None

    if action in ('checkout', 'stash'):
        return (
            f'Scope violation: git {action} rewrites the working tree across the whole '
            f'repository, which would affect files outside this shade\'s scope '
            f'[{", ".join(ctx.scope)}] and discard work belonging to other agents.'
        )

    return None


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
        return _check_shell_scope(params.get('command', ''), ctx)
    elif name in ('Git',):
        return _check_git_scope(params, ctx)

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

    # Scope is checked above and now covers Bash and Git, but it only answers
    # "where" — approval still answers "whether". A destructive command aimed
    # squarely inside a shade's own scope is in-contract and still gated here.
    try:
        from charon.infra.tool_approval import needs_approval, approve_tool_for_session
        session_id = ctx.agent_id or 'default'
        needs, risk, reason = needs_approval(
            name,
            params,
            session_id=session_id,
            state_dir=ctx.state_dir,
            operation_domain=ctx.operation_domain,
        )
        if ctx.scope and risk != 'dangerous':
            needs = False
        if needs and session_id != 'default':
            needs2, _, _ = needs_approval(
                name,
                params,
                session_id='default',
                state_dir=ctx.state_dir,
                operation_domain=ctx.operation_domain,
            )
            if not needs2:
                needs = False
        if needs:
            approved, resolution = _request_interactive_approval(
                name,
                params,
                risk,
                reason,
                ctx,
            )
            if not approved:
                suffix = {
                    'timeout': 'approval timed out',
                    'unavailable': 'approval unavailable',
                }.get(resolution, 'user denied')
                return ToolResult(
                    content=f'Blocked: {reason} ({suffix})',
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
