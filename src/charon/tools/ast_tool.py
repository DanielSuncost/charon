"""Bounded ast-grep structural search and single-file, explicitly gated rewrites.

Only the read-only ``run --stdin`` CLI is used. Preview tokens are process-local
HMACs, not approvals; apply always delegates the write to the regular Edit tool.
No ast-grep installation, directory traversal, rule/config loading, or CLI fix
flags are exposed. UTF-8 byte ranges are validated against the scanned snapshot.
"""
from __future__ import annotations

import difflib
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import threading
import time
from typing import Any

from charon.tools import ToolContext, ToolResult

MAX_FILES = 20
MAX_FILE_BYTES = 1_048_576
MAX_TOTAL_BYTES = 2_097_152
MAX_OUTPUT_BYTES = 4_194_304
MAX_MATCHES = 1000
MAX_TEMPLATE_BYTES = 65_536
_TOKEN_KEY = secrets.token_bytes(32)
_META = re.compile(r'\$\$\$[A-Z_][A-Z_0-9]*|\$[A-Z_][A-Z_0-9]*')

AST_TOOL_DEF = {
    'name': 'Ast',
    'description': (
        'Structural, multi-language search with an installed ast-grep executable. '
        'Supply exactly one of path or paths, plus language and AST pattern (e.g. foo($A)). '
        'Search is read-only; preview computes a single-file diff and previewToken. '
        'Apply requires the same path, language, pattern, replacement and previewToken; '
        'it rejects stale files and delegates to the normal approved Edit tool. '
        'Replacement supports named $VAR / $$$VAR captures (no transforms). '
        'No directory scans, install, shell commands, or multi-file apply.'
    ),
    'input_schema': {
        'type': 'object',
        'properties': {
            'action': {'type': 'string', 'enum': ['search', 'preview', 'apply'], 'default': 'search'},
            'path': {'type': 'string', 'description': 'Explicit regular file, relative to project root or inside it.'},
            'paths': {'type': 'array', 'items': {'type': 'string'}, 'minItems': 1, 'maxItems': MAX_FILES},
            'language': {'type': 'string', 'description': 'ast-grep built-in language, e.g. python, javascript, typescript, rust.'},
            'pattern': {'type': 'string', 'description': 'ast-grep structural pattern, not a text regex.'},
            'replacement': {'type': 'string', 'description': 'Preview/apply template; named $VAR and $$$VAR only. Empty means delete.'},
            'previewToken': {'type': 'string', 'description': 'Token from preview in this running Charon process; not approval.'},
            'maxMatches': {'type': 'integer', 'minimum': 1, 'maximum': MAX_MATCHES, 'default': 100},
            'timeout': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 120, 'default': 20},
        },
        'required': ['language', 'pattern'],
        'additionalProperties': False,
        # Enforced by _paths before file access; Claude rejects top-level oneOf.
    },
}


class _AstError(ValueError):
    pass


def _check_cancel(ctx: ToolContext, deadline: float) -> None:
    if ctx.cancel_event is not None and ctx.cancel_event.is_set():
        raise _AstError('Ast cancelled; no changes were written.')
    if time.monotonic() >= deadline:
        raise _AstError('Ast timed out; no changes were written.')


def _run(argv: list[str], source: bytes, ctx: ToolContext, deadline: float) -> tuple[int, bytes, bytes]:
    """Drain both pipes with a hard combined cap, while allowing cancellation."""
    _check_cancel(ctx, deadline)
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, shell=False, cwd=ctx.project_root)
    chunks: list[list[bytes]] = [[], []]
    lock = threading.Lock()
    overflow = threading.Event()
    total = 0

    def drain(stream: Any, index: int) -> None:
        nonlocal total
        try:
            while chunk := stream.read1(65536):
                with lock:
                    total += len(chunk)
                    if total > MAX_OUTPUT_BYTES:
                        overflow.set()
                        return
                    chunks[index].append(chunk)
        finally:
            stream.close()

    def feed() -> None:
        try:
            proc.stdin.write(source)
            proc.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        finally:
            proc.stdin.close()

    threads = [threading.Thread(target=drain, args=(proc.stdout, 0), daemon=True),
               threading.Thread(target=drain, args=(proc.stderr, 1), daemon=True),
               threading.Thread(target=feed, daemon=True)]
    for thread in threads:
        thread.start()
    try:
        while proc.poll() is None or any(thread.is_alive() for thread in threads):
            _check_cancel(ctx, deadline)
            if overflow.is_set():
                raise _AstError('ast-grep output exceeds the byte cap; narrow the pattern/files.')
            time.sleep(0.01)
        if overflow.is_set():
            raise _AstError('ast-grep output exceeds the byte cap; narrow the pattern/files.')
        return proc.returncode, b''.join(chunks[0]), b''.join(chunks[1])
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        for thread in threads:
            thread.join(timeout=1)


