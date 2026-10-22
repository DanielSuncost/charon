from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sys
import threading
import time

import pytest

from charon.ide import lsp
from charon.tools import ToolContext
from charon.tools.lsp_tool import LSP_TOOL_DEF, execute_lsp, notify_file_changed, post_edit_diagnostics

FIXTURE = Path(__file__).parent / 'fixtures' / 'ide_lsp_server.py'


@pytest.fixture(autouse=True)
def cleanup():
    yield
    lsp._cleanup()
    with lsp._registry_lock:
        lsp._managers.clear()


def context(tmp_path, mode='push', **kwargs):
    (tmp_path / 'main.py').write_text('a😀b\n', encoding='utf-8')
    return ToolContext(project_root=tmp_path, metadata={'ide_lsp_servers': [{
        'name': 'fake', 'command': [sys.executable, str(FIXTURE.resolve()), mode], 'extensions': ['.py'],
    }]}, **kwargs)


def call(ctx, action='hover', **kwargs):
    params = {'action': action, 'timeout': 2}
    if action not in ('status', 'shutdown'):
        params['path'] = 'main.py'
    if action in ('hover', 'definition', 'references'):
        params.update(line=1, column=3)
    params.update(kwargs)
    return execute_lsp(params, ctx)


def payload(result):
    assert not result.is_error, result.content
    return json.loads(result.content)


def hover(result):
    return json.loads(payload(result)['result']['contents']['value'])


def test_definition_and_tool_schema(tmp_path):
    ctx = context(tmp_path)
    assert LSP_TOOL_DEF['name'] == 'Lsp'
    result = payload(call(ctx, 'definition'))
    assert result['result'][0]['range']['start'] == {'line': 0, 'character': 3}
    assert result['position_encoding'] == 'utf-16'
    assert payload(call(ctx, 'references'))['result']
    assert payload(call(ctx, 'symbols'))['result'][0]['name'] == 'fake_symbol'


def test_status_does_not_start_and_shutdown(tmp_path):
    ctx = context(tmp_path)
    assert not payload(call(ctx, 'status'))['servers'][0]['running']
    assert not lsp._managers
    assert hover(call(ctx))['version'] == 1
    manager = next(iter(lsp._managers.values()))
    process = manager.client.process
    assert payload(call(ctx, 'status'))['servers'][0]['running']
    assert payload(call(ctx, 'shutdown'))['shutdown'] == 1
    assert process.poll() is not None
    assert not payload(call(ctx, 'status'))['servers'][0]['running']
    assert hover(call(ctx))['version'] == 1


def test_shared_session_and_incremental_sync(tmp_path):
    ctx = context(tmp_path)
    assert hover(call(ctx))['position']['character'] == 3
    other = ToolContext(project_root=tmp_path / '.', metadata=ctx.metadata, agent_id='other')
    (tmp_path / 'main.py').write_text('new text\n', encoding='utf-8')
    assert hover(call(other))['version'] == 2
    assert len(lsp._managers) == 1
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: hover(call(other)), range(12)))
    assert {r['version'] for r in results} == {2}


def test_changed_config_and_project_isolate_sessions(tmp_path):
    ctx = context(tmp_path)
    hover(call(ctx))
    other_root = tmp_path / 'sub'
    other_root.mkdir()
    second = context(other_root)
    hover(call(second))
    ctx.metadata['ide_lsp_servers'][0]['command'][-1] = 'pull'
    assert payload(call(ctx, 'diagnostics'))['source'] == 'pull'
    assert len(lsp._managers) == 3


@pytest.mark.parametrize('mode', ['push', 'pull'])
def test_fresh_diagnostics(tmp_path, mode):
    ctx = context(tmp_path, mode)
    report = payload(call(ctx, 'diagnostics'))
    assert report['diagnostics'] == []
    assert report['fresh'] is True
    assert report['source'] == ('pull' if mode == 'pull' else 'versioned-push')
    if mode == 'push':
        (tmp_path / 'main.py').write_text('ERROR')
        report = payload(call(ctx, 'diagnostics'))
        assert report['version'] == 2
        assert report['diagnostics'][0]['severity'] == 1


@pytest.mark.parametrize('mode', ['missing', 'stale', 'unversioned', 'unchanged'])
def test_absent_stale_unversioned_diagnostics_never_clean(tmp_path, mode):
    result = call(context(tmp_path, mode), 'diagnostics', timeout=0.35)
    assert result.is_error
    assert 'diagnostic' in result.content.lower() or 'publication' in result.content


def test_stale_clean_after_edit_is_rejected(tmp_path):
    ctx = context(tmp_path, 'old-clean')
    assert payload(call(ctx, 'diagnostics'))['diagnostics'] == []
    (tmp_path / 'main.py').write_text('ERROR')
    result = call(ctx, 'diagnostics', timeout=0.2)
    assert result.is_error
    assert 'no current versioned publication' in result.content


