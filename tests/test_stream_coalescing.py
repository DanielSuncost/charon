from __future__ import annotations

import io
import json

from backend import common


def _reset_emitter():
    with common._emit_lock:
        if common._flush_timer is not None:
            common._flush_timer.cancel()
        common._flush_timer = None
        common._pending_stream_events.clear()
        common._seen_streams.clear()


def test_first_delta_is_immediate_and_following_deltas_coalesce(monkeypatch):
    _reset_emitter()
    output = io.StringIO()
    monkeypatch.setattr(common.sys, 'stdout', output)
    monkeypatch.setenv('CHARON_STREAM_COALESCE_MS', '100')

    common.emit({'type': 'chat_delta', 'text': 'a', 'request_id': 'r1'})
    assert len(output.getvalue().splitlines()) == 1

    common.emit({'type': 'chat_delta', 'text': 'b', 'request_id': 'r1'})
    common.emit({'type': 'chat_delta', 'text': 'c', 'request_id': 'r1'})
    common.emit({'type': 'turn_complete', 'request_id': 'r1'})

    events = [json.loads(line) for line in output.getvalue().splitlines()]
    assert [event['type'] for event in events] == [
        'chat_delta', 'chat_delta', 'turn_complete',
    ]
    assert events[0]['text'] == 'a'
    assert events[1]['text'] == 'bc'
    _reset_emitter()


def test_terminal_event_resets_first_delta_behavior(monkeypatch):
    _reset_emitter()
    output = io.StringIO()
    monkeypatch.setattr(common.sys, 'stdout', output)
    monkeypatch.setenv('CHARON_STREAM_COALESCE_MS', '100')

    common.emit({'type': 'chat_delta', 'text': 'first', 'request_id': 'r1'})
    common.emit({'type': 'chat_complete', 'request_id': 'r1'})
    before = len(output.getvalue().splitlines())
    common.emit({'type': 'chat_delta', 'text': 'next', 'request_id': 'r1'})

    assert len(output.getvalue().splitlines()) == before + 1
    _reset_emitter()
