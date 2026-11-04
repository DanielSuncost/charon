"""On/off control and runtime record for the persistent agent.

The persistent agent is Charon working on the user's machine on their behalf
across chat sessions. This module owns the control surface only; the loop
that does the work runs elsewhere (a managed process) and reports here.

Three records, all under ``<state_dir>/persistent_agent/`` except the toggle:

* The toggle lives in ``autonomous_config.json`` under ``persistent`` next to
  the existing autonomous-mode settings. It is off unless the stored value
  is literally ``true``: an absent file, unreadable JSON, a wrong type or an
  environment override never turn it on. A fresh or damaged install must
  never start operating the machine.
* ``runtime.json`` is what the running process says about itself: its pid,
  a command marker that proves the pid is still ours, the last heartbeat and
  what it is doing. Readers never assume a process is alive; they check.
* ``audit.jsonl`` is appended at the moment a control action happens (on,
  off, stop, signal sent, process exited), never summarised afterwards.

Stopping means stopping. ``request_stop`` sets the flag the runner watches,
then signals the process group so a stop lands in the middle of whatever the
runner is doing, waits for the exit, and escalates to SIGKILL if it must.
It signals a pid only after confirming the pid still runs our command, so a
recycled pid from an earlier boot is never killed by mistake.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from charon.agents.autonomous import load_autonomous_config, save_autonomous_config
from charon.shade.shade_activity import pid_alive

try:
    from charon.infra.diagnostics import record as _diag
except Exception:  # diagnostics is best-effort and must never block import
    def _diag(*args, **kwargs):
        return None

RUNTIME_DIRNAME = 'persistent_agent'
CONFIG_KEY = 'persistent'
# Marker the runner puts in its own argv so a pid can be verified as ours.
RUNNER_MARKER = 'charon-persistent-agent'
# How long without a heartbeat before the band calls the runner quiet.
QUIET_AFTER_SECONDS = 120
ACTIVE_STATUSES = {'starting', 'running', 'stopping'}

DEFAULT_RUNTIME = {
    'status': 'off',          # off | starting | running | stopping | stopped
    'pid': None,
    'cmd_marker': '',
    'started_at': '',
    'heartbeat_at': '',
    'doing': '',
    'stop_requested': False,
    'stopped_at': '',
    'exit_note': '',
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ts(value: Any) -> float:
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
    except (TypeError, ValueError):
        return 0.0


# ── Toggle ──────────────────────────────────────────────────────────

def is_enabled(config: dict | None) -> bool:
    """True only when the stored value is the boolean ``True``.

    Strings such as "on", integers such as 1 and anything else count as off:
    a malformed config must fail closed."""
    if not isinstance(config, dict):
        return False
    return config.get(CONFIG_KEY) is True


def persistent_enabled(state_dir: Path) -> bool:
    return is_enabled(load_autonomous_config(state_dir))


def set_enabled(state_dir: Path, enabled: bool, *, actor: str = 'user') -> dict:
    """Persist the toggle and record who flipped it."""
    config = load_autonomous_config(state_dir)
    config[CONFIG_KEY] = bool(enabled)
    save_autonomous_config(state_dir, config)
    audit(state_dir, 'enabled' if enabled else 'disabled', actor=actor)
    return config


# ── Runtime record ──────────────────────────────────────────────────

def runtime_dir(state_dir: Path) -> Path:
    return Path(state_dir) / RUNTIME_DIRNAME


def runtime_path(state_dir: Path) -> Path:
    return runtime_dir(state_dir) / 'runtime.json'


def load_runtime(state_dir: Path) -> dict:
    rt = dict(DEFAULT_RUNTIME)
    path = runtime_path(state_dir)
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding='utf-8'))
            if isinstance(data, dict):
                rt.update(data)
    except Exception as e:
        _diag('persistent_agent', 'runtime.json unreadable; treating the persistent agent as not running', error=e)
    return rt


def write_runtime(state_dir: Path, **fields: Any) -> dict:
    """Merge fields into the runtime record. The write is atomic so a reader
    never sees a half-written file while the runner heartbeats."""
    rt = load_runtime(state_dir)
    rt.update(fields)
    rt['updated_at'] = _now_iso()
    path = runtime_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(rt, indent=2, ensure_ascii=False), encoding='utf-8')
    os.replace(tmp, path)
    return rt


def heartbeat(state_dir: Path, doing: str, *, status: str = 'running') -> dict:
    """Called by the runner as it works. Records pid and marker so control
    paths can verify the process later."""
    return write_runtime(state_dir, status=status, pid=os.getpid(), cmd_marker=RUNNER_MARKER,
                         heartbeat_at=_now_iso(), doing=str(doing or '')[:200])


def stop_requested(state_dir: Path) -> bool:
    return bool(load_runtime(state_dir).get('stop_requested'))


# ── Audit ───────────────────────────────────────────────────────────

def audit_path(state_dir: Path) -> Path:
    return runtime_dir(state_dir) / 'audit.jsonl'


def audit(state_dir: Path, event: str, **fields: Any) -> None:
    """Append one control event. Written as it happens; failures are
    diagnosed, never raised, so an audit problem cannot block a stop."""
    row = {'ts': _now_iso(), 'event': event, **fields}
    try:
        path = audit_path(state_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('a', encoding='utf-8') as f:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + '\n')
    except Exception as e:
        _diag('persistent_agent', 'audit append failed; control event not recorded', error=e, event=event)


# ── Process liveness and stop ───────────────────────────────────────

def _process_command(pid: int) -> str | None:
    """The command line of ``pid`` as ps reports it, or None if unknown."""
    try:
        out = subprocess.run(['ps', '-o', 'command=', '-p', str(int(pid))],
                             capture_output=True, text=True, timeout=5)
    except Exception:
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def pid_is_ours(pid: Any, marker: str) -> bool:
    """A pid is ours only when it is alive and its command carries the
    marker the runner recorded. Unknown means no: never signal a guess."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0 or not pid_alive(pid):
        return False
    if not marker:
        return False
    cmd = _process_command(pid)
    return bool(cmd and marker in cmd)


