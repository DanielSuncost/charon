"""Approval-gated Debug tool surface; adapters/debuggees are trusted host code.

Registry/approval integration must gate Debug as dangerous execution. Executor
validation is defense in depth, not an alternative to that approval path.
Trusted configuration lives ONLY in ToolContext.metadata['ide_debug_adapters']:
{name: {'command': [executable, ...], 'launch': {DAP launch arguments}}}.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
import threading
import time
import uuid

from charon.ide.dap import DAPClient, DAPError, DEFAULT_TIMEOUT, MAX_MESSAGE_BYTES
from charon.tools import ToolContext, ToolResult

ACTIONS = ('status', 'launch', 'breakpoints', 'continue', 'next', 'pause',
           'threads', 'stack', 'scopes', 'variables', 'terminate')
DEBUG_TOOL_DEF = {
    'name': 'Debug',
    'description': (
        'Small trusted stdio DAP debugger. Requires execution approval and an operator-configured '
        'adapter; not a sandbox. Launch uses only trusted configuration. Session IDs belong to '
        'the current project and agent. Unsupported in scope/frozen-restricted contexts. '
        'No evaluation, arbitrary requests, attach, or terminal launch. Sessions expire after '
        '30 minutes idle. Launch completes configuration immediately; use trusted stop-on-entry '
        'launch settings when breakpoints must be installed before execution.'
    ),
    'input_schema': {
        'type': 'object', 'additionalProperties': False,
        'properties': {
            'action': {'type': 'string', 'enum': list(ACTIONS)},
            'adapter': {'type': 'string', 'description': 'Trusted configured adapter name; launch only.'},
            'session_id': {'type': 'string', 'description': 'Opaque session ID; required except launch/status listing.'},
            'path': {'type': 'string', 'description': 'Project-contained regular source file; breakpoints only.'},
            'lines': {'type': 'array', 'maxItems': 100, 'items': {'type': 'integer', 'minimum': 1},
                      'description': 'Replacement source breakpoints; empty clears.'},
            'thread_id': {'type': 'integer', 'minimum': 1},
            'frame_id': {'type': 'integer', 'minimum': 0},
            'variables_reference': {'type': 'integer', 'minimum': 1},
            'start': {'type': 'integer', 'minimum': 0, 'maximum': 100000},
            'count': {'type': 'integer', 'minimum': 1, 'maximum': 100},
        },
        'required': ['action'],
    },
}
MAX_SESSIONS = 8
MAX_OWNER_SESSIONS = 2
MAX_RECORDS = 32
IDLE_SECONDS = 1800
REQUEST_TIMEOUT = DEFAULT_TIMEOUT
_ALLOWED = {
    'status': {'session_id'}, 'launch': {'adapter'},
    'breakpoints': {'session_id', 'path', 'lines'},
    'continue': {'session_id', 'thread_id'}, 'next': {'session_id', 'thread_id'},
    'pause': {'session_id', 'thread_id'}, 'threads': {'session_id'},
    'stack': {'session_id', 'thread_id', 'start', 'count'},
    'scopes': {'session_id', 'frame_id'},
    'variables': {'session_id', 'variables_reference', 'start', 'count'},
    'terminate': {'session_id'},
}


@dataclass
class _Session:
    id: str
    owner: tuple[str, str]
    adapter: str
    client: DAPClient
    cancel: threading.Event | None
    lock: threading.Lock = field(default_factory=threading.Lock)
    touched: float = field(default_factory=time.monotonic)

    def snapshot(self) -> dict:
        return {'session_id': self.id, 'adapter': self.adapter, **self.client.snapshot()}


_SESSIONS: dict[str, _Session] = {}
_LOCK = threading.RLock()
_MONITOR: threading.Thread | None = None


def _fail(session: _Session, message: str) -> None:
    session.client.failure = message
    session.client.close()


def _monitor() -> None:
    while True:
        time.sleep(0.1)
        with _LOCK:
            sessions = list(_SESSIONS.values())
        for session in sessions:
            if session.client.closed or not session.lock.acquire(blocking=False):
                continue
            try:
                if time.monotonic() - session.touched > IDLE_SECONDS:
                    raise DAPError('Debug session expired after idle timeout.')
                session.client.pump(time.monotonic() + 1, session.cancel, wait=False)
            except Exception as exc:
                _fail(session, str(exc))
            finally:
                session.lock.release()


def _owner(ctx: ToolContext) -> tuple[str, str]:
    # Empty scope means no granted paths, not unrestricted. Empty frozen denies none.
    if ctx.scope is not None or ctx.frozen:
        raise DAPError('Debug is unavailable in scope/frozen-restricted contexts: host execution bypasses file scope.')
    if not isinstance(ctx.agent_id, str) or not ctx.agent_id.strip():
        raise DAPError('Debug requires an explicit agent identity.')
    root = Path(ctx.project_root).resolve(strict=True)
    if not root.is_dir():
        raise DAPError('Debug project root must be a directory.')
    return str(root), ctx.agent_id


def _int(params: dict, key: str, *, default=None, minimum=1, maximum=2**31 - 1) -> int:
    value = params.get(key, default)
    if type(value) is not int or not minimum <= value <= maximum:
        raise DAPError(f'{key} must be an integer in [{minimum}, {maximum}].')
    return value


def _arguments(action: str, params: dict, root: str) -> tuple[str, dict]:
    if action == 'breakpoints':
        path = params.get('path')
        if not isinstance(path, str) or not path or len(path) > 4096:
            raise DAPError('breakpoints requires a source path.')
        source = (Path(root) / path).resolve(strict=True)
        if not source.is_relative_to(Path(root)) or not source.is_file():
            raise DAPError('Breakpoint source must be a regular file inside the project root.')
        lines = params.get('lines')
        if not isinstance(lines, list) or len(lines) > 100:
            raise DAPError('lines must be a list of at most 100 positive integers.')
        for line in lines:
            _int({'line': line}, 'line')
        return 'setBreakpoints', {'source': {'path': str(source)},
                                  'breakpoints': [{'line': line} for line in lines]}
    if action in ('continue', 'next', 'pause', 'stack'):
        args = {'threadId': _int(params, 'thread_id')}
        if action == 'stack':
            args.update(startFrame=_int(params, 'start', default=0, minimum=0, maximum=100000),
                        levels=_int(params, 'count', default=20, maximum=100))
            return 'stackTrace', args
        return action, args
    if action == 'scopes':
        return 'scopes', {'frameId': _int(params, 'frame_id', minimum=0)}
    if action == 'variables':
        return 'variables', {'variablesReference': _int(params, 'variables_reference'),
                             'start': _int(params, 'start', default=0, minimum=0, maximum=100000),
                             'count': _int(params, 'count', default=20, maximum=100)}
    return action, {}


def _new_session(params: dict, ctx: ToolContext, owner: tuple[str, str]) -> _Session:
    global _MONITOR
    adapter = params.get('adapter')
    if not isinstance(adapter, str) or not adapter or len(adapter) > 128:
        raise DAPError('launch requires a trusted adapter name.')
    configured = (ctx.metadata or {}).get('ide_debug_adapters', {})
    spec = configured.get(adapter) if isinstance(configured, dict) else None
    if not isinstance(spec, dict):
        raise DAPError('Adapter is not configured in trusted ide_debug_adapters metadata.')
    command, launch = spec.get('command'), spec.get('launch')
    if (not isinstance(command, list) or not 1 <= len(command) <= 128 or
            any(not isinstance(v, str) or not v or '\x00' in v or len(v) > 8192 for v in command)):
        raise DAPError('Trusted adapter command must be a bounded nonempty list of strings.')
    if not isinstance(launch, dict):
        raise DAPError('Trusted adapter launch must be a dictionary.')
    # Validate/copy before spawning. Never merge model parameters into launch.
    serialized = json.dumps(launch, allow_nan=False)
    if len(serialized.encode()) > MAX_MESSAGE_BYTES - 1024:
        raise DAPError('Trusted launch configuration exceeds message limit.')
    launch = json.loads(serialized)
    if ctx.cancel_event is not None and ctx.cancel_event.is_set():
        raise DAPError('Debug launch cancelled before process creation.')
    with _LOCK:
        live = [s for s in _SESSIONS.values() if not s.client.closed or s.client.close_error]
        if len(live) >= MAX_SESSIONS or sum(s.owner == owner for s in live) >= MAX_OWNER_SESSIONS:
            raise DAPError('Debug session limit reached; terminate an existing session.')
        while len(_SESSIONS) >= MAX_RECORDS:
            removable = next((sid for sid, s in _SESSIONS.items()
                              if s.client.closed and not s.client.close_error), None)
            if removable is None:
                raise DAPError('Debug session record limit reached.')
            del _SESSIONS[removable]
        client = DAPClient(list(command), Path(owner[0]))
        session = _Session(uuid.uuid4().hex, owner, adapter, client, ctx.cancel_event)
        session.lock.acquire()
        _SESSIONS[session.id] = session
        if _MONITOR is None:
            _MONITOR = threading.Thread(target=_monitor, name='charon-dap-monitor', daemon=True)
            _MONITOR.start()
    try:
        client.launch(adapter, launch, cancel=ctx.cancel_event, timeout=REQUEST_TIMEOUT)
    except BaseException as exc:
        _fail(session, str(exc) or type(exc).__name__)
        # Preserve session identity and cleanup information even on launch failure.
        if isinstance(exc, Exception):
            return session
        raise
    finally:
        session.lock.release()
    return session


def _result(data: dict, ctx: ToolContext, *, error: bool = False) -> ToolResult:
    cap = max(0, min(50000, ctx.max_output_bytes))
    text = json.dumps(data, ensure_ascii=True)
    truncated = len(text.encode()) > cap
    if truncated:
        # Preserve valid JSON and lifecycle identifiers; never hide cleanup failures.
        data = {'session_id': data.get('session_id'), 'state': data.get('state'),
                'closed': data.get('closed'), 'failure': str(data.get('failure') or '')[:1000],
                'cleanup_error': str(data.get('cleanup_error') or '')[:1000],
                'truncated': True, 'preview': text[:max(0, cap - 2400)]}
        text = json.dumps(data, ensure_ascii=True)
        if len(text) > cap:
            text = json.dumps({'session_id': data.get('session_id'), 'truncated': True,
                               'is_error': error, 'message': 'Debug output exceeds configured limit.'})
        # Pathologically small context caps cannot hold even the JSON envelope.
        text = text[:cap]
    return ToolResult(content=text, is_error=error, truncated=truncated)


def execute_debug(params: dict, ctx: ToolContext) -> ToolResult:
    """Execute only after normal execute_tool approval; recheck policy and input here."""
    try:
        owner = _owner(ctx)
        if not isinstance(params, dict) or params.get('action') not in ACTIONS:
            raise DAPError('Unknown or missing Debug action.')
        action = params['action']
        if set(params) - (_ALLOWED[action] | {'action'}):
            raise DAPError('Unexpected parameters for Debug action (arbitrary commands/configuration are forbidden).')
        if action == 'launch':
            session = _new_session(params, ctx, owner)
            with session.lock:
                return _result(session.snapshot(), ctx, error=bool(session.client.failure or session.client.close_error))
        sid = params.get('session_id')
        if sid is None and action == 'status':
            with _LOCK:
                owned = [s for s in _SESSIONS.values() if s.owner == owner]
            snapshots = []
            for session in owned:
                if session.lock.acquire(timeout=0.1):
                    try:
                        snapshots.append(session.snapshot())
                    finally:
                        session.lock.release()
                else:
                    snapshots.append({'session_id': session.id, 'state': 'busy'})
            return _result({'sessions': snapshots}, ctx)
        if not isinstance(sid, str) or not sid or len(sid) > 128:
            raise DAPError('session_id is required.')
        with _LOCK:
            session = _SESSIONS.get(sid)
        if session is None or session.owner != owner:
            raise DAPError('Unknown Debug session for this project and agent.')
        command, arguments = _arguments(action, params, owner[0])
        if not session.lock.acquire(timeout=0.1):
            raise DAPError('Debug session is busy; retry after its bounded request completes.')
        try:
            session.touched = time.monotonic()
            client = session.client
            if action == 'terminate':
                # Disconnect is best effort; force process-group cleanup regardless.
                if not client.closed:
                    try:
                        client.request('disconnect', {'terminateDebuggee': True},
                                       cancel=ctx.cancel_event, timeout=min(1, REQUEST_TIMEOUT))
                    except Exception as exc:
                        client.failure = f'Disconnect did not complete: {exc}'
                    finally:
                        client.close()
                return _result(session.snapshot(), ctx, error=bool(client.close_error or client.failure))
            if action == 'status':
                if not client.closed:
                    try:
                        client.pump(time.monotonic() + 1, ctx.cancel_event, wait=False)
                    except Exception as exc:
                        _fail(session, str(exc))
                return _result(session.snapshot(), ctx, error=bool(client.failure or client.close_error))
            if client.closed:
                raise DAPError('Debug session is closed; use status to inspect its failure and exit code.')
            try:
                body = client.request(command, arguments, cancel=ctx.cancel_event, timeout=REQUEST_TIMEOUT)
            except BaseException as exc:
                _fail(session, str(exc) or type(exc).__name__)
                if not isinstance(exc, Exception):
                    raise
                return _result(session.snapshot(), ctx, error=True)
            return _result({'session_id': sid, 'body': body, **client.snapshot()}, ctx)
        finally:
            session.lock.release()
    except (DAPError, OSError, ValueError, TypeError, RecursionError) as exc:
        return _result({'error': str(exc)[:3000]}, ctx, error=True)
