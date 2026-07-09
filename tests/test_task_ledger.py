"""Tests for the task ledger."""
import json

import store_adapter
from task_ledger import get_agent_ledger, get_agent_ledger_summary, format_ledger_text


def setup_function():
    store_adapter.reset_all()


def test_ledger_from_tasks(tmp_path):
    state_dir = tmp_path / 'state'
    db = store_adapter.get_db(state_dir)
    from libs.store import task_insert

    task_insert(db, {
        'id': 'task-1', 'title': 'Fix auth bug', 'instruction': 'fix it',
        'status': 'completed', 'task_type': 'agent_task',
        'owner_agent_id': 'AG-001', 'result_summary': 'Fixed auth bug in login.py',
        'created_at': '2026-03-20T14:00:00Z', 'updated_at': '2026-03-20T14:05:00Z',
        'completed_at': '2026-03-20T14:05:00Z',
    })
    task_insert(db, {
        'id': 'task-2', 'title': 'Add tests', 'instruction': 'add tests',
        'status': 'completed', 'task_type': 'agent_task',
        'owner_agent_id': 'AG-001', 'result_summary': 'Added 5 unit tests',
        'created_at': '2026-03-20T14:10:00Z', 'updated_at': '2026-03-20T14:15:00Z',
        'completed_at': '2026-03-20T14:15:00Z',
    })
    task_insert(db, {
        'id': 'task-3', 'title': 'Deploy', 'instruction': 'deploy',
        'status': 'pending', 'task_type': 'agent_task',
        'owner_agent_id': 'AG-001',
        'created_at': '2026-03-20T14:20:00Z', 'updated_at': '2026-03-20T14:20:00Z',
    })

    ledger = get_agent_ledger(state_dir, 'AG-001')
    assert len(ledger) == 3
    # Newest first
    assert ledger[0]['task_id'] == 'task-3'
    assert ledger[0]['status'] == 'pending'
    assert ledger[1]['title'] == 'Added 5 unit tests'


def test_ledger_from_working_memory(tmp_path):
    state_dir = tmp_path / 'state'
    agent_dir = state_dir / 'agents' / 'AG-002'
    agent_dir.mkdir(parents=True)
    memory = {
        'agent_id': 'AG-002',
        'notes': [
            {'ts': '2026-03-20T10:00:00Z', 'task_id': 'task-a', 'summary': 'Refactored auth module'},
            {'ts': '2026-03-20T11:00:00Z', 'task_id': 'task-b', 'summary': 'Fixed import paths'},
        ],
    }
    (agent_dir / 'working_memory.json').write_text(json.dumps(memory))

    ledger = get_agent_ledger(state_dir, 'AG-002')
    assert len(ledger) == 2
    assert 'Refactored' in ledger[1]['title']
    assert 'import paths' in ledger[0]['title']


def test_ledger_deduplicates(tmp_path):
    state_dir = tmp_path / 'state'
    db = store_adapter.get_db(state_dir)
    from libs.store import task_insert, agent_memory_upsert

    task_insert(db, {
        'id': 'task-dup', 'title': 'Do thing', 'status': 'completed',
        'task_type': 'agent_task', 'owner_agent_id': 'AG-003',
        'result_summary': 'Did the thing',
        'created_at': '2026-03-20T10:00:00Z', 'updated_at': '2026-03-20T10:00:00Z',
        'completed_at': '2026-03-20T10:05:00Z',
    })
    agent_memory_upsert(db, 'AG-003', {
        'notes': [{'ts': '2026-03-20T10:05:00Z', 'task_id': 'task-dup', 'summary': 'Did the thing'}],
    })

    ledger = get_agent_ledger(state_dir, 'AG-003')
    assert len(ledger) == 1  # deduplicated by task_id


def test_ledger_summary_stats(tmp_path):
    state_dir = tmp_path / 'state'
    db = store_adapter.get_db(state_dir)
    from libs.store import task_insert

    for i in range(5):
        task_insert(db, {
            'id': f'task-{i}', 'title': f'Task {i}', 'status': 'completed',
            'task_type': 'agent_task', 'owner_agent_id': 'AG-004',
            'result_summary': f'Done {i}',
            'created_at': f'2026-03-20T{10+i}:00:00Z',
            'updated_at': f'2026-03-20T{10+i}:00:00Z',
            'completed_at': f'2026-03-20T{10+i}:05:00Z',
        })
    task_insert(db, {
        'id': 'task-fail', 'title': 'Bad task', 'status': 'failed',
        'task_type': 'agent_task', 'owner_agent_id': 'AG-004',
        'result_summary': 'Something broke',
        'created_at': '2026-03-20T16:00:00Z', 'updated_at': '2026-03-20T16:00:00Z',
    })

    result = get_agent_ledger_summary(state_dir, 'AG-004')
    assert result['stats']['completed'] == 5
    assert result['stats']['failed'] == 1
    assert result['stats']['total'] == 6


def test_ledger_empty(tmp_path):
    ledger = get_agent_ledger(tmp_path / 'state', 'AG-NONE')
    assert ledger == []


def test_format_ledger_text(tmp_path):
    entries = [
        {'status': 'completed', 'ts_short': 'Mar 20 14:23', 'title': 'Fixed auth bug'},
        {'status': 'failed', 'ts_short': 'Mar 20 14:10', 'title': 'Deploy attempt failed'},
        {'status': 'pending', 'ts_short': 'Mar 20 14:30', 'title': 'Add rate limiting'},
    ]
    text = format_ledger_text(entries)
    assert '✓' in text
    assert '✗' in text
    assert '○' in text
    assert 'auth bug' in text


def test_exclude_pending(tmp_path):
    state_dir = tmp_path / 'state'
    db = store_adapter.get_db(state_dir)
    from libs.store import task_insert

    task_insert(db, {
        'id': 'done', 'title': 'Done', 'status': 'completed',
        'task_type': 'agent_task', 'owner_agent_id': 'AG-005',
        'result_summary': 'Done',
        'created_at': '2026-03-20T10:00:00Z', 'updated_at': '2026-03-20T10:00:00Z',
    })
    task_insert(db, {
        'id': 'pending', 'title': 'Pending', 'status': 'pending',
        'task_type': 'agent_task', 'owner_agent_id': 'AG-005',
        'created_at': '2026-03-20T11:00:00Z', 'updated_at': '2026-03-20T11:00:00Z',
    })

    with_pending = get_agent_ledger(state_dir, 'AG-005', include_pending=True)
    assert len(with_pending) == 2

    without = get_agent_ledger(state_dir, 'AG-005', include_pending=False)
    assert len(without) == 1
    assert without[0]['status'] == 'completed'
