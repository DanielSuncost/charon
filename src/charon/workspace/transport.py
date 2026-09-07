"""Session transports for the overseer tools.

A transport is how the overseer *sees* and *drives* agent sessions.  The tool layer
never talks to tmux or charond directly; it asks a ``SessionTransport``:

    list_sessions() -> [{id, callsign, title, kind, agent, status, cwd, host_ref, name}]
    read(session_id, mode='last_message'|'screen'|'history', lines=2000) -> str
    send(session_id, text, *, enter=True) -> 'tmux' | 'charond' | 'fleet'
    interrupt(session_id, key='ctrl-c'|'esc')
    foreground(session_id) -> process name | None      # digests are held when it is a shell
    label(session_id, text)                              # optional
    spawn(role, agent, cwd, prompt) -> session dict      # optional (NotImplementedError otherwise)

Implementations: LocalTmuxTransport (Acheron's tagged sessions on ``tmux -L acheron``),
CharondTransport (charond socket), FleetTransport (charon.fleet tmux helpers, thin),
CompositeTransport (merge), AutoTransport (charond when its socket exists, else tmux).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .charond_client import STATE_MAP, CharondClient, CharondError, default_sock_path

ACTIVE_WINDOW_S = 15
SHELL_RE = re.compile(r'^-?(zsh|bash|fish|sh|dash|tcsh|ksh|login)$')
ANSI_RE = re.compile(r'\x1b\[[0-9;?>]*[A-Za-z]|\x1b\][^\x07]*\x07|\x1b[()][A-Z0-9]|\x1b[=>]|\r')
DA_RESIDUE_RE = re.compile(r'\^\[\[[0-9;>?]*[a-zA-Z]')


@runtime_checkable
class SessionTransport(Protocol):
    name: str

    def list_sessions(self) -> list[dict[str, Any]]: ...
    def read(self, session_id: str, mode: str = 'last_message', lines: int = 2000) -> str: ...
    def send(self, session_id: str, text: str, *, enter: bool = True) -> str: ...
    def interrupt(self, session_id: str, key: str = 'ctrl-c') -> None: ...
    def foreground(self, session_id: str) -> str | None: ...


class TransportError(RuntimeError):
    pass


# ── text extraction (port of Acheron's extractLastMessage) ────────────────

_SEPARATOR_RE = re.compile(r'^[─╭╰│╮╯┌┐└┘═\s]+$')
_CHROME_RE = re.compile(r'esc to interrupt|\? for shortcuts|manual mode|auto-accept|bypass|context left|tokens used|for agents|shift\+tab', re.I)
_STATUS_GLYPH_RE = re.compile(r'^[⏸⏵✻✽·⧉]+\s')
_SPINNER_RE = re.compile(r'^([✻✽*]\s|[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]|\S{0,3}\s*Thinking…)')
_USER_TURN_RE = re.compile(r'^[>›]\s+\S')
_SHELL_PROMPT_RE = re.compile(r'^\S+@[\w.-]+.*[$%#]\s')


def clean_capture(raw: str) -> str:
    """Drop ANSI sequences and terminal query responses that leak into captures."""
    text = ANSI_RE.sub('', raw or '')
    return DA_RESIDUE_RE.sub('', text)


def _is_chrome(line: str) -> bool:
    t = line.strip()
    return (t == '' or bool(_SEPARATOR_RE.match(t)) or t.startswith('❯') or bool(_CHROME_RE.search(t))
            or bool(_STATUS_GLYPH_RE.match(t)) or bool(_SPINNER_RE.match(t)))


def extract_last_message(raw: str, is_agent: bool = True) -> str:
    """The agent's most recent message from a screen capture: everything after the
    user's last sent message, with TUI chrome (input box, separators, status lines,
    spinners) and marker glyphs stripped.  Deterministic, no LLM."""
    lines = [line.rstrip() for line in clean_capture(raw).split('\n')]
    end = len(lines)
    for i in range(len(lines) - 1, max(-1, len(lines) - 26), -1):
        if lines[i].lstrip().startswith('❯'):
            end = i
            break
    while end > 0 and _is_chrome(lines[end - 1]):
        end -= 1
    lines = lines[:end]
    start = 0
    for i in range(len(lines) - 1, -1, -1):
        t = lines[i].strip()
        if _USER_TURN_RE.match(t) or (not is_agent and _SHELL_PROMPT_RE.match(t)):
            start = i + 1
            break
    msg = lines[start:]
    if not ''.join(msg).strip():
        msg = lines[-100:]
    while msg and _is_chrome(msg[0]):
        msg.pop(0)
    while msg and _is_chrome(msg[-1]):
        msg.pop()
    out = []
    for line in msg:
        line = re.sub(r'^[⏺●]\s?', '', line)
        out.append(line)
    return '\n'.join(out).strip()


def dedupe_lines(text: str) -> str:
    out: list[str] = []
    prev: str | None = None
    blanks = 0
    for line in text.split('\n'):
        t = line.rstrip()
        if t == '':
            blanks += 1
            if blanks > 1:
                continue
        else:
            blanks = 0
        if t != '' and t == prev:
            continue
        out.append(t)
        prev = t
    return '\n'.join(out)


def status_from_activity(activity_epoch: float | None, *, now: float | None = None) -> str:
    if activity_epoch is None:
        return 'idle'
    now = time.time() if now is None else now
    return 'running' if now - activity_epoch < ACTIVE_WINDOW_S else 'idle'


# ── local tmux (Acheron's tagged sessions) ─────────────────────────────────

LS_FORMAT = '#{session_name}|#{@acheron_block}|#{@acheron_title}|#{@acheron_ws}|#{@acheron_agent}|#{session_activity}|#{session_attached}'


class LocalTmuxTransport:
    """Acheron tags every local session with ``@acheron_block/title/ws/agent`` on a
    dedicated socket (``tmux -L acheron``).  Session ids are ``session.<block>`` for
    tagged sessions and ``session.<tmux name>`` otherwise."""

    name = 'tmux'

    def __init__(self, socket: str | None = None, tmux_bin: str | None = None, tmux_tmpdir: str | None = None,
                 *, now: Any = None):
        self.socket = socket or os.environ.get('CHARON_TMUX_SOCKET') or 'acheron'
        self.tmux_bin = tmux_bin or os.environ.get('CHARON_TMUX_BIN') or shutil.which('tmux') or 'tmux'
        self.tmux_tmpdir = tmux_tmpdir
        self._now = now or time.time
        self._names: dict[str, str] = {}   # session id → tmux session name
        self._sessions: dict[str, dict[str, Any]] = {}

    # tmux invocation
    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        if self.tmux_tmpdir:
            env['TMUX_TMPDIR'] = str(self.tmux_tmpdir)
        return env

    def _run(self, *args: str, check: bool = True, timeout: float = 5.0) -> str:
        cmd = [self.tmux_bin]
        if self.socket:
            cmd += ['-L', self.socket]
        cmd += list(args)
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=self._env())
        if check and proc.returncode != 0:
            raise TransportError(f'tmux {args[0]} failed: {(proc.stderr or proc.stdout).strip()}')
        return proc.stdout

    @staticmethod
    def parse_ls(output: str, *, now: float | None = None) -> list[dict[str, Any]]:
        sessions = []
        for line in (output or '').splitlines():
            parts = line.split('|')
            if len(parts) < 7 or not parts[0]:
                continue
            name, block, title, ws, agent, activity, attached = parts[:7]
            try:
                activity_epoch: float | None = float(activity) if activity else None
            except ValueError:
                activity_epoch = None
            sid = f'session.{block}' if block else f'session.{name}'
            sessions.append({
                'id': sid, 'name': name, 'block_id': block or None, 'callsign': title or name, 'title': title or name,
                'workspace': ws or None, 'agent': agent or None, 'kind': 'claude' if agent else 'terminal',
                'status': status_from_activity(activity_epoch, now=now), 'activity': activity_epoch,
                'attached': attached not in ('', '0'), 'host_ref': 'local', 'cwd': None,
            })
        return sessions

    def list_sessions(self) -> list[dict[str, Any]]:
        try:
            out = self._run('list-sessions', '-F', LS_FORMAT, check=False)
        except (OSError, subprocess.SubprocessError):
            out = ''
        sessions = self.parse_ls(out, now=self._now())
        for s in sessions:
            try:
                s['cwd'] = self._run('display-message', '-p', '-t', f"={s['name']}:", '#{pane_current_path}', check=False).strip() or None
            except (OSError, subprocess.SubprocessError):
                s['cwd'] = None
        self._names = {s['id']: s['name'] for s in sessions}
        self._sessions = {s['id']: s for s in sessions}
        return sessions

    def _tmux_name(self, session_id: str) -> str:
        """Session id, tmux session name, block id or callsign → tmux session name."""
        if session_id not in self._names:
            self.list_sessions()
        if session_id in self._names:
            return self._names[session_id]
        bare = session_id[len('session.'):] if session_id.startswith('session.') else session_id
        for s in self._sessions.values():
            if s['name'] == bare or s.get('block_id') == bare:
                return s['name']
        for s in self._sessions.values():
            if str(s.get('callsign', '')).lower() == bare.lower():
                return s['name']
        return bare

    def read(self, session_id: str, mode: str = 'last_message', lines: int = 2000) -> str:
        name = self._tmux_name(session_id)
        n = max(20, min(int(lines or 2000), 20000))
        if mode == 'screen':
            n = 200
        raw = self._run('capture-pane', '-p', '-J', '-S', f'-{n}', '-t', f'={name}:')
        if mode == 'last_message':
            s = self._sessions.get(session_id) or {}
            return extract_last_message(raw, is_agent=bool(s.get('agent')) or s.get('kind') == 'claude')
        text = dedupe_lines(clean_capture(raw))
        return text[-12000:] if mode == 'screen' else text[-200000:]

    def send(self, session_id: str, text: str, *, enter: bool = True) -> str:
        name = self._tmux_name(session_id)
        target = f'={name}:'
        if '\n' in text:
            # Multi-line: paste through a tmux buffer so the pane's app sees one
            # bracketed paste (-p) instead of a newline-per-message flood.
            buf = f'charon-overseer-{os.getpid()}-{int(time.time() * 1000)}'
            self._run('set-buffer', '-b', buf, '--', text)
            self._run('paste-buffer', '-p', '-d', '-b', buf, '-t', target)
        else:
            self._run('send-keys', '-t', target, '-l', '--', text)
        if enter:
            self._run('send-keys', '-t', target, 'Enter')
        return self.name

    def interrupt(self, session_id: str, key: str = 'ctrl-c') -> None:
        name = self._tmux_name(session_id)
        self._run('send-keys', '-t', f'={name}:', 'Escape' if key == 'esc' else 'C-c')

    def foreground(self, session_id: str) -> str | None:
        name = self._tmux_name(session_id)
        try:
            return self._run('display-message', '-p', '-t', f'={name}:', '#{pane_current_command}').strip() or None
        except TransportError:
            return None

    def label(self, session_id: str, text: str) -> None:
        name = self._tmux_name(session_id)
        safe = re.sub(r"['|\x00-\x1f]", ' ', text).strip()[:80]
        self._run('set-option', '-t', f'={name}:', '@acheron_title', safe)
        if session_id in self._sessions:
            self._sessions[session_id]['callsign'] = safe
            self._sessions[session_id]['title'] = safe

    def spawn(self, role: str, agent: str, cwd: str | None, prompt: str) -> dict[str, Any]:
        raise NotImplementedError('spawning sessions needs Acheron (+ Overseer button) or charond; the tmux transport only drives existing sessions')


def is_shell(foreground: str | None) -> bool:
    return bool(foreground) and bool(SHELL_RE.match(foreground.strip()))


# ── charond ────────────────────────────────────────────────────────────────

class CharondTransport:
    name = 'charond'

    def __init__(self, sock_path: Path | str | None = None, *, client: CharondClient | None = None):
        self.sock_path = Path(sock_path) if sock_path else default_sock_path()
        self._client = client
        self._sessions: dict[str, dict[str, Any]] = {}

    @property
    def available(self) -> bool:
        return self.sock_path.exists()

    def _c(self) -> CharondClient:
        if self._client is None:
            self._client = CharondClient(self.sock_path)
        return self._client.connect()

    @staticmethod
    def _sid(info: dict[str, Any]) -> str:
        return f"session.{info.get('id')}"

    def list_sessions(self) -> list[dict[str, Any]]:
        try:
            infos = self._c().list()
        except CharondError:
            return []
        sessions = []
        for info in infos:
            sid = self._sid(info)
            sessions.append({
                'id': sid, 'name': info.get('id'), 'block_id': info.get('id'), 'callsign': info.get('title') or info.get('id'),
                'title': info.get('title') or info.get('id'), 'workspace': info.get('workspace') or None,
                'agent': None, 'kind': info.get('kind') or 'local',
                'status': STATE_MAP.get(str(info.get('state')), 'idle'), 'charond_state': info.get('state'),
                'activity': None, 'attached': None, 'host_ref': 'charond', 'cwd': None,
            })
        self._sessions = {s['id']: s for s in sessions}
        return sessions

    def _raw_id(self, session_id: str) -> str:
        if session_id not in self._sessions:
            self.list_sessions()
        if session_id in self._sessions:
            return str(self._sessions[session_id]['name'])
        bare = session_id[len('session.'):] if session_id.startswith('session.') else session_id
        for s in self._sessions.values():
            if s['name'] == bare or str(s.get('callsign', '')).lower() == bare.lower():
                return str(s['name'])
        return bare

    def read(self, session_id: str, mode: str = 'last_message', lines: int = 2000) -> str:
        raw = self._c().snapshot(self._raw_id(session_id))
        if mode == 'last_message':
            return extract_last_message(raw, is_agent=True)
        text = dedupe_lines(clean_capture(raw))
        return text[-12000:] if mode == 'screen' else text[-200000:]

    def send(self, session_id: str, text: str, *, enter: bool = True) -> str:
        data = f'\x1b[200~{text}\x1b[201~' if '\n' in text else text
        self._c().input(self._raw_id(session_id), data + ('\r' if enter else ''))
        return self.name

    def interrupt(self, session_id: str, key: str = 'ctrl-c') -> None:
        self._c().input(self._raw_id(session_id), '\x1b' if key == 'esc' else '\x03')

    def foreground(self, session_id: str) -> str | None:
        return None  # charond does not expose the pane's foreground process

    def spawn(self, role: str, agent: str, cwd: str | None, prompt: str) -> dict[str, Any]:
        raw = self._c().spawn(kind='local', cmd=[agent], cwd=cwd, title=role)
        self.list_sessions()
        return self._sessions.get(f'session.{raw}') or {'id': f'session.{raw}', 'callsign': role, 'name': raw, 'host_ref': 'charond'}


# ── fleet (charon.fleet tmux helpers; thin) ────────────────────────────────

class FleetTransport:
    """Sessions on the default tmux socket as seen by ``charon.fleet.tmux_capture``
    (Charon's own ``charon-AG-…`` agents).  Thin; remote/boat sessions are not
    reachable without live servers and raise ``NotImplementedError``."""

    name = 'fleet'

    def __init__(self) -> None:
        from charon.fleet import tmux_capture  # local import: optional dependency surface
        self._tc = tmux_capture
        self._sessions: dict[str, dict[str, Any]] = {}

    def list_sessions(self) -> list[dict[str, Any]]:
        out = []
        try:
            for s in self._tc.list_sessions():
                sid = f'session.{s.name}'
                out.append({'id': sid, 'name': s.name, 'block_id': None, 'callsign': s.name, 'title': s.name, 'workspace': None,
                            'agent': 'charon' if s.name.startswith('charon-') else None, 'kind': 'terminal', 'status': 'idle',
                            'activity': None, 'attached': bool(getattr(s, 'attached', False)), 'host_ref': 'fleet', 'cwd': None})
        except Exception:
            return []
        self._sessions = {s['id']: s for s in out}
        return out

    def _name(self, session_id: str) -> str:
        return session_id[len('session.'):] if session_id.startswith('session.') else session_id

    def read(self, session_id: str, mode: str = 'last_message', lines: int = 2000) -> str:
        raw = self._tc.capture_pane(self._name(session_id), width=200, height=min(int(lines or 200), 2000))
        return extract_last_message(raw) if mode == 'last_message' else dedupe_lines(clean_capture(raw))

    def send(self, session_id: str, text: str, *, enter: bool = True) -> str:
        if not self._tc.send_key_literal(self._name(session_id), text):
            raise TransportError(f'fleet send failed for {session_id}')
        if enter:
            self._tc.send_keys(self._name(session_id), 'Enter')
        return self.name

    def interrupt(self, session_id: str, key: str = 'ctrl-c') -> None:
        self._tc.send_keys(self._name(session_id), 'Escape' if key == 'esc' else 'C-c')

    def foreground(self, session_id: str) -> str | None:
        return None

    def spawn(self, role: str, agent: str, cwd: str | None, prompt: str) -> dict[str, Any]:
        raise NotImplementedError('fleet spawning goes through FleetOnboard / boats, not the overseer transport')


# ── composition ────────────────────────────────────────────────────────────

class CompositeTransport:
    """Merges several transports; routes each session id to the transport that listed it."""

    name = 'composite'

    def __init__(self, transports: list[Any]):
        self.transports = [t for t in transports if t is not None]
        self._owner: dict[str, Any] = {}

    def list_sessions(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        self._owner = {}
        for t in self.transports:
            try:
                sessions = t.list_sessions()
            except Exception:
                continue
            for s in sessions:
                if s['id'] in seen:
                    continue
                seen.add(s['id'])
                self._owner[s['id']] = t
                out.append(s)
        return out

    def _t(self, session_id: str) -> Any:
        if session_id not in self._owner:
            self.list_sessions()
        t = self._owner.get(session_id)
        if t is None:
            if len(self.transports) == 1:
                return self.transports[0]
            raise TransportError(f'no transport lists session {session_id}')
        return t

    def read(self, session_id: str, mode: str = 'last_message', lines: int = 2000) -> str:
        return self._t(session_id).read(session_id, mode, lines)

    def send(self, session_id: str, text: str, *, enter: bool = True) -> str:
        return self._t(session_id).send(session_id, text, enter=enter)

    def interrupt(self, session_id: str, key: str = 'ctrl-c') -> None:
        self._t(session_id).interrupt(session_id, key)

    def foreground(self, session_id: str) -> str | None:
        return self._t(session_id).foreground(session_id)

    def label(self, session_id: str, text: str) -> None:
        t = self._t(session_id)
        if hasattr(t, 'label'):
            t.label(session_id, text)

    def spawn(self, role: str, agent: str, cwd: str | None, prompt: str) -> dict[str, Any]:
        for t in self.transports:
            if hasattr(t, 'spawn'):
                try:
                    return t.spawn(role, agent, cwd, prompt)
                except NotImplementedError:
                    continue
        raise NotImplementedError('no transport can spawn sessions here')


def AutoTransport(*, socket: str | None = None, charond_sock: Path | str | None = None) -> Any:
    """charond (when its socket exists) merged with the local Acheron tmux socket."""
    charond = CharondTransport(charond_sock)
    tmux = LocalTmuxTransport(socket)
    if charond.available:
        return CompositeTransport([charond, tmux])
    return tmux
