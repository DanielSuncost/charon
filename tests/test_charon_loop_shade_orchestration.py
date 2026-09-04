from pathlib import Path
import json
import os
import subprocess
import sys

from charon import charon_loop
from charon.orchestration.graph_runtime import get_run, start_run
from charon.orchestration.graph_schema import GraphDefinition

SCRIPT = Path(__file__).resolve().parents[1] / 'src' / 'charon' / 'charon_loop.py'


def _run_loop(state_dir: Path, stop_file: Path, max_cycles: int = 40):
    env = os.environ.copy()
    env.setdefault('CHARON_STDOUT_EVENTS', '0')
    env.setdefault('CHARON_SHADE_REQUIRE_TMUX', '0')
    cmd = [
        sys.executable, str(SCRIPT),
        '--state-dir', str(state_dir),
        '--stop-file', str(stop_file),
        '--sleep-sec', '0.01',
        '--max-cycles', str(max_cycles),
    ]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=env)


def _read_json(path: Path):
    return json.loads(path.read_text())


def _read_jsonl(path: Path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_charon_task_delegates_to_shade_contract_and_indexes_phases(tmp_path):
    state_dir = tmp_path / 'state'
    stop_file = tmp_path / 'STOP'
    project = tmp_path / 'project'
    project.mkdir(parents=True, exist_ok=True)

    agents = [{
        'id': 'AG-2101',
        'name': 'charon-main',
        'mode': 'persistent',
        'goal': 'coordinate',
        'project': str(project),
        'status': 'running',
        'role': 'charon',
        'visibility': 'user',
    }]

    queue = [{
        'id': 'task-main-1',
        'title': 'agent_task:AG-2101',
        'instruction': 'Implement feature with careful sequencing and validation across api and docs with strict evidence logging.',
        'status': 'pending',
        'task_type': 'agent_task',
        'owner_agent_id': 'AG-2101',
        'actor_agent_id': 'AG-2101',
        'conversation_id': 'conv-main-1',
        'project': str(project),
        'priority': 'normal',
        'attempt_count': 0,
        'max_attempts': 3,
        'scope': ['src/api', 'docs'],
        'deps': [],
        'correlation_id': 'corr-main-1',
        'constraints': ['Do not change migrations', 'Keep API routes stable'],
        'expected_outputs': ['Updated code', 'Verification summary'],
        'phase_plan': [
            {'name': 'analysis', 'objective': 'Plan the changes'},
            {'name': 'implementation', 'objective': 'Make the changes'},
        ],
        'boundary': {'status': 'unclaimed', 'lease_owner': 'AG-2101', 'lease_expires_at': None, 'overlap_with': []},
    }]

    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / 'agents.json').write_text(json.dumps(agents, indent=2))
    (state_dir / 'queue.json').write_text(json.dumps(queue, indent=2))

    proc = _run_loop(state_dir, stop_file)
    assert proc.returncode == 0

    final_queue = _read_json(state_dir / 'queue.json')
    parent = [t for t in final_queue if t.get('id') == 'task-main-1'][0]
    assert parent['status'] == 'completed'
    assert 'Contract' in (parent.get('result_summary') or '')

    contracts = _read_json(state_dir / 'shade_contracts.json')
    assert contracts
    contract = contracts[0]
    assert contract['status'] == 'completed'
    assert contract['phase_count'] == 2

    events = _read_jsonl(state_dir / 'shade_phase_events.jsonl')
    kinds = [e.get('event_type') for e in events if e.get('contract_id') == contract.get('id')]
    assert 'phase_queued' in kinds
    assert 'phase_completed' in kinds

    child_tasks = [t for t in final_queue if (t.get('shade_phase') or {}).get('contract_id') == contract.get('id')]
    assert child_tasks
    assert all((t.get('shade_phase') or {}).get('phase_lookup_key', '').startswith(contract.get('id') + ':') for t in child_tasks)


def test_explicit_route_bypasses_automatic_shade_delegation(
    tmp_path,
    monkeypatch,
):
    state_dir = tmp_path / 'state'
    project = tmp_path / 'project'
    state_dir.mkdir()
    project.mkdir()
    agent = {
        'id': 'AG-routed',
        'name': 'routed-agent',
        'mode': 'persistent',
        'goal': 'execute the selected route',
        'project': str(project),
        'status': 'running',
        'role': 'charon',
    }
    (state_dir / 'agents.json').write_text(json.dumps([agent]))
    route = {
        'provider': 'local',
        'model_id': 'qwen3-30b-a3b',
        'context_window': 65_536,
    }
    task = {
        'id': 'task-routed-complex',
        'instruction': 'Execute a complex routed task.',
        'status': 'in_progress',
        'task_type': 'agent_task',
        'owner_agent_id': agent['id'],
        'project': str(project),
        'scope': ['src', 'tests'],
        'constraints': ['keep compatibility', 'record evidence'],
        'expected_outputs': ['implementation', 'test report'],
        'model_route': route,
    }
    calls = []

    def fake_run_task_tick(state, current, *, agent, llm_adapter):
        calls.append((state, current['id'], agent['id']))
        current['selected_model'] = dict(route)
        current['executed_model'] = dict(route)
        current['route_honored'] = True
        return True, {
            'status': 'task_succeeded',
            'summary': 'routed execution completed',
            'attempt_id': 'att-routed',
        }

    monkeypatch.setattr(
        charon_loop.AGENT_RUNTIME,
        'run_task_tick',
        fake_run_task_tick,
    )
    monkeypatch.setattr(
        charon_loop,
        '_tick_shade_contract',
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError('routed task was delegated to shades')
        ),
    )

    ok, result = charon_loop.process_task(
        task,
        state_dir,
        [task],
    )

    assert ok is True
    assert result['attempt_id'] == 'att-routed'
    assert calls == [(state_dir, task['id'], agent['id'])]
    assert task['route_honored'] is True
    assert 'shade_orchestration' not in task


def test_daemon_registers_builtin_graph_executors_after_restart(
    tmp_path,
    monkeypatch,
):
    state_dir = tmp_path / 'state'
    stop_file = tmp_path / 'STOP'
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / 'agents.json').write_text('[]')
    (state_dir / 'queue.json').write_text('[]')
    definition = GraphDefinition.from_dict({
        'schema_version': 1,
        'graph_id': 'daemon-smoke',
        'entry_nodes': ['finish'],
        'nodes': [{
            'id': 'finish',
            'handler': 'noop',
            'terminal': True,
            'config': {'output': {'ok': True}},
        }],
        'edges': [],
    })
    started = start_run(state_dir, definition)
    monkeypatch.setenv('CHARON_HEARTBEAT_INTERVAL', '1')

    proc = _run_loop(state_dir, stop_file, max_cycles=3)

    assert proc.returncode == 0, proc.stderr
    final = get_run(state_dir, started['run_id'])
    assert final['status'] == 'completed'
    assert final['node_states']['finish']['output'] == {'ok': True}
