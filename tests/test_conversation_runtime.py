from pathlib import Path
import importlib.util
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
MOD_PATH = ROOT / 'apps' / 'core-daemon' / 'conversation_runtime.py'

spec = importlib.util.spec_from_file_location('conversation_runtime', MOD_PATH)
conversation_runtime = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = conversation_runtime
spec.loader.exec_module(conversation_runtime)


def test_enqueue_message_task_appends_pending_item(tmp_path):
    state_dir = tmp_path / 'state'
    task = conversation_runtime.enqueue_agent_message_task(
        state_dir,
        actor_agent_id='AG-1',
        conversation_id='conv-a',
        message='hello branch',
    )

    queue = json.loads((state_dir / 'queue.json').read_text())
    assert queue[-1]['id'] == task['id']
    assert queue[-1]['task_type'] == 'agent_message'
    assert queue[-1]['status'] == 'pending'


def test_enqueue_intervention_task_sets_target_message(tmp_path):
    state_dir = tmp_path / 'state'
    task = conversation_runtime.enqueue_agent_intervention_task(
        state_dir,
        actor_agent_id='AG-2',
        conversation_id='conv-a',
        intervention_of_message_id='msg-root',
        message='intervene now',
    )

    queue = json.loads((state_dir / 'queue.json').read_text())
    assert queue[-1]['id'] == task['id']
    assert queue[-1]['task_type'] == 'agent_intervention'
    assert queue[-1]['intervention_of_message_id'] == 'msg-root'


def test_list_conversations_reads_index_file(tmp_path):
    state_dir = tmp_path / 'state'
    idx = {
        'conversations': {
            'conv-x': {'message_count': 2, 'last_message_id': 'msg-2', 'agents': ['AG-1']},
            'conv-y': {'message_count': 1, 'last_message_id': 'msg-3', 'agents': ['AG-2']},
        }
    }
    (state_dir).mkdir(parents=True, exist_ok=True)
    (state_dir / 'conversation_index.json').write_text(json.dumps(idx))

    rows = conversation_runtime.list_conversations(state_dir)
    assert rows[0]['conversation_id'] == 'conv-x'
    assert rows[0]['message_count'] == 2


def test_enqueue_agent_task_sets_retry_defaults(tmp_path):
    state_dir = tmp_path / 'state'
    task = conversation_runtime.enqueue_agent_task(
        state_dir,
        owner_agent_id='AG-7',
        instruction='run: echo hello',
        project='/tmp/project',
    )

    queue = json.loads((state_dir / 'queue.json').read_text())
    item = queue[-1]
    assert item['id'] == task['id']
    assert item['task_type'] == 'agent_task'
    assert item['owner_agent_id'] == 'AG-7'
    assert item['status'] == 'pending'
    assert item['attempt_count'] == 0
    assert item['max_attempts'] == 3
    assert item['scope'] == []
    assert item['deps'] == []
    assert item['correlation_id'] == item['id']
    assert item['boundary']['status'] == 'unclaimed'


def test_enqueue_agent_task_accepts_scope_deps_and_correlation(tmp_path):
    state_dir = tmp_path / 'state'
    task = conversation_runtime.enqueue_agent_task(
        state_dir,
        owner_agent_id='AG-9',
        instruction='run: echo scoped',
        project='/tmp/project',
        scope=['src/api', 'docs'],
        deps=['task-1', 'task-2'],
        correlation_id='corr-99',
    )

    assert task['scope'] == ['src/api', 'docs']
    assert task['deps'] == ['task-1', 'task-2']
    assert task['correlation_id'] == 'corr-99'



def test_enqueue_boundary_proposal_task(tmp_path):
    state_dir = tmp_path / 'state'
    task = conversation_runtime.enqueue_boundary_proposal_task(
        state_dir,
        proposer_agent_id='AG-1',
        target_agent_id='AG-2',
        project='/tmp/project',
        scope=['src/api'],
        reason='avoid overlap',
    )

    assert task['task_type'] == 'boundary_proposal'
    assert task['status'] == 'pending'
    assert task['target_agent_id'] == 'AG-2'
    assert task['scope'] == ['src/api']


def test_enqueue_boundary_resolution_task(tmp_path):
    state_dir = tmp_path / 'state'
    task = conversation_runtime.enqueue_boundary_resolution_task(
        state_dir,
        resolver_agent_id='AG-2',
        proposal_id='bnd-123',
        decision='accept',
        reason='looks good',
    )

    assert task['task_type'] == 'boundary_resolution'
    assert task['proposal_id'] == 'bnd-123'
    assert task['decision'] == 'accept'


def test_enqueue_user_intent_task(tmp_path):
    state_dir = tmp_path / 'state'
    task = conversation_runtime.enqueue_user_intent_task(
        state_dir,
        actor_agent_id='AG-42',
        message='Please implement auth middleware',
        project='/tmp/proj',
        session_id='sess-1',
    )

    queue = json.loads((state_dir / 'queue.json').read_text())
    item = queue[-1]
    assert item['id'] == task['id']
    assert item['task_type'] == 'user_intent'
    assert item['owner_agent_id'] == 'AG-42'
    assert item['session_id'] == 'sess-1'
