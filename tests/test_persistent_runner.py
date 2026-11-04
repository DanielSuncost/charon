"""The persistent loop: hosted as a managed process, resumable after a
restart, gated by goal confirmation and budgets, stoppable mid-unit."""
from __future__ import annotations

import json
import signal
import subprocess
import sys
import time

import pytest

from charon.agents import persistent_agent as pa
from charon.agents import persistent_runner as pr
from charon.agents.autonomous import (
    confirm_goal, get_goals_by_status, propose_goal, save_autonomous_config, set_goal_plan,
)
from charon.devop.managed_host import run_managed
from charon.infra import store_adapter

PROJECT = '/tmp/persistent-proj'


def setup_function():
    store_adapter.reset_all()


@pytest.fixture
def sigterm_restored():
    before = signal.getsignal(signal.SIGTERM)
    yield
    signal.signal(signal.SIGTERM, before)


def _goal(state_dir, goal_id):
    for status in ('proposed', 'confirmed', 'planning', 'executing', 'verifying', 'blocked', 'completed', 'failed'):
        for g in get_goals_by_status(state_dir, project=PROJECT, status=status):
            if g['goal_id'] == goal_id:
                return g
    return None


def _runner(state_dir, monkeypatch, replies):
    """A runner whose engine is a scripted list of (ok, summary) replies."""
    monkeypatch.setenv('CHARON_STATE_DIR', str(state_dir))
    save_autonomous_config(state_dir, {'persistent': True})
    runner = pr.PersistentRunner(state_dir, PROJECT)
    calls = []

    def fake_execute(task):
        calls.append(task)
        ok, summary = replies.pop(0)
        return ok, {'summary': summary} if ok else {'error': summary}

    monkeypatch.setattr(runner, 'execute', fake_execute)
    return runner, calls


# ── Managed host ───────────────────────────────────────────────────

def test_run_managed_returns_the_body_result(sigterm_restored):
    seen = []
    out = run_managed(lambda: 'done', stop_requested=lambda: False,
                      on_stop=lambda: seen.append('stop'), on_failure=lambda e: seen.append('fail'))
    assert out == 'done' and seen == []


def test_run_managed_stop_flag_interrupts_the_body_mid_work(sigterm_restored):
    flag = {'stop': False}
    seen = []

    def body():
        flag['stop'] = True          # ask for a stop from inside the work
        for _ in range(600):         # ...and keep working; the signal must land here
            time.sleep(0.05)
        return 'should not finish'

    with pytest.raises(SystemExit) as exc:
        run_managed(body, stop_requested=lambda: flag['stop'], on_stop=lambda: seen.append('stop'),
                    on_failure=lambda e: seen.append('fail'), poll_seconds=0.05)
    assert exc.value.code == 143
    assert seen == ['stop']


def test_run_managed_reports_failures_then_reraises(sigterm_restored):
    seen = []

    def body():
        raise RuntimeError('boom')

    with pytest.raises(RuntimeError):
        run_managed(body, stop_requested=lambda: False, on_stop=lambda: seen.append('stop'),
                    on_failure=lambda e: seen.append(str(e)))
    assert seen == ['boom']


# ── Goal lifecycle through the loop ────────────────────────────────

def test_loop_plans_executes_and_verifies_a_confirmed_goal(tmp_path, monkeypatch):
    runner, calls = _runner(tmp_path, monkeypatch, [
        (True, 'Plan:\n1. Write the parser\n2. Add tests'),
        (True, 'Wrote the parser.'),
        (True, 'Added tests, all green.'),
        (True, 'Checked both criteria.\nVERDICT: PASS'),
    ])
    goal = propose_goal(tmp_path, agent_id='x', project=PROJECT, title='Ship parser',
                        acceptance_criteria=['parser exists', 'tests pass'])
    assert runner.step() == 'idle', 'an unconfirmed goal must not be worked'
    confirm_goal(tmp_path, project=PROJECT, goal_id=goal['goal_id'])

    assert runner.step() == 'goal_planning'
    g = _goal(tmp_path, goal['goal_id'])
    assert g['status'] == 'executing'
    assert [s['description'] for s in g['plan']] == ['Write the parser', 'Add tests']
    assert 'numbered list' in calls[0]['instruction']

    assert runner.step() == 'goal_step'
    assert runner.step() == 'goal_step'
    g = _goal(tmp_path, goal['goal_id'])
    assert g['status'] == 'verifying'
    assert [s['status'] for s in g['plan']] == ['done', 'done']

    assert runner.step() == 'goal_verification'
    assert 'VERDICT' in calls[3]['instruction']
    assert _goal(tmp_path, goal['goal_id'])['status'] == 'completed'
    assert runner.step() == 'idle'

    events = [json.loads(line) for line in pa.audit_path(tmp_path).read_text().splitlines()]
    kinds = [e['event'] for e in events]
    assert kinds.count('task_started') == 4 and kinds.count('task_finished') == 4
    assert pa.load_runtime(tmp_path)['last_task']['outcome'] == 'completed'
    assert pa.load_runtime(tmp_path)['current_task'] is None


