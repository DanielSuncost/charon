"""Structural AST tool tests: mocked CLI contract, real Edit gate and optional CLI smoke."""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
import shutil
import sys
import threading
import time

import pytest

import charon.tools as tools
from charon.tools import ToolContext
from charon.tools import ast_tool as ast


def node(source: str, text: str, *, start: int | None = None) -> dict:
    raw = source.encode()
    begin = raw.index(text.encode()) if start is None else start
    return {'text': text, 'range': {'byteOffset': {'start': begin, 'end': begin + len(text.encode())},
                                   'start': {'line': 0, 'column': begin},
                                   'end': {'line': 0, 'column': begin + len(text.encode())}}}


@pytest.fixture
def ctx(tmp_path):
    return ToolContext(project_root=tmp_path, agent_id='ast-test')


@pytest.fixture
def cli(monkeypatch):
    calls = []
    state = {'matches': [], 'version': b'ast-grep 0.40.0\n', 'code': 0, 'stderr': b''}
    monkeypatch.setattr(ast.shutil, 'which', lambda name: '/mock/' + name)

    def run(argv, source, ctx, deadline):
        calls.append((argv, source))
        if argv[-1] == '--version':
            return 0, state['version'], b''
        matches = state['matches'](source) if callable(state['matches']) else state['matches']
        return state['code'], json.dumps(matches).encode(), state['stderr']

    monkeypatch.setattr(ast, '_run', run)
    return state, calls


def params(**kwargs):
    return {'path': 'sample.py', 'language': 'python', 'pattern': 'foo($A)', **kwargs}


def prepare(ctx, cli, text='foo(x)\n'):
    path = ctx.project_root / 'sample.py'
    path.write_bytes(text.encode())
    match = node(text, 'foo(x)')
    match['metaVariables'] = {'single': {'A': node(text, 'x')}}
    cli[0]['matches'] = [match]
    return path


def preview(ctx, **kwargs):
    return ast.execute_ast(params(**{'action': 'preview', 'replacement': 'bar($A)', **kwargs}), ctx)


def apply(ctx, result, **kwargs):
    return ast.execute_ast(params(**{'action': 'apply', 'replacement': 'bar($A)',
                                    'previewToken': result.details['previewToken'], **kwargs}), ctx)


def test_schema_and_default_readonly(ctx, cli):
    path = prepare(ctx, cli)
    result = ast.execute_ast(params(), ctx)
    assert ast.AST_TOOL_DEF['name'] == 'Ast'
    assert not result.is_error
    assert result.details['matchCount'] == 1
    assert path.read_text() == 'foo(x)\n'
    argv, source = cli[1][-1]
    assert source == b'foo(x)\n'
    assert argv == ['/mock/ast-grep', 'run', '--pattern', 'foo($A)', '--lang', 'python', '--json=compact', '--stdin']
    assert not any(flag in argv for flag in ['--rewrite', '--update-all', '-U', '--config'])


def test_preview_apply_unicode_and_gated_edit(ctx, cli, monkeypatch):
    path = prepare(ctx, cli, '# café 🦀\nfoo(x)\n')
    original = path.read_bytes()
    seen = []

    def dispatch(name, args, context):
        seen.append((name, args))
        assert name == 'Edit'
        assert args['baseHash'] == hashlib.sha256(original).hexdigest()
        return tools.execute_edit(args, context)

    monkeypatch.setattr(tools, 'execute_tool', dispatch)
    result = preview(ctx)
    assert not result.is_error
    assert '-foo(x)' in result.details['diff'] and '+bar(x)' in result.details['diff']
    assert path.read_bytes() == original
    assert seen == []
    applied = apply(ctx, result)
    assert not applied.is_error
    assert path.read_bytes() == '# café 🦀\nbar(x)\n'.encode()
    assert len(seen) == 1


def test_real_approval_denial_does_not_write(ctx, cli, monkeypatch):
    import charon.infra.tool_approval as approval
    path = prepare(ctx, cli)
    result = preview(ctx)
    requests = []
    monkeypatch.setattr(approval, 'needs_approval', lambda *a, **k: (True, 'write', 'test approval'))

    def deny(*args):
        requests.append(args)
        return False, 'denied'

    monkeypatch.setattr(tools, '_request_interactive_approval', deny)
    rejected = apply(ctx, result)
    assert rejected.is_error and 'Blocked' in rejected.content
    assert len(requests) == 1 and requests[0][0] == 'Edit'
    assert path.read_text() == 'foo(x)\n'


