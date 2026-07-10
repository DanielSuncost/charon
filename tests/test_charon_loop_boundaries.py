from pathlib import Path
import json
import os
import subprocess
import sys

SCRIPT = Path(__file__).resolve().parents[1] / 'src' / 'charon' / 'charon_loop.py'


def _read_json(path: Path):
    return json.loads(path.read_text())


def _read_jsonl(path: Path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _run_loop(state_dir: Path, stop_file: Path, max_cycles: int = 6):
    env = os.environ.copy()
    env.setdefault('CHARON_STDOUT_EVENTS', '0')
    cmd = [
        sys.executable, str(SCRIPT),
        '--state-dir', str(state_dir),
        '--stop-file', str(stop_file),
        '--sleep-sec', '0.01',
        '--max-cycles', str(max_cycles),
    ]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=20, env=env)


def test_boundary_proposal_and_resolution_flow(tmp_path):
    state_dir = tmp_path / 'state'
    stop_file = tmp_path / 'STOP'
    project = tmp_path / 'project'
    project.mkdir(parents=True, exist_ok=True)

    agents = [
        {'id': 'AG-1101', 'name': 'charon-a', 'mode': 'persistent', 'goal': 'coord', 'project': str(project), 'status': 'running'},
        {'id': 'AG-1102', 'name': 'charon-b', 'mode': 'persistent', 'goal': 'coord', 'project': str(project), 'status': 'running'},
    ]

    proposal_task = {
        'id': 'bnd-task-1',
        'title': 'boundary_proposal:AG-1101->AG-1102',
        'status': 'pending',
        'task_type': 'boundary_proposal',
        'actor_agent_id': 'AG-1101',
        'target_agent_id': 'AG-1102',
        'project': str(project),
        'scope': ['src/api'],
        'reason': 'split API ownership',
        'conversation_id': 'conv-bnd-1',
        'correlation_id': 'corr-bnd-1',
        'attempt_count': 0,
        'max_attempts': 2,
    }

    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / 'agents.json').write_text(json.dumps(agents, indent=2))
    (state_dir / 'queue.json').write_text(json.dumps([proposal_task], indent=2))

    proc = _run_loop(state_dir, stop_file, max_cycles=2)
    assert proc.returncode == 0

    boundaries = _read_json(state_dir / 'boundaries.json')
    assert boundaries
    proposal_id = boundaries[0]['id']
    assert boundaries[0]['status'] == 'proposed'

    target_inbox = _read_jsonl(state_dir / 'agents' / 'AG-1102' / 'inbox.jsonl')
    assert any(e.get('event_type') == 'boundary_proposal_received' for e in target_inbox)

    resolution_task = {
        'id': 'bndres-task-1',
        'title': f'boundary_resolution:{proposal_id}',
        'status': 'pending',
        'task_type': 'boundary_resolution',
        'actor_agent_id': 'AG-1102',
        'proposal_id': proposal_id,
        'decision': 'accept',
        'reason': 'accepted',
        'conversation_id': 'conv-bnd-2',
        'correlation_id': 'corr-bnd-2',
        'attempt_count': 0,
        'max_attempts': 2,
    }

    (state_dir / 'queue.json').write_text(json.dumps([resolution_task], indent=2))
    proc2 = _run_loop(state_dir, stop_file, max_cycles=2)
    assert proc2.returncode == 0

    boundaries2 = _read_json(state_dir / 'boundaries.json')
    assert boundaries2[0]['status'] == 'accepted'

    proposer_inbox = _read_jsonl(state_dir / 'agents' / 'AG-1101' / 'inbox.jsonl')
    assert any(e.get('event_type') == 'boundary_resolution_received' for e in proposer_inbox)
