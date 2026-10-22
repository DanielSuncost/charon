"""Live heartbeat for running shades: what each one is doing right now.

A shade's contract only changes at phase boundaries, and a phase can run for
many minutes, so the contract alone can't tell a busy worker from a dead one.
The shade loop feeds every engine event to an ActivityRecorder, which keeps
``<state>/shade_activity/<contract_id>.json`` current with the owning pid, the
last event time and a short "doing now" description. Readers combine that
with the in-process thread registry to tell working, quiet and lost apart.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

try:
    from charon.infra.diagnostics import record as _diag
except Exception:  # diagnostics is best-effort and must never block import
    def _diag(*args, **kwargs):
        return None

_WRITE_EVERY_SECONDS = 5.0
_threads: dict[str, threading.Thread] = {}
_threads_lock = threading.Lock()


def _activity_path(state_dir: Path, contract_id: str) -> Path:
    return Path(state_dir) / 'shade_activity' / f'{contract_id}.json'


def register_thread(contract_id: str, thread: threading.Thread) -> None:
    with _threads_lock:
        _threads[contract_id] = thread


def thread_alive(contract_id: str) -> bool | None:
    """True/False for a shade run by this process, None if it isn't ours."""
    with _threads_lock:
        thread = _threads.get(contract_id)
    return None if thread is None else thread.is_alive()


def pid_alive(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, ValueError, TypeError):
        return False
    return True


def load_activity(state_dir: Path, contract_id: str) -> dict:
    try:
        data = json.loads(_activity_path(state_dir, contract_id).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def last_activity_at(state_dir: Path, contract_id: str) -> float:
    return float(load_activity(state_dir, contract_id).get('last_event_at') or 0)


def _short(value, limit: int) -> str:
    text = ' '.join(str(value or '').split())
    return text if len(text) <= limit else text[:limit - 1] + '…'


def describe_tool_call(name: str, arguments) -> str:
    args = arguments
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            args = {}
    args = args if isinstance(args, dict) else {}
    for key in ('path', 'file_path', 'filePath'):
        if args.get(key):
            return f'{name} {Path(str(args[key])).name}'
    for key in ('command', 'query', 'pattern', 'url', 'action'):
        if args.get(key):
            return f'{name} {_short(args[key], 40)}'
    return name


class ActivityRecorder:
    """Turns engine events into a throttled on-disk heartbeat."""

    def __init__(self, state_dir: Path, contract_id: str):
        self.path = _activity_path(state_dir, contract_id)
        self.state = {
            'contract_id': contract_id,
            'pid': os.getpid(),
            'started_at': time.time(),
            'last_event_at': time.time(),
            'doing': 'starting',
            'doing_since': time.time(),
            'tool_calls': 0,
        }
        self._written_at = 0.0
        self._write()

    def _set_doing(self, doing: str) -> bool:
        if doing == self.state['doing']:
            return False
        self.state['doing'] = doing
        self.state['doing_since'] = time.time()
        return True

    def note(self, event_type: str, data: dict | None = None) -> None:
        data = data or {}
        self.state['last_event_at'] = time.time()
        if event_type == 'tool_call':
            self.state['tool_calls'] += 1
            changed = self._set_doing(describe_tool_call(str(data.get('tool_name') or 'tool'), data.get('arguments')))
        elif event_type == 'text_delta':
            changed = self._set_doing('writing')
        elif event_type in ('thinking_delta', 'turn_start', 'message_start'):
            changed = self._set_doing('thinking')
        elif event_type == 'phase':
            changed = self._set_doing(str(data.get('doing') or 'starting phase'))
        else:
            changed = False
        if changed or time.time() - self._written_at >= _WRITE_EVERY_SECONDS:
            self._write()

    def _write(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix('.tmp')
            tmp.write_text(json.dumps(self.state))
            tmp.replace(self.path)
            self._written_at = time.time()
        except OSError as exc:
            _diag('shade_activity', 'heartbeat write failed; team status may show this shade as quiet', error=exc)

