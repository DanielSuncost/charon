"""charon_loop: the `overseer_cycle` task type, driven as a subprocess like the other loop tests."""
from pathlib import Path
import json
import os
import subprocess
import sys

import pytest

from charon.conversation.conversation_runtime import enqueue_overseer_cycle
from charon.workspace import cycle as C
from charon.workspace.store import WorkspaceStore

SCRIPT = Path(__file__).resolve().parents[1] / 'src' / 'charon' / 'charon_loop.py'
OV = {'kind': 'agent', 'id': 'agent.overseer.ws1'}
SYS = {'kind': 'system', 'id': 'system.charon'}


def _read_jsonl(path: Path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


@pytest.fixture(autouse=True)
def _json_only_persistence(monkeypatch):
    """JSON-only persistence: the enqueue helper runs in this process and
    would otherwise hold the SQLite mirror open, making every write in the
    loop subprocess wait out the busy timeout. Scoped via monkeypatch (not
    a bare module-level os.environ assignment) so it reverts after each
    test instead of leaking CHARON_NO_SQLITE=1 into every other test that
    runs later in the same pytest process."""
    monkeypatch.setenv('CHARON_NO_SQLITE', '1')


def _run_loop(state_dir: Path, stop_file: Path, max_cycles: int = 2):
    env = os.environ.copy()
    env.setdefault('CHARON_STDOUT_EVENTS', '0')
    env['CHARON_NO_SQLITE'] = '1'
    cmd = [sys.executable, str(SCRIPT), '--state-dir', str(state_dir), '--stop-file', str(stop_file),
           '--sleep-sec', '0.01', '--max-cycles', str(max_cycles)]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30, env=env)


def _workspace(tmp_path: Path) -> tuple[WorkspaceStore, Path]:
    root = tmp_path / 'ws'
    store = WorkspaceStore.open(root, workspace_id='workspace.charon.ws1', replica_id='replica.charon.test',
                                slug='ws1', title='Alpha', roots=[{'kind': 'directory', 'locator': str(tmp_path)}])
    store.create('session', {'id': 'session.b1', 'kind': 'worker', 'status': 'idle', 'actor': {'kind': 'agent', 'id': 'agent.b1'},
                             'host_ref': 'local', 'task_ids': [], 'started_at': store.iso(), 'event_cursors': {},
                             'extensions': {'callsign': 'Builder', 'title': 'Builder'}}, actor=SYS)
    wi = store.create('work_item', {'kind': 'task', 'title': 'Wire MCP endpoint'}, actor=OV)
    store.transition('work_item', wi['id'], wi['revision'], 'ready', actor=OV)
    gate = store.create_gate(kind='question', question='Drop mosh from scope?', options=['yes', 'no'], actor=OV)
    store.decide_gate(gate['id'], 'yes')
    return store, root


def _state(tmp_path: Path) -> tuple[Path, Path]:
    state_dir = tmp_path / 'state'
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / 'agents.json').write_text(json.dumps([{
        'id': 'AG-OVERSEER', 'name': 'overseer', 'mode': 'persistent', 'role': 'overseer',
        'goal': 'oversee', 'project': str(tmp_path), 'status': 'running',
    }], indent=2))
    return state_dir, tmp_path / 'STOP'


