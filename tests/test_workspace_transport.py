"""Session transports: tmux parsing/extraction (mocked + one real private server) and
the charond JSON-lines client against a fake Unix-socket daemon."""
from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

import pytest

from charon.workspace import transport as T
from charon.workspace.charond_client import CharondClient, CharondError

CLAUDE_SCREEN = """\
 ▐▛███▛█   Claude Code v2.1.259
▝▜██████▀  Sonnet 5 with xhigh effort · Claude Max
> implement the hub endpoint and run the tests
⠋ Thinking… (1s)
⏺ I added the /mcp route to hub.rs and wired the bridge.
  Ran the tests: 12 passed, 0 failed.
✓ Done.
─────────────────────────────────────────────────────────────────────────────────────────
❯ Try "fix lint errors"
─────────────────────────────────────────────────────────────────────────────────────────
  ⏵⏵ auto mode on (shift+tab to cycle) · esc to interrupt · ← for agents            /rc
"""


def test_extract_last_message_strips_chrome_and_takes_last_turn():
    msg = T.extract_last_message(CLAUDE_SCREEN)
    assert msg.startswith('I added the /mcp route')
    assert 'Ran the tests: 12 passed' in msg
    assert '✓ Done.' in msg
    assert 'Try "fix lint errors"' not in msg and 'auto mode' not in msg and 'implement the hub' not in msg


def test_clean_capture_drops_ansi_and_terminal_query_residue():
    raw = '\x1b[32mhello\x1b[0m ^[[>84;0;0c world\x1b[24;80R\r\n'
    assert T.clean_capture(raw) == 'hello  world\n'


def test_parse_ls_tags_ids_status_and_kind():
    now = 1_000_000.0
    out = ('ach-07e154|07e15450-82c2|Builder|ws1|claude|999995|1\n'
           'ach-531285|53128597-05f6|Tester|ws1|codex|990000|0\n'
           'scratch||||' + '|999999|0\n')
    sessions = T.LocalTmuxTransport.parse_ls(out, now=now)
    by = {s['name']: s for s in sessions}
    assert by['ach-07e154']['id'] == 'session.07e15450-82c2' and by['ach-07e154']['callsign'] == 'Builder'
    assert by['ach-07e154']['status'] == 'running' and by['ach-07e154']['kind'] == 'claude' and by['ach-07e154']['attached']
    assert by['ach-531285']['status'] == 'idle' and by['ach-531285']['agent'] == 'codex'
    assert by['scratch']['id'] == 'session.scratch' and by['scratch']['kind'] == 'terminal' and by['scratch']['callsign'] == 'scratch'


def test_local_tmux_transport_with_mocked_tmux(monkeypatch):
    calls: list[list[str]] = []

    class P:
        def __init__(self, out: str = '', rc: int = 0):
            self.stdout, self.stderr, self.returncode = out, '', rc

    def fake_run(cmd, **kw):
        calls.append(cmd)
        sub = cmd[3:]  # tmux -L sock <args...>
        if sub[0] == 'list-sessions':
            return P('ach-07e154|07e15450|Builder|ws1|claude|1|0\n')
        if sub[0] == 'display-message':
            return P('/proj\n' if '#{pane_current_path}' in sub else 'zsh\n')
        if sub[0] == 'capture-pane':
            return P(CLAUDE_SCREEN)
        return P('')

    monkeypatch.setattr(T.subprocess, 'run', fake_run)
    t = T.LocalTmuxTransport(socket='test-sock', tmux_bin='tmux', now=lambda: 1_000_000.0)
    sessions = t.list_sessions()
    assert sessions[0]['cwd'] == '/proj' and sessions[0]['id'] == 'session.07e15450'
    assert 'Ran the tests' in t.read('session.07e15450')
    assert t.read('Builder', 'screen').count('\n') >= 3
    assert t.send('session.07e15450', 'hello') == 'tmux'
    send = [c for c in calls if c[3] == 'send-keys']
    assert send[0][3:] == ['send-keys', '-t', '=ach-07e154:', '-l', '--', 'hello'] and send[1][-1] == 'Enter'
    t.send('session.07e15450', 'line1\nline2')
    assert any(c[3] == 'set-buffer' for c in calls) and any(c[3] == 'paste-buffer' and '-p' in c for c in calls)
    t.interrupt('session.07e15154' if False else 'session.07e15450', 'esc')
    assert calls[-1][-1] == 'Escape'
    assert t.foreground('session.07e15450') == 'zsh' and T.is_shell('zsh') and not T.is_shell('2.1.259')
    t.label('session.07e15450', "Builder's | new")
    lab = [c for c in calls if c[3] == 'set-option'][0]
    assert lab[3:] == ['set-option', '-t', '=ach-07e154:', '@acheron_title', 'Builder s   new']
    with pytest.raises(NotImplementedError):
        t.spawn('Reviewer', 'claude', None, 'x')


