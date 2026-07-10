"""tmux session capture and input for Charon.

Provides functions to:
- List all tmux sessions (local)
- Capture screen content from a tmux pane
- Send keystrokes to a tmux pane
- Capture via SSH for remote sessions

All operations are synchronous subprocess calls — designed to be called
from the TUI backend's refresh loop.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


def tmux_available() -> bool:
    return bool(shutil.which('tmux'))


@dataclass
class TmuxSession:
    name: str
    windows: int
    attached: bool
    created: str


def list_sessions() -> list[TmuxSession]:
    """List all local tmux sessions."""
    if not tmux_available():
        return []
    try:
        result = subprocess.run(
            ['tmux', 'list-sessions', '-F',
             '#{session_name}\t#{session_windows}\t#{session_attached}\t#{session_created}'],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return []
        sessions = []
        for line in result.stdout.strip().splitlines():
            parts = line.split('\t')
            if len(parts) >= 3:
                sessions.append(TmuxSession(
                    name=parts[0],
                    windows=int(parts[1]) if parts[1].isdigit() else 1,
                    attached=parts[2] == '1',
                    created=parts[3] if len(parts) > 3 else '',
                ))
        return sessions
    except Exception:
        return []


def capture_pane(session_name: str, width: int = 80, height: int = 24) -> str:
    """Capture the visible content of a tmux pane.

    Returns the screen content as a string (rows separated by newlines).
    """
    if not tmux_available():
        return '(tmux not available)'
    try:
        # Resize the pane to match our grid cell size before capturing
        # This ensures we get content that fits our display
        subprocess.run(
            ['tmux', 'resize-window', '-t', session_name,
             '-x', str(width), '-y', str(height)],
            capture_output=True, timeout=3,
        )
    except Exception:
        pass

    try:
        result = subprocess.run(
            ['tmux', 'capture-pane', '-t', session_name, '-p', '-e'],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return f'(capture failed: {result.stderr.strip()[:60]})'
        return result.stdout
    except subprocess.TimeoutExpired:
        return '(capture timed out)'
    except Exception as e:
        return f'(capture error: {e})'


def send_keys(session_name: str, keys: str) -> bool:
    """Send keystrokes to a tmux session.

    The keys string is passed directly to tmux send-keys.
    Special keys like Enter, Escape, etc. should use tmux names.
    """
    if not tmux_available():
        return False
    try:
        result = subprocess.run(
            ['tmux', 'send-keys', '-t', session_name, keys],
            capture_output=True, timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def send_key_literal(session_name: str, key: str) -> bool:
    """Send a single literal key to a tmux session.

    Unlike send_keys, this sends the key literally (not interpreted).
    """
    if not tmux_available():
        return False
    try:
        result = subprocess.run(
            ['tmux', 'send-keys', '-t', session_name, '-l', key],
            capture_output=True, timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


# ── Remote capture via SSH ────────────────────────────────────────────

def remote_list_sessions(ssh_target: str, control_path: str | None = None) -> list[TmuxSession]:
    """List tmux sessions on a remote host via SSH."""
    cmd = _ssh_cmd(ssh_target, control_path) + [
        'tmux', 'list-sessions', '-F',
        '#{session_name}\t#{session_windows}\t#{session_attached}',
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            return []
        sessions = []
        for line in result.stdout.strip().splitlines():
            parts = line.split('\t')
            if len(parts) >= 3:
                sessions.append(TmuxSession(
                    name=parts[0],
                    windows=int(parts[1]) if parts[1].isdigit() else 1,
                    attached=parts[2] == '1',
                    created='',
                ))
        return sessions
    except Exception:
        return []


def remote_capture_pane(ssh_target: str, session_name: str,
                        control_path: str | None = None) -> str:
    """Capture a tmux pane on a remote host via SSH."""
    cmd = _ssh_cmd(ssh_target, control_path) + [
        'tmux', 'capture-pane', '-t', session_name, '-p',
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if result.returncode != 0:
            return '(remote capture failed)'
        return result.stdout
    except Exception as e:
        return f'(remote error: {e})'


def remote_send_keys(ssh_target: str, session_name: str, keys: str,
                     control_path: str | None = None) -> bool:
    """Send keys to a tmux session on a remote host via SSH."""
    cmd = _ssh_cmd(ssh_target, control_path) + [
        'tmux', 'send-keys', '-t', session_name, keys,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=10)
        return result.returncode == 0
    except Exception:
        return False


def setup_ssh_control(ssh_target: str) -> str:
    """Set up an SSH ControlMaster for persistent connections.

    Returns the control socket path.
    """
    control_path = f'/tmp/charon-ssh-{ssh_target.replace("@", "-")}'
    try:
        subprocess.run(
            ['ssh', '-MNf',
             '-S', control_path,
             '-o', 'ControlPersist=600',
             '-o', 'StrictHostKeyChecking=accept-new',
             ssh_target],
            capture_output=True, timeout=15,
        )
    except Exception:
        pass
    return control_path


def _ssh_cmd(ssh_target: str, control_path: str | None = None) -> list[str]:
    cmd = ['ssh']
    if control_path:
        cmd.extend(['-S', control_path])
    cmd.extend(['-o', 'BatchMode=yes', '-o', 'ConnectTimeout=5', ssh_target])
    return cmd


# ── Boat registration discovery ──────────────────────────────────────

def discover_boat_sessions() -> list[dict]:
    """Find sessions registered via charons-boat."""
    boat_dir = Path.home() / '.charon' / 'boats'
    if not boat_dir.exists():
        return []
    sessions = []
    for f in boat_dir.glob('*.json'):
        try:
            data = json.loads(f.read_text())
            # Verify the tmux session still exists
            name = data.get('session', '')
            if name and any(s.name == name for s in list_sessions()):
                data['source'] = 'boat'
                sessions.append(data)
        except Exception:
            continue
    return sessions
