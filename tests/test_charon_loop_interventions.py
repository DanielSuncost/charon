from pathlib import Path
import json
import subprocess
import sys

SCRIPT = Path(__file__).resolve().parents[1] / 'src' / 'charon' / 'charon_loop.py'


def _read_jsonl(path: Path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_loop_records_graph_message_event_when_task_has_conversation_fields(tmp_path):
    state_dir = tmp_path / 'state'
    stop_file = tmp_path / 'STOP'
    state_dir.mkdir(parents=True, exist_ok=True)

    queue = [
        {
            'id': 'msg-1',
            'title': 'Plan work',
            'status': 'pending',
            'task_type': 'agent_message',
            'conversation_id': 'conv-loop-1',
            'actor_agent_id': 'AG-0001',
            'message': 'Starting branch A',
        }
    ]
    (state_dir / 'queue.json').write_text(json.dumps(queue, indent=2))

    cmd = [
        sys.executable, str(SCRIPT),
        '--state-dir', str(state_dir),
        '--stop-file', str(stop_file),
        '--sleep-sec', '0.01',
        '--max-cycles', '2',
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0

    events = _read_jsonl(state_dir / 'interventions.jsonl')
    assert len(events) == 1
    assert events[0]['event_type'] == 'agent_message'
    assert events[0]['conversation_id'] == 'conv-loop-1'

    run_events = _read_jsonl(state_dir / 'run.log')
    names = [e['event'] for e in run_events]
    assert 'task_graph_recorded' in names
