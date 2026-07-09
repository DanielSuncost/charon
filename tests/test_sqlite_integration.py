"""Integration tests: verify SQLite store is actually populated when runtime modules execute.

These tests run the actual runtime functions and then check that state
landed in SQLite, not just JSON files.
"""
import importlib.util
import sys
from pathlib import Path

import store_adapter
from libs.store import (
    task_get, boundary_list, contract_list, shade_event_list,
    agent_memory_get, agent_inbox_list, agent_profile_get,
    goal_project_get, goal_session_get, goal_context_packet_get,
)

ROOT = Path(__file__).resolve().parents[1]
DAEMON = ROOT / 'apps' / 'core-daemon'


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


agent_lifecycle = _load('agent_lifecycle_integ', DAEMON / 'agent_lifecycle.py')
agent_runtime = _load('agent_runtime_integ', DAEMON / 'agent_runtime.py')
conversation_runtime = _load('conversation_runtime_integ', DAEMON / 'conversation_runtime.py')
shade_orchestrator = _load('shade_orchestrator_integ', DAEMON / 'shade_orchestrator.py')
boundary_runtime = _load('boundary_runtime_integ', DAEMON / 'boundary_runtime.py')
goal_runtime = _load('goal_runtime_integ', DAEMON / 'goal_runtime.py')


def setup_function():
    store_adapter.reset_all()


# ---------------------------------------------------------------
# agent_runtime: working memory, inbox, attempts
# ---------------------------------------------------------------

def test_agent_runtime_syncs_to_sqlite(tmp_path):
    state_dir = tmp_path / 'state'
    project_dir = tmp_path / 'project'
    project_dir.mkdir(parents=True, exist_ok=True)

    agent = {
        'id': 'AG-INT-01', 'name': 'integ-agent', 'mode': 'persistent',
        'goal': 'test', 'project': str(project_dir), 'status': 'running',
    }
    task = {
        'id': 'task-integ-01', 'task_type': 'agent_task',
        'instruction': 'run: echo integration-test',
        'project': str(project_dir),
    }

    ok, result = agent_runtime.run_task_tick(state_dir, task, agent=agent, llm_adapter=None)
    assert ok

    db = store_adapter.get_db(state_dir)

    # Check working memory synced
    mem = agent_memory_get(db, 'AG-INT-01')
    assert mem is not None
    assert mem['last_task_id'] == 'task-integ-01'

    # Check profile synced
    profile = agent_profile_get(db, 'AG-INT-01')
    assert profile is not None
    assert profile['agent_id'] == 'AG-INT-01'

    # Check inbox has events
    inbox = agent_inbox_list(db, 'AG-INT-01')
    assert len(inbox) > 0
    event_types = [e['event_type'] for e in inbox]
    assert 'task_received' in event_types
    assert 'task_succeeded' in event_types


# ---------------------------------------------------------------
# conversation_runtime: task enqueueing
# ---------------------------------------------------------------

def test_conversation_runtime_enqueue_syncs_to_sqlite(tmp_path):
    state_dir = tmp_path / 'state'
    state_dir.mkdir(parents=True, exist_ok=True)

    task = conversation_runtime.enqueue_agent_task(
        state_dir,
        owner_agent_id='AG-INT-02',
        instruction='do something',
        title='test enqueue',
        project='/tmp/test',
    )

    db = store_adapter.get_db(state_dir)
    db_task = task_get(db, task['id'])
    assert db_task is not None
    assert db_task['title'] == 'test enqueue'
    assert db_task['status'] == 'pending'


def test_user_intent_enqueue_syncs_to_sqlite(tmp_path):
    state_dir = tmp_path / 'state'
    state_dir.mkdir(parents=True, exist_ok=True)

    task = conversation_runtime.enqueue_user_intent_task(
        state_dir,
        actor_agent_id='AG-INT-03',
        message='fix the bug',
        project='/tmp/test',
    )

    db = store_adapter.get_db(state_dir)
    db_task = task_get(db, task['id'])
    assert db_task is not None
    assert db_task['task_type'] == 'user_intent'


# ---------------------------------------------------------------
# boundary_runtime
# ---------------------------------------------------------------

def test_boundary_runtime_syncs_to_sqlite(tmp_path):
    state_dir = tmp_path / 'state'
    state_dir.mkdir(parents=True, exist_ok=True)

    proposal = boundary_runtime.create_proposal(
        state_dir,
        proposer_agent_id='AG-A',
        target_agent_id='AG-B',
        project='/proj',
        scope=['src/api'],
        reason='overlap detected',
    )

    db = store_adapter.get_db(state_dir)
    boundaries = boundary_list(db)
    assert len(boundaries) == 1
    assert boundaries[0]['proposer_agent_id'] == 'AG-A'

    # Resolve
    boundary_runtime.resolve_proposal(
        state_dir,
        proposal_id=proposal['id'],
        resolver_agent_id='AG-B',
        decision='accept',
        reason='agreed',
    )

    from libs.store import boundary_get
    updated = boundary_get(db, proposal['id'])
    assert updated['status'] == 'accepted'


# ---------------------------------------------------------------
# shade_orchestrator
# ---------------------------------------------------------------

def test_shade_orchestrator_syncs_to_sqlite(tmp_path):
    state_dir = tmp_path / 'state'
    state_dir.mkdir(parents=True, exist_ok=True)

    contract = shade_orchestrator.create_contract(
        state_dir,
        parent_task_id='task-parent',
        parent_agent_id='AG-PARENT',
        shade_agent_id='AG-SHADE',
        conversation_id='conv-test',
        project='/proj',
        goal='implement feature X',
    )

    db = store_adapter.get_db(state_dir)
    contracts = contract_list(db)
    assert len(contracts) == 1
    assert contracts[0]['goal'] == 'implement feature X'

    # Check phase events
    events = shade_event_list(db, contract['id'])
    assert len(events) >= 1
    assert events[0]['event_type'] == 'contract_created'


# ---------------------------------------------------------------
# goal_runtime
# ---------------------------------------------------------------

def test_goal_runtime_syncs_to_sqlite(tmp_path):
    state_dir = tmp_path / 'state'
    state_dir.mkdir(parents=True, exist_ok=True)

    result = goal_runtime.ingest_user_intent(
        state_dir,
        agent_id='AG-GOAL',
        project='/tmp/myproject',
        session_id='ses-001',
        conversation_id='conv-001',
        text='fix the login bug',
    )

    db = store_adapter.get_db(state_dir)

    # Check project synced
    proj = goal_project_get(db, result['project_id'])
    assert proj is not None
    assert len(proj.get('goals', [])) >= 1

    # Check session synced
    ses = goal_session_get(db, result['session_id'])
    assert ses is not None

    # Build and check context packet
    goal_runtime.build_context_packet(
        state_dir,
        agent_id='AG-GOAL',
        project_id=result['project_id'],
        session_id=result['session_id'],
    )
    db_packet = goal_context_packet_get(db, 'AG-GOAL')
    assert db_packet is not None
    assert db_packet['agent_id'] == 'AG-GOAL'
