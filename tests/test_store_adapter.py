"""Tests for the store adapter — singleton DB, auto-migration, and module wiring."""
import json

import store_adapter  # noqa: E402


def setup_function():
    store_adapter.reset_all()


def test_get_db_returns_db_handle(tmp_path):
    db = store_adapter.get_db(tmp_path / 'state')
    assert db is not None
    assert (tmp_path / 'state' / 'charon.db').exists()


def test_get_db_is_singleton(tmp_path):
    state_dir = tmp_path / 'state'
    db1 = store_adapter.get_db(state_dir)
    db2 = store_adapter.get_db(state_dir)
    assert db1 is db2


def test_auto_migration_from_json(tmp_path):
    state_dir = tmp_path / 'state'
    state_dir.mkdir(parents=True, exist_ok=True)

    # Write some JSON state
    agents = [
        {'id': 'AG-0001', 'name': 'test-agent', 'mode': 'persistent',
         'goal': 'test', 'project': '/tmp/test', 'status': 'running',
         'created_at': '2026-01-01T00:00:00Z', 'last_active': '2026-01-01T00:00:00Z',
         'role': 'charon', 'visibility': 'user'},
    ]
    (state_dir / 'agents.json').write_text(json.dumps(agents))

    queue = [
        {'id': 'task-1', 'title': 'test task', 'status': 'pending',
         'created_at': '2026-01-01T00:00:00Z', 'updated_at': '2026-01-01T00:00:00Z'},
    ]
    (state_dir / 'queue.json').write_text(json.dumps(queue))

    # Open DB — should auto-migrate
    db = store_adapter.get_db(state_dir)

    # Verify migration
    agent = store_adapter.agent_get(db, 'AG-0001')
    assert agent is not None
    assert agent['name'] == 'test-agent'

    tasks = store_adapter.task_all(db)
    assert len(tasks) >= 1
    assert any(t['id'] == 'task-1' for t in tasks)


def test_close_db(tmp_path):
    state_dir = tmp_path / 'state'
    store_adapter.get_db(state_dir)
    store_adapter.close_db(state_dir)
    # Getting again should create a new one
    db = store_adapter.get_db(state_dir)
    assert db is not None


def test_with_store_context_manager(tmp_path):
    state_dir = tmp_path / 'state'
    with store_adapter.with_store(state_dir) as db:
        store_adapter.agent_insert(db, {
            'id': 'AG-9999', 'name': 'ctx-test', 'mode': 'persistent',
            'goal': '', 'project': '', 'status': 'running',
            'created_at': '2026-01-01T00:00:00Z', 'last_active': '2026-01-01T00:00:00Z',
        })
        agent = store_adapter.agent_get(db, 'AG-9999')
        assert agent is not None
        assert agent['name'] == 'ctx-test'


def test_agent_crud_through_adapter(tmp_path):
    db = store_adapter.get_db(tmp_path / 'state')

    # Insert
    store_adapter.agent_insert(db, {
        'id': 'AG-0010', 'name': 'alpha', 'mode': 'persistent',
        'goal': 'build stuff', 'project': '/proj', 'status': 'running',
        'created_at': '2026-01-01T00:00:00Z', 'last_active': '2026-01-01T00:00:00Z',
    })

    # Get
    a = store_adapter.agent_get(db, 'AG-0010')
    assert a['goal'] == 'build stuff'

    # List
    agents = store_adapter.agent_list(db)
    assert len(agents) == 1

    # Update
    store_adapter.agent_update(db, 'AG-0010', status='stopped')
    a2 = store_adapter.agent_get(db, 'AG-0010')
    assert a2['status'] == 'stopped'


def test_task_crud_through_adapter(tmp_path):
    db = store_adapter.get_db(tmp_path / 'state')

    store_adapter.task_insert(db, {
        'id': 'task-100', 'title': 'do thing', 'instruction': 'run it',
        'status': 'pending', 'task_type': 'agent_task',
        'created_at': '2026-01-01T00:00:00Z', 'updated_at': '2026-01-01T00:00:00Z',
    })

    t = store_adapter.task_get(db, 'task-100')
    assert t['title'] == 'do thing'

    pending = store_adapter.task_pending(db)
    assert len(pending) == 1

    store_adapter.task_update(db, 'task-100', status='completed')
    t2 = store_adapter.task_get(db, 'task-100')
    assert t2['status'] == 'completed'

    stats = store_adapter.task_queue_stats(db)
    assert stats['completed'] == 1
    assert stats['pending'] == 0


def test_onboarding_through_adapter(tmp_path):
    db = store_adapter.get_db(tmp_path / 'state')

    ob = store_adapter.onboarding_get(db)
    assert ob.get('complete') is False

    store_adapter.onboarding_set(db, {
        'complete': True, 'provider': 'lmstudio', 'model': 'qwen3',
    })
    ob2 = store_adapter.onboarding_get(db)
    assert ob2['complete'] is True
    assert ob2['provider'] == 'lmstudio'