def _executable(ctx: ToolContext, deadline: float) -> str:
    for name in ('ast-grep', 'sg'):
        executable = shutil.which(name)
        if executable:
            code, out, err = _run([executable, '--version'], b'', ctx, deadline)
            if code == 0 and re.match(rb'^ast-grep\s+\d', (out + err).strip()):
                return executable
    raise _AstError(
        'No validated ast-grep executable found on PATH. Install ast-grep separately '
        'using its official instructions, then retry. Nothing was installed or modified. '
        'An unrelated sg executable is not supported.'
    )


def _paths(params: dict, ctx: ToolContext) -> list[Path]:
    if ('path' in params) == ('paths' in params):
        raise _AstError('Supply exactly one of path or paths (explicit files only).')
    raw = [params['path']] if 'path' in params else params['paths']
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_FILES:
        raise _AstError(f'Expected 1-{MAX_FILES} explicit files.')
    root = ctx.project_root.resolve(strict=True)
    result = []
    for item in raw:
        if not isinstance(item, str) or not item or '\x00' in item:
            raise _AstError('Every path must be a nonempty file path.')
        lexical = Path(item)
        lexical = lexical if lexical.is_absolute() else root / lexical
        target = lexical.resolve(strict=True)
        if not target.is_relative_to(root):
            raise _AstError(f'Path escapes project root (including symlinks): {item}')
        if not stat.S_ISREG(target.stat().st_mode):
            raise _AstError(f'Expected a regular file, not a directory or special file: {item}')
        if target in result:
            raise _AstError(f'Duplicate file: {item}')
        result.append(target)
    return result


def _snapshot(path: Path, root: Path) -> bytes:
    # Check again at each snapshot. O_NOFOLLOW prevents a final-component link swap.
    if path.resolve(strict=True) != path or not path.is_relative_to(root.resolve()):
        raise _AstError('Path changed or escapes project root.')
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    with os.fdopen(fd, 'rb') as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
            raise _AstError(f'Not a regular file or exceeds {MAX_FILE_BYTES} byte cap: {path}')
        source = handle.read(MAX_FILE_BYTES + 1)
    if len(source) > MAX_FILE_BYTES:
        raise _AstError('File exceeds byte cap.')
    source.decode('utf-8')
    if b'\x00' in source:
        raise _AstError('Only UTF-8 text files without NUL bytes are supported.')
    return source


def _write_scope(path: Path, ctx: ToolContext) -> None:
    from charon.tools import _path_within
    if ctx.frozen and any(_path_within(path, entry, ctx.project_root) for entry in ctx.frozen):
        raise _AstError(f'Frozen-path violation: {path}')
    if ctx.scope and not any(_path_within(path, entry, ctx.project_root) for entry in ctx.scope):
        raise _AstError(f'Scope violation: {path} is outside allowed scope.')


def _span(node: dict, source: bytes) -> tuple[int, int]:
    try:
        span = node['range']['byteOffset']
        start, end = span['start'], span['end']
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(source):
            raise ValueError('invalid byte range')
        text = source[start:end].decode('utf-8')
        # Decode the prefix too: a start inside a UTF-8 sequence must be rejected.
        source[:start].decode('utf-8')
        if node['text'] != text:
            raise ValueError('match text does not agree with snapshot')
        return start, end
    except (KeyError, TypeError, UnicodeError, ValueError) as exc:
        raise _AstError(f'Invalid ast-grep UTF-8 byte range/text: {exc}') from exc