def test_dirty_hook_and_post_edit_no_start(tmp_path):
    ctx = context(tmp_path)
    assert notify_file_changed('main.py', ctx) == 0
    assert post_edit_diagnostics('main.py', ctx).is_error
    assert not lsp._managers
    payload(call(ctx, 'diagnostics'))
    assert notify_file_changed('main.py', ctx) == 1
    assert payload(post_edit_diagnostics('main.py', ctx))['version'] == 2


def test_disk_change_during_pull_is_not_clean(tmp_path):
    ctx = context(tmp_path, 'slow-pull')
    hover(call(ctx))
    timer = threading.Timer(0.08, lambda: (tmp_path / 'main.py').write_text('ERROR'))
    timer.start()
    result = call(ctx, 'diagnostics')
    timer.join()
    assert result.is_error
    assert 'changed during request' in result.content


@pytest.mark.parametrize('mode', ['exit', 'bad-frame', 'crash-query'])
def test_exit_and_protocol_failure(tmp_path, mode):
    started = time.monotonic()
    result = call(context(tmp_path, mode))
    assert result.is_error
    assert 'exited or protocol failed' in result.content
    assert time.monotonic() - started < 2


def test_timeout_cancel_and_late_responses(tmp_path):
    notices = []
    rpc = lsp.JsonRpcClient([sys.executable, str(FIXTURE.resolve()), 'hang'], tmp_path,
                           lambda method, params: notices.append((method, params)))
    try:
        rpc.request('initialize', {}, time.monotonic() + 2)
        with pytest.raises(lsp.LspError, match='timed out'):
            rpc.request('textDocument/hover', {}, time.monotonic() + 0.1)
        until = time.monotonic() + 1
        while not notices and time.monotonic() < until:
            time.sleep(0.01)
        assert notices[0][0] == 'test/cancelled'
        event = threading.Event()
        timer = threading.Timer(0.1, event.set)
        timer.start()
        with pytest.raises(lsp.LspError, match='cancelled'):
            rpc.request('textDocument/hover', {}, time.monotonic() + 2, event)
        timer.join()
        assert not rpc._pending
    finally:
        rpc.close()
    assert rpc.process.poll() is not None


def test_unresponsive_stdin_does_not_block_caller(tmp_path):
    rpc = lsp.JsonRpcClient([sys.executable, str(FIXTURE.resolve()), 'never-read'], tmp_path)
    started = time.monotonic()
    try:
        with pytest.raises(lsp.LspError, match='timed out'):
            rpc.request('initialize', {'large': 'x' * 1_000_000}, time.monotonic() + 0.1)
    finally:
        rpc.close()
    assert time.monotonic() - started < 2
    assert rpc.process.poll() is not None
    assert not any(thread.is_alive() for thread in rpc._threads)


def test_manager_lock_wait_is_bounded(tmp_path):
    ctx = context(tmp_path)
    hover(call(ctx))
    manager = next(iter(lsp._managers.values()))
    manager.lock.acquire()
    try:
        result = call(ctx, timeout=0.1)
        assert result.is_error
        assert 'timed out' in result.content
        assert manager.users == 0
    finally:
        manager.lock.release()


def test_registry_capacity_reclaimed_after_shutdown(tmp_path, monkeypatch):
    monkeypatch.setattr(lsp, 'MAX_SERVERS', 1)
    ctx = context(tmp_path)
    hover(call(ctx))
    call(ctx, 'shutdown')
    ctx.metadata['ide_lsp_servers'][0]['command'][-1] = 'pull'
    assert payload(call(ctx, 'diagnostics'))['source'] == 'pull'
    assert len(lsp._managers) == 1


def test_stderr_is_drained_and_bounded(tmp_path):
    ctx = context(tmp_path, 'stderr')
    hover(call(ctx))
    client = next(iter(lsp._managers.values())).client
    assert 0 < len(client.stderr_tail) <= 8192


def test_server_cannot_apply_edits_or_execute_commands(tmp_path):
    ctx = context(tmp_path, 'edits')
    # Server requests are interleaved with initialization; a second query ensures
    # all queued client responses have been processed.
    hover(call(ctx))
    responses = hover(call(ctx))['server_responses']
    assert responses['apply']['applied'] is False
    assert responses['command']['code'] == -32601
    assert (tmp_path / 'main.py').read_text() == 'a😀b\n'


@pytest.mark.parametrize('action', ['rename', 'workspace/executeCommand', 'workspace/applyEdit'])
def test_mutating_actions_rejected(tmp_path, action):
    assert call(context(tmp_path), action).is_error
    assert not lsp._managers


def test_command_argument_rejected(tmp_path):
    result = call(context(tmp_path), command=['anything'])
    assert result.is_error
    assert not lsp._managers


