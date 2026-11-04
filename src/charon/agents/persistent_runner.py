"""The persistent agent's loop, hosted as a managed process.

Launched by ``persistent_agent.ensure_runner`` through the run-process tool
in its own session, so it outlives the chat that started it and a Charon
restart finds it still running (or finds its runtime record and relaunches).

What one iteration does: ask ``self_assign_next_task`` for the next unit of
work from a confirmed goal (the confirm gate and the per-goal token budgets
live there), run it through the same engine path the daemon uses for agent
tasks, apply the outcome to the goal, and record the unit in the runtime
file and audit log before and after. When there is nothing to do it sleeps
until a goal, config or runtime file changes rather than polling a model.

What it never does: start on its own (the toggle must be literally true),
act on an unconfirmed goal, invent work when a goal is blocked (it names
the blocker and idles), or cap a unit of work by wall clock. A stop
(SIGTERM from a chat command or the stop flag) interrupts the unit where
it is; the step is left pending so a restart picks it up again.

The approval gate is not switchable for this process: the env override that
disables tool approval is removed before any engine exists.
"""
from __future__ import annotations

import argparse
import os
import re
import time
from pathlib import Path

from charon.agents import persistent_agent as pa
from charon.agents.autonomous import (
    _update_goal_field, _update_goal_status, complete_goal, fail_goal,
    get_goals_by_status, load_autonomous_config, self_assign_next_task, set_goal_plan,
    start_executing, start_planning, start_verifying,
)
from charon.devop.managed_host import run_managed

try:
    from charon.infra.diagnostics import record as _diag
except Exception:  # diagnostics is best-effort and must never block import
    def _diag(*args, **kwargs):
        return None

AGENT_ROLE = 'persistent'
IDLE_DOING = 'idle · waiting for a confirmed goal'
WAIT_POLL_SECONDS = 1.0
# While idle, re-check for work at least this often even without a file
# change, in case a change landed in a place we do not watch.
IDLE_RECHECK_SECONDS = 30.0

PLANNING_FORMAT = (
    '\n\nOutput format: a numbered list, one step per line, like "1. Do X". '
    'No other numbered lists.'
)
VERDICT_FORMAT = (
    '\n\nEnd your report with exactly one line: VERDICT: PASS if every criterion '
    'is met with evidence, otherwise VERDICT: FAIL.'
)


def _goal(state_dir: Path, project: str, goal_id: str) -> dict | None:
    for status in ('confirmed', 'planning', 'executing', 'verifying', 'blocked', 'completed', 'failed'):
        for g in get_goals_by_status(state_dir, project=project, status=status):
            if g.get('goal_id') == goal_id:
                return g
    return None


def parse_plan(text: str) -> list[dict]:
    steps = []
    for line in str(text or '').splitlines():
        m = re.match(r'^\s*(\d+)[.)]\s+(.+?)\s*$', line)
        if m:
            steps.append({'step': len(steps) + 1, 'description': m.group(2)[:400], 'status': 'pending'})
    return steps


def parse_verdict(text: str) -> str | None:
    found = re.findall(r'VERDICT:\s*(PASS|FAIL)', str(text or ''), re.IGNORECASE)
    return found[-1].upper() if found else None


