"""Session registry — tracks live Charon instances for cross-session visibility.

Each running Charon instance registers itself in .charon_state/live_sessions/.
Other instances can discover and read conversations from registered sessions.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

try:
    from charon.infra.diagnostics import record as _diag
except Exception:  # diagnostics is best-effort and must never block import
    def _diag(*args, **kwargs):
        return None


def _live_dir(state_dir: Path) -> Path:
    d = state_dir / 'live_sessions'
    d.mkdir(parents=True, exist_ok=True)
    return d


def register_session(state_dir: Path, session_id: str, pid: int | None = None) -> None:
    """Register this Charon instance as a live session."""
    path = _live_dir(state_dir) / f'{session_id}.json'
    path.write_text(json.dumps({
        'session_id': session_id,
        'pid': pid or os.getpid(),
        'started': time.time(),
        'last_heartbeat': time.time(),
    }))


def heartbeat(state_dir: Path, session_id: str) -> None:
    """Update heartbeat timestamp."""
    path = _live_dir(state_dir) / f'{session_id}.json'
    if path.exists():
        try:
            data = json.loads(path.read_text())
            data['last_heartbeat'] = time.time()
            path.write_text(json.dumps(data))
        except Exception as e:
            _diag('session_registry', 'heartbeat update failed; session may look stale to peers', error=e, session_id=session_id)


def unregister_session(state_dir: Path, session_id: str) -> None:
    """Remove session registration."""
    path = _live_dir(state_dir) / f'{session_id}.json'
    try:
        path.unlink(missing_ok=True)
    except Exception as e:
        _diag('session_registry', 'session unregister failed; stale registration lingers', error=e, session_id=session_id)


def list_live_sessions(state_dir: Path, max_age: float = 30.0) -> list[dict]:
    """List all live sessions (heartbeat within max_age seconds)."""
    result = []
    now = time.time()
    for f in _live_dir(state_dir).glob('*.json'):
        try:
            data = json.loads(f.read_text())
            age = now - data.get('last_heartbeat', 0)
            if age < max_age:
                data['age'] = age
                data['alive'] = _is_pid_alive(data.get('pid', 0))
                result.append(data)
            elif age > max_age * 3:
                # Stale — clean up
                f.unlink(missing_ok=True)
        except Exception:
            continue
    return result


def _is_pid_alive(pid: int) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def send_steer(state_dir: Path, target_session_id: str, message: str) -> bool:
    """Send a steering message to another Charon instance."""
    steer_dir = state_dir / 'live_sessions' / 'steer'
    steer_dir.mkdir(parents=True, exist_ok=True)
    steer_file = steer_dir / f'{target_session_id}.jsonl'
    try:
        with steer_file.open('a') as f:
            f.write(json.dumps({
                'message': message,
                'timestamp': time.time(),
                'from_pid': os.getpid(),
            }) + '\n')
        return True
    except Exception as e:
        _diag('session_registry', 'steer message write failed; steer silently dropped', error=e, target_session_id=target_session_id)
        return False


def read_steers(state_dir: Path, session_id: str) -> list[dict]:
    """Read and consume steering messages for this session."""
    steer_file = state_dir / 'live_sessions' / 'steer' / f'{session_id}.jsonl'
    if not steer_file.exists():
        return []
    try:
        lines = steer_file.read_text().splitlines()
        steer_file.unlink(missing_ok=True)
        return [json.loads(ln) for ln in lines if ln.strip()]
    except Exception as e:
        _diag('session_registry', 'steer file read/parse failed; pending steers dropped', error=e, session_id=session_id)
        return []
