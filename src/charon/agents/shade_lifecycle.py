"""Authoritative finite-state machine for retained-shade lifecycles.

A shade normally self-terminates the instant its contract finishes —
shade_tool._run_shade calls agent_lifecycle.set_status(shade_id, 'stopped')
unconditionally. A *retained* shade (SpawnShade's retain=True) instead goes
`idle` and stays addressable: charon.rlm(child_agent_id=...) can call it
again later. Reactivating it means constructing a brand-new
ConversationEngine for the same agent_id (relying on that engine's lossless-
context store to reload its prior conversation) rather than keeping a live
OS thread parked indefinitely — so a retained shade survives the whole
charon daemon restarting, matching "persists across session boundaries"
rather than just "persists across tool calls."

agent_lifecycle.status is a compatibility *projection* of this machine's
state, not a second source of truth — exactly the same relationship
libris_lifecycle.py has with Libris's legacy operation.json/topic.json
documents. Every transition here syncs that field as a side effect.

Reactivation is TOCTOU-safe by construction, not by convention: callers
must gate on try_reactivate()'s *success* (it dispatches the 'reactivate'
event through DurableMachineStore, whose per-instance file lock plus
state-validated transitions are the sole arbiter), never on a prior,
separate read of get_state(). Two concurrent try_reactivate() calls on the
same shade_id can't both win — the loser's dispatch raises
TransitionRejected because the transition target state 'running' is no
longer 'idle' by the time it acquires the lock.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

from charon.orchestration.fsm import MachineInstance, MachineSpec, TransitionSpec
from charon.orchestration.fsm_store import DurableMachineStore

try:
    from charon.infra.diagnostics import record as _diag
except Exception:  # diagnostics is best-effort and must never block import
    def _diag(*args, **kwargs):
        return None


# 'spawned' and 'running' are operationally identical (a shade actively
# working its phase loop) — kept as two states only so a freshly created
# instance and a just-reactivated one both read sensibly; both accept the
# same 'contract_completed' event.
STATE_TO_AGENT_STATUS = {
    'spawned': 'running',
    'running': 'running',
    'idle': 'idle',
    'stopped': 'stopped',
}

_TRANSITIONS = (
    TransitionSpec('contract_completed', {'spawned', 'running'}, 'idle'),
    TransitionSpec('reactivate', 'idle', 'running'),
    TransitionSpec('terminate', {'spawned', 'running', 'idle'}, 'stopped'),
)

SHADE_MACHINE = MachineSpec(
    name='shade.lifecycle',
    states=frozenset(STATE_TO_AGENT_STATUS),
    initial_state='spawned',
    transitions=_TRANSITIONS,
    terminal_states={'stopped'},
    version=1,
)


def _store(state_dir: Path) -> DurableMachineStore:
    return DurableMachineStore(Path(state_dir), SHADE_MACHINE)


def _sync_agent_status(shade_id: str, state: str) -> None:
    try:
        from charon.agents.agent_lifecycle import set_status
        set_status(shade_id, STATE_TO_AGENT_STATUS.get(state, 'stopped'))
    except Exception as e:
        _diag('shade_lifecycle', 'agent_lifecycle.status projection sync failed', error=e, shade_id=shade_id)


def initialize(state_dir: Path, shade_id: str) -> MachineInstance:
    """Create the lifecycle instance for a freshly spawned, retained shade.

    Idempotent: returns the existing instance if one already exists (e.g. a
    duplicate init after a crash) rather than raising.
    """
    store = _store(state_dir)
    existing = store.get(shade_id)
    if existing is not None:
        return existing
    instance = store.create(shade_id, state='spawned')
    _sync_agent_status(shade_id, instance.state)
    return instance


def mark_idle(state_dir: Path, shade_id: str) -> MachineInstance:
    """Transition a just-finished, retained shade from spawned/running to idle."""
    result = _store(state_dir).dispatch(shade_id, 'contract_completed', event_id=f'idle-{uuid.uuid4().hex}')
    _sync_agent_status(shade_id, result.instance.state)
    return result.instance


def try_reactivate(state_dir: Path, shade_id: str) -> MachineInstance:
    """Atomically claim an idle shade for reactivation — see module docstring
    for why this must be the sole gate, not a prior state read.

    Raises KeyError if no such lifecycle exists, TransitionRejected
    (charon.orchestration.fsm) if it isn't currently idle.
    """
    result = _store(state_dir).dispatch(shade_id, 'reactivate', event_id=f'reactivate-{uuid.uuid4().hex}')
    _sync_agent_status(shade_id, result.instance.state)
    return result.instance


def mark_stopped(state_dir: Path, shade_id: str) -> MachineInstance:
    result = _store(state_dir).dispatch(shade_id, 'terminate', event_id=f'stop-{uuid.uuid4().hex}')
    _sync_agent_status(shade_id, result.instance.state)
    return result.instance


def get_state(state_dir: Path, shade_id: str) -> str | None:
    """Read-only: this shade's current lifecycle state, or None if it was
    never retained (no lifecycle instance exists)."""
    instance = _store(state_dir).get(shade_id)
    return instance.state if instance else None


def get_instance(state_dir: Path, shade_id: str) -> MachineInstance | None:
    """Read-only: the full lifecycle instance (state + updated_at + data),
    or None if this shade was never retained. `updated_at` is stamped fresh
    on every dispatched transition (orchestration/fsm.py), so for an idle
    shade it's exactly "when this shade went idle" — the basis for TTL math
    without needing a separate timestamp field."""
    return _store(state_dir).get(shade_id)


def list_idle_shade_ids(state_dir: Path) -> list[str]:
    """All shade agent ids currently idle (retained, addressable).

    DurableMachineStore has no bulk-list method, so this goes through
    agent_lifecycle's agent registry (kept in sync by _sync_agent_status on
    every transition) rather than the FSM store directly.
    """
    from charon.agents.agent_lifecycle import list_agents
    return [
        a['id'] for a in list_agents()
        if a.get('role') == 'shade' and a.get('status') == 'idle' and a.get('id')
    ]


def reap_expired_idle_shades(state_dir: Path, *, max_idle_seconds: int) -> list[str]:
    """Stop any retained shade that's been idle longer than max_idle_seconds.

    Mirrors reconcile_stale_shade_contracts's exact shape (shade_orchestrator.py)
    and reconcile_stale_automation_runs's before it: list candidates, parse a
    timestamp, skip if fresh, else act + collect. Unlike those two, this is
    meant to be called on every heartbeat tick, not just once at daemon
    startup — a shade idle for hours inside a long-running daemon would
    otherwise never be caught.
    """
    reaped: list[str] = []
    now = datetime.now(timezone.utc).timestamp()
    for shade_id in list_idle_shade_ids(state_dir):
        instance = get_instance(state_dir, shade_id)
        if instance is None or instance.state != 'idle':
            continue
        try:
            idle_since = datetime.fromisoformat(instance.updated_at.replace('Z', '+00:00')).timestamp()
        except Exception:
            idle_since = 0.0
        if idle_since <= 0 or (now - idle_since) < max_idle_seconds:
            continue
        try:
            mark_stopped(state_dir, shade_id)
            reaped.append(shade_id)
        except Exception as e:
            _diag('shade_lifecycle', 'expired idle shade failed to stop', error=e, shade_id=shade_id)
    return reaped