class PersistentRunner:
    def __init__(self, state_dir: Path, project: str):
        self.state_dir = Path(state_dir)
        self.project = project
        self.agent: dict | None = None
        self.current_task: dict | None = None
        self.exit_note = ''

    # ── Hosting hooks ───────────────────────────────────────────────

    def should_stop(self) -> bool:
        return pa.stop_requested(self.state_dir) or not pa.persistent_enabled(self.state_dir)

    def on_stop(self) -> None:
        task = self.current_task or {}
        if task:
            note = f'stopped mid-task: {task.get("title", "")[:80]} (left pending for resume)'
        elif self.exit_note:
            note = self.exit_note
        elif not pa.persistent_enabled(self.state_dir):
            note = 'turned off'
        else:
            note = 'stop requested'
        pa.audit(self.state_dir, 'runner_stopped', pid=os.getpid(), note=note, task_id=task.get('id'))
        pa.write_runtime(self.state_dir, status='stopped', pid=None, doing='', stopped_at=pa._now_iso(),
                         exit_note=note, current_task=None)

    def on_failure(self, exc: BaseException) -> None:
        note = f'crashed: {type(exc).__name__}: {str(exc)[:200]}'
        pa.audit(self.state_dir, 'runner_failed', pid=os.getpid(), note=note)
        pa.write_runtime(self.state_dir, status='stopped', pid=None, doing='', stopped_at=pa._now_iso(),
                         exit_note=note, current_task=None, crash_counted=False)

    # ── Agent record ────────────────────────────────────────────────

    def ensure_agent(self) -> dict:
        from charon.agents.agent_lifecycle import create_agent, load_agents
        rt = pa.load_runtime(self.state_dir)
        wanted = str(rt.get('agent_id') or '')
        if wanted:
            for a in load_agents(self.state_dir):
                if isinstance(a, dict) and a.get('id') == wanted:
                    self.agent = a
                    return a
        self.agent = create_agent(
            name='persistent', mode='persistent', goal='Work on confirmed goals on the user\'s behalf',
            project=self.project, role=AGENT_ROLE, visibility='background', require_tmux=False,
        )
        pa.write_runtime(self.state_dir, agent_id=self.agent['id'])
        return self.agent

    # ── Loop ────────────────────────────────────────────────────────

    def run(self) -> None:
        previous = pa.load_runtime(self.state_dir)
        resumed_from = previous.get('current_task') or previous.get('last_task')
        pa.write_runtime(self.state_dir, status='running', pid=os.getpid(), cmd_marker=pa.RUNNER_MARKER,
                         started_at=pa._now_iso(), heartbeat_at=pa._now_iso(), stop_requested=False,
                         project=self.project, doing='starting', current_task=None, exit_note='')
        self.ensure_agent()
        self.record_computer_access()
        pa.audit(self.state_dir, 'runner_started', pid=os.getpid(), project=self.project,
                 agent_id=self.agent['id'], resumed_after=(resumed_from or {}).get('title'))
        self.recover_interrupted()
        while True:
            if not pa.persistent_enabled(self.state_dir):
                self.exit_note = 'turned off'
                self.on_stop()
                return
            did = self.step()
            if did == 'idle':
                self.wait_for_change()

    def record_computer_access(self) -> dict:
        """Probe the macOS grants from this process, the one that will make
        the desktop calls, and record what it found. Grants depend on the
        launch chain, so the chat backend's own view is not a substitute."""
        from charon.computer.permissions import probe
        try:
            access = probe(where='the persistent runner')
        except Exception as exc:
            access = {'supported': False, 'ready': False, 'missing': [], 'where': 'the persistent runner',
                      'message': [f'Desktop access check failed: {type(exc).__name__}: {exc}']}
        pa.write_runtime(self.state_dir, computer_access=access)
        pa.audit(self.state_dir, 'computer_access_checked', ready=access.get('ready'),
                 missing=access.get('missing'), grant_target=(access.get('responsible') or {}).get('grant_target'))
        return access

    def recover_interrupted(self) -> list[str]:
        """A goal left in 'planning' by a stop has no plan and would never
        be picked again; hand it back to 'confirmed' so planning reruns.
        Executing and verifying goals resume through self-assignment as is."""
        recovered = []
        for g in get_goals_by_status(self.state_dir, project=self.project, status='planning'):
            if not g.get('plan'):
                _update_goal_status(self.state_dir, project=self.project, goal_id=g.get('goal_id'),
                                    new_status='confirmed', extra={'resumed_from': 'planning'})
                recovered.append(str(g.get('goal_id')))
        if recovered:
            pa.audit(self.state_dir, 'goals_resumed', goal_ids=recovered, note='planning restarted after interruption')
        return recovered

    def step(self) -> str:
        """One unit: run the next self-assigned task, or report idle."""
        if self.agent is None:
            self.ensure_agent()
        config = load_autonomous_config(self.state_dir)
        # The persistent toggle is the on switch for self-assignment here; the
        # goal must still be user-confirmed and inside its budget to be picked.
        task = self_assign_next_task(self.state_dir, agent_id=self.agent['id'], project=self.project,
                                     config={**config, 'enabled': True})
        if not task:
            pa.heartbeat(self.state_dir, IDLE_DOING)
            return 'idle'
        self.run_task(task)
        return task.get('task_type') or 'task'

    def wait_for_change(self) -> None:
        """Sleep until a goal, config or runtime file changes (or the recheck
        interval passes), checking the stop condition each second."""
        from charon.agents.goal_runtime import _project_path, _safe_id
        watched = [
            self.state_dir / 'autonomous_config.json',
            pa.runtime_path(self.state_dir),
            _project_path(self.state_dir, _safe_id(self.project or 'default-project', 'project')),
        ]

        def snapshot() -> tuple:
            out = []
            for p in watched:
                try:
                    out.append(p.stat().st_mtime_ns)
                except OSError:
                    out.append(None)
            return tuple(out)

        before = snapshot()
        deadline = time.time() + IDLE_RECHECK_SECONDS
        while time.time() < deadline:
            time.sleep(WAIT_POLL_SECONDS)
            if self.should_stop() or snapshot() != before:
                return

    # ── One task ────────────────────────────────────────────────────

    def run_task(self, task: dict) -> tuple[bool, dict]:
        ref = task.get('goal_ref') or {}
        goal_id = str(ref.get('goal_id') or '')
        kind = task.get('task_type') or ''
        record = {'id': task.get('id'), 'title': task.get('title'), 'type': kind, 'goal_id': goal_id,
                  'started_at': pa._now_iso()}
        self.current_task = record
        pa.write_runtime(self.state_dir, current_task=record)
        pa.heartbeat(self.state_dir, task.get('title') or kind)
        pa.audit(self.state_dir, 'task_started', **record)

        if kind == 'goal_planning':
            start_planning(self.state_dir, project=self.project, goal_id=goal_id)
            task['instruction'] = (task.get('instruction') or '') + PLANNING_FORMAT
        elif kind == 'goal_step':
            goal = _goal(self.state_dir, self.project, goal_id) or {}
            if goal.get('status') != 'executing':
                start_executing(self.state_dir, project=self.project, goal_id=goal_id)
        elif kind == 'goal_verification':
            task['instruction'] = (task.get('instruction') or '') + VERDICT_FORMAT

        # A stop signal raises SystemExit out of here: the task is not done,
        # the step stays pending, and on_stop records why.
        ok, result = self.execute(task)
        summary = str(result.get('summary') or result.get('error') or '').strip()
        outcome = self.apply_outcome(task, ok, summary)
        finished = {**record, 'finished_at': pa._now_iso(), 'ok': ok, 'outcome': outcome,
                    'summary': summary[:300]}
        self.current_task = None
        pa.write_runtime(self.state_dir, current_task=None, last_task=finished)
        pa.audit(self.state_dir, 'task_finished', **finished)
        return ok, result

    def execute(self, task: dict) -> tuple[bool, dict]:
        """Run the task through the daemon's engine path. Token usage is
        rolled into the goal there (see agent_runtime), which is what the
        budget check in self_assign_next_task enforces."""
        from charon.agents.agent_runtime import _get_or_create_engine, _run_task_with_engine
        engine, ready = _get_or_create_engine(self.state_dir, self.agent, task)
        if engine is None or not ready:
            return False, {'error': 'no provider configured; nothing can run until sign-in completes'}
        # Nobody is here to answer a clarifying question. Withhold Clarify by
        # tool list, as devop does, rather than via the interactive flag,
        # which would impose a wall-clock cap.
        engine._available_tools = [t for t in getattr(engine, '_available_tools', []) if t.get('name') != 'Clarify']
        engine.tools = [t for t in getattr(engine, 'tools', []) if t.get('name') != 'Clarify']
        return _run_task_with_engine(self.state_dir, task, self.agent, engine)

    def apply_outcome(self, task: dict, ok: bool, summary: str) -> str:
        ref = task.get('goal_ref') or {}
        goal_id = str(ref.get('goal_id') or '')
        kind = task.get('task_type') or ''
        if not goal_id:
            return 'no_goal'
        if not ok:
            # Name the blocker and stop working this goal. The user can
            # /confirm it again to retry.
            _update_goal_status(self.state_dir, project=self.project, goal_id=goal_id, new_status='blocked',
                                extra={'block_reason': f'{kind} failed: {summary[:300]}'})
            return 'blocked'
        if kind == 'goal_planning':
            steps = parse_plan(summary)
            if not steps:
                _update_goal_status(self.state_dir, project=self.project, goal_id=goal_id, new_status='blocked',
                                    extra={'block_reason': 'plan_unparseable: no numbered steps in the planning output'})
                return 'blocked'
            set_goal_plan(self.state_dir, project=self.project, goal_id=goal_id, plan=steps)
            start_executing(self.state_dir, project=self.project, goal_id=goal_id)
            return f'planned {len(steps)} steps'
        if kind == 'goal_step':
            goal = _goal(self.state_dir, self.project, goal_id) or {}
            plan = [dict(s) for s in (goal.get('plan') or []) if isinstance(s, dict)]
            step_no = ref.get('step')
            for s in plan:
                if s.get('step') == step_no and s.get('status') == 'pending':
                    s['status'] = 'done'
                    s['summary'] = summary[:300]
                    break
            _update_goal_field(self.state_dir, project=self.project, goal_id=goal_id, field='plan', value=plan)
            if not any(s.get('status') == 'pending' for s in plan):
                start_verifying(self.state_dir, project=self.project, goal_id=goal_id)
                return f'step {step_no} done; all steps done, verifying'
            return f'step {step_no} done'
        if kind == 'goal_verification':
            verdict = parse_verdict(summary)
            if verdict == 'PASS':
                complete_goal(self.state_dir, project=self.project, goal_id=goal_id, evidence=summary[:1000])
                return 'completed'
            if verdict == 'FAIL':
                fail_goal(self.state_dir, project=self.project, goal_id=goal_id, reason=summary[:500])
                return 'failed verification'
            _update_goal_status(self.state_dir, project=self.project, goal_id=goal_id, new_status='blocked',
                                extra={'block_reason': 'verdict_missing: verification report had no VERDICT line'})
            return 'blocked'
        return 'done'


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--state-dir', type=Path, required=True)
    parser.add_argument('--project', required=True)
    args = parser.parse_args(argv)
    state_dir = args.state_dir.resolve()
    os.environ['CHARON_STATE_DIR'] = str(state_dir)
    # The approval gate cannot be switched off for this process, whatever
    # the launching environment carried.
    os.environ.pop('CHARON_SKIP_APPROVAL', None)
    if not pa.persistent_enabled(state_dir):
        pa.audit(state_dir, 'runner_refused', pid=os.getpid(), note='persistent agent is off')
        raise SystemExit('Persistent agent is off; not starting.')
    runner = PersistentRunner(state_dir, args.project)
    run_managed(runner.run, stop_requested=runner.should_stop,
                on_stop=runner.on_stop, on_failure=runner.on_failure)


if __name__ == '__main__':
    main()
