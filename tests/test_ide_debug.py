"""Small DAP slice tested against a real, fragmented stdio subprocess."""
import json
import os
from pathlib import Path
import signal
import sys
import threading
import time

import pytest

from charon.ide.dap import MAX_EVENT_BYTES
from charon.tools import ToolContext
from charon.tools import debug_tool as debug

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='Initial DAP slice requires POSIX')
FIXTURE = Path(__file__).parent / 'fixtures' / 'ide_dap_adapter.py'


@pytest.fixture(autouse=True)
def cleanup(monkeypatch):
    monkeypatch.setattr(debug, 'REQUEST_TIMEOUT', 1.5)
    yield
    with debug._LOCK:
        sessions = list(debug._SESSIONS.values())
    for session in sessions:
        with session.lock:
            session.client.close()
            assert session.client.proc.poll() is not None
    with debug._LOCK:
        debug._SESSIONS.clear()


@pytest.fixture
def ctx(tmp_path):
    return context(tmp_path)


def context(root, agent='agent-a', mode='normal'):
    return ToolContext(project_root=root, agent_id=agent, metadata={
        'ide_debug_adapters': {'fake': {'command': [sys.executable, '-u', str(FIXTURE), mode],
                                     'launch': {'program': 'trusted.py', 'stopOnEntry': True}}}})


def call(ctx, action, **kwargs):
    result = debug.execute_debug({'action': action, **kwargs}, ctx)
    return result, json.loads(result.content)


def launch(ctx):
    result, body = call(ctx, 'launch', adapter='fake')
    assert not result.is_error, result.content
    return body['session_id']


def test_status_is_not_an_implicit_launch(ctx):
    result, body = call(ctx, 'status')
    assert not result.is_error and body == {'sessions': []}
    assert not debug._SESSIONS


def test_idle_adapter_exit_is_reaped_and_visible(ctx):
    sid = launch(ctx)
    session = debug._SESSIONS[sid]
    session.client.proc.kill()
    deadline = time.monotonic() + 3
    while not session.client.closed and time.monotonic() < deadline:
        time.sleep(0.02)
    result, body = call(ctx, 'status', session_id=sid)
    assert result.is_error and body['closed']
    assert body['exit_code'] is not None
    assert 'exit' in body['failure'].lower()


def test_global_session_cap_and_closed_record_bound(tmp_path, monkeypatch):
    monkeypatch.setattr(debug, 'MAX_SESSIONS', 2)
    monkeypatch.setattr(debug, 'MAX_RECORDS', 2)
    first = context(tmp_path, agent='first')
    second = context(tmp_path, agent='second')
    third = context(tmp_path, agent='third')
    first_sid = launch(first)
    launch(second)
    assert call(third, 'launch', adapter='fake')[0].is_error
    call(first, 'terminate', session_id=first_sid)
    launch(third)
    assert len(debug._SESSIONS) == 2
    assert first_sid not in debug._SESSIONS


@pytest.mark.parametrize('cap', [0, 1, 40, 100])
def test_tiny_output_caps_are_honored(ctx, cap):
    ctx.max_output_bytes = cap
    result = debug.execute_debug({'action': 'not-supported'}, ctx)
    assert result.is_error and len(result.content.encode()) <= cap


def test_handshake_and_supported_actions(ctx):
    sid = launch(ctx)
    source = ctx.project_root / 'main.py'
    source.write_text('answer = 42\n')
    requests = [
        ('breakpoints', {'path': 'main.py', 'lines': [1]}),
        ('breakpoints', {'path': 'main.py', 'lines': []}),
        ('threads', {}), ('stack', {'thread_id': 1}), ('scopes', {'frame_id': 0}),
        ('variables', {'variables_reference': 1}),
        ('continue', {'thread_id': 1}), ('pause', {'thread_id': 1}), ('next', {'thread_id': 1}),
    ]
    for action, args in requests:
        result, body = call(ctx, action, session_id=sid, **args)
        assert not result.is_error, result.content
        assert body['session_id'] == sid
    _, status = call(ctx, 'status', session_id=sid)
    assert status['state'] in ('running', 'stopped')
    client = debug._SESSIONS[sid].client
    result, terminated = call(ctx, 'terminate', session_id=sid)
    assert not result.is_error, result.content
    assert terminated['closed'] and client.proc.poll() is not None


def test_owner_project_and_agent_isolation(ctx, tmp_path):
    sid = launch(ctx)
    for outsider in [context(tmp_path, agent='other'), context(tmp_path / 'other')]:
        outsider.project_root.mkdir(exist_ok=True)
        for action in ('status', 'threads', 'terminate'):
            result, _ = call(outsider, action, session_id=sid)
            assert result.is_error
        result, body = call(outsider, 'status')
        assert not result.is_error and body['sessions'] == []
    assert not debug._SESSIONS[sid].client.closed


@pytest.mark.parametrize('restriction', ['scope', 'frozen'])
@pytest.mark.parametrize('action', debug.ACTIONS)
def test_every_action_rejects_restricted_context(ctx, restriction, action):
    setattr(ctx, restriction, [] if restriction == 'scope' else ['secret'])
    result, _ = call(ctx, action, adapter='fake')
    assert result.is_error and 'restricted' in result.content
    assert not debug._SESSIONS


@pytest.mark.parametrize('params', [
    {'action': 'launch', 'adapter': 'fake', 'command': ['anything']},
    {'action': 'launch', 'adapter': 'fake', 'launch': {'program': 'untrusted'}},
    {'action': 'launch', 'adapter': 'unknown'}, {'action': 'evaluate'},
    {'action': 'custom_request'}, {'action': 'write_memory'}, {},
])
def test_no_arbitrary_adapter_or_request(ctx, params):
    result = debug.execute_debug(params, ctx)
    assert result.is_error
    assert not debug._SESSIONS