def process_alive(rt: dict) -> bool:
    return pid_is_ours(rt.get('pid'), str(rt.get('cmd_marker') or ''))


def _signal(pid: int, sig: int) -> bool:
    """Signal the runner's whole process group so a shell wrapper and its
    Python child stop together. Falls back to the pid alone."""
    try:
        os.killpg(os.getpgid(pid), sig)
        return True
    except Exception:
        pass
    try:
        os.kill(pid, sig)
        return True
    except Exception:
        return False


def _is_zombie(pid: int) -> bool:
    """An exited child its parent has not reaped yet still answers
    kill(pid, 0); ps shows it as Z. That counts as exited for our purposes."""
    try:
        out = subprocess.run(['ps', '-o', 'stat=', '-p', str(int(pid))],
                             capture_output=True, text=True, timeout=5)
    except Exception:
        return False
    return out.returncode == 0 and out.stdout.strip().startswith('Z')


def _exited(pid: int) -> bool:
    return not pid_alive(pid) or _is_zombie(pid)


def _wait_exit(pid: int, timeout: float) -> bool:
    deadline = time.time() + max(0.0, timeout)
    while True:
        if _exited(pid):
            return True
        if time.time() >= deadline:
            return _exited(pid)
        time.sleep(0.1)


def request_stop(state_dir: Path, *, timeout: float = 5.0, actor: str = 'user') -> dict:
    """Stop the running agent, if any, and say what happened.

    Order matters: the stop flag is written first so a runner between
    actions sees it, then SIGTERM interrupts whatever it is in the middle
    of, then SIGKILL if it ignored that. The result reports each step so
    the caller can show the user the truth rather than "stop requested".
    """
    rt = load_runtime(state_dir)
    pid = rt.get('pid')
    alive = process_alive(rt)
    result: dict[str, Any] = {
        'pid': pid, 'was_running': alive, 'signalled': False, 'killed': False, 'exited': not alive,
        'note': '',
    }
    write_runtime(state_dir, stop_requested=True, status='stopping' if alive else rt.get('status', 'off'))
    audit(state_dir, 'stop_requested', actor=actor, pid=pid, was_running=alive)

    if alive:
        pid = int(pid)
        result['signalled'] = _signal(pid, signal.SIGTERM)
        audit(state_dir, 'signal_sent', signal='SIGTERM', pid=pid, delivered=result['signalled'])
        exited = result['signalled'] and _wait_exit(pid, timeout)
        if not exited and not _exited(pid):
            result['killed'] = _signal(pid, signal.SIGKILL)
            audit(state_dir, 'signal_sent', signal='SIGKILL', pid=pid, delivered=result['killed'])
            exited = _wait_exit(pid, 2.0)
        result['exited'] = bool(exited)
        if exited:
            result['note'] = f'stopped pid {pid}' + (' with SIGKILL' if result['killed'] else '')
        else:
            result['note'] = f'pid {pid} did not exit; it is still running'
    elif pid and rt.get('status') in ACTIVE_STATUSES:
        result['note'] = f'runner pid {pid} was already gone'
    else:
        result['note'] = 'no runner process was running'

    if result['exited']:
        write_runtime(state_dir, status='stopped', pid=None, doing='', stopped_at=_now_iso(),
                      exit_note=result['note'])
    audit(state_dir, 'stop_result', actor=actor, **{k: v for k, v in result.items() if k != 'pid'}, pid=pid)
    return result


