from pathlib import Path
import json
import os
import subprocess
import sys

SCRIPT = Path(__file__).resolve().parents[1] / 'apps' / 'core-daemon' / 'charon_loop.py'


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
