"""Tests for shade_lifecycle: the durable FSM behind retained shades.

agent_lifecycle.set_status is monkeypatched to a recorder rather than left
to hit the real global agents.json — these tests are about the FSM's own
transitions and the projection-sync side effect, not agent_lifecycle's
storage.
"""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from charon.agents import shade_lifecycle as sl
from charon.orchestration.fsm import TransitionRejected


@pytest.fixture
def status_calls(monkeypatch):
    calls = []
    monkeypatch.setattr(
        'charon.agents.agent_lifecycle.set_status',
        lambda agent_id, status: calls.append((agent_id, status)),
    )
    return calls


def test_initialize_creates_spawned_instance_and_projects_running(tmp_path, status_calls):
    instance = sl.initialize(tmp_path, 'AG-SHADE-1')
    assert instance.state == 'spawned'
    assert sl.get_state(tmp_path, 'AG-SHADE-1') == 'spawned'
    assert status_calls[-1] == ('AG-SHADE-1', 'running')


def test_initialize_is_idempotent(tmp_path, status_calls):
    first = sl.initialize(tmp_path, 'AG-SHADE-1')
    second = sl.initialize(tmp_path, 'AG-SHADE-1')
    assert first.state == second.state == 'spawned'


def test_full_lifecycle_spawned_idle_running_stopped(tmp_path, status_calls):
    sl.initialize(tmp_path, 'AG-SHADE-1')

    idle = sl.mark_idle(tmp_path, 'AG-SHADE-1')
    assert idle.state == 'idle'
    assert status_calls[-1] == ('AG-SHADE-1', 'idle')

    running = sl.try_reactivate(tmp_path, 'AG-SHADE-1')
    assert running.state == 'running'
    assert status_calls[-1] == ('AG-SHADE-1', 'running')

    idle_again = sl.mark_idle(tmp_path, 'AG-SHADE-1')
    assert idle_again.state == 'idle'

    stopped = sl.mark_stopped(tmp_path, 'AG-SHADE-1')
    assert stopped.state == 'stopped'
    assert status_calls[-1] == ('AG-SHADE-1', 'stopped')
    assert sl.get_state(tmp_path, 'AG-SHADE-1') == 'stopped'


def test_stopped_is_terminal_no_further_transitions(tmp_path, status_calls):
    sl.initialize(tmp_path, 'AG-SHADE-1')
    sl.mark_stopped(tmp_path, 'AG-SHADE-1')
    with pytest.raises(TransitionRejected):
        sl.try_reactivate(tmp_path, 'AG-SHADE-1')
    with pytest.raises(TransitionRejected):
        sl.mark_idle(tmp_path, 'AG-SHADE-1')


def test_reactivate_running_shade_is_rejected(tmp_path, status_calls):
    """reactivate is only legal from idle — a running shade can't be
    reactivated (it's already active)."""
    sl.initialize(tmp_path, 'AG-SHADE-1')
    with pytest.raises(TransitionRejected):
        sl.try_reactivate(tmp_path, 'AG-SHADE-1')


def test_try_reactivate_unknown_shade_raises_keyerror(tmp_path, status_calls):
    with pytest.raises(KeyError):
        sl.try_reactivate(tmp_path, 'AG-NEVER-SPAWNED')


def test_get_state_returns_none_for_unknown_shade(tmp_path):
    assert sl.get_state(tmp_path, 'AG-NEVER-SPAWNED') is None


def test_concurrent_reactivation_only_one_winner(tmp_path, status_calls):
    """Two callers racing to reactivate the same idle shade: exactly one
    must succeed, gated by DurableMachineStore.dispatch()'s own locking —
    never a prior separate read-then-act."""
    sl.initialize(tmp_path, 'AG-SHADE-1')
    sl.mark_idle(tmp_path, 'AG-SHADE-1')

    def _attempt(_i):
        try:
            sl.try_reactivate(tmp_path, 'AG-SHADE-1')
            return 'ok'
        except TransitionRejected:
            return 'rejected'

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(_attempt, range(8)))

    assert results.count('ok') == 1
    assert results.count('rejected') == 7


def _backdate_instance(tmp_path, shade_id, *, seconds_ago):
    """No public API lets a transition stamp an arbitrary updated_at (fsm.py's
    low-level dispatch() takes now=, but DurableMachineStore doesn't expose
    it) — patch the on-disk snapshot directly, same trick used for
    reconcile_stale_shade_contracts's tests."""
    path = tmp_path / 'orchestration' / 'machines' / 'shade.lifecycle' / shade_id / 'instance.json'
    data = json.loads(path.read_text())
    data['updated_at'] = (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()
    path.write_text(json.dumps(data))


def test_get_instance_returns_full_instance_with_updated_at(tmp_path, status_calls):
    sl.initialize(tmp_path, 'AG-SHADE-1')
    sl.mark_idle(tmp_path, 'AG-SHADE-1')
    instance = sl.get_instance(tmp_path, 'AG-SHADE-1')
    assert instance.state == 'idle'
    assert instance.updated_at


def test_get_instance_none_for_unknown_shade(tmp_path):
    assert sl.get_instance(tmp_path, 'AG-NEVER-SPAWNED') is None


def test_list_idle_shade_ids_filters_by_role_and_status(tmp_path, monkeypatch):
    monkeypatch.setattr('charon.agents.agent_lifecycle.list_agents', lambda: [
        {'id': 'AG-1', 'role': 'shade', 'status': 'idle'},
        {'id': 'AG-2', 'role': 'shade', 'status': 'running'},
        {'id': 'AG-3', 'role': 'charon', 'status': 'idle'},
        {'id': 'AG-4', 'role': 'shade', 'status': 'idle'},
    ])
    assert sl.list_idle_shade_ids(tmp_path) == ['AG-1', 'AG-4']


def test_reap_expired_idle_shades_stops_stale_and_leaves_fresh(tmp_path, status_calls, monkeypatch):
    sl.initialize(tmp_path, 'AG-STALE')
    sl.mark_idle(tmp_path, 'AG-STALE')
    _backdate_instance(tmp_path, 'AG-STALE', seconds_ago=1000)

    sl.initialize(tmp_path, 'AG-FRESH')
    sl.mark_idle(tmp_path, 'AG-FRESH')

    monkeypatch.setattr('charon.agents.agent_lifecycle.list_agents', lambda: [
        {'id': 'AG-STALE', 'role': 'shade', 'status': 'idle'},
        {'id': 'AG-FRESH', 'role': 'shade', 'status': 'idle'},
    ])

    reaped = sl.reap_expired_idle_shades(tmp_path, max_idle_seconds=300)

    assert reaped == ['AG-STALE']
    assert sl.get_state(tmp_path, 'AG-STALE') == 'stopped'
    assert sl.get_state(tmp_path, 'AG-FRESH') == 'idle'


def test_reap_expired_idle_shades_noop_when_none_stale(tmp_path, status_calls, monkeypatch):
    sl.initialize(tmp_path, 'AG-FRESH')
    sl.mark_idle(tmp_path, 'AG-FRESH')
    monkeypatch.setattr('charon.agents.agent_lifecycle.list_agents', lambda: [
        {'id': 'AG-FRESH', 'role': 'shade', 'status': 'idle'},
    ])

    reaped = sl.reap_expired_idle_shades(tmp_path, max_idle_seconds=300)

    assert reaped == []
    assert sl.get_state(tmp_path, 'AG-FRESH') == 'idle'
