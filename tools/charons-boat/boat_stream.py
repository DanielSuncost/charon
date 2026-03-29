#!/usr/bin/env python3
"""charons-boat stream — stream PTY-backed boat sessions as JSON lines over stdio."""
from __future__ import annotations

import json
import socket
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

BOAT_DIR = Path.home() / ".charon" / "boats"
HEARTBEAT_INTERVAL = 30


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _send(msg: dict) -> None:
    sys.stdout.write(json.dumps(msg, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _load_sessions() -> list[dict]:
    sessions: list[dict] = []
    if not BOAT_DIR.exists():
        return sessions
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
        })
    return sessions


class FocusConnection:
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


def _input_loop() -> None:
    focused = FocusConnection()
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
            if sess and focused.connect(sess):
                pass
        elif typ == "input":
            focused.input(msg.get("data", ""))
        elif typ == "resize":
            focused.resize(int(msg.get("cols", 80)), int(msg.get("rows", 24)))
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
