#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import os
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import fcntl
import termios

BOAT_DIR = Path(os.environ.get("CHARON_BOAT_DIR", Path.home() / ".charon" / "boats"))
SOCK_DIR = Path(os.environ.get("CHARON_BOAT_SOCK_DIR", Path.home() / ".charon" / "boats" / "sockets"))


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class BoatSession:
    def __init__(self, name: str, command: list[str], cols: int, rows: int):
        self.name = name
        self.session_id = f"boat-{name}"
        self.command = command
        self.cols = cols
        self.rows = rows
        self.status = "starting"
        self.created = now()
        self.master_fd: int | None = None
        self.slave_fd: int | None = None
        self.proc: subprocess.Popen | None = None
        self.sock_path = SOCK_DIR / f"{self.session_id}.sock"
        self.reg_path = BOAT_DIR / f"{self.session_id}.json"
        self.clients: set[socket.socket] = set()
        self.clients_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.server: socket.socket | None = None
        self.history = bytearray()
        self.history_limit = 1024 * 1024

    def write_registry(self) -> None:
        payload = {
            "session": self.session_id,
            "id": self.session_id,
            "name": self.name,
            "command": " ".join(self.command),
            "pid": self.proc.pid if self.proc else None,
            "created": self.created,
            "status": self.status,
            "transport": "pty",
            "socket": str(self.sock_path),
            "cols": self.cols,
            "rows": self.rows,
        }
        tmp = self.reg_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2) + "\n")
        tmp.replace(self.reg_path)

    def resize(self, cols: int, rows: int) -> None:
        self.cols = max(1, int(cols))
        self.rows = max(1, int(rows))
        if self.master_fd is not None:
            winsz = struct.pack("HHHH", self.rows, self.cols, 0, 0)
            fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, winsz)
        if self.proc is not None:
            try:
                os.killpg(self.proc.pid, signal.SIGWINCH)
            except Exception:
                pass
        self.write_registry()

    def broadcast(self, msg: dict) -> None:
        line = (json.dumps(msg, separators=(",", ":")) + "\n").encode()
        dead: list[socket.socket] = []
        with self.clients_lock:
            for client in list(self.clients):
                try:
                    client.sendall(line)
                except Exception:
                    dead.append(client)
            for client in dead:
                self.clients.discard(client)
                try:
                    client.close()
                except Exception:
                    pass

    def handle_client(self, conn: socket.socket) -> None:
        f = conn.makefile("r")
        subscribed = False
        try:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                typ = msg.get("type", "")
                if typ == "subscribe":
                    subscribed = True
                    with self.clients_lock:
                        self.clients.add(conn)
                    try:
                        if self.history:
                            conn.sendall((json.dumps({
                                "type": "output",
                                "session": self.session_id,
                                "data": base64.b64encode(bytes(self.history)).decode("ascii"),
                            }, separators=(",", ":")) + "\n").encode())
                        conn.sendall((json.dumps({
                            "type": "status",
                            "session": self.session_id,
                            "status": self.status,
                        }, separators=(",", ":")) + "\n").encode())
                    except Exception:
                        pass
                elif typ == "input":
                    data = base64.b64decode(msg.get("data", ""))
                    if self.master_fd is not None and data:
                        os.write(self.master_fd, data)
                elif typ == "resize":
                    self.resize(int(msg.get("cols", self.cols)), int(msg.get("rows", self.rows)))
                elif typ == "ping":
                    try:
                        conn.sendall((json.dumps({"type": "pong"}) + "\n").encode())
                    except Exception:
                        pass
        finally:
            if subscribed:
                with self.clients_lock:
                    self.clients.discard(conn)
            try:
                conn.close()
            except Exception:
                pass

    def pty_reader(self) -> None:
        assert self.master_fd is not None
        while not self.stop_event.is_set():
            try:
                data = os.read(self.master_fd, 4096)
            except BlockingIOError:
                time.sleep(0.01)
                continue
            except OSError:
                break
            if not data:
                break
            self.history.extend(data)
            if len(self.history) > self.history_limit:
                del self.history[:-self.history_limit]
            self.broadcast({
                "type": "output",
                "session": self.session_id,
                "data": base64.b64encode(data).decode("ascii"),
            })

    def start_server(self) -> None:
        SOCK_DIR.mkdir(parents=True, exist_ok=True)
        try:
            os.unlink(self.sock_path)
        except FileNotFoundError:
            pass
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(str(self.sock_path))
        srv.listen(16)
        self.server = srv

        def _accept() -> None:
            while not self.stop_event.is_set():
                try:
                    conn, _ = srv.accept()
                except OSError:
                    break
                t = threading.Thread(target=self.handle_client, args=(conn,), daemon=True)
                t.start()

        threading.Thread(target=_accept, daemon=True).start()

    def run(self) -> int:
        BOAT_DIR.mkdir(parents=True, exist_ok=True)
        SOCK_DIR.mkdir(parents=True, exist_ok=True)
        self.master_fd, self.slave_fd = os.openpty()
        os.set_blocking(self.master_fd, False)
        self.resize(self.cols, self.rows)
        child_env = os.environ.copy()
        child_env["CHARON_BOAT_WRAPPED"] = "1"
        child_env["CHARON_BOAT_SESSION"] = self.session_id
        self.proc = subprocess.Popen(
            self.command,
            stdin=self.slave_fd,
            stdout=self.slave_fd,
            stderr=self.slave_fd,
            preexec_fn=os.setsid,
            close_fds=True,
            env=child_env,
        )
        os.close(self.slave_fd)
        self.slave_fd = None
        self.status = "running"
        self.write_registry()
        self.start_server()
        threading.Thread(target=self.pty_reader, daemon=True).start()

        rc = self.proc.wait()
        self.status = "exited"
        self.write_registry()
        self.broadcast({"type": "status", "session": self.session_id, "status": self.status})
        self.stop_event.set()
        if self.server is not None:
            try:
                self.server.close()
            except Exception:
                pass
        try:
            os.unlink(self.sock_path)
        except Exception:
            pass
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except Exception:
                pass
        return rc


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--cols", type=int, default=80)
    ap.add_argument("--rows", type=int, default=24)
    ap.add_argument("cmd", nargs=argparse.REMAINDER)
    ns = ap.parse_args()
    cmd = ns.cmd
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        print("boat_session.py: missing command", file=sys.stderr)
        return 1
    session = BoatSession(ns.name, cmd, ns.cols, ns.rows)
    return session.run()


if __name__ == "__main__":
    raise SystemExit(main())