def test_failed_unit_blocks_the_goal_and_names_the_blocker(tmp_path, monkeypatch):
    runner, _ = _runner(tmp_path, monkeypatch, [(False, 'no provider configured; nothing can run')])
    goal = propose_goal(tmp_path, agent_id='x', project=PROJECT, title='Blocked goal')
    confirm_goal(tmp_path, project=PROJECT, goal_id=goal['goal_id'])
    assert runner.step() == 'goal_planning'
    g = _goal(tmp_path, goal['goal_id'])
    assert g['status'] == 'blocked'
    assert g['block_reason'].startswith('goal_planning failed: no provider configured')
    assert runner.step() == 'idle', 'a blocked goal is not retried on its own'


def test_verification_without_a_verdict_blocks_rather_than_guessing(tmp_path, monkeypatch):
    runner, _ = _runner(tmp_path, monkeypatch, [(True, 'Looked at things. Seems fine.')])
    goal = propose_goal(tmp_path, agent_id='x', project=PROJECT, title='Verify me', acceptance_criteria=['c1'])
    confirm_goal(tmp_path, project=PROJECT, goal_id=goal['goal_id'])
    set_goal_plan(tmp_path, project=PROJECT, goal_id=goal['goal_id'], plan=[{'step': 1, 'description': 'x', 'status': 'done'}])
    pr.start_verifying(tmp_path, project=PROJECT, goal_id=goal['goal_id'])
    assert runner.step() == 'goal_verification'
    g = _goal(tmp_path, goal['goal_id'])
    assert g['status'] == 'blocked' and g['block_reason'].startswith('verdict_missing')


def test_exhausted_token_budget_stops_self_assignment(tmp_path, monkeypatch):
    runner, calls = _runner(tmp_path, monkeypatch, [(True, 'should never run')])
    goal = propose_goal(tmp_path, agent_id='x', project=PROJECT, title='Costly', token_budget=10)
    confirm_goal(tmp_path, project=PROJECT, goal_id=goal['goal_id'])
    from charon.agents.autonomous import add_goal_tokens_used
    add_goal_tokens_used(tmp_path, project=PROJECT, goal_id=goal['goal_id'], tokens=10)
    assert runner.step() == 'idle'
    assert calls == []
    assert _goal(tmp_path, goal['goal_id'])['block_reason'] == 'token_budget_exhausted'


def test_stop_mid_unit_leaves_the_step_pending_for_resume(tmp_path, monkeypatch):
    runner, _ = _runner(tmp_path, monkeypatch, [])
    goal = propose_goal(tmp_path, agent_id='x', project=PROJECT, title='Interrupted')
    confirm_goal(tmp_path, project=PROJECT, goal_id=goal['goal_id'])
    set_goal_plan(tmp_path, project=PROJECT, goal_id=goal['goal_id'],
                  plan=[{'step': 1, 'description': 'long step', 'status': 'pending'}])

    def interrupted(task):
        runner.on_stop()             # what the SIGTERM handler does...
        raise SystemExit(143)        # ...before unwinding the unit

    monkeypatch.setattr(runner, 'execute', interrupted)
    with pytest.raises(SystemExit):
        runner.step()
    rt = pa.load_runtime(tmp_path)
    assert rt['status'] == 'stopped' and rt['current_task'] is None
    assert rt['exit_note'].startswith('stopped mid-task: Step 1: long step')
    assert _goal(tmp_path, goal['goal_id'])['plan'][0]['status'] == 'pending'

    # After a restart the same step is picked again.
    runner2, calls2 = _runner(tmp_path, monkeypatch, [(True, 'did the long step')])
    assert runner2.step() == 'goal_step'
    assert calls2[0]['goal_ref']['step'] == 1


def test_restart_hands_an_interrupted_planning_goal_back(tmp_path, monkeypatch):
    runner, _ = _runner(tmp_path, monkeypatch, [])
    goal = propose_goal(tmp_path, agent_id='x', project=PROJECT, title='Half planned')
    confirm_goal(tmp_path, project=PROJECT, goal_id=goal['goal_id'])
    pr.start_planning(tmp_path, project=PROJECT, goal_id=goal['goal_id'])
    assert runner.step() == 'idle', 'a goal stuck in planning is invisible to self-assignment'
    assert runner.recover_interrupted() == [goal['goal_id']]
    assert _goal(tmp_path, goal['goal_id'])['status'] == 'confirmed'


# ── Reconcile across restarts ──────────────────────────────────────

def _sleeper(marker=pa.RUNNER_MARKER):
    return subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)', marker], start_new_session=True)


