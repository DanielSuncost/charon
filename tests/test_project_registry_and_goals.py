import json

from charon.infra import project_registry
from charon.agents import goal_runtime
from charon.tools import memory_tools as mem_tools
from charon import tools as tools_mod


def test_project_registry_reuses_same_root(tmp_path):
    state_dir = tmp_path / 'state'
    project_root = tmp_path / 'repo'
    project_root.mkdir(parents=True)

    p1 = project_registry.ensure_project(state_dir, project_root)
    p2 = project_registry.ensure_project(state_dir, project_root)

    assert p1['id'] == p2['id']
    reg = project_registry.load_registry(state_dir)
    assert reg['root_map'][str(project_root.resolve())] == p1['id']


def test_goals_live_under_canonical_project_dir(tmp_path):
    state_dir = tmp_path / 'state'
    project_root = tmp_path / 'repo'
    project_root.mkdir(parents=True)

    meta = goal_runtime.ingest_user_intent(
        state_dir,
        agent_id='AG-1',
        project=str(project_root),
        session_id='sess-a',
        conversation_id='conv-a',
        text='Implement login flow',
    )

    goals_path = state_dir / 'projects' / meta['project_id'] / 'goals.json'
    assert goals_path.exists()
    doc = json.loads(goals_path.read_text())
    assert len(doc['goals']) == 1


def test_project_knowledge_uses_canonical_project_storage(tmp_path):
    state_dir = tmp_path / 'state'
    project_root = tmp_path / 'repo'
    project_root.mkdir(parents=True)
    ctx = tools_mod.ToolContext(project_root=project_root, agent_id='AG-1', state_dir=state_dir)

    res = mem_tools.execute_project_knowledge({'action': 'add', 'content': 'Use uv for Python envs'}, ctx)
    assert not res.is_error

    proj = project_registry.ensure_project(state_dir, project_root)
    kp = state_dir / 'projects' / proj['id'] / 'KNOWLEDGE.md'
    assert kp.exists()
    assert 'Use uv for Python envs' in kp.read_text()