# ── Status for humans and the band ──────────────────────────────────

def status_summary(state_dir: Path, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    enabled = persistent_enabled(state_dir)
    rt = load_runtime(state_dir)
    alive = process_alive(rt)
    hb = _ts(rt.get('heartbeat_at'))
    if alive:
        state = 'running'
    elif rt.get('status') in ACTIVE_STATUSES:
        state = 'process gone'
    elif enabled:
        state = 'on, no runner process'
    else:
        state = 'off'
    return {
        'enabled': enabled,
        'state': state,
        'alive': alive,
        'pid': rt.get('pid') if alive else None,
        'doing': rt.get('doing') or '',
        'started_at': rt.get('started_at') or '',
        'heartbeat_age': (now - hb) if hb else None,
        'stop_requested': bool(rt.get('stop_requested')),
        'exit_note': rt.get('exit_note') or '',
    }


def status_lines(summary: dict) -> list[str]:
    lines = [f'Persistent agent: {"ON" if summary["enabled"] else "OFF"} ({summary["state"]})']
    if summary['alive']:
        age = summary['heartbeat_age']
        hb = f'{int(age)}s ago' if age is not None else 'never'
        lines.append(f'Runner pid: {summary["pid"]} · heartbeat {hb}')
        lines.append(f'Doing: {summary["doing"] or "idle"}')
    elif summary['exit_note']:
        lines.append(f'Last stop: {summary["exit_note"]}')
    if summary['stop_requested'] and summary['alive']:
        lines.append('Stop requested; runner has not exited yet')
    return lines


def band_row(state_dir: Path, now: float | None = None) -> dict | None:
    """A row in the shape the chat's team band renders, or None when there
    is nothing to show (off and no process)."""
    now = time.time() if now is None else now
    s = status_summary(state_dir, now)
    if not s['enabled'] and not s['alive'] and s['state'] != 'process gone':
        return None
    rt = load_runtime(state_dir)
    started = _ts(rt.get('started_at')) or _ts(rt.get('updated_at')) or now
    last = _ts(rt.get('heartbeat_at')) or started
    if s['alive']:
        health = 'quiet' if now - last >= QUIET_AFTER_SECONDS else 'ok'
        doing = s['doing'] or 'idle'
    elif s['state'] == 'process gone':
        health, doing = 'lost', 'runner process exited'
    else:
        health, doing = 'ok', 'on · no runner process'
    return {
        'contract_id': 'persistent-agent',
        'agent_id': 'persistent-agent',
        'label': 'persistent',
        'goal': 'persistent agent',
        'doing': doing,
        'phase': '', 'phase_index': 0, 'phase_count': 0,
        'started_at': started,
        'last_event_at': last,
        'health': health,
        'pid': s['pid'],
        'enabled': s['enabled'],
    }


class PersistentBandTracker:
    """Turns successive band rows into transcript lines on real transitions:
    turned on, turned off, process exited, process back."""

    def __init__(self):
        self.prev: dict | None = None

    def tick(self, state_dir: Path, now: float | None = None) -> tuple[dict | None, list[str]]:
        now = time.time() if now is None else now
        row = band_row(state_dir, now)
        lines: list[str] = []
        before = self.prev
        if row and not before:
            lines.append(f'Persistent agent ON — {row["doing"]}')
        elif before and not row:
            lines.append('Persistent agent OFF — nothing is running on your behalf')
        elif row and before:
            if row['health'] == 'lost' and before['health'] != 'lost':
                pid = f' (pid {before["pid"]})' if before.get('pid') else ''
                lines.append(f'✗ Persistent agent runner exited{pid} — not running')
            elif row['health'] != 'lost' and before['health'] == 'lost':
                lines.append(f'Persistent agent running again — {row["doing"]}')
            elif not row['enabled'] and before['enabled']:
                lines.append('Persistent agent turned OFF; runner still alive, stop in progress')
        self.prev = row
        return row, lines


def band_headline(row: dict) -> str:
    """Headline for the band when the persistent agent is the only row."""
    if row['health'] == 'lost':
        return 'Persistent agent ON · runner exited'
    if row['health'] == 'quiet':
        return 'Persistent agent ON · runner quiet'
    if row.get('pid'):
        return 'Persistent agent ON · running'
    return 'Persistent agent ON · no runner process'
