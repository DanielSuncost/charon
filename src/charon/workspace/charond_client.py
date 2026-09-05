"""Minimal JSON-lines client for charond (``~/.charon/charond.sock``).

Protocol reference: ``crates/charon-tui/src/protocol.rs`` — one JSON object per line,
``{"type": "<snake_case tag>", ...}``, terminal bytes base64 in ``data``.  This client
covers what the overseer transport needs: ``hello`` → ``welcome``, ``list`` →
``inventory``, ``input``, and ``attach{replay:true}`` → ``snapshot`` → ``detach``.
"""
from __future__ import annotations

import base64
import json
import os
import socket
from pathlib import Path
from typing import Any

PROTO = 1
CLIENT_NAME = 'charon-overseer'

STATE_MAP = {  # charond session state → Acheron-style status used by the overseer tools
    'idle': 'idle', 'working': 'running', 'blocked': 'waiting', 'done': 'idle', 'exited': 'disconnected',
}


def default_sock_path() -> Path:
    raw = os.environ.get('CHARON_SOCK')
    if raw:
        return Path(raw)
    state_dir = os.environ.get('CHARON_DIR')
    base = Path(state_dir) if state_dir else Path.home() / '.charon'
    return base / 'charond.sock'


class CharondError(RuntimeError):
    pass


class CharondClient:
    def __init__(self, sock_path: Path | str | None = None, *, timeout: float = 5.0):
        self.sock_path = Path(sock_path) if sock_path else default_sock_path()
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._buf = b''
        self.daemon_version: str | None = None

    # ── connection ────────────────────────────────────────────────────
    @property
    def available(self) -> bool:
        return self.sock_path.exists()

    def connect(self) -> 'CharondClient':
        if self._sock is not None:
            return self
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        try:
            s.connect(str(self.sock_path))
        except OSError as exc:
            raise CharondError(f'cannot connect to charond at {self.sock_path}: {exc}') from exc
        self._sock = s
        self._send({'type': 'hello', 'proto': PROTO, 'client': CLIENT_NAME, 'pid': os.getpid()})
        msg = self._recv_until({'welcome', 'error'})
        if msg.get('type') != 'welcome':
            raise CharondError(f'charond handshake failed: {msg}')
        self.daemon_version = msg.get('daemon_version')
        return self

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None
                self._buf = b''

    def __enter__(self) -> 'CharondClient':
        return self.connect()

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ── framing ───────────────────────────────────────────────────────
    def _send(self, msg: dict[str, Any]) -> None:
        assert self._sock is not None
        self._sock.sendall((json.dumps(msg, separators=(',', ':')) + '\n').encode('utf-8'))

    def _recv_line(self) -> dict[str, Any]:
        assert self._sock is not None
        while b'\n' not in self._buf:
            chunk = self._sock.recv(65536)
            if not chunk:
                raise CharondError('charond closed the connection')
            self._buf += chunk
        line, self._buf = self._buf.split(b'\n', 1)
        line = line.strip()
        if not line:
            return self._recv_line()
        return json.loads(line.decode('utf-8'))

    def _recv_until(self, types: set[str], *, session: str | None = None, max_frames: int = 10_000) -> dict[str, Any]:
        for _ in range(max_frames):
            msg = self._recv_line()
            if msg.get('type') in types and (session is None or msg.get('session') in (None, session)):
                return msg
            if msg.get('type') == 'error' and 'error' not in types:
                raise CharondError(f"charond error {msg.get('code')}: {msg.get('message')}")
        raise CharondError('charond: too many frames without the expected reply')

    # ── operations ────────────────────────────────────────────────────
    def list(self) -> list[dict[str, Any]]:
        self.connect()
        self._send({'type': 'list'})
        return list(self._recv_until({'inventory'}).get('sessions') or [])

    def input(self, session: str, data: str | bytes) -> None:
        self.connect()
        raw = data.encode('utf-8') if isinstance(data, str) else bytes(data)
        self._send({'type': 'input', 'session': session, 'data': base64.b64encode(raw).decode('ascii')})

    def snapshot(self, session: str, *, cols: int = 200, rows: int = 50) -> str:
        """Attach with replay, take the scrollback snapshot, detach.  Returns decoded text."""
        self.connect()
        self._send({'type': 'attach', 'session': session, 'cols': cols, 'rows': rows, 'replay': True})
        try:
            msg = self._recv_until({'snapshot'}, session=session)
        finally:
            try:
                self._send({'type': 'detach', 'session': session})
            except Exception:
                pass
        return base64.b64decode(msg.get('data') or b'').decode('utf-8', errors='replace')

    def spawn(self, *, kind: str = 'local', cmd: list[str] | None = None, cwd: str | None = None, title: str | None = None,
              session: str | None = None, workspace: str | None = None, cols: int = 120, rows: int = 40) -> str:
        self.connect()
        msg: dict[str, Any] = {'type': 'spawn', 'kind': kind, 'cmd': cmd or [], 'cols': cols, 'rows': rows}
        for key, value in (('cwd', cwd), ('title', title), ('session', session), ('workspace', workspace)):
            if value:
                msg[key] = value
        self._send(msg)
        reply = self._recv_until({'spawned'})
        return str(reply.get('session'))

    def ping(self) -> bool:
        self.connect()
        self._send({'type': 'ping', 'ts': 0})
        return self._recv_until({'pong'}).get('type') == 'pong'