def _replacement(template: str, match: dict, source: bytes) -> bytes:
    variables = match.get('metaVariables', {})
    if not isinstance(variables, dict):
        raise _AstError('Invalid ast-grep metaVariables.')

    def replace(found: re.Match) -> str:
        token = found.group()
        multi = token.startswith('$$$')
        name = token[3:] if multi else token[1:]
        group = variables.get('multi' if multi else 'single', {})
        if not isinstance(group, dict) or name not in group:
            raise _AstError(f'Unbound replacement capture: {token}')
        if multi:
            nodes = group[name]
            if not isinstance(nodes, list):
                raise _AstError(f'Invalid variadic capture: {token}')
            spans = [_span(node, source) for node in nodes]
            if not spans:
                return ''
            if any(b[0] < a[1] for a, b in zip(spans, spans[1:], strict=False)):
                raise _AstError(f'Unordered/overlapping variadic capture: {token}')
            start, end = spans[0][0], spans[-1][1]
        else:
            start, end = _span(group[name], source)
        match_start, match_end = _span(match, source)
        if not match_start <= start < end <= match_end:
            raise _AstError(f'Capture outside match: {token}')
        return source[start:end].decode('utf-8')

    # Deliberately no ast-grep transforms, indentation rewriting or $$ anonymous captures.
    if re.search(r'(?<!\$)\$\$(?!\$)', template):
        raise _AstError('Replacement supports $VAR and $$$VAR, not $$ captures.')
    pieces = []
    cursor = size = 0
    for found in _META.finditer(template):
        for piece in (template[cursor:found.start()], replace(found)):
            encoded = piece.encode('utf-8')
            size += len(encoded)
            if size > MAX_FILE_BYTES:
                raise _AstError('Expanded replacement exceeds the byte cap.')
            pieces.append(encoded)
        cursor = found.end()
    tail = template[cursor:].encode('utf-8')
    if size + len(tail) > MAX_FILE_BYTES:
        raise _AstError('Expanded replacement exceeds the byte cap.')
    return b''.join([*pieces, tail])


def _scan(executable: str, source: bytes, params: dict, ctx: ToolContext,
          deadline: float, remaining: int) -> list[dict]:
    argv = [executable, 'run', '--pattern', params['pattern'], '--lang', params['language'],
            '--json=compact', '--stdin']
    code, out, err = _run(argv, source, ctx, deadline)
    if code not in (0, 1):
        raise _AstError(f'ast-grep failed ({code}): {err.decode("utf-8", errors="replace")[:2000]}')
    # Exit 1 is the CLI's no-match result. Any diagnostic is conservatively an error.
    if err.strip():
        raise _AstError(f'ast-grep diagnostic: {err.decode("utf-8", errors="replace")[:2000]}')
    matches = json.loads(out or b'[]')
    if not isinstance(matches, list) or any(not isinstance(m, dict) for m in matches):
        raise _AstError('ast-grep did not return a JSON match array.')
    if len(matches) > remaining:
        raise _AstError('Match cap exceeded; narrow the pattern/files. No partial rewrite is offered.')
    for match in matches:
        _span(match, source)
    return matches


def _token(ctx: ToolContext, path: Path, params: dict, source: bytes, proposed: bytes) -> str:
    bound = [ctx.agent_id, str(ctx.project_root.resolve()), str(path), params['language'],
             params['pattern'], params['replacement'], hashlib.sha256(source).hexdigest(),
             hashlib.sha256(proposed).hexdigest()]
    return hmac.new(_TOKEN_KEY, json.dumps(bound, ensure_ascii=True).encode(), hashlib.sha256).hexdigest()


def _result(data: dict, ctx: ToolContext) -> ToolResult:
    content = json.dumps(data, ensure_ascii=False, indent=2)
    # Keep details consistent with visible bounded output, not an unbounded second channel.
    cap = max(1, min(ctx.max_output_bytes, 50_000))
    encoded = content.encode('utf-8')
    if len(encoded) > cap or len(content.splitlines()) > ctx.max_output_lines:
        content = '\n'.join(content.splitlines()[:ctx.max_output_lines])
        content = content.encode('utf-8')[:cap].decode('utf-8', errors='ignore')
        content += '\n[Output truncated; narrow the search for full results.]'
        return ToolResult(content=content, truncated=True)
    return ToolResult(content=content, details=data)


