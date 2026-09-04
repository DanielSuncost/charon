import threading
from types import SimpleNamespace

from backend import common
from backend.async_runtime import AsyncRuntime
from backend.chat_mixin import (
    ChatMixin,
    _tick_orchestrations_once,
    _tool_result_preview,
)
from charon.orchestration.graph_runtime import get_run, start_run
from charon.orchestration.graph_schema import GraphDefinition, NodeSpec


class _FakeEngine:
    def __init__(self):
        self.messages = []

    def steer(self, message):
        self.messages.append(message)

    @property
    def pending_messages(self):
        return len(self.messages)


def test_chat_complete_follows_worker_cleanup_across_async_runtime(monkeypatch):
    runtime = AsyncRuntime()
    backend = SimpleNamespace(
        _engine_lock=threading.Lock(),
        _chat_busy=False,
        _active_chat_request_id=None,
        _active_chat_message='',
        _active_chat_started_at=0.0,
        _active_tool={},
    )

    def handle_chat(_message, request_id):
        async def finish():
            common.emit({
                'type': 'chat_complete',
                'summary': 'done',
                'request_id': request_id,
            })

        runtime.run(finish())

    backend.handle_chat = handle_chat
    observed = []

    def capture(events):
        observed.extend(
            (event, backend._chat_busy, backend._engine_lock.locked())
            for event in events
        )

    monkeypatch.setattr(common, '_write_events_locked', capture)
    try:
        ChatMixin._chat_worker(backend, 'hello', 'req-lifecycle')
    finally:
        runtime.shutdown()

    assert observed == [({
        'type': 'chat_complete',
        'summary': 'done',
        'request_id': 'req-lifecycle',
    }, False, False)]


def test_tool_result_preview_bounds_lines_and_long_single_lines():
    content = '\n'.join([f'line {i} ' + ('x' * 400) for i in range(30)])

    preview, truncated, total_chars, total_lines = _tool_result_preview(content)

    assert truncated is True
    assert total_chars == len(content)
    assert total_lines == 30
    assert len(preview) <= 2025
    assert len(preview.splitlines()) <= 10
    assert 'line(s) omitted' in preview
    assert all(len(line) <= 241 for line in preview.splitlines() if 'omitted' not in line)


def test_live_steer_is_acknowledged_with_current_tool_status(monkeypatch):
    emitted = []
    monkeypatch.setattr(common, 'emit', emitted.append)
    engine = _FakeEngine()
    backend = SimpleNamespace(
        engine=engine,
        _chat_busy=True,
        _active_tool={
            'tool_name': 'Bash',
            'tool_call_id': 'call-1',
            'started_at': 1.0,
        },
    )

    ChatMixin.handle_steer(backend, 'what is happening?', 'req-2')

    assert engine.messages == ['what is happening?']
    assert [event['type'] for event in emitted] == ['steer_queued', 'status']
    assert 'Still working in Bash' in emitted[-1]['message']
    assert 'will answer' in emitted[-1]['message']


def test_steer_rejects_an_idle_engine(monkeypatch):
    emitted = []
    monkeypatch.setattr(common, 'emit', emitted.append)
    backend = SimpleNamespace(engine=_FakeEngine(), _chat_busy=False, _active_tool={})

    ChatMixin.handle_steer(backend, 'hello?', 'req-idle')

    assert emitted == [{
        'type': 'error',
        'error': 'No active chat run to steer.',
        'request_id': 'req-idle',
    }]


def test_tui_heartbeat_advances_graph_without_standalone_daemon(tmp_path):
    graph = GraphDefinition(
        graph_id='tui_heartbeat',
        nodes=[NodeSpec('finish', 'noop', terminal=True)],
        edges=[],
        entry_nodes=['finish'],
    )
    run = start_run(tmp_path, graph, run_id='tui_heartbeat_run')
    emitted = []

    result = _tick_orchestrations_once(
        tmp_path,
        announced={},
        emit_fn=emitted.append,
    )

    assert run['status'] == 'running'
    assert result['graph'][0]['run_id'] == 'tui_heartbeat_run'
    assert result['graph'][0]['status'] == 'completed'
    assert get_run(tmp_path, 'tui_heartbeat_run')['status'] == 'completed'
    assert emitted == [{
        'type': 'status',
        'message': 'Graph operation tui_heartbeat_run completed',
    }]


def test_tui_heartbeat_deduplicates_repeated_terminal_and_error_events(
    tmp_path,
    monkeypatch,
):
    from charon.libris import libris_durable
    from charon.orchestration import graph_executors, graph_runtime, runtime

    calls = {'legacy_register': 0, 'graph_register': 0}
    registered_state_dirs = []

    def register_legacy(state_dir):
        calls['legacy_register'] += 1
        registered_state_dirs.append(state_dir)

    def register_graph():
        calls['graph_register'] += 1

    monkeypatch.setattr(libris_durable, 'register', register_legacy)
    monkeypatch.setattr(graph_executors, 'register_builtin_executors', register_graph)
    monkeypatch.setattr(
        runtime,
        'tick_operations',
        lambda *_args, **_kwargs: [{
            'op_id': 'op_done',
            'action': 'done',
            'status': 'done',
        }],
    )
    monkeypatch.setattr(
        graph_runtime,
        'tick_runs',
        lambda *_args, **_kwargs: [{
            'run_id': 'broken_run',
            'action': 'error',
            'status': 'unreadable',
            'error': 'invalid snapshot',
        }],
    )
    announced = {}
    emitted = []

    _tick_orchestrations_once(
        tmp_path,
        announced=announced,
        emit_fn=emitted.append,
    )
    _tick_orchestrations_once(
        tmp_path,
        announced=announced,
        emit_fn=emitted.append,
    )

    assert calls == {'legacy_register': 2, 'graph_register': 2}
    assert registered_state_dirs == [tmp_path, tmp_path]
    assert emitted == [
        {
            'type': 'status',
            'message': 'Durable operation op_done done',
        },
        {
            'type': 'error',
            'error': 'Graph operation broken_run error: invalid snapshot',
        },
    ]