def test_path_confinement_and_nonregular(tmp_path):
    ctx = context(tmp_path)
    assert call(ctx, path='../outside.py').is_error
    assert call(ctx, path='.').is_error
    (tmp_path / 'escape.py').symlink_to(Path(__file__).resolve())
    assert call(ctx, path='escape.py').is_error
    assert not lsp._managers


def test_regular_in_project_symlink_is_allowed(tmp_path):
    ctx = context(tmp_path)
    (tmp_path / 'alias.py').symlink_to(tmp_path / 'main.py')
    assert hover(call(ctx, path='alias.py'))['version'] == 1


@pytest.mark.skipif(not hasattr(os, 'mkfifo'), reason='POSIX FIFO test')
def test_fifo_is_rejected_without_opening(tmp_path):
    ctx = context(tmp_path)
    os.mkfifo(tmp_path / 'pipe.py')
    assert call(ctx, path='pipe.py').is_error
    assert not lsp._managers


def test_document_size_and_encoding_limits(tmp_path):
    ctx = context(tmp_path)
    (tmp_path / 'main.py').write_bytes(b'x' * (lsp.MAX_DOCUMENT + 1))
    result = call(ctx)
    assert result.is_error and '2 MiB' in result.content
    (tmp_path / 'main.py').write_bytes(b'\xff')
    result = call(ctx)
    assert result.is_error and 'UTF-8' in result.content


def test_no_server_and_trusted_configuration(tmp_path, monkeypatch):
    ctx = context(tmp_path)
    ctx.metadata = {'ide_lsp_servers': []}
    assert call(ctx).is_error
    assert not payload(call(ctx, 'status'))['available']
    assert lsp.server_specs({'ide_lsp_servers': []}) == ()
    monkeypatch.setattr(lsp.shutil, 'which', lambda binary: '/trusted/' + binary)
    specs = lsp.server_specs({})
    assert specs[0].command == ('/trusted/pyright-langserver', '--stdio')
    assert specs[1].command == ('/trusted/rust-analyzer',)
    ctx.metadata = {'ide_lsp_servers': [{'name': 'bad', 'command': 'shell command', 'extensions': ['.py']}]}
    assert call(ctx, 'status').is_error


@pytest.mark.parametrize('line,column', [(0, 1), (1, 0), (99, 1), (1, 99), (True, 1), (1, '2')])
def test_invalid_positions(tmp_path, line, column):
    assert call(context(tmp_path), line=line, column=column).is_error


def test_position_crlf_and_trailing_empty_line():
    assert lsp.position('x😀\r\n', 1, 3) == {'line': 0, 'character': 3}
    assert lsp.position('x😀\r\n', 2, 1) == {'line': 1, 'character': 0}


@pytest.mark.parametrize('timeout', [-1, 0, 31, float('nan'), float('inf'), True, 'bad'])
def test_timeout_validation(tmp_path, timeout):
    assert call(context(tmp_path), timeout=timeout).is_error


def test_precancelled_tool_call_never_starts_server(tmp_path):
    cancel = threading.Event()
    cancel.set()
    ctx = context(tmp_path, cancel_event=cancel)
    result = call(ctx)
    assert result.is_error and 'cancelled' in result.content
    assert all(manager.client is None for manager in lsp._managers.values())


def test_startup_timeout_terminates_unresponsive_server(tmp_path):
    ctx = context(tmp_path, 'never-read')
    started = time.monotonic()
    result = call(ctx, timeout=0.1)
    assert result.is_error and 'timed out' in result.content
    assert time.monotonic() - started < 2
    assert next(iter(lsp._managers.values())).client is None


def test_outbound_frames_are_bounded(tmp_path, monkeypatch):
    rpc = lsp.JsonRpcClient([sys.executable, str(FIXTURE.resolve()), 'push'], tmp_path)
    monkeypatch.setattr(lsp, 'MAX_FRAME', 256)
    try:
        with pytest.raises(lsp.LspError, match='frame limit'):
            rpc.request('initialize', {'large': 'x' * 512}, time.monotonic() + 1)
        assert not rpc._pending
    finally:
        rpc.close()


def test_shutdown_remains_available_after_invalid_config(tmp_path):
    ctx = context(tmp_path)
    hover(call(ctx))
    process = next(iter(lsp._managers.values())).client.process
    ctx.metadata = {'ide_lsp_servers': 'bad configuration'}
    assert payload(call(ctx, 'shutdown'))['shutdown'] == 1
    assert process.poll() is not None


def test_output_limits(tmp_path):
    result = call(context(tmp_path, max_output_bytes=45, max_output_lines=3))
    assert result.truncated
    assert len(result.content.encode()) <= 45
    assert len(result.content.splitlines()) <= 3


@pytest.mark.parametrize('mode', ['unsupported', 'utf8'])
def test_honest_capability_errors(tmp_path, mode):
    assert call(context(tmp_path, mode)).is_error
