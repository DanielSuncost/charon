from pathlib import Path
import json
import os
import subprocess
import sys

SCRIPT = Path(__file__).resolve().parents[1] / 'apps' / 'core-daemon' / 'charon_loop.py'


def _read_json(path: Path):
    return json.loads(path.read_text())


def _read_jsonl(path: Path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _run_loop(state_dir: Path, stop_file: Path, max_cycles: int = 6, extra_env: dict | None = None):
    env = os.environ.copy()
    env.setdefault('CHARON_STDOUT_EVENTS', '0')
    if extra_env:
        env.update(extra_env)
    cmd = [
        sys.executable, str(SCRIPT),
        '--state-dir', str(state_dir),
        '--stop-file', str(stop_file),
        '--sleep-sec', '0.01',
        '--max-cycles', str(max_cycles),
    ]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=20, env=env)


def test_loop_executes_agent_task_and_persists_runtime_state(tmp_path):
    state_dir = tmp_path / 'state'
    stop_file = tmp_path / 'STOP'
    project = tmp_path / 'project'
    project.mkdir(parents=True, exist_ok=True)

    agents = [{
        'id': 'AG-0001',
        'name': 'worker',
        'mode': 'persistent',
        'goal': 'execute tasks',
        'project': str(project),
        'status': 'running',
    }]
    queue = [{
        'id': 'task-100',
        'title': 'agent_task:AG-0001',
        'instruction': 'run: echo charon-v0',
        'status': 'pending',
        'task_type': 'agent_task',
        'owner_agent_id': 'AG-0001',
        'actor_agent_id': 'AG-0001',
        'conversation_id': 'conv-100',
        'project': str(project),
        'priority': 'normal',
        'attempt_count': 0,
        'max_attempts': 3,
    }]

    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / 'agents.json').write_text(json.dumps(agents, indent=2))
    (state_dir / 'queue.json').write_text(json.dumps(queue, indent=2))

    proc = _run_loop(state_dir, stop_file, max_cycles=3)
    assert proc.returncode == 0

    final_queue = _read_json(state_dir / 'queue.json')
    task = final_queue[0]
    assert task['status'] == 'completed'
    assert task['attempt_count'] == 1
    assert 'charon-v0' in (task.get('result_summary') or '')

    mem_file = state_dir / 'agents' / 'AG-0001' / 'working_memory.json'
    assert mem_file.exists()

    graph_events = _read_jsonl(state_dir / 'interventions.jsonl')
    assert any(e.get('conversation_id') == 'conv-100' for e in graph_events)


def test_loop_retries_and_terminally_fails_after_max_attempts(tmp_path):
    state_dir = tmp_path / 'state'
    stop_file = tmp_path / 'STOP'
    project = tmp_path / 'project'
    project.mkdir(parents=True, exist_ok=True)

    agents = [{
        'id': 'AG-0002',
        'name': 'worker',
        'mode': 'persistent',
        'goal': 'execute tasks',
        'project': str(project),
        'status': 'running',
    }]
    queue = [{
        'id': 'task-200',
        'title': 'agent_task:AG-0002',
        'instruction': 'write: ../escape.txt | blocked',
        'status': 'pending',
        'task_type': 'agent_task',
        'owner_agent_id': 'AG-0002',
        'actor_agent_id': 'AG-0002',
        'conversation_id': 'conv-200',
        'project': str(project),
        'priority': 'normal',
        'attempt_count': 0,
        'max_attempts': 2,
    }]

    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / 'agents.json').write_text(json.dumps(agents, indent=2))
    (state_dir / 'queue.json').write_text(json.dumps(queue, indent=2))

    proc = _run_loop(state_dir, stop_file, max_cycles=6)
    assert proc.returncode == 0

    final_queue = _read_json(state_dir / 'queue.json')
    task = final_queue[0]
    assert task['status'] == 'failed'
    assert task['attempt_count'] == 2
    assert 'escapes project root' in (task.get('result_summary') or '')

    run_events = _read_jsonl(state_dir / 'run.log')
    names = [e['event'] for e in run_events]
    assert 'task_failure' in names
    assert 'task_failed_terminal' in names


