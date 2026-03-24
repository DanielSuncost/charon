from pathlib import Path
import json
import subprocess

SCRIPT = Path(__file__).resolve().parents[1] / 'apps' / 'core-daemon' / 'charon_loop.py'


def read_events(log_path: Path):
    if not log_path.exists():
        return []
    return [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]


def test_loop_completes_bootstrap_tasks(tmp_path):
    state_dir = tmp_path / 'state'
    stop_file = tmp_path / 'STOP'

    cmd = [
        'python3', str(SCRIPT),
        '--state-dir', str(state_dir),
        '--stop-file', str(stop_file),
        '--sleep-sec', '0.01',
        '--max-cycles', '10',
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0

    queue = json.loads((state_dir / 'queue.json').read_text())
    assert all(t['status'] == 'completed' for t in queue)

    events = read_events(state_dir / 'run.log')
    names = [e['event'] for e in events]
    assert 'loop_start' in names
    assert 'task_success' in names
    assert 'loop_exit' in names


def test_stop_file_halts_loop(tmp_path):
    state_dir = tmp_path / 'state'
    stop_file = tmp_path / 'STOP'
    stop_file.write_text('stop')

    cmd = [
        'python3', str(SCRIPT),
        '--state-dir', str(state_dir),
        '--stop-file', str(stop_file),
        '--sleep-sec', '0.01',
        '--max-cycles', '10',
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0

    events = read_events(state_dir / 'run.log')
    names = [e['event'] for e in events]
    assert 'loop_stop_file_detected' in names
    assert names[-1] == 'loop_exit'


def test_debug_trace_writes_verbose_log(tmp_path):
    state_dir = tmp_path / 'state'
    stop_file = tmp_path / 'STOP'

    cmd = [
        'python3', str(SCRIPT),
        '--state-dir', str(state_dir),
        '--stop-file', str(stop_file),
        '--sleep-sec', '0.01',
        '--max-cycles', '4',
        '--debug-trace',
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    assert proc.returncode == 0

    debug_log = state_dir / 'debug.log'
    assert debug_log.exists()
    rows = [json.loads(line) for line in debug_log.read_text().splitlines() if line.strip()]
    names = [r['event'] for r in rows]
    assert 'loop_start_trace' in names
    assert 'task_started' in names
    assert any(name.startswith('process_task_') for name in names)
