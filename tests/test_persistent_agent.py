"""The persistent agent's on/off control: fails closed, stops for real,
and shows up honestly in the chat's team band."""
from __future__ import annotations

import json
import subprocess
import sys
import time

from charon.agents import persistent_agent as pa
from charon.agents.autonomous import load_autonomous_config, save_autonomous_config
from charon.infra import store_adapter


def setup_function():
    store_adapter.reset_all()


MARKER = 'charon-persistent-test-marker'


def _sleeper(marker=MARKER):
    # A child whose argv carries the marker, like the real runner's does.
    return subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)', marker],
                            start_new_session=True)


# ── Fails closed ───────────────────────────────────────────────────

def test_absent_config_is_off(tmp_path):
    assert pa.persistent_enabled(tmp_path) is False
    assert pa.is_enabled(load_autonomous_config(tmp_path)) is False


def test_malformed_config_is_off(tmp_path):
    (tmp_path / 'autonomous_config.json').write_text('{not json')
    assert pa.persistent_enabled(tmp_path) is False
    (tmp_path / 'autonomous_config.json').write_text('[true]')
    assert pa.persistent_enabled(tmp_path) is False


def test_only_a_literal_true_turns_it_on(tmp_path):
    for bad in ('on', 'true', 'yes', 1, [True], {'enabled': True}):
        save_autonomous_config(tmp_path, {'persistent': bad})
        assert pa.persistent_enabled(tmp_path) is False, repr(bad)
    save_autonomous_config(tmp_path, {'persistent': True})
    assert pa.persistent_enabled(tmp_path) is True
    assert pa.is_enabled(None) is False
    assert pa.is_enabled('persistent') is False


def test_env_autonomous_override_does_not_turn_it_on(tmp_path, monkeypatch):
    monkeypatch.setenv('CHARON_AUTONOMOUS', '1')
    cfg = load_autonomous_config(tmp_path)
    assert cfg['enabled'] is True
    assert pa.is_enabled(cfg) is False


# ── Toggle persists and is audited ─────────────────────────────────

def test_set_enabled_persists_and_audits(tmp_path):
    pa.set_enabled(tmp_path, True)
    assert load_autonomous_config(tmp_path)['persistent'] is True
    assert pa.persistent_enabled(tmp_path) is True
    pa.set_enabled(tmp_path, False)
    assert load_autonomous_config(tmp_path)['persistent'] is False
    events = [json.loads(line)["event"] for line in pa.audit_path(tmp_path).read_text().splitlines()]
    assert events == ['enabled', 'disabled']


# ── Stop means stop ────────────────────────────────────────────────

def test_stop_with_nothing_running_reports_that(tmp_path):
    result = pa.request_stop(tmp_path)
    assert result['was_running'] is False
    assert result['signalled'] is False
    assert result['exited'] is True
    assert 'no runner process' in result['note']
    assert pa.load_runtime(tmp_path)['stop_requested'] is True


def test_stop_signals_the_live_runner_and_waits_for_exit(tmp_path):
    proc = _sleeper()
    try:
        pa.write_runtime(tmp_path, status='running', pid=proc.pid, cmd_marker=MARKER,
                         started_at=pa._now_iso(), heartbeat_at=pa._now_iso(), doing='typing')
        assert pa.status_summary(tmp_path)['alive'] is True
        result = pa.request_stop(tmp_path, timeout=5.0)
        assert result['was_running'] is True
        assert result['signalled'] is True
        assert result['exited'] is True
        assert proc.wait(timeout=5) != 0
        rt = pa.load_runtime(tmp_path)
        assert rt['status'] == 'stopped' and rt['pid'] is None and rt['stop_requested'] is True
        events = [json.loads(line) for line in pa.audit_path(tmp_path).read_text().splitlines()]
        assert [e['event'] for e in events] == ['stop_requested', 'signal_sent', 'stop_result']
        assert events[1]['signal'] == 'SIGTERM' and events[1]['pid'] == proc.pid
    finally:
        if proc.poll() is None:
            proc.kill()


def test_stop_never_signals_a_pid_that_is_not_our_runner(tmp_path):
    # A recycled pid from an earlier boot must not be killed on the strength
    # of a stale runtime record.
    proc = _sleeper(marker='some-other-program')
    try:
        pa.write_runtime(tmp_path, status='running', pid=proc.pid, cmd_marker=MARKER)
        assert pa.process_alive(pa.load_runtime(tmp_path)) is False
        result = pa.request_stop(tmp_path, timeout=1.0)
        assert result['was_running'] is False
        assert result['signalled'] is False
        assert 'already gone' in result['note']
        time.sleep(0.2)
        assert proc.poll() is None, 'unrelated process was signalled'
    finally:
        proc.kill()


