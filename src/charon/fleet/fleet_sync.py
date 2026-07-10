"""Fleet sync — background polling of remote server status via SSH + boat protocol."""
from __future__ import annotations

import base64
import json
import subprocess
import threading
import time

from charon.fleet.fleet_registry import load_fleet

_fleet_cache: dict[str, dict] = {}
_cache_lock = threading.Lock()
_sync_thread: threading.Thread | None = None
_stop_event = threading.Event()

POLL_INTERVAL = 30.0
SSH_TIMEOUT = 10


def get_cached_fleet_status() -> dict:
    """Return the current fleet status cache (non-blocking)."""
    with _cache_lock:
        return dict(_fleet_cache)


def _build_ssh_command(server: dict) -> list[str]:
    """Build the SSH command to connect to a remote boat."""
    cmd = ['ssh']
    for opt in server.get('ssh_options', []):
        cmd.append(opt)
    cmd.extend(['-o', f'ConnectTimeout={SSH_TIMEOUT}', '-o', 'BatchMode=yes'])
    user = server.get('user', '')
    host = server.get('host', '')
    target = f'{user}@{host}' if user else host
    cmd.append(target)
    boat_command = server.get('boat_command', 'charons-boat stream')
    cmd.extend(boat_command.split())
    return cmd


def _poll_server(server: dict) -> dict:
    """Poll a single server for session status.

    Connects via SSH, reads the initial sessions list, then disconnects.
    Returns {agent_name: {status, session_id, ...}} dict.
    """
    cmd = _build_ssh_command(server)
    result: dict[str, dict] = {}

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        # Read lines until we get the sessions list or timeout
        deadline = time.monotonic() + SSH_TIMEOUT + 5
        sessions_msg = None

        while time.monotonic() < deadline:
            if proc.stdout is None:
                break
            line = proc.stdout.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get('type') == 'sessions':
                sessions_msg = msg
                break
            if msg.get('type') == 'ping':
                if proc.stdin:
                    proc.stdin.write('{"type":"pong"}\n')
                    proc.stdin.flush()

        # Close the SSH connection
        if proc.stdin:
            try:
                proc.stdin.close()
            except Exception:
                pass
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

        if sessions_msg:
            for sess in sessions_msg.get('sessions', []):
                name = sess.get('name', sess.get('id', ''))
                result[name] = {
                    'session_id': sess.get('id', ''),
                    'status': sess.get('status', 'unknown'),
                    'agent': sess.get('agent', ''),
                    'cols': sess.get('cols', 80),
                    'rows': sess.get('rows', 24),
                }

    except Exception:
        pass

    return result


def _auto_start_agents(server: dict, running_sessions: dict) -> None:
    """Start any auto_start agents that aren't running on the server."""
    for agent_cfg in server.get('agents', []):
        if not agent_cfg.get('auto_start', False):
            continue
        agent_name = agent_cfg.get('name', '')
        if agent_name in running_sessions:
            continue
        # Agent not running — try to start it via SSH
        agent_type = agent_cfg.get('type', 'bash')
        user = server.get('user', '')
        host = server.get('host', '')
        target = f'{user}@{host}' if user else host
        try:
            subprocess.run(
                ['ssh', '-o', 'BatchMode=yes', '-o', f'ConnectTimeout={SSH_TIMEOUT}',
                 target, 'charons-boat', 'wrap', '--name', agent_name, '--', agent_type],
                capture_output=True, timeout=SSH_TIMEOUT + 5,
            )
        except Exception:
            pass


def _poll_all() -> None:
    """Poll all fleet servers and update the cache."""
    fleet = load_fleet()
    new_cache: dict[str, dict] = {}

    for server in fleet.get('servers', []):
        server_id = server.get('id', server.get('host', ''))
        try:
            status = _poll_server(server)
            # Auto-start any configured agents that aren't running
            _auto_start_agents(server, status)
            new_cache[server_id] = {
                'sessions': status,
                'online': True,
                'last_poll': time.time(),
            }
        except Exception:
            new_cache[server_id] = {
                'sessions': {},
                'online': False,
                'last_poll': time.time(),
            }

    with _cache_lock:
        _fleet_cache.update(new_cache)


def _sync_loop() -> None:
    """Background thread that polls fleet status periodically."""
    while not _stop_event.is_set():
        try:
            _poll_all()
        except Exception:
            pass
        _stop_event.wait(POLL_INTERVAL)


def start_fleet_sync() -> None:
    """Start the background fleet sync thread (idempotent)."""
    global _sync_thread
    if _sync_thread is not None and _sync_thread.is_alive():
        return
    _stop_event.clear()
    _sync_thread = threading.Thread(target=_sync_loop, daemon=True, name='fleet-sync')
    _sync_thread.start()


def stop_fleet_sync() -> None:
    """Stop the background fleet sync thread."""
    _stop_event.set()


def send_to_remote_agent(server_id: str, agent_name: str, message: str) -> bool:
    """Send a message to a remote agent via its boat session."""
    fleet = load_fleet()
    server = next((s for s in fleet.get('servers', []) if s.get('id') == server_id), None)
    if not server:
        return False

    with _cache_lock:
        server_status = _fleet_cache.get(server_id, {})

    sessions = server_status.get('sessions', {})
    session_info = sessions.get(agent_name, {})
    session_id = session_info.get('session_id', '')
    if not session_id:
        return False

    cmd = _build_ssh_command(server)
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        if proc.stdin:
            # Focus the session, then send input
            proc.stdin.write(json.dumps({'type': 'focus', 'session': session_id}) + '\n')
            encoded = base64.b64encode(message.encode()).decode('ascii')
            proc.stdin.write(json.dumps({'type': 'input', 'session': session_id, 'data': encoded}) + '\n')
            proc.stdin.flush()
            proc.stdin.close()
        proc.wait(timeout=5)
        return True
    except Exception:
        return False


def get_remote_agent_history(server_id: str, agent_name: str, timeout: float = 5.0) -> str:
    """Get recent output from a remote agent by connecting and reading its output buffer."""
    fleet = load_fleet()
    server = next((s for s in fleet.get('servers', []) if s.get('id') == server_id), None)
    if not server:
        return ''

    with _cache_lock:
        server_status = _fleet_cache.get(server_id, {})

    sessions = server_status.get('sessions', {})
    session_info = sessions.get(agent_name, {})
    session_id = session_info.get('session_id', '')
    if not session_id:
        return ''

    cmd = _build_ssh_command(server)
    output_chunks: list[str] = []
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        if proc.stdin:
            proc.stdin.write(json.dumps({'type': 'focus', 'session': session_id}) + '\n')
            proc.stdin.flush()

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and proc.stdout:
            line = proc.stdout.readline()
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get('type') == 'output' and msg.get('session') == session_id:
                data = msg.get('data', '')
                try:
                    decoded = base64.b64decode(data).decode('utf-8', errors='replace')
                    output_chunks.append(decoded)
                except Exception:
                    pass

        if proc.stdin:
            try:
                proc.stdin.close()
            except Exception:
                pass
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    except Exception:
        pass

    return ''.join(output_chunks)
