"""Deterministic checks for input arriving after the engine's final queue check."""
import asyncio
import json
import threading

from backend import common
from backend.chat_mixin import ChatMixin
from backend.input_lifecycle import mailbox
from charon.conversation.conversation_engine import ConversationEngine
from charon.providers import ModelInfo, StreamDelta


class Provider:
    async def stream(self, **kwargs):
        yield StreamDelta(type='text', text='Completed normally.')
        yield StreamDelta(type='done', text='{}')


class Backend(ChatMixin):
    def __init__(self, tmp_path):
        self.engine = ConversationEngine(Provider(), ModelInfo(provider='mock', model_id='mock'),
                                         project_root=tmp_path, auto_compact=False)
        self._engine_lock = threading.Lock()
        self._chat_busy = False
        self._active_agent_id = 'test-input-handoff'
        self.submissions = []
        self.after_engine = None
        self.on_event = None

    def handle_chat(self, message, request_id):
        self.submissions.append(message)

        async def run():
            async for event in self.engine.submit(message):
                if self.on_event:
                    self.on_event(event)
                if event.type == 'steer_delivered':
                    mailbox(self).delivered_by_engine('steer', event.data['content'], request_id)
                elif event.type == 'follow_up_delivered':
                    mailbox(self).delivered_by_engine('follow_up', event.data['content'], request_id)
        asyncio.run(run())
        if self.after_engine:
            self.after_engine(message)
        common.emit({'type': 'chat_complete', 'request_id': request_id})


def setup_backend(tmp_path, monkeypatch):
    monkeypatch.setattr(common, 'STATE_DIR', tmp_path)
    events = []
    monkeypatch.setattr(common, '_write_events_locked', events.extend)
    return Backend(tmp_path), events


def test_late_enter_and_followup_drain_after_clean_completion(tmp_path, monkeypatch):
    backend, events = setup_backend(tmp_path, monkeypatch)
    with mailbox(backend).lock:
        mailbox(backend).accept('first', 'r1', 'chat')

    def late_input(message):
        if message == 'first':
            assert backend._chat_busy and not backend.engine._running
            backend.handle_steer('late Enter', 'r2')
            backend.handle_follow_up('late followup', 'r3')
    backend.after_engine = late_input
    backend._chat_worker('first', 'r1')
    assert backend.submissions == ['first', 'late Enter']
    assert backend.engine.pending_messages == 0
    assert not backend._chat_busy
    assert not mailbox(backend).records
    assert [e['request_id'] for e in events if e['type'] == 'chat_complete'] == ['r1']
    users = [m.content for m in backend.engine.messages if m.role == 'user']
    assert users == ['first', 'late Enter', 'late followup']
    trace = [json.loads(line) for line in (tmp_path / 'input_lifecycle.jsonl').read_text().splitlines()]
    assert [e['stage'] for e in trace if e['request_id'] == 'r2'] == [
        'accepted', 'acknowledged', 'stalled', 'delivered', 'completed']
    assert any(e['type'] == 'input_stalled' and e['request_id'] == 'r2' for e in events)
    assert all({'request_id', 'steering_depth', 'deferred_depth', 'engine_running', 'worker_busy'} <= e.keys()
               for e in trace)
    exited = next(e for e in trace if e['stage'] == 'worker_exit')
    assert exited['worker_busy'] is False
    assert exited['engine_running'] is False
    assert exited['steering_depth'] == exited['deferred_depth'] == 0


def test_enter_after_worker_exit_starts_instead_of_rejecting(tmp_path, monkeypatch):
    backend, _events = setup_backend(tmp_path, monkeypatch)
    finished = threading.Event()
    original = backend._chat_worker

    def worker(*args):
        try:
            original(*args)
        finally:
            finished.set()
    backend._chat_worker = worker
    backend.handle_steer('Enter after exit', 'r4')
    assert finished.wait(3)
    assert backend.submissions == ['Enter after exit']
    assert not backend._chat_busy


def test_worker_exception_still_delivers_accepted_input_and_finishes(tmp_path, monkeypatch):
    backend, events = setup_backend(tmp_path, monkeypatch)
    mailbox(backend).accept('first', 'r1', 'chat')

    def fail_first(message, request_id):
        backend.submissions.append(message)
        if message == 'first':
            backend.handle_steer('recover me', 'r2')
            raise RuntimeError('post-engine failure')
        common.emit({'type': 'chat_complete', 'request_id': request_id})
    backend.handle_chat = fail_first
    backend._chat_worker('first', 'r1')
    assert backend.submissions == ['first', 'recover me']
    assert not backend._chat_busy
    assert not mailbox(backend).records
    assert any(e['type'] == 'error' and 'post-engine failure' in e['error'] for e in events)


def test_active_steering_delivers_duplicate_text_once_per_request(tmp_path, monkeypatch):
    backend, events = setup_backend(tmp_path, monkeypatch)
    mailbox(backend).accept('first', 'r1', 'chat')
    sent = False

    def during_stream(event):
        nonlocal sent
        if event.type == 'text_delta' and not sent:
            sent = True
            backend.handle_steer('same text', 'r2')
            backend.handle_steer('same text', 'r3')
    backend.on_event = during_stream
    backend._chat_worker('first', 'r1')
    assert backend.submissions == ['first'], 'active steering stays in the active submission'
    assert [m.content for m in backend.engine.messages if m.role == 'user'] == ['first', 'same text', 'same text']
    assert not any(e['type'] == 'input_stalled' for e in events)
    for request in ['r2', 'r3']:
        assert [e['stage'] for e in events if e['type'] == 'input_lifecycle' and e['request_id'] == request] == [
            'accepted', 'acknowledged', 'delivered', 'completed']


def test_abort_does_not_park_acknowledged_input(tmp_path, monkeypatch):
    backend, _events = setup_backend(tmp_path, monkeypatch)
    mailbox(backend).accept('first', 'r1', 'chat')
    aborted = False

    def abort_once(event):
        nonlocal aborted
        if event.type == 'turn_start' and not aborted:
            aborted = True
            backend.handle_steer('after abort', 'r2')
            backend.engine.abort()
    backend.on_event = abort_once
    backend._chat_worker('first', 'r1')
    assert backend.submissions == ['first', 'after abort']
    assert not backend._chat_busy
    assert not mailbox(backend).records
    assert backend.engine.pending_messages == 0
