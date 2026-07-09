#!/usr/bin/env python3
"""charons-boat stream — stream PTY-backed boat sessions AND tmux sessions as JSON lines over stdio."""
from __future__ import annotations

import base64
import json
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

BOAT_DIR = Path.home() / ".charon" / "boats"
HEARTBEAT_INTERVAL = 30

# Known agent process names for tmux auto-discovery
KNOWN_AGENTS = {
    'claude': 'claude-code',
    'pi': 'pi',
    'codex': 'codex',
    'hermes': 'hermes',
    'opencode': 'opencode',
    'aider': 'aider',
    'cursor': 'cursor',
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _send(msg: dict) -> None:
    sys.stdout.write(json.dumps(msg, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _detect_agent_type(pane_pid: str, cmd: str) -> str | None:
    """Check if a tmux pane runs a known agent process."""
    cmd_lower = cmd.lower()
    for key, agent_type in KNOWN_AGENTS.items():
        if key in cmd_lower:
            return agent_type
    # Check child processes
    try:
        result = subprocess.run(
            ['pgrep', '-a', '-P', pane_pid],
            capture_output=True, text=True, timeout=3,
        )
        if result.returncode == 0:
            for line in result.stdout.strip().splitlines():
                line_lower = line.lower()
                for key, agent_type in KNOWN_AGENTS.items():
                    if key in line_lower:
                        return agent_type
    except Exception:
        pass
    return None


def _load_sessions() -> list[dict]:
    sessions: list[dict] = []

    # 1. Boat-wrapped PTY sessions
    if BOAT_DIR.exists():
        for path in sorted(BOAT_DIR.glob("*.json")):
            try:
                reg = json.loads(path.read_text())
            except Exception:
                continue
            if reg.get("transport") != "pty":
                continue
            sock = reg.get("socket", "")
            status = reg.get("status", "idle")
            if sock and not Path(sock).exists() and status == "running":
                status = "stale"
            if status not in ("running", "starting"):
                continue
            sessions.append({
                "id": reg.get("session") or reg.get("id") or path.stem,
                "name": reg.get("name") or path.stem,
                "agent": str(reg.get("command", "")).split(" ")[0] or path.stem,
                "status": status,
                "cols": int(reg.get("cols", 80)),
                "rows": int(reg.get("rows", 24)),
                "socket": sock,
                "transport": "pty",
            })

    # 2. Tmux sessions with known agent processes
    boat_ids = {s["id"] for s in sessions}
    boat_names = {s["name"] for s in sessions}
    try:
        result = subprocess.run(
            ['tmux', 'list-panes', '-a', '-F',
             '#{session_name}\t#{pane_pid}\t#{pane_current_command}\t#{pane_tty}'],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            seen_sessions: set[str] = set()
            for line in result.stdout.strip().splitlines():
                parts = line.split('\t')
                if len(parts) < 4:
                    continue
                sess_name, pane_pid, cmd, tty = parts[0], parts[1], parts[2], parts[3]
                tmux_id = f"tmux-{sess_name}"
                # Skip if already a boat session or already seen
                if tmux_id in boat_ids or sess_name in boat_names or sess_name in seen_sessions:
                    continue
                # Also skip boat-prefixed tmux sessions (those are boat-managed)
                if sess_name.startswith("boat-"):
                    continue
                agent_type = _detect_agent_type(pane_pid, cmd)
                if agent_type:
                    seen_sessions.add(sess_name)
                    # Get actual terminal dimensions
                    cols, rows = 80, 24
                    try:
                        dim = subprocess.run(
                            ['tmux', 'display-message', '-t', sess_name, '-p', '#{window_width}\t#{window_height}'],
                            capture_output=True, text=True, timeout=3,
                        )
                        if dim.returncode == 0:
                            dp = dim.stdout.strip().split('\t')
                            if len(dp) >= 2:
                                cols, rows = int(dp[0]), int(dp[1])
                    except Exception:
                        pass
                    sessions.append({
                        "id": tmux_id,
                        "name": sess_name,
                        "agent": agent_type,
                        "status": "running",
                        "cols": cols,
                        "rows": rows,
                        "transport": "tmux",
                        "tmux_session": sess_name,
                        "pane_tty": tty,
                    })
    except Exception:
        pass

    return sessions


class FocusConnection:
    """Focus connection for boat PTY sessions via Unix socket."""

    def __init__(self) -> None:
        self.sock: socket.socket | None = None
        self.file = None
        self.session_id = ""
        self.stop = threading.Event()
        self.reader: threading.Thread | None = None

    def close(self) -> None:
        self.stop.set()
        if self.sock is not None:
            try:
                self.sock.close()
            except Exception:
                pass
        self.sock = None
        self.file = None
        self.session_id = ""
        self.reader = None
        self.stop = threading.Event()

    def connect(self, session: dict) -> bool:
        self.close()
        sock_path = session.get("socket", "")
        if not sock_path:
            return False
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(sock_path)
        f = sock.makefile("r")
        self.sock = sock
        self.file = f
        self.session_id = session["id"]
        self._send({"type": "subscribe"})

        def _reader() -> None:
            try:
                for line in f:
                    if self.stop.is_set():
                        break
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        msg = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    _send(msg)
            finally:
                self.close()

        self.reader = threading.Thread(target=_reader, daemon=True)
        self.reader.start()
        return True

    def _send(self, msg: dict) -> None:
        if self.sock is None:
            return
        self.sock.sendall((json.dumps(msg, separators=(",", ":")) + "\n").encode())

    def input(self, data: str) -> None:
        self._send({"type": "input", "data": data})

    def resize(self, cols: int, rows: int) -> None:
        self._send({"type": "resize", "cols": cols, "rows": rows})


class TmuxFocusConnection:
    """Focus connection for tmux sessions — uses capture-pane + pane TTY write."""

    def __init__(self) -> None:
        self.tmux_session = ""
        self.session_id = ""
        self.pane_tty = ""
        self.stop = threading.Event()
        self.poll_thread: threading.Thread | None = None

    def close(self) -> None:
        self.stop.set()
        self.tmux_session = ""
        self.session_id = ""
        self.pane_tty = ""
        self.poll_thread = None
        self.stop = threading.Event()

    def connect(self, session: dict) -> bool:
        self.close()
        self.tmux_session = session.get("tmux_session", "")
        self.session_id = session["id"]
        self.pane_tty = session.get("pane_tty", "")
        if not self.tmux_session:
            return False

        # Verify session exists
        try:
            check = subprocess.run(
                ['tmux', 'has-session', '-t', self.tmux_session],
                capture_output=True, timeout=3,
            )
            if check.returncode != 0:
                return False
        except Exception:
            return False

        # Send initial full screen capture
        self._capture_and_send(full=True)

        # Start polling thread
        sess = self.tmux_session
        sid = self.session_id

        def _poll() -> None:
            last_content = ""
            while not self.stop.is_set():
                try:
                    result = subprocess.run(
                        ['tmux', 'capture-pane', '-t', sess, '-p', '-e'],
                        capture_output=True, text=False, timeout=3,
                    )
                    if result.returncode == 0:
                        content = result.stdout
                        if content != last_content.encode() if isinstance(last_content, str) else content != last_content:
                            # Send clear + home + content as output
                            raw = b"\x1b[2J\x1b[H" + (content if isinstance(content, bytes) else content.encode())
                            encoded = base64.b64encode(raw).decode("ascii")
                            _send({"type": "output", "session": sid, "data": encoded})
                            last_content = content
                    else:
                        # Session gone
                        _send({"type": "status", "session": sid, "status": "exited"})
                        break
                except Exception:
                    pass
                time.sleep(0.1)

        self.poll_thread = threading.Thread(target=_poll, daemon=True)
        self.poll_thread.start()
        return True

    def _capture_and_send(self, full: bool = False) -> None:
        try:
            result = subprocess.run(
                ['tmux', 'capture-pane', '-t', self.tmux_session, '-p', '-e'],
                capture_output=True, text=False, timeout=3,
            )
            if result.returncode == 0:
                raw = b"\x1b[2J\x1b[H" + result.stdout
                encoded = base64.b64encode(raw).decode("ascii")
                _send({"type": "output", "session": self.session_id, "data": encoded})
        except Exception:
            pass

    def input(self, data: str) -> None:
        """Send input to the tmux pane."""
        if not self.tmux_session:
            return
        try:
            decoded = base64.b64decode(data)
        except Exception:
            return
        # Prefer writing directly to pane TTY for raw byte fidelity
        if self.pane_tty and Path(self.pane_tty).exists():
            try:
                with open(self.pane_tty, 'wb') as f:
                    f.write(decoded)
                return
            except Exception:
                pass
        # Fallback: tmux send-keys with literal flag
        try:
            text = decoded.decode('utf-8', errors='replace')
            subprocess.run(
                ['tmux', 'send-keys', '-t', self.tmux_session, '-l', text],
                capture_output=True, timeout=3,
            )
        except Exception:
            pass

    def resize(self, cols: int, rows: int) -> None:
        if not self.tmux_session:
            return
        try:
            subprocess.run(
                ['tmux', 'resize-window', '-t', self.tmux_session,
                 '-x', str(cols), '-y', str(rows)],
                capture_output=True, timeout=3,
            )
        except Exception:
            pass


def _input_loop() -> None:
    focused = FocusConnection()
    tmux_focused = TmuxFocusConnection()
    active_focus: FocusConnection | TmuxFocusConnection | None = None
    sessions = {s["id"]: s for s in _load_sessions()}
    _send({"type": "sessions", "sessions": list(sessions.values())})
    last_heartbeat = time.monotonic()
    last_refresh = time.monotonic()

    for line in sys.stdin:
        now = time.monotonic()
        if now - last_heartbeat > HEARTBEAT_INTERVAL:
            _send({"type": "ping", "ts": _now()})
            last_heartbeat = now
        if now - last_refresh > 5.0:
            sessions = {s["id"]: s for s in _load_sessions()}
            _send({"type": "sessions", "sessions": list(sessions.values())})
            last_refresh = now

        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        typ = msg.get("type", "")
        if typ == "focus":
            sid = msg.get("session", "")
            sessions = {s["id"]: s for s in _load_sessions()}
            sess = sessions.get(sid)
            if sess:
                transport = sess.get("transport", "pty")
                if transport == "tmux":
                    focused.close()
                    if tmux_focused.connect(sess):
                        active_focus = tmux_focused
                else:
                    tmux_focused.close()
                    if focused.connect(sess):
                        active_focus = focused
        elif typ == "input":
            if active_focus is not None:
                active_focus.input(msg.get("data", ""))
        elif typ == "resize":
            if active_focus is not None:
                active_focus.resize(int(msg.get("cols", 80)), int(msg.get("rows", 24)))
        elif typ == "pong":
            pass


def main() -> int:
    try:
        _input_loop()
        return 0
    except (BrokenPipeError, KeyboardInterrupt):
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
