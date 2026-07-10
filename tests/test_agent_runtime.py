import json


from charon.agents import agent_runtime


def test_run_task_tick_with_shell_instruction_updates_memory_and_inbox(tmp_path):
    state_dir = tmp_path / 'state'
    project_dir = tmp_path / 'project'
    project_dir.mkdir(parents=True, exist_ok=True)

    agent = {
        'id': 'AG-1001',
        'name': 'builder',
        'mode': 'persistent',
        'goal': 'ship features',
        'project': str(project_dir),
        'status': 'running',
    }
    task = {
        'id': 'task-1',
        'task_type': 'agent_task',
        'instruction': 'run: echo hello-charon',
        'project': str(project_dir),
    }

    ok, result = agent_runtime.run_task_tick(state_dir, task, agent=agent, llm_adapter=None)
    assert ok
    assert result['status'] == 'task_succeeded'
    assert 'hello-charon' in (result.get('summary') or '')

    mem = json.loads((state_dir / 'agents' / 'AG-1001' / 'working_memory.json').read_text())
    assert mem['last_task_id'] == 'task-1'
    assert mem['last_task_summary']

    inbox = (state_dir / 'agents' / 'AG-1001' / 'inbox.jsonl').read_text().splitlines()
    assert any('task_received' in line for line in inbox)
    assert any('task_succeeded' in line for line in inbox)


def test_run_task_tick_rejects_path_escape_write_file(tmp_path):
    state_dir = tmp_path / 'state'
    project_dir = tmp_path / 'project'
    project_dir.mkdir(parents=True, exist_ok=True)

    agent = {
        'id': 'AG-1002',
        'name': 'writer',
        'mode': 'persistent',
        'goal': 'write files safely',
        'project': str(project_dir),
        'status': 'running',
    }
    task = {
        'id': 'task-2',
        'task_type': 'agent_task',
        'instruction': 'write: ../escape.txt | nope',
        'project': str(project_dir),
    }

    ok, result = agent_runtime.run_task_tick(state_dir, task, agent=agent, llm_adapter=None)
    assert not ok
    assert result['status'] == 'task_failed'
    assert 'escapes project root' in (result.get('error') or '')


class _StubLLM:
    @staticmethod
    def query_local_model(prompt):
        return True, '{"action":"final","summary":"model says hi"}'


def test_run_task_tick_uses_llm_planner_when_onboarding_provider_complete(tmp_path):
    state_dir = tmp_path / 'state'
    project_dir = tmp_path / 'project'
    project_dir.mkdir(parents=True, exist_ok=True)

    (state_dir / 'onboarding.json').parent.mkdir(parents=True, exist_ok=True)
    (state_dir / 'onboarding.json').write_text(json.dumps({
        'complete': True,
        'provider_mode': 'provider',
        'provider': 'opencode',
    }))

    agent = {
        'id': 'AG-1003',
        'name': 'planner',
        'mode': 'persistent',
        'goal': 'answer questions',
        'project': str(project_dir),
        'status': 'running',
    }
    task = {
        'id': 'task-3',
        'task_type': 'agent_task',
        'instruction': 'hello there',
        'project': str(project_dir),
    }

    ok, result = agent_runtime.run_task_tick(state_dir, task, agent=agent, llm_adapter=_StubLLM())
    assert ok
    assert result['status'] == 'task_succeeded'
    assert result['summary'] == 'model says hi'
