"""Live status of the shades working for this chat, plus the periodic
transcript updates the chat view shows while they run.

The band is fed from contracts (phase, status) and each shade's heartbeat
(what it is doing, when it last produced an event, which process runs it).
Health is reported as seen, never assumed: a shade with no events for the
stall window is 'quiet', and one whose worker thread or process is gone is
'lost' — its contract is failed and the transcript says so, so the band can
never show a countdown over dead work.
"""
from __future__ import annotations

import os
import time
from datetime import datetime
from pathlib import Path

from charon.infra import config
from charon.shade.shade_activity import load_activity, pid_alive, thread_alive
from charon.shade.shade_orchestrator import get_contract, load_contracts, mark_contract_lost

ACTIVE = {'running', 'pending', 'queued', 'blocked', 'paused', 'waiting'}
STOPPED_LINGER_SECONDS = 60


def update_interval_seconds() -> int:
    try:
        return max(30, int(os.environ.get('CHARON_TEAM_UPDATE_SECONDS', '300')))
    except ValueError:
        return 300


def _ts(value) -> float:
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
    except (TypeError, ValueError):
        return 0.0


def _compact(value, limit: int) -> str:
    text = ' '.join(str(value or '').split())
    return text if len(text) <= limit else text[:limit - 1] + '…'


def _duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f'{seconds}s'
    if seconds < 3600:
        return f'{seconds // 60}m'
    return f'{seconds // 3600}h{(seconds % 3600) // 60:02d}m'


def _lineage(contracts: list[dict], owner_id: str) -> set[str]:
    # Follow children of children, including through finished parents.
    owners = {owner_id}
    while True:
        children = {str(c.get('shade_agent_id')) for c in contracts
                    if c.get('parent_agent_id') in owners and c.get('shade_agent_id')}
        if children <= owners:
            return owners
        owners |= children


def _agent_row(state_dir: Path, contract: dict, now: float, stall_after: int) -> dict:
    cid = str(contract.get('id') or '')
    phases = contract.get('phases') or []
    phase = next((p for p in phases if p.get('phase_id') == contract.get('current_phase_id')), {})
    if phase.get('status') == 'completed':
        phase = next((p for p in phases if p.get('status') in {'running', 'queued', 'pending'}), phase)
    done = sum(1 for p in phases if p.get('status') == 'completed')
    activity = load_activity(state_dir, cid)
    started = _ts(contract.get('created_at')) or now
    last = max(float(activity.get('last_event_at') or 0), _ts(contract.get('updated_at')), started)

    pid = activity.get('pid')
    ours = thread_alive(cid)
    if ours is not None:
        alive = ours
    elif pid and int(pid) != os.getpid():
        alive = pid_alive(int(pid))
    elif pid:
        alive = False           # recorded by this process, but no thread of ours runs it
    else:
        alive = None            # no heartbeat yet (or spawned before heartbeats existed)

    if alive is False:
        health = 'lost'
    elif now - last >= stall_after:
        health = 'quiet'
    else:
        health = 'ok'
    metadata = contract.get('metadata') if isinstance(contract.get('metadata'), dict) else {}
    agent_id = str(contract.get('shade_agent_id') or '')
    return {
        'contract_id': cid,
        'agent_id': agent_id,
        'label': _compact(metadata.get('role') or agent_id or cid, 16),
        'goal': _compact(contract.get('goal'), 120),
        'doing': _compact(activity.get('doing') or 'starting', 60),
        'phase': _compact(phase.get('name'), 20),
        'phase_index': min(done + 1, len(phases)) if phases else 0,
        'phase_count': len(phases),
        'started_at': started,
        'last_event_at': last,
        'health': health,
        'pid': pid,
    }


def collect_team(state_dir: Path, owner_id: str, now: float | None = None) -> list[dict]:
    if not owner_id:
        return []
    now = time.time() if now is None else now
    contracts = load_contracts(state_dir)
    owners = _lineage(contracts, owner_id)
    stall_after = config.shade_contract_stall_seconds()
    rows = [_agent_row(state_dir, c, now, stall_after) for c in contracts
            if c.get('parent_agent_id') in owners and c.get('status') in ACTIVE]
    rows.sort(key=lambda r: r['started_at'])
    return rows


def agent_line(row: dict, now: float) -> str:
    phase = f"{row['phase']} {row['phase_index']}/{row['phase_count']}" if row['phase_count'] else ''
    if row['health'] == 'lost':
        state = 'worker gone — not running'
    elif row['health'] == 'quiet':
        state = f"quiet {_duration(now - row['last_event_at'])} — last: {row['doing']}"
    else:
        state = row['doing']
    parts = [p for p in (phase, state) if p]
    return f"{row['label']} · {' · '.join(parts)} · {_duration(now - row['started_at'])}"


