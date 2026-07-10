from pathlib import Path
import json
import os
import subprocess
import sys

SCRIPT = Path(__file__).resolve().parents[1] / 'src' / 'charon' / 'charon_loop.py'


def _run_loop(state_dir: Path, stop_file: Path, max_cycles: int = 8):
    env = os.environ.copy()
    env.setdefault('CHARON_STDOUT_EVENTS', '0')
    cmd = [
        sys.executable, str(SCRIPT),
        '--state-dir', str(state_dir),
        '--stop-file', str(stop_file),
        '--sleep-sec', '0.01',
        '--max-cycles', str(max_cycles),
    ]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=25, env=env)


def test_user_intent_spawns_agent_task_and_updates_goal_packet(tmp_path):
    state_dir = tmp_path / 'state'
    stop_file = tmp_path / 'STOP'
    project = tmp_path / 'project'
    project.mkdir(parents=True, exist_ok=True)

    agents = [{
        'id': 'AG-501',
        'name': 'charon-main',
        'mode': 'persistent',
        'goal': 'help user',
        'project': str(project),
        'status': 'running',
        'role': 'charon',
        'visibility': 'user',
    }]
    queue = [{
        'id': 'intent-1',
        'title': 'user_intent:AG-501',
        'instruction': 'run: echo hello-intent',
        'message': 'run: echo hello-intent',
        'status': 'pending',
        'task_type': 'user_intent',
        'owner_agent_id': 'AG-501',
        'actor_agent_id': 'AG-501',
        'conversation_id': 'conv-intent-1',
        'session_id': 'sess-intent-1',
        'project': str(project),
        'priority': 'normal',
        'attempt_count': 0,
        'max_attempts': 1,
    }]

    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / 'agents.json').write_text(json.dumps(agents, indent=2))
    (state_dir / 'queue.json').write_text(json.dumps(queue, indent=2))

    proc = _run_loop(state_dir, stop_file)
    assert proc.returncode == 0

    final_queue = json.loads((state_dir / 'queue.json').read_text())
    intent = [t for t in final_queue if t['id'] == 'intent-1'][0]
    spawned = [t for t in final_queue if t.get('task_type') == 'agent_task' and t.get('goal_ref')]

    assert intent['status'] == 'completed'
    assert spawned
    assert any(t.get('status') == 'completed' for t in spawned)

    packet = json.loads((state_dir / 'context_packets' / 'AG-501.json').read_text())
    assert packet['agent_id'] == 'AG-501'
    assert packet['goal_count_session'] >= 1
