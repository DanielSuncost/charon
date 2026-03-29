#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import os
import select
import shutil
import signal
import socket
import sys
import termios
import tty
from pathlib import Path

BOAT_DIR = Path(os.environ.get("CHARON_BOAT_DIR", Path.home() / ".charon" / "boats"))


def load_registration(session: str) -> dict:
    session_name = session if session.startswith("boat-") else f"boat-{session}"
    reg_path = BOAT_DIR / f"{session_name}.json"
    if not reg_path.exists():
        raise SystemExit(f"boat session not found: {session}")
    return json.loads(reg_path.read_text())


def send(sock: socket.socket, msg: dict) -> None:
    sock.sendall((json.dumps(msg, separators=(",", ":")) + "\n").encode())


def current_size() -> tuple[int, int]:
    sz = shutil.get_terminal_size((80, 24))
    return sz.columns, sz.lines


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("session")
    ns = ap.parse_args()

    reg = load_registration(ns.session)
    sock_path = reg.get("socket", "")
    if not sock_path or not Path(sock_path).exists():
        raise SystemExit(f"boat session socket missing: {sock_path}")

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.connect(sock_path)
    send(sock, {"type": "subscribe"})
    cols, rows = current_size()
    send(sock, {"type": "resize", "cols": cols, "rows": rows})

    stdin_tty = sys.stdin.isatty()
    old_tty = termios.tcgetattr(sys.stdin.fileno()) if stdin_tty else None

    def on_winch(signum, frame):
        c, r = current_size()
        try:
            send(sock, {"type": "resize", "cols": c, "rows": r})
        except Exception:
            pass

    signal.signal(signal.SIGWINCH, on_winch)
    if stdin_tty:
        tty.setraw(sys.stdin.fileno())

    buffer = b""
    try:
        while True:
            watch = [sock]
            if stdin_tty:
                watch.append(sys.stdin)
            rlist, _, _ = select.select(watch, [], [])
            if sock in rlist:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if not line:
                        continue
                    try:
                        msg = json.loads(line.decode())
                    except Exception:
                        continue
                    if msg.get("type") == "output":
                        data = base64.b64decode(msg.get("data", ""))
                        if data:
                            os.write(sys.stdout.fileno(), data)
                    elif msg.get("type") == "status" and msg.get("status") == "exited":
                        return 0
            if sys.stdin in rlist:
                data = os.read(sys.stdin.fileno(), 4096)
                if not data:
                    break
                # Ctrl-] detaches locally.
                if data == b"\x1d":
                    return 0
                send(sock, {"type": "input", "data": base64.b64encode(data).decode("ascii")})
    finally:
        if old_tty is not None:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_tty)
        try:
            sock.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
