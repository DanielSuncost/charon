"""Client for the Charon Desktop helper (ai.charon.desktop).

The helper is a separate signed binary (source in apps/desktop-helper) and
the only process that holds the macOS desktop grants. Python never calls
the capture, injection or event-tap APIs itself; it asks the helper over
one end of an AF_UNIX socketpair passed at spawn (one JSON object per line
each way). Nothing else can reach that channel: there is no path, port or
name to connect to.

The helper authenticates this side too, from the socket's audit token
against a requirement compiled into it at build time (see
apps/desktop-helper/Sources/Caller.swift), and refuses any other caller.

Before trusting a helper binary, ``verify`` checks its signature: it must
carry the identifier ``ai.charon.desktop`` and a real team identity, never
an ad-hoc signature, because the grants are pinned to that identity and a
binary that merely sits at the expected path must not inherit them. Set
``CHARON_DESKTOP_TEAM`` to require a specific team.

On start the helper re-execs itself with responsibility disclaimed, so the
grants macOS checks are its own whichever process launched it. ``start``
confirms that from the helper's own report and refuses otherwise.
"""
from __future__ import annotations

import json
import os
import select
import socket
import subprocess
import threading
from pathlib import Path

BUNDLE_ID = 'ai.charon.desktop'
EXECUTABLE = 'Contents/MacOS/CharonDesktop'
PROTOCOL = 1
# Liveness bound for one request/reply on the pipe. The helper answers
# these instantly; this is not a cap on any unit of agent work.
REPLY_TIMEOUT_SECONDS = 10.0
# Verbs that wait for the user to answer the helper's dialog. The helper
# itself denies after 120 s unanswered; this bound only has to outlast it.
PROMPTING_VERBS = {'grant_request'}
PROMPT_REPLY_TIMEOUT_SECONDS = 150.0
BUILD_HINT = 'CHARON_DESKTOP_SIGN_IDENTITY="<Developer ID>" apps/desktop-helper/build.sh'


class HelperError(RuntimeError):
    pass


def default_app_path() -> Path:
    env = os.environ.get('CHARON_DESKTOP_APP')
    if env:
        return Path(env).expanduser()
    return Path(__file__).resolve().parents[3] / 'build' / 'desktop' / 'CharonDesktop.app'


def verify(app: Path) -> dict:
    """Signature facts for the helper bundle, with ``error`` set when it
    must not be used."""
    app = Path(app)
    info: dict = {'app': str(app), 'identifier': '', 'team': '', 'adhoc': False, 'error': ''}
    if not (app / EXECUTABLE).exists():
        info['error'] = f'Charon Desktop helper is not built (expected {app}). Build it with: {BUILD_HINT}'
        return info
    try:
        check = subprocess.run(['codesign', '--verify', '--strict', str(app)], capture_output=True, text=True, timeout=15)
        desc = subprocess.run(['codesign', '-dv', str(app)], capture_output=True, text=True, timeout=15).stderr
    except (OSError, subprocess.SubprocessError) as exc:
        info['error'] = f'cannot check the helper signature: {exc}'
        return info
    for line in desc.splitlines():
        key, _, value = line.partition('=')
        if key == 'Identifier':
            info['identifier'] = value
        elif key == 'TeamIdentifier':
            info['team'] = '' if value == 'not set' else value
        elif key == 'Signature' and value == 'adhoc':
            info['adhoc'] = True
    if 'flags=0x2(adhoc)' in desc:
        info['adhoc'] = True
    want_team = os.environ.get('CHARON_DESKTOP_TEAM', '').strip()
    if check.returncode != 0:
        info['error'] = f'the helper signature is invalid: {(check.stderr or "").strip()[:200]}'
    elif info['identifier'] != BUNDLE_ID:
        info['error'] = f'the helper is signed as {info["identifier"] or "nothing"}, not {BUNDLE_ID}'
    elif info['adhoc'] or not info['team']:
        info['error'] = ('the helper is ad-hoc signed; macOS would drop its grants on every rebuild. '
                         f'Rebuild with a Developer ID: {BUILD_HINT}')
    elif want_team and info['team'] != want_team:
        info['error'] = f'the helper is signed by team {info["team"]}, not the required {want_team}'
    return info