def test_no_config_or_identity_fails_closed(ctx):
    ctx.metadata = {}
    assert call(ctx, 'launch', adapter='fake')[0].is_error
    ctx.agent_id = ''
    assert call(ctx, 'status')[0].is_error


def test_breakpoint_path_and_ids(ctx, tmp_path):
    sid = launch(ctx)
    outside = tmp_path.parent / (tmp_path.name + '-outside.py')
    outside.write_text('pass\n')
    try:
        (tmp_path / 'escape.py').symlink_to(outside)
        for path in [str(outside), 'escape.py', '.', 'missing.py']:
            result, _ = call(ctx, 'breakpoints', session_id=sid, path=path, lines=[1])
            assert result.is_error
        source = tmp_path / 'a.py'
        source.write_text('pass\n')
        for lines in [[True], [0], [-1], ['1'], list(range(1, 102))]:
            assert call(ctx, 'breakpoints', session_id=sid, path='a.py', lines=lines)[0].is_error
        assert call(ctx, 'stack', session_id=sid, thread_id=True)[0].is_error
        assert call(ctx, 'variables', session_id=sid, variables_reference=1, count=101)[0].is_error
        assert not debug._SESSIONS[sid].client.closed
    finally:
        outside.unlink()


@pytest.mark.parametrize('mode,expected', [
    ('timeout', 'timed out'), ('exit', 'exit'), ('oversize', 'framing'),
    ('header', 'header'), ('malformed', 'JSON'), ('reverse', 'reverse'),
])
def test_failures_are_visible_and_reaped(tmp_path, mode, expected):
    ctx = context(tmp_path, mode=mode)
    sid = launch(ctx)
    started = time.monotonic()
    result, body = call(ctx, 'threads', session_id=sid)
    assert result.is_error and expected.lower() in result.content.lower()
    assert body['closed']
    assert debug._SESSIONS[sid].client.proc.poll() is not None
    assert time.monotonic() - started < 5


def test_cancellation_during_request_and_while_idle(tmp_path):
    ctx = context(tmp_path, mode='timeout')
    ctx.cancel_event = threading.Event()
    sid = launch(ctx)
    timer = threading.Timer(0.1, ctx.cancel_event.set)
    timer.start()
    try:
        result, body = call(ctx, 'threads', session_id=sid)
        assert result.is_error and 'cancel' in result.content
        assert body['closed']
    finally:
        timer.join()
    ctx = context(tmp_path, agent='idle')
    ctx.cancel_event = threading.Event()
    sid = launch(ctx)
    ctx.cancel_event.set()
    deadline = time.monotonic() + 3
    while not debug._SESSIONS[sid].client.closed and time.monotonic() < deadline:
        time.sleep(0.02)
    assert debug._SESSIONS[sid].client.closed
    assert 'cancel' in debug._SESSIONS[sid].client.failure


def test_cancelled_launch_does_not_spawn(ctx):
    ctx.cancel_event = threading.Event()
    ctx.cancel_event.set()
    assert call(ctx, 'launch', adapter='fake')[0].is_error
    assert not debug._SESSIONS


@pytest.mark.parametrize('mode', ['launch-timeout', 'duplicate-length'])
def test_launch_failure_preserves_session_and_reaps(tmp_path, mode):
    ctx = context(tmp_path, mode=mode)
    result, body = call(ctx, 'launch', adapter='fake')
    assert result.is_error and body['closed'] and body['failure']
    session = debug._SESSIONS[body['session_id']]
    assert session.client.proc.poll() is not None
    assert call(ctx, 'status', session_id=session.id)[0].is_error


def test_session_caps(ctx):
    launch(ctx)
    launch(ctx)
    result, _ = call(ctx, 'launch', adapter='fake')
    assert result.is_error and 'limit' in result.content


def test_output_buffers_bounded(tmp_path):
    ctx = context(tmp_path, mode='flood')
    sid = launch(ctx)
    result, body = call(ctx, 'threads', session_id=sid)
    assert not result.is_error, result.content
    client = debug._SESSIONS[sid].client
    assert client.event_bytes <= MAX_EVENT_BYTES
    assert client.events_dropped > 0
    assert len(client.stderr) <= 8192 and client.stderr_truncated
    ctx.max_output_bytes = 1000
    result, _ = call(ctx, 'status', session_id=sid)
    assert result.truncated and len(result.content.encode()) <= 1000


def test_idle_exit_and_expiry_observable(ctx, monkeypatch):
    sid = launch(ctx)
    monkeypatch.setattr(debug, 'IDLE_SECONDS', 0)
    deadline = time.monotonic() + 3
    while not debug._SESSIONS[sid].client.closed and time.monotonic() < deadline:
        time.sleep(0.02)
    result, body = call(ctx, 'status', session_id=sid)
    assert result.is_error and body['closed'] and 'idle timeout' in result.content


def test_process_group_child_cleanup(tmp_path):
    ctx = context(tmp_path, mode='child')
    sid = launch(ctx)
    result, body = call(ctx, 'threads', session_id=sid)
    assert not result.is_error
    pid = body['body']['child_pid']
    try:
        os.kill(pid, 0)
        call(ctx, 'terminate', session_id=sid)
        # A killed orphan can remain a zombie until init reaps it; ps distinguishes it.
        import subprocess
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            state = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)], capture_output=True,
                                   text=True, timeout=1).stdout.strip()
            if not state or state.startswith('Z'):
                break
            time.sleep(0.02)
        assert not state or state.startswith('Z')
    finally:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
