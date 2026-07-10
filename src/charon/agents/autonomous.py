"""Autonomous work mode — goal-driven self-assignment with user confirmation.

Toggleable via /autonomous on|off or config. When enabled:
- Agent infers goals from conversation at regular intervals
- Proposes goals to user for confirmation (with acceptance criteria)
- Self-assigns tasks from confirmed goals when queue is idle
- Respects time and token budgets
- Checkpoints via git at every meaningful step
- Only works on goals explicitly approved by the user

When disabled: standard reactive mode, agent only works on queued tasks.
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from charon.infra import config as env_config


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_id(prefix: str = 'goal') -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


# ── Configuration ───────────────────────────────────────────────────

DEFAULT_AUTONOMOUS_CONFIG = {
    'enabled': False,               # off by default, must be explicitly enabled
    'time_budget_minutes': 0,       # 0 = no time limit
    'token_budget': 0,              # 0 = no token limit
    'git_checkpoint': True,         # commit at every goal step completion
    'goal_inference_interval': 10,  # heartbeats between goal inference attempts
    'require_confirmation': True,   # goals must be user-confirmed before execution
    'auto_propose_from_backlog': True,  # propose backlog ideas when idle
}


def load_autonomous_config(state_dir: Path) -> dict:
    """Load autonomous mode config for an agent."""
    config = dict(DEFAULT_AUTONOMOUS_CONFIG)
    try:
        cfg_path = state_dir / 'autonomous_config.json'
        if cfg_path.exists():
            user_cfg = json.loads(cfg_path.read_text())
            if isinstance(user_cfg, dict):
                config.update(user_cfg)
    except Exception:
        pass
    # Env overrides
    env_override = env_config.autonomous_override()
    if env_override is not None:
        config['enabled'] = env_override
    return config


def save_autonomous_config(state_dir: Path, config: dict) -> None:
    cfg_path = state_dir / 'autonomous_config.json'
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps(config, indent=2))


# ── Goal states ─────────────────────────────────────────────────────

GOAL_STATES = {
    'backlog',      # idea captured, not prioritized
    'proposed',     # agent proposed, awaiting user confirmation
    'confirmed',    # user confirmed, ready for planning/execution
    'planning',     # agent is decomposing into sub-tasks
    'executing',    # agent or shades are working
    'verifying',    # checking acceptance criteria
    'completed',    # done, criteria met
    'failed',       # could not complete
    'blocked',      # waiting on something
    'active',       # legacy compat
}


def propose_goal(
    state_dir: Path,
    *,
    agent_id: str,
    project: str,
    title: str,
    plan: list[dict] | None = None,
    acceptance_criteria: list[str] | None = None,
    time_budget_minutes: int = 0,
    token_budget: int = 0,
) -> dict:
    """Create a goal in 'proposed' state, awaiting user confirmation."""
    from charon.agents.goal_runtime import (
        _safe_id, _project_path, _session_path, _read_json, _write_json,
        _default_project_doc, _default_session_doc, _now_iso,
    )

    project_id = _safe_id(project or 'default-project', 'project')
    session_id = _safe_id(f'autonomous-{agent_id}', 'session')

    ppath = _project_path(state_dir, project_id)
    spath = _session_path(state_dir, session_id)

    proj = _read_json(ppath, _default_project_doc(project_id))
    ses = _read_json(spath, _default_session_doc(session_id, project_id, agent_id))

    goal = {
        'goal_id': _new_id('goal'),
        'parent_goal_id': None,
        'title': str(title).strip()[:240],
        'intent_type': 'autonomous',
        'constraints': [],
        'acceptance_criteria': list(acceptance_criteria or []),
        'status': 'proposed',
        'priority': 'normal',
        'linked_tasks': [],
        'linked_messages': [],
        'evidence': [],
        'plan': list(plan or []),
        'time_budget_minutes': time_budget_minutes,
        'token_budget': token_budget,
        'tokens_used': 0,
        'started_at': None,
        'project_id': project_id,
        'session_id': session_id,
        'conversation_id': f'autonomous-{agent_id}',
        'proposed_by': agent_id,
        'confirmed_by': None,
        'created_at': _now_iso(),
        'updated_at': _now_iso(),
    }

    proj_goals = list(proj.get('goals') or [])
    ses_goals = list(ses.get('goals') or [])
    proj_goals.append(goal)
    ses_goals.append(goal)
    proj['goals'] = proj_goals[-500:]
    ses['goals'] = ses_goals[-500:]
    proj['updated_at'] = _now_iso()
    ses['updated_at'] = _now_iso()

    _write_json(ppath, proj)
    _write_json(spath, ses)

    # Sync to SQLite
    try:
        from charon.agents.goal_runtime import _use_store, _get_db, _db_project_upsert, _db_session_upsert
        if _use_store():
            db = _get_db(state_dir)
            _db_project_upsert(db, project_id, proj)
            _db_session_upsert(db, session_id, project_id, ses)
    except Exception:
        pass

    return goal


def confirm_goal(state_dir: Path, *, project: str, goal_id: str, confirmed_by: str = 'user') -> dict | None:
    """Move a proposed goal to confirmed state."""
    return _update_goal_status(state_dir, project=project, goal_id=goal_id,
                               new_status='confirmed', extra={'confirmed_by': confirmed_by})


def reject_goal(state_dir: Path, *, project: str, goal_id: str) -> dict | None:
    """Move a proposed goal back to backlog."""
    return _update_goal_status(state_dir, project=project, goal_id=goal_id,
                               new_status='backlog')


def start_planning(state_dir: Path, *, project: str, goal_id: str) -> dict | None:
    return _update_goal_status(state_dir, project=project, goal_id=goal_id,
                               new_status='planning')


def start_executing(state_dir: Path, *, project: str, goal_id: str) -> dict | None:
    return _update_goal_status(state_dir, project=project, goal_id=goal_id,
                               new_status='executing',
                               extra={'started_at': _now()})


def start_verifying(state_dir: Path, *, project: str, goal_id: str) -> dict | None:
    return _update_goal_status(state_dir, project=project, goal_id=goal_id,
                               new_status='verifying')


def complete_goal(state_dir: Path, *, project: str, goal_id: str, evidence: str = '') -> dict | None:
    return _update_goal_status(state_dir, project=project, goal_id=goal_id,
                               new_status='completed',
                               extra={'evidence_note': evidence})


def fail_goal(state_dir: Path, *, project: str, goal_id: str, reason: str = '') -> dict | None:
    return _update_goal_status(state_dir, project=project, goal_id=goal_id,
                               new_status='failed',
                               extra={'failure_reason': reason})


def set_goal_plan(state_dir: Path, *, project: str, goal_id: str, plan: list[dict]) -> dict | None:
    """Attach a plan (list of steps) to a goal."""
    return _update_goal_field(state_dir, project=project, goal_id=goal_id,
                              field='plan', value=plan)


def set_acceptance_criteria(state_dir: Path, *, project: str, goal_id: str, criteria: list[str]) -> dict | None:
    """Set acceptance criteria on a goal."""
    return _update_goal_field(state_dir, project=project, goal_id=goal_id,
                              field='acceptance_criteria', value=criteria)


# ── Goal queries ────────────────────────────────────────────────────

def get_goals_by_status(state_dir: Path, *, project: str, status: str) -> list[dict]:
    """Get all goals with a given status."""
    from charon.agents.goal_runtime import _safe_id, _project_path, _read_json, _default_project_doc
    project_id = _safe_id(project or 'default-project', 'project')
    proj = _read_json(_project_path(state_dir, project_id), _default_project_doc(project_id))
    return [g for g in (proj.get('goals') or [])
            if isinstance(g, dict) and g.get('status') == status]


def get_next_confirmed_goal(state_dir: Path, *, project: str) -> dict | None:
    """Get the highest-priority confirmed goal that needs work."""
    goals = get_goals_by_status(state_dir, project=project, status='confirmed')
    if goals:
        return goals[0]
    # Also check executing goals that might have pending plan steps
    executing = get_goals_by_status(state_dir, project=project, status='executing')
    for g in executing:
        plan = g.get('plan') or []
        pending_steps = [s for s in plan if isinstance(s, dict) and s.get('status') == 'pending']
        if pending_steps:
            return g
    return None


def get_goals_needing_verification(state_dir: Path, *, project: str) -> list[dict]:
    """Get goals in verifying state."""
    return get_goals_by_status(state_dir, project=project, status='verifying')


def get_proposed_goals(state_dir: Path, *, project: str) -> list[dict]:
    """Get goals awaiting user confirmation."""
    return get_goals_by_status(state_dir, project=project, status='proposed')


# ── Self-assignment ─────────────────────────────────────────────────

def self_assign_next_task(
    state_dir: Path,
    *,
    agent_id: str,
    project: str,
    config: dict,
) -> dict | None:
    """Find the next task to work on from confirmed goals.

    Returns a task dict ready for the queue, or None if nothing to do.
    Called by the daemon loop when the queue is idle and autonomous mode is on.
    """
    if not config.get('enabled', False):
        return None

    # Check time budget
    if config.get('time_budget_minutes') and config.get('_autonomous_start_time'):
        elapsed = (time.time() - config['_autonomous_start_time']) / 60
        if elapsed >= config['time_budget_minutes']:
            return None  # budget exhausted

    now = _now()

    # Priority 1: Goals needing verification
    verifying = get_goals_needing_verification(state_dir, project=project)
    for g in verifying:
        criteria = g.get('acceptance_criteria') or []
        if criteria:
            return {
                'id': f'task-verify-{uuid.uuid4().hex[:8]}',
                'title': f'Verify: {g.get("title", "")[:80]}',
                'instruction': (
                    f'Verify these acceptance criteria for goal "{g.get("title", "")}":\n'
                    + '\n'.join(f'- {c}' for c in criteria)
                    + '\n\nRun tests, check files, or use other tools to verify each criterion. '
                    'Report which criteria pass and which fail.'
                ),
                'status': 'pending',
                'task_type': 'goal_verification',
                'owner_agent_id': agent_id,
                'actor_agent_id': agent_id,
                'project': project,
                'goal_ref': {
                    'goal_id': g.get('goal_id'),
                    'project_id': g.get('project_id'),
                },
                'created_at': now,
                'updated_at': now,
                'attempt_count': 0,
                'max_attempts': 2,
            }

    # Priority 2: Executing goals with pending plan steps
    next_goal = get_next_confirmed_goal(state_dir, project=project)
    if next_goal:
        plan = next_goal.get('plan') or []
        pending_steps = [s for s in plan if isinstance(s, dict) and s.get('status') == 'pending']

        if not plan and next_goal.get('status') == 'confirmed':
            # Needs planning first
            return {
                'id': f'task-plan-{uuid.uuid4().hex[:8]}',
                'title': f'Plan: {next_goal.get("title", "")[:80]}',
                'instruction': (
                    f'Create a step-by-step execution plan for this goal:\n'
                    f'"{next_goal.get("title", "")}"\n\n'
                    f'Acceptance criteria:\n'
                    + '\n'.join(f'- {c}' for c in (next_goal.get('acceptance_criteria') or ['(none specified)']))
                    + '\n\nOutput a numbered list of concrete steps. '
                    'Each step should be a single actionable task.'
                ),
                'status': 'pending',
                'task_type': 'goal_planning',
                'owner_agent_id': agent_id,
                'actor_agent_id': agent_id,
                'project': project,
                'goal_ref': {
                    'goal_id': next_goal.get('goal_id'),
                    'project_id': next_goal.get('project_id'),
                },
                'created_at': now,
                'updated_at': now,
                'attempt_count': 0,
                'max_attempts': 2,
            }

        if pending_steps:
            step = pending_steps[0]
            return {
                'id': f'task-step-{uuid.uuid4().hex[:8]}',
                'title': f'Step {step.get("step", "?")}: {step.get("description", "")[:80]}',
                'instruction': (
                    f'Goal: {next_goal.get("title", "")}\n'
                    f'Step {step.get("step", "?")}: {step.get("description", "")}\n\n'
                    f'Execute this step. When done, report what you did concisely.'
                ),
                'status': 'pending',
                'task_type': 'goal_step',
                'owner_agent_id': agent_id,
                'actor_agent_id': agent_id,
                'project': project,
                'goal_ref': {
                    'goal_id': next_goal.get('goal_id'),
                    'project_id': next_goal.get('project_id'),
                    'step': step.get('step'),
                },
                'created_at': now,
                'updated_at': now,
                'attempt_count': 0,
                'max_attempts': 3,
            }

    return None  # Nothing to do


# ── Goal inference from conversation ────────────────────────────────

GOAL_INFERENCE_PROMPT = """Analyze this recent conversation and identify any goals the user has expressed or implied.

