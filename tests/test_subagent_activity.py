from datetime import datetime, timezone, timedelta

from backend import subagent_activity as activity
from backend import common
from chat_backend import ChatBackend


def contract(parent='owner', child='shade1', status='running', **extra):
    return {'id': child + '-contract', 'parent_agent_id': parent, 'shade_agent_id': child,
            'status': status, 'goal': 'Implement\n LSP support',
            'updated_at': datetime.now(timezone.utc).isoformat(),
            'current_phase_id': 'P1', 'phases': [{'phase_id': 'P1', 'name': 'verification', 'status': 'running'}],
            **extra}


def test_only_owners_tree_including_running_grandchildren(tmp_path, monkeypatch):
    rows = [contract(status='completed'), contract('shade1', 'grandchild'),
            contract('unrelated', 'other'), contract(child='finished', status='failed'),
            contract(child='queued', status='queued')]
    monkeypatch.setattr(activity, 'load_contracts', lambda _: rows)
    result = activity.collect_subagent_activity(tmp_path, 'owner')
    assert {r['agent_id'] for r in result['items']} == {'grandchild', 'queued'}
    assert result['total'] == 2
    assert all(r['title'] == 'Implement LSP support' for r in result['items'])
    assert result['owner_id'] == 'owner'


def test_empty_snapshot_clears_finished_tasks(tmp_path, monkeypatch):
    rows = [contract()]
    monkeypatch.setattr(activity, 'load_contracts', lambda _: rows)
    assert activity.collect_subagent_activity(tmp_path, 'owner')['total'] == 1
    rows[0]['status'] = 'completed'
    assert activity.collect_subagent_activity(tmp_path, 'owner')['items'] == []
    assert activity.collect_subagent_activity(tmp_path, '')['items'] == []


def test_stall_and_between_phases_are_honest(tmp_path, monkeypatch):
    old = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    row = contract(updated_at=old, phases=[
        {'phase_id': 'P1', 'name': 'implementation', 'status': 'completed'},
        {'phase_id': 'P2', 'name': 'verification', 'status': 'pending'},
    ])
    monkeypatch.setattr(activity, 'load_contracts', lambda _: [row])
    result = activity.collect_subagent_activity(tmp_path, 'owner')['items'][0]
    assert result['status'] == 'stalled'
    assert result['phase'] == 'verification'


def test_cyclic_lineage_is_bounded_and_payload_capped(tmp_path, monkeypatch):
    rows = [contract(child=f'shade{i}') for i in range(70)] + [contract('shade0', 'owner')]
    monkeypatch.setattr(activity, 'load_contracts', lambda _: rows)
    result = activity.collect_subagent_activity(tmp_path, 'owner')
    assert len(result['items']) == 50
    assert result['total'] == 71


def test_backend_bound_identity_and_visible_collection_failure(tmp_path, monkeypatch):
    backend = ChatBackend()
    backend._active_agent_id = 'session'
    backend._bound_agent_id = 'agent'
    monkeypatch.setattr(common, 'STATE_DIR', tmp_path)
    monkeypatch.setattr(activity, 'load_contracts', lambda _: [contract(parent='agent')])
    assert backend._get_subagent_activity()['total'] == 1
    def broken(_):
        raise OSError('unavailable')
    monkeypatch.setattr(activity, 'load_contracts', broken)
    snapshot = backend._get_subagent_activity()
    assert snapshot['owner_id'] == 'agent'
    assert snapshot['error'] and snapshot['items'] == []