@pytest.mark.parametrize('changed', ['agent', 'project', 'pattern', 'replacement', 'language', 'path', 'content'])
def test_token_binding_and_staleness(ctx, cli, monkeypatch, changed):
    path = prepare(ctx, cli)
    result = preview(ctx)
    kwargs = {}
    if changed == 'agent':
        ctx = replace(ctx, agent_id='other-agent')
    elif changed == 'project':
        other = ctx.project_root / 'other'
        other.mkdir()
        (other / 'sample.py').write_text('foo(x)\n')
        ctx = replace(ctx, project_root=other)
    elif changed == 'path':
        (ctx.project_root / 'other.py').write_text('foo(x)\n')
        kwargs['path'] = 'other.py'
    elif changed == 'content':
        path.write_text('foo(x)\n# changed\n')
    else:
        kwargs[changed] = {'pattern': 'foo($B)', 'replacement': 'baz($A)', 'language': 'javascript'}[changed]
    monkeypatch.setattr(tools, 'execute_tool', lambda *a: pytest.fail('must not dispatch a write'))
    rejected = apply(ctx, result, **kwargs)
    assert rejected.is_error and 'previewToken' in rejected.content
    assert path.read_text().startswith('foo(x)')


@pytest.mark.parametrize('restriction', ['frozen', 'scope', 'absolute_frozen'])
def test_scope_and_frozen_checked_before_apply(ctx, cli, monkeypatch, restriction):
    path = prepare(ctx, cli)
    result = preview(ctx)
    if restriction == 'frozen':
        ctx.frozen = ['sample.py']
    elif restriction == 'absolute_frozen':
        ctx.frozen = [str(path)]
    else:
        ctx.scope = ['allowed']
    monkeypatch.setattr(tools, 'execute_tool', lambda *a: pytest.fail('write dispatched'))
    rejected = apply(ctx, result)
    assert rejected.is_error
    assert 'violation' in rejected.content
    assert path.read_bytes() == b'foo(x)\n'


@pytest.mark.parametrize('bad_path', ['..', '.', 'missing.py', '../outside.py'])
def test_reject_bad_paths(ctx, cli, bad_path):
    result = ast.execute_ast(params(path=bad_path), ctx)
    assert result.is_error
    assert not cli[1]


def test_symlink_escape_and_fifo(ctx, cli, tmp_path):
    outside = tmp_path.parent / (tmp_path.name + '-outside.py')
    outside.write_text('foo(x)')
    try:
        (tmp_path / 'sample.py').symlink_to(outside)
        result = ast.execute_ast(params(), ctx)
        assert result.is_error and 'escapes project root' in result.content
        assert not cli[1]
    finally:
        outside.unlink()
    import os
    fifo = tmp_path / 'pipe.py'
    os.mkfifo(fifo)
    result = ast.execute_ast(params(path='pipe.py'), ctx)
    assert result.is_error and 'regular file' in result.content


def test_symlink_inside_root_is_canonical_and_alias_swap_rejected(ctx, cli, monkeypatch):
    path = prepare(ctx, cli)
    alias = ctx.project_root / 'alias.py'
    alias.symlink_to(path)
    result = preview(ctx, path='alias.py')
    assert not result.is_error
    other = ctx.project_root / 'other.py'
    other.write_text('foo(x)\n')
    alias.unlink()
    alias.symlink_to(other)
    monkeypatch.setattr(tools, 'execute_tool', lambda *a: pytest.fail('write dispatched'))
    rejected = apply(ctx, result, path='alias.py')
    assert rejected.is_error and 'previewToken' in rejected.content
    assert path.read_text() == other.read_text() == 'foo(x)\n'


def test_nested_overlapping_matches_rejected(ctx, cli):
    path = ctx.project_root / 'sample.py'
    source = 'foo(foo(x))\n'
    path.write_text(source)
    cli[0]['matches'] = [node(source, 'foo(foo(x))'), node(source, 'foo(x)')]
    result = preview(ctx, replacement='new()')
    assert result.is_error and 'Overlapping' in result.content
    assert path.read_text() == source


def test_unicode_wrong_byte_offset_and_match_text_rejected(ctx, cli):
    source = 'éfoo(x)'
    path = ctx.project_root / 'sample.py'
    path.write_text(source)
    cli[0]['matches'] = [node(source, 'foo(x)', start=1)]
    assert ast.execute_ast(params(), ctx).is_error
    cli[0]['matches'] = [node(source, 'bar(x)', start=2)]
    assert ast.execute_ast(params(), ctx).is_error


