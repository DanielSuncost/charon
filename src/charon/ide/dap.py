"""Small stdio DAP transport for trusted host adapters (not a sandbox).

POSIX only: each adapter owns a new process group, which is killed on close.
No reverse requests, sockets, terminal launching, expression evaluation, or
adapter discovery. Detached children that deliberately escape the process group
are outside this lifecycle guarantee. Calls are synchronous and serialized by
the tool's session lock; cancellation is checked during every blocking wait.
"""
from __future__ import annotations

import atexit
from collections import deque
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import threading
import time
import weakref
from typing import Any

MAX_MESSAGE_BYTES = 256 * 1024
MAX_HEADER_BYTES = 8192
MAX_EVENT_BYTES = 32 * 1024
MAX_PENDING = 16
DEFAULT_TIMEOUT = 10.0
_LIVE: weakref.WeakSet = weakref.WeakSet()


class DAPError(RuntimeError):
    """Protocol, cancellation, exit, or timeout error safe to report to callers."""


class DAPClient:
    def __init__(self, command: list[str], cwd: Path):
        if os.name != 'posix':
            raise DAPError('Debug currently requires POSIX process-group cleanup.')
        self.proc = subprocess.Popen(
            command, cwd=str(cwd), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, bufsize=0, start_new_session=True,
        )
        self.selector = selectors.DefaultSelector()
        self.buffer = bytearray()
        self.responses: dict[int, dict] = {}
        self.pending: set[int] = set()
        self.events: deque[str] = deque()
        self.event_bytes = 0
        self.events_dropped = 0
        self.stderr = bytearray()
        self.stderr_truncated = False
        self.seq = 0
        self.state = 'starting'
        self.initialized = False
        self.closed = False
        self.close_error: str | None = None
        self.failure: str | None = None
        self.capabilities: dict[str, Any] = {}
        _LIVE.add(self)
        try:
            for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
                os.set_blocking(stream.fileno(), False)
            self.selector.register(self.proc.stdout, selectors.EVENT_READ, 'stdout')
            self.selector.register(self.proc.stderr, selectors.EVENT_READ, 'stderr')
        except BaseException:
            self.close()
            raise

    def _check(self, deadline: float, cancel: threading.Event | None) -> None:
        if cancel is not None and cancel.is_set():
            raise DAPError('Debug operation cancelled; adapter shutdown required.')
        if self.closed:
            raise DAPError('Debug adapter is closed.')
        if time.monotonic() >= deadline:
            raise DAPError('Debug adapter request timed out; adapter shutdown required.')

    def _record_event(self, message: dict) -> None:
        event = message.get('event')
        if event == 'initialized':
            self.initialized = True
        elif event == 'stopped':
            self.state = 'stopped'
        elif event == 'continued':
            self.state = 'running'
        elif event in ('terminated', 'exited'):
            self.state = str(event)
        raw = json.dumps(message, ensure_ascii=True)
        if len(raw) > 2048:
            raw = raw[:2048] + ' [event truncated]'
        while self.events and (self.event_bytes + len(raw) > MAX_EVENT_BYTES or len(self.events) >= 64):
            self.event_bytes -= len(self.events.popleft())
            self.events_dropped += 1
        self.events.append(raw)
        self.event_bytes += len(raw)

    def _parse(self) -> None:
        while self.buffer:
            end = self.buffer.find(b'\r\n\r\n')
            if end < 0:
                if len(self.buffer) > MAX_HEADER_BYTES:
                    raise DAPError('DAP header exceeds byte limit.')
                return
            if end > MAX_HEADER_BYTES:
                raise DAPError('DAP header exceeds byte limit.')
            try:
                lengths = []
                for line in bytes(self.buffer[:end]).decode('ascii').split('\r\n'):
                    key, value = line.split(':', 1)
                    if key.lower() == 'content-length':
                        if not value.strip().isascii() or not value.strip().isdecimal():
                            raise ValueError('invalid length')
                        lengths.append(int(value.strip()))
                if len(lengths) != 1 or not 0 < lengths[0] <= MAX_MESSAGE_BYTES:
                    raise ValueError('missing, duplicate, or oversized Content-Length')
            except (ValueError, UnicodeError) as exc:
                raise DAPError(f'Invalid DAP framing: {exc}') from exc
            size = lengths[0]
            start = end + 4
            if len(self.buffer) < start + size:
                return
            payload = bytes(self.buffer[start:start + size])
            del self.buffer[:start + size]
            try:
                message = json.loads(payload)
            except (ValueError, UnicodeError, RecursionError) as exc:
                raise DAPError('Invalid DAP JSON message.') from exc
            if not isinstance(message, dict):
                raise DAPError('DAP message must be an object.')
            kind = message.get('type')
            if kind == 'event':
                self._record_event(message)
            elif kind == 'response':
                seq = message.get('request_seq')
                if type(seq) is not int or seq not in self.pending or seq in self.responses:
                    raise DAPError('Unexpected or duplicate DAP response.')
                self.responses[seq] = message
            elif kind == 'request':
                raise DAPError('Adapter reverse requests are unsupported (including runInTerminal).')
            else:
                raise DAPError('Unknown DAP message type.')

    def pump(self, deadline: float, cancel: threading.Event | None = None, *, wait: bool = True) -> None:
        self._check(deadline, cancel)
        for key, _ in self.selector.select(min(0.05, max(0, deadline - time.monotonic())) if wait else 0):
            try:
                data = os.read(key.fd, 16384)
            except BlockingIOError:
                continue
            if not data:
                self.selector.unregister(key.fileobj)
                if key.data == 'stdout':
                    raise DAPError(f'Debug adapter stdout closed (exit code {self.proc.poll()}).')
                continue
            if key.data == 'stderr':
                self.stderr.extend(data)
                if len(self.stderr) > 8192:
                    del self.stderr[:-8192]
                    self.stderr_truncated = True
            else:
                self.buffer.extend(data)
                self._parse()
        if self.proc.poll() is not None:
            raise DAPError(f'Debug adapter exited with code {self.proc.returncode}.')

    def send(self, command: str, arguments: dict, deadline: float, cancel: threading.Event | None = None) -> int:
        self._check(deadline, cancel)
        if len(self.pending) >= MAX_PENDING:
            raise DAPError('Too many pending DAP requests.')
        self.seq += 1
        payload = json.dumps({'seq': self.seq, 'type': 'request', 'command': command,
                              'arguments': arguments}, allow_nan=False).encode('utf-8')
        if len(payload) > MAX_MESSAGE_BYTES:
            raise DAPError('DAP request exceeds byte limit.')
        frame = f'Content-Length: {len(payload)}\r\n\r\n'.encode('ascii') + payload
        self.pending.add(self.seq)
        view = memoryview(frame)
        while view:
            self._check(deadline, cancel)
            try:
                written = os.write(self.proc.stdin.fileno(), view[:16384])
                if not written:
                    raise DAPError('DAP adapter stdin closed.')
                view = view[written:]
            except BlockingIOError:
                self.pump(deadline, cancel)
            except BrokenPipeError as exc:
                raise DAPError('DAP adapter stdin closed.') from exc
        return self.seq

    def receive(self, seq: int, deadline: float, cancel: threading.Event | None = None) -> dict:
        while seq not in self.responses:
            self.pump(deadline, cancel)
        self._check(deadline, cancel)
        response = self.responses.pop(seq)
        self.pending.remove(seq)
        if response.get('success') is not True:
            raise DAPError(f"DAP request failed: {str(response.get('message', 'unspecified error'))[:2000]}")
        body = response.get('body', {})
        if not isinstance(body, dict):
            raise DAPError('DAP response body must be an object.')
        return body

    def request(self, command: str, arguments: dict, *, cancel: threading.Event | None = None,
                timeout: float = DEFAULT_TIMEOUT) -> dict:
        deadline = time.monotonic() + timeout
        return self.receive(self.send(command, arguments, deadline, cancel), deadline, cancel)

    def launch(self, adapter: str, arguments: dict, *, cancel: threading.Event | None = None,
               timeout: float = DEFAULT_TIMEOUT) -> dict:
        deadline = time.monotonic() + timeout
        seq = self.send('initialize', {'adapterID': adapter, 'clientID': 'charon',
            'linesStartAt1': True, 'columnsStartAt1': True, 'pathFormat': 'path',
            'supportsRunInTerminalRequest': False, 'supportsVariablePaging': True}, deadline, cancel)
        self.capabilities = self.receive(seq, deadline, cancel)
        launch_seq = self.send('launch', arguments, deadline, cancel)
        while not self.initialized:
            # Detect launch failure without waiting for a missing initialized event.
            if launch_seq in self.responses and self.responses[launch_seq].get('success') is not True:
                self.receive(launch_seq, deadline, cancel)
            self.pump(deadline, cancel)
        if self.capabilities.get('supportsConfigurationDoneRequest'):
            seq = self.send('configurationDone', {}, deadline, cancel)
            self.receive(seq, deadline, cancel)
        body = self.receive(launch_seq, deadline, cancel)
        if self.state == 'starting':
            self.state = 'running'
        return body

    def snapshot(self) -> dict:
        return {'state': self.state, 'pid': self.proc.pid, 'exit_code': self.proc.poll(),
                'closed': self.closed, 'failure': self.failure, 'cleanup_error': self.close_error,
                'events': list(self.events), 'events_dropped': self.events_dropped,
                'stderr': self.stderr.decode('utf-8', errors='replace'),
                'stderr_truncated': self.stderr_truncated}

    def close(self) -> str | None:
        """Boundedly reap adapter and kill its group, even if group leader exited."""
        if self.closed:
            return self.close_error
        self.closed = True
        try:
            try:
                os.killpg(self.proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError as exc:
                self.close_error = f'Process-group cleanup failed: {exc}'
                if self.proc.poll() is None:
                    try:
                        self.proc.kill()
                    except OSError as kill_exc:
                        self.close_error += f'; adapter kill failed: {kill_exc}'
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.close_error = 'Adapter could not be reaped within 2 seconds; manual cleanup required.'
            except OSError as exc:
                self.close_error = f'Adapter reap failed; manual cleanup required: {exc}'
        finally:
            self.selector.close()
            for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
                try:
                    stream.close()
                except OSError as exc:
                    self.close_error = f'Adapter pipe cleanup failed: {exc}'
            self.state = 'closed'
        return self.close_error


def _shutdown() -> None:
    for client in list(_LIVE):
        client.close()


atexit.register(_shutdown)
