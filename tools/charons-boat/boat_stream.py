#!/usr/bin/env python3
"""charons-boat stream — stream tmux sessions as JSON lines over stdio.

This is the remote-side daemon. The local Rust TUI connects by running:
    ssh user@remote charons-boat stream

Protocol: JSON lines over stdin/stdout. See protocol.md for details.
"""
from __future__ import annotations

import base64
import json
import os
import select
import subprocess
import sys
import time
import threading
from datetime import datetime, timezone
from pathlib import Path

BOAT_DIR = Path(os.environ.get("CHARON_BOAT_DIR", Path.home() / ".charon" / "boats"))
POLL_INTERVAL = 0.1  # seconds between tmux captures
HEARTBEAT_INTERVAL = 30
APPROVAL_PATTERNS = [
    "approve?", "[y/n]", "[Y/n]", "(y/n)", "Allow ", "Confirm ",
    "Do you want to", "Should I ", "Proceed?", "Continue?",
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _send(msg: dict) -> None:
    """Send a JSON line to stdout."""
    line = json.dumps(msg, separators=(",", ":"))
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def _tmux_sessions() -> list[dict]:
    """List tmux sessions with metadata."""
    try:
        # Get session list
        result = subprocess.run(
            ["tmux", "list-sessions", "-F",
             "#{session_name}\t#{session_attached}\t#{session_windows}"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return []

        sessions = []
        for line in result.stdout.strip().splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            name = parts[0]
            attached = parts[1] == "1"

            # Get window dimensions from the first pane
            cols, rows = 120, 40
            try:
                dim_result = subprocess.run(
                    ["tmux", "display-message", "-t", name, "-p", "#{window_width}\t#{window_height}"],
                    capture_output=True, text=True, timeout=2,
                )
                if dim_result.returncode == 0:
                    dim_parts = dim_result.stdout.strip().split("\t")
                    if len(dim_parts) >= 2 and dim_parts[0].isdigit():
                        cols = int(dim_parts[0])
                        rows = int(dim_parts[1])
            except Exception:
                pass

            # Check registration file for agent info
            agent = name
            reg_file = BOAT_DIR / f"{name}.json"
            if reg_file.exists():
                try:
                    reg = json.loads(reg_file.read_text())
                    cmd = reg.get("command", "")
                    agent = cmd.split()[0] if cmd else name
                except Exception:
                    pass

            sessions.append({
                "id": name,
                "name": name,
                "agent": agent,
                "status": "running" if attached else "idle",
                "cols": cols,
                "rows": rows,
            })
        return sessions
    except Exception:
        return []


def _tmux_capture(session_name: str) -> str | None:
    """Capture the current pane content of a tmux session."""
    try:
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", session_name, "-p", "-e"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            return result.stdout
        return None
    except Exception:
        return None


def _tmux_send_keys(session_name: str, keys: str) -> bool:
    """Send keys to a tmux session."""
    try:
        result = subprocess.run(
            ["tmux", "send-keys", "-t", session_name, "-l", keys],
            capture_output=True, timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def _tmux_send_raw(session_name: str, data: bytes) -> bool:
    """Send raw bytes to a tmux session's pane."""
    try:
        # Get the pane TTY
        result = subprocess.run(
            ["tmux", "display-message", "-t", session_name, "-p", "#{pane_tty}"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return False
        tty = result.stdout.strip()
        if tty and os.path.exists(tty):
            with open(tty, "wb") as f:
                f.write(data)
            return True
        return False
    except Exception:
        return False


def _check_approval(text: str) -> str | None:
    """Check if terminal output contains an approval prompt."""
    last_lines = text.strip().split("\n")[-5:]
    last_text = "\n".join(last_lines).lower()
    for pattern in APPROVAL_PATTERNS:
        if pattern.lower() in last_text:
            return last_lines[-1].strip()
    return None


def _input_reader(focused: dict, sessions_by_id: dict):
    """Read JSON commands from stdin in a separate thread."""
    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue

            msg_type = msg.get("type", "")

            if msg_type == "focus":
                focused["id"] = msg.get("session", "")
                focused["last_screen"] = ""  # force full redraw

            elif msg_type == "input":
                session_id = msg.get("session", focused.get("id", ""))
                data = base64.b64decode(msg.get("data", ""))
                if session_id and data:
                    _tmux_send_raw(session_id, data)

            elif msg_type == "resize":
                session_id = msg.get("session", focused.get("id", ""))
                cols = msg.get("cols", 120)
                rows = msg.get("rows", 40)
                if session_id:
                    subprocess.run(
                        ["tmux", "resize-window", "-t", session_id,
                         "-x", str(cols), "-y", str(rows)],
                        capture_output=True, timeout=5,
                    )

            elif msg_type == "pong":
                pass  # acknowledged

            elif msg_type == "approve":
                session_id = msg.get("session", focused.get("id", ""))
                approved = msg.get("approved", False)
                if session_id:
                    key = "y\n" if approved else "n\n"
                    _tmux_send_keys(session_id, key)

    except (EOFError, BrokenPipeError):
        pass
    finally:
        # Client disconnected — exit cleanly
        os._exit(0)


def stream():
    """Main streaming loop."""
    BOAT_DIR.mkdir(parents=True, exist_ok=True)

    focused = {"id": "", "last_screen": ""}
    sessions_by_id: dict[str, dict] = {}

    # Start input reader thread
    reader_thread = threading.Thread(
        target=_input_reader, args=(focused, sessions_by_id), daemon=True,
    )
    reader_thread.start()

    # Send initial session list
    sessions = _tmux_sessions()
    sessions_by_id = {s["id"]: s for s in sessions}
    _send({"type": "sessions", "sessions": sessions})

    # Auto-focus the first session
    if sessions and not focused["id"]:
        focused["id"] = sessions[0]["id"]

    last_heartbeat = time.monotonic()
    last_session_check = time.monotonic()
    notified_approvals: set[str] = set()

    while True:
        now = time.monotonic()

        # Heartbeat
        if now - last_heartbeat > HEARTBEAT_INTERVAL:
            _send({"type": "ping", "ts": _now()})
            last_heartbeat = now

        # Refresh session list periodically
        if now - last_session_check > 5.0:
            new_sessions = _tmux_sessions()
            new_ids = {s["id"] for s in new_sessions}
            old_ids = set(sessions_by_id.keys())
            if new_ids != old_ids:
                sessions_by_id = {s["id"]: s for s in new_sessions}
                _send({"type": "sessions", "sessions": new_sessions})
            last_session_check = now

        # Capture focused session
        if focused["id"]:
            screen = _tmux_capture(focused["id"])
            if screen is not None and screen != focused["last_screen"]:
                focused["last_screen"] = screen

                # Send as raw output (base64 encoded)
                _send({
                    "type": "output",
                    "session": focused["id"],
                    "data": base64.b64encode(screen.encode("utf-8", errors="replace")).decode("ascii"),
                })

                # Check for approval prompts
                approval_text = _check_approval(screen)
                if approval_text:
                    approval_key = f"{focused['id']}:{approval_text[:50]}"
                    if approval_key not in notified_approvals:
                        notified_approvals.add(approval_key)
                        _send({
                            "type": "notify",
                            "session": focused["id"],
                            "kind": "approval",
                            "text": approval_text,
                        })
                else:
                    # Clear old approvals for this session
                    notified_approvals = {k for k in notified_approvals
                                         if not k.startswith(focused["id"] + ":")}

        # Also scan non-focused sessions for approval notifications
        for sid, sess in sessions_by_id.items():
            if sid == focused["id"]:
                continue
            screen = _tmux_capture(sid)
            if screen:
                approval_text = _check_approval(screen)
                if approval_text:
                    approval_key = f"{sid}:{approval_text[:50]}"
                    if approval_key not in notified_approvals:
                        notified_approvals.add(approval_key)
                        _send({
                            "type": "notify",
                            "session": sid,
                            "kind": "approval",
                            "text": approval_text,
                        })

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    try:
        stream()
    except (KeyboardInterrupt, BrokenPipeError):
        pass