def test_variadic_capture_preserves_separator_text(ctx, cli, monkeypatch):
    source = 'foo(a, /* keep */ b)\n'
    path = ctx.project_root / 'sample.py'
    path.write_text(source)
    match = node(source, source.strip())
    match['metaVariables'] = {'multi': {'ARGS': [node(source, 'a'), node(source, 'b')]}}
    cli[0]['matches'] = [match]
    result = preview(ctx, replacement='bar($$$ARGS)')
    assert not result.is_error
    assert '+bar(a, /* keep */ b)' in result.details['diff']


def test_unbound_capture_rejected(ctx, cli):
    path = prepare(ctx, cli)
    result = preview(ctx, replacement='bar($NOPE)')
    assert result.is_error and 'Unbound' in result.content
    assert path.read_text() == 'foo(x)\n'


def test_crlf_preserved_through_edit(ctx, cli, monkeypatch):
    path = prepare(ctx, cli, '# header\r\nfoo(x)\r\n')
    result = preview(ctx)
    monkeypatch.setattr(tools, 'execute_tool', lambda name, args, context: tools.execute_edit(args, context))
    assert not apply(ctx, result).is_error
    assert path.read_bytes() == b'# header\r\nbar(x)\r\n'


def test_match_files_bytes_and_output_caps(ctx, cli, monkeypatch):
    path = prepare(ctx, cli)
    cli[0]['matches'] *= 2
    result = preview(ctx, maxMatches=1)
    assert result.is_error and 'Match cap' in result.content
    assert ast.execute_ast(params(paths=['sample.py'] * 21), ctx).is_error
    assert ast.execute_ast({'paths': ['sample.py'] * 21, 'pattern': 'x', 'language': 'python'}, ctx).is_error
    monkeypatch.setattr(ast, 'MAX_FILE_BYTES', 3)
    assert ast.execute_ast(params(), ctx).is_error
    assert path.read_bytes() == b'foo(x)\n'


def test_multi_file_search_and_rewrite_rejection(ctx, cli):
    prepare(ctx, cli)
    (ctx.project_root / 'two.py').write_text('foo(x)\n')
    args = {'paths': ['sample.py', 'two.py'], 'language': 'python', 'pattern': 'foo($A)'}
    result = ast.execute_ast(args, ctx)
    assert not result.is_error and result.details['matchCount'] == 2
    result = ast.execute_ast({**args, 'action': 'preview', 'replacement': 'bar($A)'}, ctx)
    assert result.is_error and 'exactly one file' in result.content


def test_output_truncated_honestly(ctx, cli):
    prepare(ctx, cli)
    result = ast.execute_ast(params(), replace(ctx, max_output_bytes=50))
    assert result.truncated and result.details is None
    assert 'truncated' in result.content


def test_missing_or_unrelated_executable(ctx, cli, monkeypatch):
    prepare(ctx, cli)
    monkeypatch.setattr(ast.shutil, 'which', lambda name: None)
    result = ast.execute_ast(params(), ctx)
    assert result.is_error and 'Install ast-grep separately' in result.content
    monkeypatch.setattr(ast.shutil, 'which', lambda name: '/usr/bin/sg' if name == 'sg' else None)
    cli[0]['version'] = b'sg from util-linux 2.0'
    result = ast.execute_ast(params(), ctx)
    assert result.is_error and 'unrelated sg' in result.content
    assert all(call[0][-1] == '--version' for call in cli[1])
    cli[0]['version'] = b'ast-grep 0.40.0'
    assert not ast.execute_ast(params(), ctx).is_error


@pytest.mark.parametrize('args', [{'action': 'apply'}, {'action': 'rewrite'}, {'rewrite': True},
                                  {'language': '--update-all'}, {'maxMatches': 0}, {'timeout': -1},
                                  {'timeout': float('nan')}, {'paths': ['sample.py']}])
def test_invalid_parameters_no_write(ctx, cli, args):
    path = prepare(ctx, cli)
    assert ast.execute_ast(params(**args), ctx).is_error
    assert path.read_bytes() == b'foo(x)\n'