For each goal, extract:
1. A clear, concise title (what the user wants done)
2. Acceptance criteria (how to know it's done - be specific and testable)
3. A rough plan (3-7 steps)

If the user hasn't expressed any clear goals, return an empty list.
Only extract goals you're confident about — don't guess.

Output JSON:
{
  "goals": [
    {
      "title": "...",
      "acceptance_criteria": ["criterion 1", "criterion 2"],
      "plan": [
        {"step": 1, "description": "..."},
        {"step": 2, "description": "..."}
      ]
    }
  ],
  "reasoning": "brief explanation"
}

Recent conversation:
"""


async def infer_goals_from_conversation(
    state_dir: Path,
    *,
    agent_id: str,
    messages: list,
    provider: Any,
    model: Any,
) -> list[dict]:
    """Analyze recent conversation to extract implied goals.

    Returns a list of goal dicts ready for propose_goal().
    Called periodically when autonomous mode is enabled.
    """
    if not messages:
        return []

    # Build conversation text from recent messages (last 20)
    recent = messages[-20:]
    conv_lines = []
    for m in recent:
        role = getattr(m, 'role', m.get('role', '')) if isinstance(m, dict) else getattr(m, 'role', '')
        content = getattr(m, 'content', m.get('content', '')) if isinstance(m, dict) else getattr(m, 'content', '')
        if role in ('user', 'assistant') and content:
            conv_lines.append(f'[{role}]: {str(content)[:300]}')

    if not conv_lines:
        return []

    prompt = GOAL_INFERENCE_PROMPT + '\n'.join(conv_lines)

    text_parts = []
    try:
        async for delta in provider.stream(
            messages=[{'role': 'user', 'content': prompt}],
            model=model,
            system_prompt='You are a goal extraction assistant. Output only valid JSON.',
            max_tokens=2048,
        ):
            if hasattr(delta, 'type') and delta.type == 'text':
                text_parts.append(delta.text)
    except Exception:
        return []

    response = ''.join(text_parts).strip()

    # Parse JSON
    try:
        import re
        if '```' in response:
            match = re.search(r'```(?:json)?\s*\n?(.*?)\n?```', response, re.DOTALL)
            if match:
                response = match.group(1).strip()
        data = json.loads(response)
        if isinstance(data, dict) and 'goals' in data:
            return data['goals']
    except Exception:
        pass

    return []


# ── Helpers ─────────────────────────────────────────────────────────

def _update_goal_status(state_dir: Path, *, project: str, goal_id: str,
                        new_status: str, extra: dict | None = None) -> dict | None:
    from charon.agents.goal_runtime import (
        _safe_id, _project_path, _read_json, _write_json, _default_project_doc, _now_iso,
    )
    project_id = _safe_id(project or 'default-project', 'project')
    ppath = _project_path(state_dir, project_id)
    proj = _read_json(ppath, _default_project_doc(project_id))

    found = None
    for g in (proj.get('goals') or []):
        if isinstance(g, dict) and g.get('goal_id') == goal_id:
            g['status'] = new_status
            g['updated_at'] = _now_iso()
            if extra:
                g.update(extra)
            found = g
            break

    if found:
        proj['updated_at'] = _now_iso()
        _write_json(ppath, proj)
        try:
            from charon.agents.goal_runtime import _use_store, _get_db, _db_project_upsert
            if _use_store():
                _db_project_upsert(_get_db(state_dir), project_id, proj)
        except Exception:
            pass

    return found


def _update_goal_field(state_dir: Path, *, project: str, goal_id: str,
                       field: str, value: Any) -> dict | None:
    from charon.agents.goal_runtime import (
        _safe_id, _project_path, _read_json, _write_json, _default_project_doc, _now_iso,
    )
    project_id = _safe_id(project or 'default-project', 'project')
    ppath = _project_path(state_dir, project_id)
    proj = _read_json(ppath, _default_project_doc(project_id))

    found = None
    for g in (proj.get('goals') or []):
        if isinstance(g, dict) and g.get('goal_id') == goal_id:
            g[field] = value
            g['updated_at'] = _now_iso()
            found = g
            break

    if found:
        proj['updated_at'] = _now_iso()
        _write_json(ppath, proj)
        try:
            from charon.agents.goal_runtime import _use_store, _get_db, _db_project_upsert
            if _use_store():
                _db_project_upsert(_get_db(state_dir), project_id, proj)
        except Exception:
            pass

    return found