def test_loop_runs_overseer_cycle_delivers_to_agent_and_recurs(tmp_path):
    store, root = _workspace(tmp_path)
    state_dir, stop_file = _state(tmp_path)
    task = enqueue_overseer_cycle(state_dir, workspace_root=root, delivery={'kind': 'agent', 'owner_agent_id': 'AG-OVERSEER'},
                                  interval_minutes=10, workspace_name='Alpha', replica_id='replica.charon.loop')
    assert task['task_type'] == 'overseer_cycle' and task['workspace_root'] == str(root)

    proc = _run_loop(state_dir, stop_file, max_cycles=2)
    assert proc.returncode == 0, proc.stderr[-2000:]

    run_events = _read_jsonl(state_dir / 'run.log')
    names = [e['event'] for e in run_events]
    assert 'overseer_cycle_delivered' in names, names
    delivered = next(e for e in run_events if e['event'] == 'overseer_cycle_delivered')
    assert delivered['cycle'] == 1 and delivered['events'] >= 2 and delivered['delivery'] == 'agent'

    # the cycle landed in the workspace chain, under the loop's replica
    fresh = WorkspaceStore.open(root, workspace_id='workspace.charon.ws1', replica_id='replica.charon.loop')
    cyc = [e for e in fresh.events if e['event_type'] == 'cycle.started']
    assert len(cyc) == 1 and cyc[0]['replica_id'] == 'replica.charon.loop' and cyc[0]['payload']['cycle'] == 1
    assert any('decided by user: yes' in line for line in cyc[0]['payload']['lines'])
    assert fresh.verify_chain('replica.charon.loop')['ok']
    assert fresh.get_extension(C.EXT_CYCLE_COUNT) == 1

    queue = json.loads((state_dir / 'queue.json').read_text())
    agent_tasks = [t for t in queue if t.get('task_type') == 'agent_task' and t.get('owner_agent_id') == 'AG-OVERSEER']
    assert agent_tasks, 'digest was enqueued as an agent_task for the native overseer'
    assert agent_tasks[0]['instruction'].startswith('[acheron cycle 1 · ')
    assert agent_tasks[0]['instruction'].rstrip().endswith('Follow the overseer skill.')

    recurring = [t for t in queue if t.get('task_type') == 'overseer_cycle' and t.get('status') == 'pending']
    assert recurring, 'overseer_cycle re-enqueued'
    nxt = recurring[0]
    assert nxt['not_before'] and nxt['interval_minutes'] == 10.0
    for key in ('workspace_root', 'delivery', 'cadence', 'workspace_name', 'replica_id'):
        assert nxt.get(key) == task.get(key), key
    done = [t for t in queue if t.get('id') == task['id']][0]
    assert done['status'] == 'completed' and done['result_summary'].startswith('cycle 1 delivered')


def test_loop_holds_cycle_when_nothing_changed_and_still_succeeds(tmp_path):
    store, root = _workspace(tmp_path)
    state_dir, stop_file = _state(tmp_path)
    # consume everything first
    C.run_cycle(store, deliver=lambda t: True)
    task = enqueue_overseer_cycle(state_dir, workspace_root=root, delivery={'kind': 'agent', 'owner_agent_id': 'AG-OVERSEER'},
                                  interval_minutes=None)
    proc = _run_loop(state_dir, stop_file, max_cycles=2)
    assert proc.returncode == 0, proc.stderr[-2000:]
    names = [e['event'] for e in _read_jsonl(state_dir / 'run.log')]
    assert 'overseer_cycle_held' in names and 'overseer_cycle_delivered' not in names
    queue = json.loads((state_dir / 'queue.json').read_text())
    done = [t for t in queue if t.get('id') == task['id']][0]
    assert done['status'] == 'completed' and done['result_summary'] == 'held: no new events'
    assert not [t for t in queue if t.get('task_type') == 'agent_task']


def test_loop_fails_cycle_task_with_missing_workspace(tmp_path):
    state_dir, stop_file = _state(tmp_path)
    task = enqueue_overseer_cycle(state_dir, workspace_root=tmp_path / 'nope', delivery={'kind': 'agent', 'owner_agent_id': 'AG-OVERSEER'}, interval_minutes=None)
    proc = _run_loop(state_dir, stop_file, max_cycles=2)
    assert proc.returncode == 0, proc.stderr[-2000:]
    queue = json.loads((state_dir / 'queue.json').read_text())
    t = [x for x in queue if x.get('id') == task['id']][0]
    assert t['status'] != 'completed'
    assert any(e['event'] == 'task_failed' or e['event'].startswith('task_fail') for e in _read_jsonl(state_dir / 'run.log'))
