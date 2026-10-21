"""Request identity and durable evidence for input that has not reached history."""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import uuid

from backend import common


class InputMailbox:
    def __init__(self, backend):
        self.backend = backend
        # Acceptance and worker exit must make one atomic handoff decision.
        self.lock = threading.RLock()
        self.records: list[dict] = []

    def snapshot(self):
        engine = getattr(self.backend, 'engine', None)
        pending = [r for r in self.records if r['stage'] == 'acknowledged']
        backend_deferred = sum(r['kind'] == 'follow_up' for r in pending)
        tui_deferred = getattr(self.backend, '_tui_deferred_depth', 0)
        return {
            'steering_depth': sum(r['kind'] in {'steer', 'chat'} for r in pending),
            'deferred_depth': backend_deferred + tui_deferred,
            'backend_deferred_depth': backend_deferred,
            'tui_deferred_depth': tui_deferred,
            'pending_inputs': [{'request_id': r['request_id'], 'kind': r['kind']} for r in pending],
            'engine_running': bool(getattr(engine, '_running', False)),
            'worker_busy': bool(getattr(self.backend, '_chat_busy', False)),
        }

    def event(self, stage, record, **extra):
        if stage in {'accepted', 'acknowledged', 'delivered', 'completed'}:
            record['stage'] = stage
        event = {
            'type': 'input_lifecycle', 'stage': stage,
            'ts': time.time(), 'session_id': str(getattr(self.backend, '_active_agent_id', '') or ''),
            'request_id': record['request_id'], 'kind': record['kind'],
            'source': 'backend', 'chars': len(record['message']),
            'pid': os.getpid(),
            'content_sha256': hashlib.sha256(record['message'].encode()).hexdigest(),
            **self.snapshot(), **extra,
        }
        try:
            path = common.STATE_DIR / 'input_lifecycle.jsonl'
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(event) + '\n')
        except OSError as error:
            common.emit({'type': 'error', 'error': f'Could not record input lifecycle: {error}'})
        common.emit(event)

    def accept(self, message, request_id, kind):
        record = {'message': message, 'request_id': request_id or f'input-{uuid.uuid4().hex}',
                  'kind': kind, 'stage': 'accepted', 'worker_request_id': None}
        self.records.append(record)
        self.event('accepted', record)
        self.event('acknowledged', record)
        return record

    def deliver(self, record, worker_request_id):
        record['worker_request_id'] = worker_request_id
        self.event('delivered', record, worker_request_id=worker_request_id)

    def delivered_by_engine(self, kind, message, worker_request_id):
        with self.lock:
            record = next((r for r in self.records if r['stage'] == 'acknowledged'
                           and r['kind'] == kind and r['message'] == message), None)
            if record:
                self.deliver(record, worker_request_id)
            return record['request_id'] if record else None

    def complete(self, worker_request_id):
        for record in list(self.records):
            if record['stage'] == 'delivered' and record['worker_request_id'] == worker_request_id:
                self.event('completed', record, worker_request_id=worker_request_id)
                self.records.remove(record)

    def next_pending(self):
        pending = [r for r in self.records if r['stage'] == 'acknowledged']
        return next((r for r in pending if r['kind'] != 'follow_up'), pending[0] if pending else None)

    def stalled(self, record, reason):
        # Keep it acknowledged until delivery; the event is a diagnostic state.
        self.event('stalled', record, reason=reason)
        common.emit({'type': 'input_stalled', 'request_id': record['request_id'],
                     'message': f"Stalled input {record['request_id']}: {reason}", **self.snapshot()})


def mailbox(backend):
    if not hasattr(backend, '_input_mailbox'):
        backend._input_mailbox = InputMailbox(backend)
    return backend._input_mailbox