@pytest.mark.skipif(shutil.which('tmux') is None, reason='tmux not installed')
def test_local_tmux_transport_against_a_private_server(tmp_path):
    sock = f'charon-test-{uuid.uuid4().hex[:8]}'
    # Unix socket paths must stay under SUN_LEN (104 bytes); pytest's tmp_path is far too deep.
    tmpdir = Path(tempfile.mkdtemp(prefix='ct-', dir='/tmp'))
    env = {**os.environ, 'TMUX_TMPDIR': str(tmpdir)}
    base = ['tmux', '-L', sock, '-f', '/dev/null']
    subprocess.run(base + ['new-session', '-d', '-s', 'ach-test01', '-x', '100', '-y', '30', 'cat'], env=env, check=True, timeout=10)
    try:
        for opt, val in (('@acheron_block', 'blk-test-0001'), ('@acheron_title', 'Builder'), ('@acheron_agent', 'claude')):
            subprocess.run(base + ['set-option', '-t', '=ach-test01:', opt, val], env=env, check=True, timeout=10)
        t = T.LocalTmuxTransport(socket=sock, tmux_bin='tmux', tmux_tmpdir=str(tmpdir))
        sessions = t.list_sessions()
        assert [s['id'] for s in sessions] == ['session.blk-test-0001'] and sessions[0]['callsign'] == 'Builder'
        assert t.send('Builder', 'hello from the overseer') == 'tmux'
        deadline = time.time() + 5
        text = ''
        while time.time() < deadline and 'hello from the overseer' not in text:
            time.sleep(0.2)
            text = t.read('session.blk-test-0001', 'screen')
        assert 'hello from the overseer' in text
        assert t.foreground('session.blk-test-0001') in ('cat', 'zsh', 'bash', 'sh')
    finally:
        subprocess.run(base + ['kill-server'], env=env, timeout=10)
        shutil.rmtree(tmpdir, ignore_errors=True)


# ── charond client / transport against a fake daemon ─────────────────────

class FakeCharond:
    def __init__(self, path: Path):
        self.path = path
        self.inputs: list[tuple[str, bytes]] = []
        self.sessions = [
            {'id': 'sess-a', 'title': 'Builder', 'kind': 'local', 'workspace': 'ws', 'tab': '', 'ephemeral': False, 'cols': 80, 'rows': 24, 'state': 'working', 'seq': 5},
            {'id': 'sess-b', 'title': 'Tester', 'kind': 'tmux', 'workspace': 'ws', 'tab': '', 'ephemeral': False, 'cols': 80, 'rows': 24, 'state': 'blocked', 'seq': 2},
        ]
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(str(path))
        self.srv.listen(2)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self._handle, args=(conn,), daemon=True).start()

    def _handle(self, conn: socket.socket) -> None:
        f = conn.makefile('rwb')
        for line in f:
            msg = json.loads(line)
            t = msg.get('type')
            if t == 'hello':
                reply = {'type': 'welcome', 'proto': 1, 'daemon_version': 'fake', 'pid': 1}
            elif t == 'list':
                reply = {'type': 'inventory', 'sessions': self.sessions}
            elif t == 'input':
                self.inputs.append((msg['session'], base64.b64decode(msg['data'])))
                continue
            elif t == 'attach':
                f.write((json.dumps({'type': 'output', 'session': msg['session'], 'data': base64.b64encode(b'noise').decode(), 'seq': 1}) + '\n').encode())
                reply = {'type': 'snapshot', 'session': msg['session'], 'data': base64.b64encode(CLAUDE_SCREEN.encode()).decode(), 'cols': 80, 'rows': 24, 'seq': 2}
            elif t == 'detach':
                continue
            elif t == 'spawn':
                self.sessions.append({'id': 'sess-c', 'title': msg.get('title') or 'new', 'kind': 'local', 'workspace': '', 'tab': '', 'ephemeral': False, 'cols': 80, 'rows': 24, 'state': 'idle', 'seq': 0})
                reply = {'type': 'spawned', 'session': 'sess-c'}
            elif t == 'ping':
                reply = {'type': 'pong', 'ts': msg.get('ts', 0)}
            else:
                reply = {'type': 'error', 'code': 'bad', 'message': f'unknown {t}'}
            f.write((json.dumps(reply) + '\n').encode())
            f.flush()

    def close(self) -> None:
        self.srv.close()


