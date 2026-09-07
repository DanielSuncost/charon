"""reconcile_stale_shade_contracts: repair contracts orphaned by a hard-killed
worker process (e.g. a PyKernel rlm() call whose subprocess got SIGKILLed
after a failed soft interrupt) — mirrors
automation_runtime.reconcile_stale_automation_runs for the same class of
problem.

Forces the JSON-only contract store (monkeypatching _use_store to False):
both the JSON and SQLite update paths unconditionally re-stamp updated_at to
"now" on every write, so there's no way to persist an arbitrarily stale
updated_at through the public API otherwise — these tests need one to set
up a "gone quiet" contract in the first place.
"""
import pytest
from datetime import datetime, timedelta, timezone

from charon.shade import shade_orchestrator as so


@pytest.fixture(autouse=True)
def _json_only_store(monkeypatch):
    monkeypatch.setattr(so, '_use_store', lambda: False)


def _make_contract(state_dir, *, updated_at=None, status=None):
    contract = so.create_contract(
        state_dir, parent_task_id='', parent_agent_id='AG-ROOT', shade_agent_id='AG-SHADE',
        conversation_id='conv-1', project=str(state_dir), goal='test goal',
    )
    if updated_at is not None or status is not None:
        contracts = so.load_contracts(state_dir)
        for c in contracts:
            if c['id'] == contract['id']:
                if updated_at is not None:
                    c['updated_at'] = updated_at
                if status is not None:
                    c['status'] = status
        so.save_contracts(state_dir, contracts)
    return contract


def test_reconcile_marks_stale_running_contract_as_failed(tmp_path):
    stale_ts = (datetime.now(timezone.utc) - timedelta(seconds=1000)).isoformat()
    contract = _make_contract(tmp_path, updated_at=stale_ts)

    recovered = so.reconcile_stale_shade_contracts(tmp_path, stale_after_seconds=300)

    assert recovered == [contract['id']]
    reloaded = {c['id']: c for c in so.load_contracts(tmp_path)}[contract['id']]
    assert reloaded['status'] == 'failed'
    assert 'reconciled' in reloaded['last_error']


def test_reconcile_leaves_fresh_running_contract_alone(tmp_path):
    contract = _make_contract(tmp_path)  # updated_at defaults to "now"

    recovered = so.reconcile_stale_shade_contracts(tmp_path, stale_after_seconds=300)

    assert recovered == []
    reloaded = {c['id']: c for c in so.load_contracts(tmp_path)}[contract['id']]
    assert reloaded['status'] == 'running'


def test_reconcile_ignores_already_terminal_contracts(tmp_path):
    stale_ts = (datetime.now(timezone.utc) - timedelta(seconds=1000)).isoformat()
    _make_contract(tmp_path, updated_at=stale_ts, status='completed')

    recovered = so.reconcile_stale_shade_contracts(tmp_path, stale_after_seconds=300)

    assert recovered == []


def test_reconcile_default_threshold_comes_from_config(tmp_path, monkeypatch):
    """Calling with no explicit stale_after_seconds falls back to
    config.shade_contract_stall_seconds(), not a bare literal — so the
    threshold used here and the one rlm()'s poll loop checks against
    (is_contract_stale, same default) can never drift apart."""
    monkeypatch.setattr(so.config, 'shade_contract_stall_seconds', lambda: 10)
    stale_ts = (datetime.now(timezone.utc) - timedelta(seconds=20)).isoformat()
    contract = _make_contract(tmp_path, updated_at=stale_ts)

    recovered = so.reconcile_stale_shade_contracts(tmp_path)

    assert recovered == [contract['id']]


# ---------------------------------------------------------------------------
# is_contract_stale: the standalone check rlm()'s poll loop uses to notice a
# contract has gone quiet without waiting for the next reconcile pass.
# ---------------------------------------------------------------------------

def test_is_contract_stale_true_when_running_and_quiet_too_long():
    stale_ts = (datetime.now(timezone.utc) - timedelta(seconds=1000)).isoformat()
    contract = {'status': 'running', 'updated_at': stale_ts}
    assert so.is_contract_stale(contract, stale_after_seconds=300) is True


def test_is_contract_stale_false_when_fresh():
    fresh_ts = datetime.now(timezone.utc).isoformat()
    contract = {'status': 'running', 'updated_at': fresh_ts}
    assert so.is_contract_stale(contract, stale_after_seconds=300) is False


def test_is_contract_stale_false_when_not_running():
    stale_ts = (datetime.now(timezone.utc) - timedelta(seconds=1000)).isoformat()
    contract = {'status': 'completed', 'updated_at': stale_ts}
    assert so.is_contract_stale(contract, stale_after_seconds=300) is False


def test_is_contract_stale_false_when_updated_at_missing_or_unparseable():
    assert so.is_contract_stale({'status': 'running'}, stale_after_seconds=300) is False
    assert so.is_contract_stale({'status': 'running', 'updated_at': 'not-a-date'}, stale_after_seconds=300) is False


def test_is_contract_stale_uses_config_default_when_not_given(monkeypatch):
    monkeypatch.setattr(so.config, 'shade_contract_stall_seconds', lambda: 10)
    contract = {'status': 'running', 'updated_at': (datetime.now(timezone.utc) - timedelta(seconds=20)).isoformat()}
    assert so.is_contract_stale(contract) is True
    contract2 = {'status': 'running', 'updated_at': (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()}
    assert so.is_contract_stale(contract2) is False
