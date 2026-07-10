"""tmux/boat session helpers: sockets, capture, readiness waits."""
from __future__ import annotations

import base64
import json
import os
import socket
import time
from pathlib import Path

from backend import common
from backend.textutils import _extract_meaningful_text, _last_visible_line, _normalize_visible_text

try:
    from charon.infra.diagnostics import record as _diag
except Exception:  # diagnostics is best-effort and must never block import
    def _diag(*args, **kwargs):
        return None


def _boat_registry_path(session_name: str) -> Path:
    name = str(session_name or '').strip()
    if name and not name.startswith('boat-'):
        name = f'boat-{name}'
    return Path.home() / '.charon' / 'boats' / f'{name}.json'


def _boat_socket_for_session(session_name: str) -> str:
    try:
        reg = common._load_json(_boat_registry_path(session_name), {})
        return str(reg.get('socket') or '').strip()
    except Exception:
        return ''


def _terminate_boat_session(session_name: str) -> bool:
    name = str(session_name or '').strip()
    if not name:
        return False
    reg_path = _boat_registry_path(name)
    reg = common._load_json(reg_path, {}) if reg_path.exists() else {}
    killed = False

    pid = int(reg.get('pid') or 0) if str(reg.get('pid') or '').isdigit() else 0
    if pid > 0:
        try:
            os.kill(pid, 15)
            killed = True
            time.sleep(0.2)
        except ProcessLookupError:
            killed = True
        except Exception as e:
            _diag('boat', 'SIGTERM to boat process failed; session may not terminate', error=e, pid=pid)
        try:
            os.kill(pid, 0)
            try:
                os.kill(pid, 9)
                killed = True
            except Exception as e:
                _diag('boat', 'SIGKILL to boat process failed; session may not terminate', error=e, pid=pid)
        except Exception:
            pass

    socket_path = str(reg.get('socket') or '').strip()
    for candidate in [socket_path, str(reg_path), str(reg_path.with_suffix('.log'))]:
        if not candidate:
            continue
        try:
            Path(candidate).unlink(missing_ok=True)
        except Exception as e:
            _diag('boat', 'failed to remove boat session file; stale socket/registry file left behind', error=e, file=candidate)
    return killed or reg_path.exists() or bool(socket_path)


def _boat_send_input(session_name: str, text: str) -> bool:
    sock_path = _boat_socket_for_session(session_name)
    if not sock_path:
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(2.0)
            sock.connect(sock_path)
            payload = {
                'type': 'input',
                'data': base64.b64encode(text.encode('utf-8')).decode('ascii'),
            }
            sock.sendall((json.dumps(payload) + '\n').encode('utf-8'))
        return True
    except Exception as e:
        _diag('boat', 'sending input to boat socket failed; send reported as failure', error=e, name=session_name)
        return False