class TeamTracker:
    """Turns successive snapshots into a status event and transcript updates."""

    def __init__(self, interval: int | None = None):
        self.interval = interval or update_interval_seconds()
        self.prev: dict[str, dict] = {}
        self.next_update_at: float | None = None
        self.team_started_at: float | None = None
        self.stopped: dict | None = None
        self.outcomes: dict[str, str] = {}     # this team's members → how each ended

    def _summary(self, rows: list[dict], now: float, title: str) -> list[str]:
        lines = [title] + [f'  {agent_line(r, now)}' for r in rows]
        if self.next_update_at:
            at = time.strftime('%H:%M', time.localtime(self.next_update_at))
            lines.append(f'  next update in {_duration(self.next_update_at - now)} ({at})')
        return lines

    def tick(self, state_dir: Path, owner_id: str, now: float | None = None) -> tuple[dict | None, list[str]]:
        now = time.time() if now is None else now
        rows = collect_team(state_dir, owner_id, now)
        lines: list[str] = []

        lost = [r for r in rows if r['health'] == 'lost']
        for r in lost:
            pid = f' (pid {r["pid"]})' if r.get('pid') else ''
            mark_contract_lost(state_dir, r['contract_id'], f'worker exited{pid} without finishing')
        live = [r for r in rows if r['health'] != 'lost']

        transitions = []
        for cid, before in self.prev.items():
            if any(r['contract_id'] == cid for r in live):
                continue
            if any(r['contract_id'] == cid for r in lost):
                transitions.append(f"✗ {before['label']} stopped: its worker exited without finishing")
                self.outcomes[cid] = 'lost'
                continue
            contract = get_contract(state_dir, cid) or {}
            status = contract.get('status') or 'gone'
            took = _duration(now - before['started_at'])
            self.outcomes[cid] = status
            if status == 'completed':
                transitions.append(f"✓ {before['label']} finished ({before['phase_count']}/{before['phase_count']} phases, {took})")
            else:
                error = _compact(contract.get('last_error'), 80)
                transitions.append(f"✗ {before['label']} {status} after {took}" + (f': {error}' if error else ''))
        for r in live:
            before = self.prev.get(r['contract_id'])
            if r['health'] == 'quiet' and (before is None or before['health'] != 'quiet'):
                transitions.append(f"⚠ {r['label']} has produced nothing for {_duration(now - r['last_event_at'])} — last: {r['doing']}")
            elif r['health'] == 'ok' and before and before['health'] == 'quiet':
                transitions.append(f"{r['label']} is producing again: {r['doing']}")

        if live:
            if self.team_started_at is None:
                self.team_started_at = now
            for r in live:
                self.outcomes.setdefault(r['contract_id'], 'running')
            if transitions or self.next_update_at is None:
                self.next_update_at = now + self.interval
            elif now >= self.next_update_at:
                self.next_update_at = now + self.interval
                lines = self._summary(live, now, f'Team update · {len(live)} working on your task')
            self.stopped = None
        else:
            if lost and not self.stopped:
                self.stopped = {'count': len(lost), 'until': now + STOPPED_LINGER_SECONDS,
                                'message': f'Team stopped — the worker running {len(lost)} agent{"s" if len(lost) != 1 else ""} exited'}
                transitions.append(f'✗ {self.stopped["message"]}; nothing is running for this task now')
            elif self.prev and not lost and not self.stopped:
                took = _duration(now - (self.team_started_at or now))
                total = len(self.outcomes)
                ok = sum(1 for v in self.outcomes.values() if v == 'completed')
                mark = '✓' if ok == total else '✗'
                transitions.append(f'{mark} Team done — {ok}/{total} agents completed their work ({took})')
            self.next_update_at = None
            self.team_started_at = None
            self.outcomes = {}
        if transitions:
            lines = transitions + lines
            if live:
                lines.append(f'  {len(live)} still working · next update in {_duration(self.next_update_at - now)}')

        self.prev = {r['contract_id']: r for r in live}
        if live:
            status = {'state': 'working', 'agents': live, 'next_update_at': self.next_update_at,
                      'stall_after': config.shade_contract_stall_seconds()}
        elif self.stopped and now < self.stopped['until']:
            status = {'state': 'stopped', 'agents': [], 'message': self.stopped['message'], 'next_update_at': None}
        else:
            self.stopped = None
            status = {'state': 'idle', 'agents': [], 'next_update_at': None}
        status['sent_at'] = now
        return status, lines