def test_cancellation_and_cli_errors(ctx, cli):
    prepare(ctx, cli)
    ctx.cancel_event = threading.Event()
    ctx.cancel_event.set()
    result = ast.execute_ast(params(), ctx)
    assert result.is_error and 'cancelled' in result.content
    assert not cli[1]
    ctx.cancel_event.clear()
    cli[0]['code'], cli[0]['stderr'] = 2, b'pattern parse failed'
    result = ast.execute_ast(params(), ctx)
    assert result.is_error and 'pattern parse failed' in result.content


def test_bounded_runner_argv_timeout_cancellation_and_output(ctx, monkeypatch):
    code, out, err = ast._run([sys.executable, '-c', 'import sys; print(sys.stdin.read())'],
                             b'hello', ctx, time.monotonic() + 5)
    assert code == 0 and out == b'hello\n' and err == b''
    with pytest.raises(ast._AstError, match='timed out'):
        ast._run([sys.executable, '-c', 'import time; time.sleep(10)'], b'', ctx, time.monotonic() + .05)
    monkeypatch.setattr(ast, 'MAX_OUTPUT_BYTES', 200)
    with pytest.raises(ast._AstError, match='byte cap'):
        ast._run([sys.executable, '-c', 'print("x" * 100000)'], b'', ctx, time.monotonic() + 5)
    ctx.cancel_event = threading.Event()
    timer = threading.Timer(.05, ctx.cancel_event.set)
    timer.start()
    try:
        with pytest.raises(ast._AstError, match='cancelled'):
            ast._run([sys.executable, '-c', 'import time; time.sleep(10)'], b'', ctx, time.monotonic() + 5)
    finally:
        timer.join()


def test_file_changed_during_edit_approval_is_rejected(ctx, cli, monkeypatch):
    import charon.infra.tool_approval as approval
    path = prepare(ctx, cli)
    result = preview(ctx)
    monkeypatch.setattr(approval, 'needs_approval', lambda *a, **k: (True, 'write', 'test approval'))
    monkeypatch.setattr(approval, 'approve_tool_for_session', lambda *a, **k: None)

    def approve_after_change(*args):
        path.write_text('foo(x)\n# concurrent edit\n')
        return True, 'approved'

    monkeypatch.setattr(tools, '_request_interactive_approval', approve_after_change)
    rejected = apply(ctx, result)
    assert rejected.is_error and 'stale edit' in rejected.content
    assert path.read_text() == 'foo(x)\n# concurrent edit\n'


def test_match_cap_is_global_and_duplicate_paths_rejected(ctx, cli):
    prepare(ctx, cli)
    (ctx.project_root / 'two.py').write_text('foo(x)\n')
    args = {'paths': ['sample.py', 'two.py'], 'language': 'python', 'pattern': 'foo($A)', 'maxMatches': 1}
    rejected = ast.execute_ast(args, ctx)
    assert rejected.is_error and 'Match cap' in rejected.content
    rejected = ast.execute_ast({**args, 'paths': ['sample.py', './sample.py']}, ctx)
    assert rejected.is_error and 'Duplicate' in rejected.content


def test_total_input_and_expanded_replacement_caps(ctx, cli, monkeypatch):
    prepare(ctx, cli)
    (ctx.project_root / 'two.py').write_text('foo(x)\n')
    monkeypatch.setattr(ast, 'MAX_TOTAL_BYTES', 10)
    rejected = ast.execute_ast({'paths': ['sample.py', 'two.py'], 'language': 'python', 'pattern': 'x'}, ctx)
    assert rejected.is_error and 'Total input byte cap' in rejected.content
    monkeypatch.setattr(ast, 'MAX_FILE_BYTES', 10)
    rejected = preview(ctx, replacement='$A' * 20)
    assert rejected.is_error and 'byte cap' in rejected.content


def test_real_cli_smoke_if_installed(ctx):
    if not shutil.which('ast-grep') and not shutil.which('sg'):
        pytest.skip('ast-grep is not installed; never install during tests')
    try:
        ast._executable(ctx, time.monotonic() + 5)
    except ast._AstError:
        pytest.skip('No validated ast-grep executable')
    for language, path, source in [('python', 'sample.py', 'foo(x)\n'),
                                   ('javascript', 'sample.js', 'foo(x);\n')]:
        (ctx.project_root / path).write_text(source)
        result = preview(ctx, path=path, language=language)
        assert not result.is_error, result.content
        assert result.details['matchCount'] == 1
        assert 'bar(x)' in result.details['diff']
