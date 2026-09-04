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
