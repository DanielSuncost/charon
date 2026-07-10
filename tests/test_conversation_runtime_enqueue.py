import json

from charon.infra import store_adapter
from charon.infra.store import task_get
from charon.conversation import conversation_runtime


def setup_function():
    store_adapter.reset_all()


def test_enqueue_agent_task_writes_queue_and_sqlite(tmp_path):
    state_dir = tmp_path / 'state'
    state_dir.mkdir(parents=True, exist_ok=True)

    task = conversation_runtime.enqueue_agent_task(
        state_dir,
        owner_agent_id='AG-1',
        instruction='do the thing',
        title='queued task',
        project='/tmp/project',
    )

    queue = json.loads((state_dir / 'queue.json').read_text())
    assert any(row['id'] == task['id'] for row in queue)

    db_task = task_get(store_adapter.get_db(state_dir), task['id'])
    assert db_task is not None
    assert db_task['task_type'] == 'agent_task'
    assert db_task['status'] == 'pending'


def test_enqueue_user_intent_task_writes_queue_and_sqlite(tmp_path):
    state_dir = tmp_path / 'state'
    state_dir.mkdir(parents=True, exist_ok=True)

    task = conversation_runtime.enqueue_user_intent_task(
        state_dir,
        actor_agent_id='AG-2',
        message='fix the bug',
        project='/tmp/project',
        conversation_id='conv-1',
    )

    queue = json.loads((state_dir / 'queue.json').read_text())
    assert any(row['id'] == task['id'] for row in queue)

    db_task = task_get(store_adapter.get_db(state_dir), task['id'])
    assert db_task is not None
    assert db_task['task_type'] == 'user_intent'
    assert db_task['conversation_id'] == 'conv-1'