def _destination_terms(destination) -> dict:
    """The terms a capture states. Only a minted, local Destination has any;
    everything else raises CaptureRefused before the helper is asked."""
    from charon.computer.destination import CaptureRefused, Destination
    if not isinstance(destination, Destination):
        raise CaptureRefused('a capture needs a destination from destination_for(provider, model)')
    return destination.terms()


class DesktopHelper:
    """One running helper process. Not thread-safe across processes; calls
    from threads in this process are serialised."""

    def __init__(self, app: Path | None = None):
        self.app = Path(app) if app else default_app_path()
        self.proc: subprocess.Popen | None = None
        self.sock: socket.socket | None = None
        self.caller: dict = {}
        # Unsolicited notices from the helper, oldest first (kill switch
        # trips). Delivered alongside replies; read and clear them here.
        self.events: list[dict] = []
        self.identity: dict = {}
        self.signature: dict = {}
        self._lock = threading.Lock()
        self._next_id = 0
        self._buf = b''

    def start(self) -> dict:
        self.signature = verify(self.app)
        if self.signature['error']:
            raise HelperError(self.signature['error'])
        env = {k: v for k, v in os.environ.items() if k != 'CHARON_DESKTOP_DISCLAIMED'}
        ours, theirs = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.proc = subprocess.Popen([str(self.app / EXECUTABLE), 'serve', '--fd', str(theirs.fileno())],
                                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL, env=env, pass_fds=(theirs.fileno(),))
        finally:
            theirs.close()
        self.sock = ours
        try:
            hello = self.call('hello')
        except HelperError as exc:
            self.close()
            if 'caller refused' in str(exc):
                raise HelperError(f'{exc}. The helper trusts only the Python it was built for; '
                                  f'if Python was upgraded, rebuild it: {BUILD_HINT}') from exc
            raise
        ident = hello.get('identity') or {}
        self.caller = hello.get('caller') or {}
        if ident.get('bundle_id') != BUNDLE_ID or ident.get('protocol') != PROTOCOL:
            self.close()
            raise HelperError(f'unexpected helper identity: {ident}')
        if not ident.get('is_self_responsible'):
            self.close()
            raise HelperError('the helper could not take responsibility for its own grants; macOS would '
                              f'attribute them to pid {ident.get("responsible_pid")} instead')
        self.identity = ident
        return ident

    def call(self, cmd: str, **fields) -> dict:
        if self.sock is None:
            raise HelperError('the helper is not running')
        with self._lock:
            self._next_id += 1
            req = {'id': self._next_id, 'cmd': cmd, **fields}
            try:
                self.sock.sendall((json.dumps(req) + '\n').encode())
            except OSError as exc:
                raise HelperError(f'the helper exited: {exc}') from exc
            while True:
                line = self._readline(PROMPT_REPLY_TIMEOUT_SECONDS if cmd in PROMPTING_VERBS
                                      else REPLY_TIMEOUT_SECONDS)
                try:
                    reply = json.loads(line)
                except ValueError as exc:
                    raise HelperError(f'malformed reply from the helper: {line[:200]!r}') from exc
                if 'event' in reply and 'id' not in reply:
                    # Unsolicited, e.g. the kill switch tripping.
                    self.events.append(reply)
                    continue
                break
        if reply.get('fatal'):
            raise HelperError(str(reply.get('error')))
        if reply.get('id') != req['id']:
            raise HelperError('the helper replied out of order')
        return reply

    def _readline(self, timeout: float = REPLY_TIMEOUT_SECONDS) -> str:
        while b'\n' not in self._buf:
            ready, _, _ = select.select([self.sock], [], [], timeout)
            if not ready:
                raise HelperError('the helper did not answer')
            chunk = self.sock.recv(65536)
            if not chunk:
                raise HelperError(f'the helper exited (code {self.proc.poll()})')
            self._buf += chunk
        line, _, self._buf = self._buf.partition(b'\n')
        return line.decode('utf-8', 'replace').strip()

    # ── Grants: the caller may ask, narrow and revoke; only the user grants.

    def request_grant(self, action_class: str, target_bundle_id: str, minutes: float, reason: str = '',
                      destination=None) -> dict:
        """Ask the user, in the helper's own dialog, for a time-boxed grant.
        Returns the reply; ``ok`` is False unless the user clicked Allow."""
        terms = {}
        if destination is not None or action_class == 'desktop.observe.screen':
            terms = _destination_terms(destination)
        return self.call('grant_request', **{'class': action_class, 'target': target_bundle_id,
                                             'minutes': minutes, 'reason': reason, 'terms': terms})

    def grants(self) -> list[dict]:
        return list(self.call('grants_list').get('grants') or [])

    def narrow_grant(self, grant_id: str, minutes: float) -> dict:
        return self.call('grant_narrow', grant=grant_id, minutes=minutes)

    def revoke_grant(self, grant_id: str) -> dict:
        return self.call('grant_revoke', grant=grant_id)

    def revoke_all(self) -> dict:
        return self.call('grants_revoke_all')

    def capture(self, target_bundle_id: str, destination) -> dict:
        """A redacted screenshot of the target app's own windows, for a
        request to ``destination`` (from ``destination.destination_for``),
        which must be local: otherwise the helper is not asked. On success
        ``reply['capture']`` is a ``SealedCapture``; only
        ``destination.image_block`` releases its pixels. Nothing is written
        to disk."""
        from charon.computer.destination import seal
        reply = self.call('capture', target=target_bundle_id, terms=_destination_terms(destination))
        cap = reply.get('capture')
        if reply.get('ok') and isinstance(cap, dict):
            reply['capture'] = seal(cap, target_bundle_id, destination)
        return reply

    def click(self, target_bundle_id: str, capture_id: str, x: float, y: float,
              button: str = 'left', count: int = 1) -> dict:
        """Click at pixel (x, y) of a screenshot the helper still holds. The
        helper maps it to the screen and re-checks before every event."""
        return self.call('pointer_click', target=target_bundle_id, capture=capture_id,
                         x=float(x), y=float(y), button=button, count=count)

    def scroll(self, target_bundle_id: str, capture_id: str, x: float, y: float, dy: int, dx: int = 0) -> dict:
        return self.call('pointer_scroll', target=target_bundle_id, capture=capture_id,
                         x=float(x), y=float(y), dx=dx, dy=dy)

    def type_text(self, target_bundle_id: str, capture_id: str, x: float, y: float, text: str) -> dict:
        """Type ``text`` into the focused text field, which must contain pixel
        (x, y) of a held screenshot. No control characters (no Return, no
        Tab); at most 500 characters. The audit records only the length.

        Known: characters go out as Unicode strings on a placeholder key, so
        an app that reads key codes instead receives repeated placeholder
        letters ("hello" -> "aaaaa"). A correctness limit, not the helper
        typing something else; see apps/desktop-helper/README.md."""
        return self.call('type_text', target=target_bundle_id, capture=capture_id, x=float(x), y=float(y), text=text)

    def press_shortcut(self, target_bundle_id: str, capture_id: str, x: float, y: float, shortcut: str) -> dict:
        """Press one shortcut from the helper's fixed allowlist (arrows, tab,
        escape, home/end, page up/down, backspace, select_all, undo, find)."""
        return self.call('press_shortcut', target=target_bundle_id, capture=capture_id,
                         x=float(x), y=float(y), shortcut=shortcut)

    def verify_audit(self) -> dict:
        return self.call('audit_verify')

    def close(self) -> None:
        proc, self.proc = self.proc, None
        sock, self.sock = self.sock, None
        if sock is not None:
            sock.close()  # EOF: the helper exits on its own
        if proc is None:
            return
        try:
            proc.wait(timeout=2)
        except Exception:
            proc.kill()
            proc.wait()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.close()