def _boat_capture_output(
    session_name: str,
    timeout: float = 20.0,
    prompt_hint: str = '',
    quiet_period: float = 0.9,
    completion_timeout: float = 10.0,
) -> dict:
    sock_path = _boat_socket_for_session(session_name)
    if not sock_path:
        return {'ok': False, 'error': 'missing socket', 'output': '', 'last_line': ''}
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.25)
            sock.connect(sock_path)
            reader = sock.makefile('r', encoding='utf-8', errors='ignore')
            sock.sendall((json.dumps({'type': 'subscribe'}) + '\n').encode('utf-8'))
            drain_deadline = time.time() + 0.5
            while time.time() < drain_deadline:
                try:
                    _ = reader.readline()
                except Exception:
                    break

            prompt_norm = _normalize_visible_text(prompt_hint)
            start_deadline = time.time() + timeout
            completion_deadline: float | None = None
            chunks: list[str] = []
            last_line = ''
            meaningful_since: float | None = None
            last_data_at = time.time()
            while True:
                now = time.time()
                if meaningful_since is None and now >= start_deadline:
                    break
                if meaningful_since is not None and completion_deadline is not None and now >= completion_deadline:
                    aggregate = ''.join(chunks)
                    meaningful = _extract_meaningful_text(aggregate, prompt_hint)
                    return {
                        'ok': True,
                        'output': aggregate[-4000:],
                        'last_line': _last_visible_line(meaningful),
                        'message_text': meaningful,
                    }
                try:
                    line = reader.readline()
                except socket.timeout:
                    if meaningful_since is not None and (time.time() - last_data_at) >= quiet_period:
                        aggregate = ''.join(chunks)
                        meaningful = _extract_meaningful_text(aggregate, prompt_hint)
                        return {
                            'ok': True,
                            'output': aggregate[-4000:],
                            'last_line': _last_visible_line(meaningful),
                            'message_text': meaningful,
                        }
                    continue
                except Exception:
                    break
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except Exception:
                    continue
                if msg.get('type') != 'output':
                    continue
                try:
                    raw = base64.b64decode(str(msg.get('data') or ''))
                    text_chunk = raw.decode('utf-8', errors='ignore')
                except Exception:
                    text_chunk = ''
                if not text_chunk:
                    continue
                last_data_at = time.time()
                chunks.append(text_chunk)
                candidate = _last_visible_line(text_chunk)
                if candidate:
                    last_line = candidate
                aggregate = ''.join(chunks)
                aggregate_norm = _normalize_visible_text(aggregate)
                last_norm = _normalize_visible_text(last_line)
                is_echo = bool(prompt_norm) and (
                    (last_norm and (last_norm in prompt_norm or prompt_norm in last_norm))
                    or (aggregate_norm and aggregate_norm in prompt_norm)
                )
                meaningful = _extract_meaningful_text(aggregate, prompt_hint)
                has_real_response = bool(meaningful) and not is_echo
                if has_real_response:
                    if meaningful_since is None:
                        meaningful_since = time.time()
                    completion_deadline = time.time() + completion_timeout
            meaningful = _extract_meaningful_text(''.join(chunks), prompt_hint)
            return {
                'ok': False,
                'error': 'timeout waiting for output',
                'output': ''.join(chunks)[-4000:],
                'last_line': _last_visible_line(meaningful),
                'message_text': meaningful,
            }
    except Exception as e:
        return {'ok': False, 'error': str(e), 'output': '', 'last_line': ''}


def _boat_prompt_and_capture(
    session_name: str,
    text: str,
    timeout: float = 20.0,
    quiet_period: float = 0.9,
    completion_timeout: float = 10.0,
) -> dict:
    if not _boat_send_input(session_name, text):
        return {'ok': False, 'error': 'failed to send input', 'output': '', 'last_line': '', 'message_text': ''}
    return _boat_capture_output(
        session_name,
        timeout=timeout,
        prompt_hint=text,
        quiet_period=quiet_period,
        completion_timeout=completion_timeout,
    )


def _wait_for_boat_socket(session_name: str, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        sock = _boat_socket_for_session(session_name)
        if sock and Path(sock).exists():
            return True
        time.sleep(0.2)
    return False


def _wait_for_boat_ready(session_name: str, timeout: float = 30.0) -> bool:
    sock_path = _boat_socket_for_session(session_name)
    if not sock_path:
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            sock.connect(sock_path)
            reader = sock.makefile('r', encoding='utf-8', errors='ignore')
            sock.sendall((json.dumps({'type': 'subscribe'}) + '\n').encode('utf-8'))
            deadline = time.time() + timeout
            observed = ''
            while time.time() < deadline:
                try:
                    line = reader.readline()
                except socket.timeout:
                    continue
                except Exception as e:
                    _diag('boat', 'boat output stream read failed during readiness wait; ready-wait degrades to timeout', error=e, name=session_name)
                    break
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except Exception:
                    continue
                if msg.get('type') != 'output':
                    continue
                try:
                    raw = base64.b64decode(str(msg.get('data') or ''))
                    chunk = raw.decode('utf-8', errors='ignore')
                except Exception:
                    chunk = ''
                if not chunk:
                    continue
                observed += chunk
                norm = _normalize_visible_text(observed[-4000:])
                if ('type a message' in norm) or ('❯' in observed[-4000:]) or ('hermes' in norm and len(norm) > 20):
                    return True
            return False
    except Exception as e:
        _diag('boat', 'boat socket connect failed during readiness wait; session treated as not ready', error=e, name=session_name)
        return False