# ── Status and band ────────────────────────────────────────────────

def test_status_lines_say_on_off_and_runner_state(tmp_path):
    assert pa.status_lines(pa.status_summary(tmp_path))[0] == 'Persistent agent: OFF (off)'
    pa.set_enabled(tmp_path, True)
    assert pa.status_lines(pa.status_summary(tmp_path))[0] == 'Persistent agent: ON (on, no runner process)'
    pa.write_runtime(tmp_path, status='running', pid=999999999, cmd_marker=MARKER)
    assert pa.status_lines(pa.status_summary(tmp_path))[0] == 'Persistent agent: ON (process gone)'


def test_band_tracker_reports_on_exit_and_off_transitions(tmp_path):
    tracker = pa.PersistentBandTracker()
    assert tracker.tick(tmp_path) == (None, [])

    pa.set_enabled(tmp_path, True)
    row, lines = tracker.tick(tmp_path)
    assert row['label'] == 'persistent' and row['health'] == 'ok'
    assert lines == ['Persistent agent ON — on · no runner process']
    assert pa.band_headline(row) == 'Persistent agent ON · no runner process'

    proc = _sleeper()
    try:
        pa.write_runtime(tmp_path, status='running', pid=proc.pid, cmd_marker=MARKER,
                         started_at=pa._now_iso(), heartbeat_at=pa._now_iso(), doing='reading screen')
        row, lines = tracker.tick(tmp_path)
        assert row['health'] == 'ok' and row['doing'] == 'reading screen' and row['pid'] == proc.pid
        assert lines == []
        assert pa.band_headline(row) == 'Persistent agent ON · running'
    finally:
        proc.kill()
        proc.wait()

    row, lines = tracker.tick(tmp_path)
    assert row['health'] == 'lost'
    assert lines == [f'✗ Persistent agent runner exited (pid {proc.pid}) — not running']

    pa.set_enabled(tmp_path, False)
    pa.request_stop(tmp_path)
    row, lines = tracker.tick(tmp_path)
    assert row is None
    assert lines == ['Persistent agent OFF — nothing is running on your behalf']


# ── Chat command ───────────────────────────────────────────────────

def test_persistent_command_turns_on_shows_status_and_stops(tmp_path, monkeypatch):
    from backend import common
    from chat_backend import ChatBackend

    state_dir = tmp_path / 'state'
    state_dir.mkdir()
    monkeypatch.setattr(common, 'STATE_DIR', state_dir)
    emitted = []
    monkeypatch.setattr(common, 'emit', lambda e: emitted.append(e))
    backend = ChatBackend()
    monkeypatch.setattr(backend, '_get_refresh_payload', lambda: {})
    launches = []
    monkeypatch.setattr(pa, '_launch', lambda sd, project: launches.append(project) or {'pid': 4242, 'process_id': 'proc-x'})

    backend.handle_command('/persistent status', 'r0')
    assert emitted[-1]['message'].startswith('Persistent agent: OFF')

    backend.handle_command('/persistent on', 'r1')
    assert load_autonomous_config(state_dir)['persistent'] is True
    assert emitted[-2]['message'] == 'Persistent agent ON (runner started, pid 4242).'
    assert len(launches) == 1

    proc = _sleeper()
    try:
        pa.write_runtime(state_dir, status='running', pid=proc.pid, cmd_marker=MARKER)
        backend.handle_command('/persistent off', 'r2')
        assert load_autonomous_config(state_dir)['persistent'] is False
        assert emitted[-2]['type'] == 'status'
        assert emitted[-2]['message'] == f'Persistent agent OFF (stopped pid {proc.pid}).'
        assert proc.wait(timeout=5) != 0
    finally:
        if proc.poll() is None:
            proc.kill()

    backend.handle_command('/persistent sideways', 'r3')
    assert emitted[-1]['type'] == 'error'
    assert '/persistent on | off | status' in emitted[-1]['error']
    cmds = [item['cmd'] for item in backend._command_catalog()]
    assert '/persistent on' in cmds and '/persistent off' in cmds