def test_ensure_runner_leaves_an_off_agent_alone(tmp_path, monkeypatch):
    monkeypatch.setattr(pa, '_launch', lambda *a: pytest.fail('must not launch'))
    assert pa.ensure_runner(tmp_path, PROJECT)['action'] == 'idle'


def test_ensure_runner_adopts_a_live_runner_after_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(pa, '_launch', lambda *a: pytest.fail('must not launch'))
    pa.set_enabled(tmp_path, True)
    proc = _sleeper()
    try:
        pa.write_runtime(tmp_path, status='running', pid=proc.pid, cmd_marker=pa.RUNNER_MARKER)
        assert pa.ensure_runner(tmp_path, PROJECT) == {'action': 'adopted', 'pid': proc.pid}
    finally:
        proc.kill()
        proc.wait()


def test_ensure_runner_relaunches_a_dead_runner_and_records_the_loss(tmp_path, monkeypatch):
    launches = []
    monkeypatch.setattr(pa, '_launch', lambda sd, project: launches.append(project) or {'pid': 777, 'process_id': 'proc-1'})
    pa.set_enabled(tmp_path, True)
    pa.write_runtime(tmp_path, status='running', pid=999999999, cmd_marker=pa.RUNNER_MARKER)
    out = pa.ensure_runner(tmp_path, PROJECT)
    assert out['action'] == 'launched' and launches == [PROJECT]
    rt = pa.load_runtime(tmp_path)
    assert rt['status'] == 'starting' and rt['pid'] == 777
    assert len(rt['recent_crashes']) == 1, 'a lost runner counts toward the crash loop'
    events = [json.loads(line)['event'] for line in pa.audit_path(tmp_path).read_text().splitlines()]
    assert events[-2:] == ['runner_lost', 'runner_launched']


def test_ensure_runner_gives_up_after_a_crash_loop_and_turns_off(tmp_path, monkeypatch):
    monkeypatch.setattr(pa, '_launch', lambda *a: pytest.fail('must not launch'))
    pa.set_enabled(tmp_path, True)
    now = time.time()
    pa.write_runtime(tmp_path, status='stopped', exit_note='crashed: boom', crash_counted=False,
                     recent_crashes=[now - 30, now - 20])
    out = pa.ensure_runner(tmp_path, PROJECT, now=now)
    assert out['action'] == 'gave_up'
    assert pa.persistent_enabled(tmp_path) is False
    assert 'turned off' in pa.load_runtime(tmp_path)['exit_note']


# ── The real process ───────────────────────────────────────────────

def _wait(pred, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.2)
    return pred()


def test_real_runner_heartbeats_exits_when_turned_off_and_stops_on_signal(tmp_path, monkeypatch):
    state = tmp_path / 'state'
    state.mkdir()
    project = tmp_path / 'proj'
    project.mkdir()
    monkeypatch.setenv('CHARON_STATE_DIR', str(state))
    monkeypatch.setenv('CHARON_SKIP_APPROVAL', '1')   # must not reach the runner
    pa.set_enabled(state, True)

    out = pa.ensure_runner(state, str(project))
    assert out['action'] == 'launched'
    assert _wait(lambda: pa.load_runtime(state).get('doing') == pr.IDLE_DOING, 60), pa.load_runtime(state)
    rt = pa.load_runtime(state)
    assert rt['status'] == 'running' and pa.process_alive(rt), rt
    assert pa.status_summary(state)['state'] == 'running'
    runner_pid = rt['pid']
    env = subprocess.run(['ps', '-o', 'command=', '-p', str(runner_pid)], capture_output=True, text=True).stdout
    assert pa.RUNNER_MARKER in env
    assert pa.ensure_runner(state, str(project))['action'] == 'adopted'
    agents = json.loads((state / 'agents.json').read_text())
    assert any(a.get('role') == 'persistent' for a in agents)

    # Turned off from outside: the runner notices and exits by itself.
    pa.set_enabled(state, False)
    assert _wait(lambda: pa.load_runtime(state)['status'] == 'stopped', 20), pa.load_runtime(state)
    assert _wait(lambda: not pa.pid_alive(runner_pid) or pa._is_zombie(runner_pid), 10)
    assert pa.load_runtime(state)['exit_note'] == 'turned off'

    # Back on: relaunched, then stopped by signal through request_stop.
    pa.set_enabled(state, True)
    assert pa.ensure_runner(state, str(project))['action'] == 'launched'
    assert _wait(lambda: pa.load_runtime(state).get('doing') == pr.IDLE_DOING and
                 pa.load_runtime(state)['pid'] != runner_pid, 60), pa.load_runtime(state)
    pa.set_enabled(state, False)
    result = pa.request_stop(state, timeout=10)
    assert result['was_running'] and result['exited'], result
    assert pa.load_runtime(state)['status'] == 'stopped'
    events = [json.loads(line)['event'] for line in pa.audit_path(state).read_text().splitlines()]
    assert 'runner_started' in events and 'runner_stopped' in events and 'signal_sent' in events