def execute_ast(params: dict, ctx: ToolContext) -> ToolResult:
    """Execute a structural search, read-only rewrite preview, or explicit apply."""
    try:
        if not isinstance(params, dict) or set(params) - set(AST_TOOL_DEF['input_schema']['properties']):
            raise _AstError('Unknown Ast parameters; raw CLI flags/configs are not accepted.')
        action = params.get('action', 'search')
        if action not in ('search', 'preview', 'apply'):
            raise _AstError('action must be search, preview or apply.')
        for key in ('pattern', 'language'):
            if not isinstance(params.get(key), str) or not params[key] or len(params[key].encode()) > MAX_TEMPLATE_BYTES:
                raise _AstError(f'{key} must be a nonempty bounded string.')
        if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}', params['language']):
            raise _AstError('language must be an ast-grep language identifier, not flags or a path.')
        maximum = params.get('maxMatches', 100)
        if type(maximum) is not int or not 1 <= maximum <= MAX_MATCHES:
            raise _AstError(f'maxMatches must be between 1 and {MAX_MATCHES}.')
        timeout = params.get('timeout', 20)
        if type(timeout) not in (int, float) or not 0 < timeout <= 120:
            raise _AstError('timeout must be greater than 0 and at most 120 seconds.')
        deadline = time.monotonic() + timeout
        _check_cancel(ctx, deadline)
        paths = _paths(params, ctx)
        if action != 'search':
            if len(paths) != 1:
                raise _AstError('Rewrite preview/apply requires exactly one file.')
            if not isinstance(params.get('replacement'), str) or len(params['replacement'].encode()) > MAX_TEMPLATE_BYTES:
                raise _AstError('replacement must be an explicit bounded string (empty is allowed).')
        if action == 'apply':
            if not isinstance(params.get('previewToken'), str) or not re.fullmatch('[0-9a-f]{64}', params['previewToken']):
                raise _AstError('Apply requires a previewToken from a prior preview; preview is not approval.')
            _write_scope(paths[0], ctx)
        sources = []
        total_bytes = 0
        for path in paths:
            _check_cancel(ctx, deadline)
            source = _snapshot(path, ctx.project_root)
            total_bytes += len(source)
            if total_bytes > MAX_TOTAL_BYTES:
                raise _AstError('Total input byte cap exceeded; supply fewer/smaller files.')
            sources.append(source)
        executable = _executable(ctx, deadline)
        results = []
        all_matches = []
        for path, source in zip(paths, sources, strict=True):
            _check_cancel(ctx, deadline)
            matches = _scan(executable, source, params, ctx, deadline, maximum - len(all_matches))
            all_matches.extend(matches)
            results.append({'path': str(path.relative_to(ctx.project_root.resolve())),
                            'baseHash': hashlib.sha256(source).hexdigest(),
                            'matches': [{'text': m['text'], 'range': m['range']} for m in matches]})
        if action == 'search':
            return _result({'action': action, 'matchCount': len(all_matches), 'files': results}, ctx)
        path, source = paths[0], sources[0]
        ordered = sorted([(*_span(m, source), m) for m in all_matches],
                         key=lambda item: (item[0], item[1]))
        if any(current[0] < previous[1] for previous, current in zip(ordered, ordered[1:], strict=False)):
            raise _AstError('Overlapping AST matches; narrow the pattern. No changes were written.')
        size = len(source) - sum(end - start for start, end, _ in ordered)
        spans = []
        for start, end, match in ordered:
            _check_cancel(ctx, deadline)
            replacement = _replacement(params['replacement'], match, source)
            size += len(replacement)
            if size > MAX_FILE_BYTES:
                raise _AstError('Rewritten file would exceed the byte cap.')
            spans.append((start, end, replacement))
        pieces = []
        cursor = 0
        for start, end, replacement in spans:
            pieces.extend([source[cursor:start], replacement])
            cursor = end
        proposed = b''.join([*pieces, source[cursor:]])
        token = _token(ctx, path, params, source, proposed)
        if action == 'preview':
            diff = ''.join(difflib.unified_diff(source.decode().splitlines(keepends=True),
                                              proposed.decode().splitlines(keepends=True),
                                              fromfile=str(path), tofile=str(path)))
            return _result({'action': action, 'path': results[0]['path'], 'matchCount': len(spans),
                            'baseHash': hashlib.sha256(source).hexdigest(), 'previewToken': token,
                            'approval': 'Preview is read-only, not approval. Apply uses the normal Edit gate.',
                            'diff': diff}, ctx)
        if not hmac.compare_digest(token, params['previewToken']):
            raise _AstError('Stale or mismatched previewToken (agent/project/path/pattern/replacement/content changed). Preview again.')
        _check_cancel(ctx, deadline)
        # Re-resolve supplied aliases as well as the canonical path before dispatching.
        if _paths(params, ctx) != paths or _snapshot(path, ctx.project_root) != source:
            raise _AstError('Stale preview: path or file content changed. Preview again.')
        _write_scope(path, ctx)
        if proposed == source:
            return _result({'action': action, 'changed': False, 'matchCount': len(spans)}, ctx)
        from charon.tools import execute_tool
        # Edit reads universal newlines. Replace the entire snapshot exactly once;
        # newText retains all original byte-level newline choices outside matches.
        old_text = source.decode().replace('\r\n', '\n').replace('\r', '\n')
        return execute_tool('Edit', {'path': str(path), 'oldText': old_text,
                                    'newText': proposed.decode(),
                                    'baseHash': hashlib.sha256(source).hexdigest()}, ctx)
    except (ValueError, TypeError, KeyError, OSError, RuntimeError) as exc:
        return ToolResult(content=f'Ast error: {exc}', is_error=True)


__all__ = ['AST_TOOL_DEF', 'execute_ast']
