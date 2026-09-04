"""Shared bootstrap and low-level helpers for the chat backend package.

Holds the sys.path bootstrap for src/ (the charon package) plus the module-level
state (ROOT, STATE_DIR) and the emit/_load_json primitives. Other backend
modules access mutable/patchable state via attribute lookup (common.emit,
common.STATE_DIR) so tests can monkeypatch it in one place.
"""
from __future__ import annotations

import json
import os
import sys
import threading
from collections import OrderedDict
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Iterator

# Suppress noisy library output that would corrupt the JSON protocol
os.environ['TRANSFORMERS_VERBOSITY'] = 'error'
os.environ['TOKENIZERS_PARALLELISM'] = 'false'
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / 'src'))

STATE_DIR = ROOT / '.charon_state'

try:
    from charon.infra import config
    from charon.infra.diagnostics import record as _diag
except Exception:  # diagnostics is best-effort and must never block import
    config = None
    def _diag(*args, **kwargs):
        return None


_emit_lock = threading.Lock()
_pending_stream_events: OrderedDict[tuple[str, str], dict] = OrderedDict()
_seen_streams: set[tuple[str, str]] = set()
_flush_timer: threading.Timer | None = None
_deferred_chat_completes: ContextVar[list[dict] | None] = ContextVar(
    'charon_deferred_chat_completes',
    default=None,
)


@contextmanager
def defer_chat_complete() -> Iterator[list[dict]]:
    """Hold terminal events until the owning chat worker has released state.

    Context variables follow the coroutine submitted to ``AsyncRuntime``, so
    completions emitted on its event-loop thread are deferred alongside ones
    emitted synchronously by command handlers.
    """
    events: list[dict] = []
    token = _deferred_chat_completes.set(events)
    try:
        yield events
    finally:
        _deferred_chat_completes.reset(token)


def _write_events_locked(events: list[dict]) -> None:
    if not events:
        return
    sys.stdout.write(''.join(
        json.dumps(event, ensure_ascii=False) + '\n' for event in events
    ))
    sys.stdout.flush()


def _flush_stream_events_locked() -> None:
    global _flush_timer
    timer = _flush_timer
    events = list(_pending_stream_events.values())
    _pending_stream_events.clear()
    _flush_timer = None
    if timer is not None and timer is not threading.current_thread():
        timer.cancel()
    _write_events_locked(events)


def _flush_stream_events() -> None:
    with _emit_lock:
        _flush_stream_events_locked()


def _schedule_stream_flush_locked(delay_ms: int) -> None:
    global _flush_timer
    if _flush_timer is not None:
        return

    def flush_if_current() -> None:
        with _emit_lock:
            if _flush_timer is timer:
                _flush_stream_events_locked()

    timer = threading.Timer(delay_ms / 1000.0, flush_if_current)
    timer.daemon = True
    _flush_timer = timer
    timer.start()


def emit(event: dict):
    """Send a JSON event to the frontend with bounded stream coalescing.

    The first delta is always immediate. Subsequent deltas for the same request
    are combined for at most CHARON_STREAM_COALESCE_MS, and every lifecycle
    event first flushes pending text to preserve protocol ordering.
    """
    if event.get('type') == 'chat_complete':
        deferred = _deferred_chat_completes.get()
        if deferred is not None:
            deferred.append(dict(event))
            return

    with _emit_lock:
        event_type = str(event.get('type') or '')
        request_id = str(event.get('request_id') or '')
        coalescible = event_type in {'chat_delta', 'thinking_delta'}
        delay_ms = config.stream_coalesce_ms() if config is not None else 0

        if coalescible and delay_ms > 0:
            key = (request_id, event_type)
            if key not in _seen_streams:
                _flush_stream_events_locked()
                _seen_streams.add(key)
                _write_events_locked([event])
                return
            pending = _pending_stream_events.get(key)
            if pending is None:
                _pending_stream_events[key] = dict(event)
            else:
                pending['text'] = str(pending.get('text') or '') + str(event.get('text') or '')
            _schedule_stream_flush_locked(delay_ms)
            return

        _flush_stream_events_locked()
        _write_events_locked([event])
        if event_type == 'chat_complete':
            _seen_streams.difference_update(
                [key for key in _seen_streams if key[0] == request_id]
            )


def _load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception as e:
        _diag('common', 'state JSON file unreadable; using default value', error=e, file=path.name)
        return default