@pytest.fixture
def fake_charond():
    short = Path(tempfile.mkdtemp(prefix='cd-', dir='/tmp'))  # SUN_LEN: keep the socket path short
    d = FakeCharond(short / 'charond.sock')
    yield d
    d.close()
    shutil.rmtree(short, ignore_errors=True)


def test_charond_client_handshake_list_input_snapshot(fake_charond):
    c = CharondClient(fake_charond.path)
    assert c.available
    with c:
        assert c.daemon_version == 'fake' and c.ping()
        ids = [s['id'] for s in c.list()]
        assert ids == ['sess-a', 'sess-b']
        c.input('sess-a', 'hi\r')
        assert 'Ran the tests' in c.snapshot('sess-a')
        assert c.spawn(cmd=['claude'], title='Reviewer') == 'sess-c'
    time.sleep(0.1)
    assert fake_charond.inputs == [('sess-a', b'hi\r')]


def test_charond_client_errors_when_socket_missing(tmp_path):
    c = CharondClient(tmp_path / 'nope.sock')
    assert not c.available
    with pytest.raises(CharondError):
        c.list()


def test_charond_transport_maps_states_reads_and_sends(fake_charond):
    t = T.CharondTransport(fake_charond.path)
    sessions = t.list_sessions()
    by = {s['id']: s for s in sessions}
    assert by['session.sess-a']['status'] == 'running' and by['session.sess-b']['status'] == 'waiting'
    assert by['session.sess-a']['callsign'] == 'Builder' and by['session.sess-a']['host_ref'] == 'charond'
    assert 'Ran the tests' in t.read('session.sess-a')
    assert t.send('session.sess-a', 'go') == 'charond'
    t.send('session.sess-a', 'a\nb', enter=False)
    t.interrupt('session.sess-a')
    time.sleep(0.1)
    assert fake_charond.inputs[0] == ('sess-a', b'go\r')
    assert fake_charond.inputs[1] == ('sess-a', b'\x1b[200~a\nb\x1b[201~')
    assert fake_charond.inputs[2] == ('sess-a', b'\x03')
    assert t.foreground('session.sess-a') is None
    spawned = t.spawn('Reviewer', 'claude', None, 'x')
    assert spawned['id'] == 'session.sess-c'


def test_auto_transport_prefers_charond_when_present_and_composite_routes(fake_charond, monkeypatch):
    monkeypatch.setenv('CHARON_TMUX_SOCKET', f'charon-none-{uuid.uuid4().hex[:6]}')
    auto = T.AutoTransport(charond_sock=fake_charond.path)
    assert isinstance(auto, T.CompositeTransport)
    ids = [s['id'] for s in auto.list_sessions()]
    assert 'session.sess-a' in ids
    assert auto.send('session.sess-b', 'x') == 'charond'
    plain = T.AutoTransport(charond_sock=fake_charond.path.with_name('missing.sock'))
    assert isinstance(plain, T.LocalTmuxTransport)