def test_loop_recovers_stale_in_progress_task(tmp_path):
    state_dir = tmp_path / 'state'
    stop_file = tmp_path / 'STOP'
    project = tmp_path / 'project'
    project.mkdir(parents=True, exist_ok=True)

    agents = [{
        'id': 'AG-0003',
        'name': 'worker',
        'mode': 'persistent',
        'goal': 'recover tasks',
        'project': str(project),
        'status': 'running',
    }]
    queue = [{
        'id': 'task-300',
        'title': 'agent_task:AG-0003',
        'instruction': 'run: echo recovered',
        'status': 'in_progress',
        'started_at': '2000-01-01T00:00:00+00:00',
        'task_type': 'agent_task',
        'owner_agent_id': 'AG-0003',
        'actor_agent_id': 'AG-0003',
        'conversation_id': 'conv-300',
        'project': str(project),
        'priority': 'normal',
        'attempt_count': 0,
        'max_attempts': 2,
    }]

    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / 'agents.json').write_text(json.dumps(agents, indent=2))
    (state_dir / 'queue.json').write_text(json.dumps(queue, indent=2))

    proc = _run_loop(state_dir, stop_file, max_cycles=4, extra_env={'CHARON_STALE_IN_PROGRESS_SEC': '1'})
    assert proc.returncode == 0

    final_queue = _read_json(state_dir / 'queue.json')
    task = final_queue[0]
    assert task['status'] == 'completed'
    assert task.get('recovered_from_in_progress') == 1

    run_events = _read_jsonl(state_dir / 'run.log')
    assert any(e.get('event') == 'queue_recovered' for e in run_events)


def test_loop_records_overlap_coordination_for_competing_charons(tmp_path):
    state_dir = tmp_path / 'state'
    stop_file = tmp_path / 'STOP'
    project = tmp_path / 'project'
    project.mkdir(parents=True, exist_ok=True)

    agents = [
        {
            'id': 'AG-0101',
            'name': 'charon-a',
            'mode': 'persistent',
            'goal': 'work',
            'project': str(project),
            'status': 'running',
        },
        {
            'id': 'AG-0102',
            'name': 'charon-b',
            'mode': 'persistent',
            'goal': 'work',
            'project': str(project),
            'status': 'running',
        },
    ]

    queue = [
        {
            'id': 'task-ovl-1',
            'title': 'agent_task:AG-0101',
            'instruction': 'run: echo one',
            'status': 'pending',
            'task_type': 'agent_task',
            'owner_agent_id': 'AG-0101',
            'actor_agent_id': 'AG-0101',
            'conversation_id': 'conv-ovl-1',
            'project': str(project),
            'priority': 'normal',
            'attempt_count': 0,
            'max_attempts': 2,
            'scope': ['src/api'],
            'deps': [],
            'correlation_id': 'corr-ovl-1',
            'boundary': {'status': 'unclaimed', 'lease_owner': 'AG-0101', 'lease_expires_at': None, 'overlap_with': []},
        },
        {
            'id': 'task-ovl-2',
            'title': 'agent_task:AG-0102',
            'instruction': 'run: echo two',
            'status': 'pending',
            'task_type': 'agent_task',
            'owner_agent_id': 'AG-0102',
            'actor_agent_id': 'AG-0102',
            'conversation_id': 'conv-ovl-2',
            'project': str(project),
            'priority': 'normal',
            'attempt_count': 0,
            'max_attempts': 2,
            'scope': ['src'],
            'deps': [],
            'correlation_id': 'corr-ovl-2',
            'boundary': {'status': 'unclaimed', 'lease_owner': 'AG-0102', 'lease_expires_at': None, 'overlap_with': []},
        },
    ]

    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / 'agents.json').write_text(json.dumps(agents, indent=2))
    (state_dir / 'queue.json').write_text(json.dumps(queue, indent=2))

    proc = _run_loop(state_dir, stop_file, max_cycles=3)
    assert proc.returncode == 0

    run_events = _read_jsonl(state_dir / 'run.log')
    overlap_events = [e for e in run_events if e.get('event') == 'task_overlap_detected']
    assert overlap_events

    interventions = _read_jsonl(state_dir / 'interventions.jsonl')
    assert any((e.get('branch_label') == 'coordination' or e.get('payload', {}).get('content', '').startswith('Coordination:')) for e in interventions)
