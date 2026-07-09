"""Tests for autonomous goal-driven work."""
from pathlib import Path

import store_adapter
from autonomous import (
    load_autonomous_config, save_autonomous_config,
    propose_goal, confirm_goal, reject_goal,
    start_executing, complete_goal, fail_goal,
    set_goal_plan, set_acceptance_criteria,
    get_goals_by_status, get_proposed_goals, self_assign_next_task,
)


def setup_function():
    store_adapter.reset_all()


# ── Config ──────────────────────────────────────────────────────────

def test_config_defaults():
    config = load_autonomous_config(Path('/nonexistent'))
    assert config['enabled'] is False
    assert config['require_confirmation'] is True
    assert config['git_checkpoint'] is True


def test_config_save_load(tmp_path):
    state_dir = tmp_path / 'state'
    save_autonomous_config(state_dir, {'enabled': True, 'time_budget_minutes': 120})
    config = load_autonomous_config(state_dir)
    assert config['enabled'] is True
    assert config['time_budget_minutes'] == 120


# ── Goal lifecycle ──────────────────────────────────────────────────

def test_propose_goal(tmp_path):
    state_dir = tmp_path / 'state'
    goal = propose_goal(
        state_dir,
        agent_id='AG-001',
        project='/tmp/proj',
        title='Add rate limiting',
        acceptance_criteria=['Tests pass', '429 after 100 req/min'],
        plan=[
            {'step': 1, 'description': 'Research middleware', 'status': 'pending'},
            {'step': 2, 'description': 'Implement rate limiter', 'status': 'pending'},
        ],
    )
    assert goal['status'] == 'proposed'
    assert goal['title'] == 'Add rate limiting'
    assert len(goal['acceptance_criteria']) == 2
    assert len(goal['plan']) == 2
    assert goal['proposed_by'] == 'AG-001'


def test_confirm_goal(tmp_path):
    state_dir = tmp_path / 'state'
    goal = propose_goal(state_dir, agent_id='AG-001', project='/tmp/p', title='Test goal')
    result = confirm_goal(state_dir, project='/tmp/p', goal_id=goal['goal_id'])
    assert result is not None
    assert result['status'] == 'confirmed'
    assert result['confirmed_by'] == 'user'


def test_reject_goal_to_backlog(tmp_path):
    state_dir = tmp_path / 'state'
    goal = propose_goal(state_dir, agent_id='AG-001', project='/tmp/p', title='Maybe later')
    result = reject_goal(state_dir, project='/tmp/p', goal_id=goal['goal_id'])
    assert result is not None
    assert result['status'] == 'backlog'


def test_full_lifecycle(tmp_path):
    state_dir = tmp_path / 'state'
    project = '/tmp/proj'

    # Propose
    goal = propose_goal(state_dir, agent_id='AG-001', project=project,
                        title='Build feature', acceptance_criteria=['Tests pass'])
    assert goal['status'] == 'proposed'

    # Confirm
    confirm_goal(state_dir, project=project, goal_id=goal['goal_id'])
    goals = get_goals_by_status(state_dir, project=project, status='confirmed')
    assert len(goals) == 1

    # Start executing
    start_executing(state_dir, project=project, goal_id=goal['goal_id'])
    goals = get_goals_by_status(state_dir, project=project, status='executing')
    assert len(goals) == 1

    # Complete
    complete_goal(state_dir, project=project, goal_id=goal['goal_id'], evidence='All tests pass')
    goals = get_goals_by_status(state_dir, project=project, status='completed')
    assert len(goals) == 1


def test_fail_goal(tmp_path):
    state_dir = tmp_path / 'state'
    goal = propose_goal(state_dir, agent_id='AG-001', project='/tmp/p', title='Will fail')
    confirm_goal(state_dir, project='/tmp/p', goal_id=goal['goal_id'])
    result = fail_goal(state_dir, project='/tmp/p', goal_id=goal['goal_id'], reason='Out of budget')
    assert result['status'] == 'failed'


# ── Plan and criteria ───────────────────────────────────────────────

def test_set_plan_and_criteria(tmp_path):
    state_dir = tmp_path / 'state'
    goal = propose_goal(state_dir, agent_id='AG-001', project='/tmp/p', title='Planned goal')
    confirm_goal(state_dir, project='/tmp/p', goal_id=goal['goal_id'])

    set_goal_plan(state_dir, project='/tmp/p', goal_id=goal['goal_id'], plan=[
        {'step': 1, 'description': 'Step one', 'status': 'pending'},
        {'step': 2, 'description': 'Step two', 'status': 'pending'},
    ])
    set_acceptance_criteria(state_dir, project='/tmp/p', goal_id=goal['goal_id'],
                            criteria=['Tests pass', 'No regressions'])

    goals = get_goals_by_status(state_dir, project='/tmp/p', status='confirmed')
    assert len(goals[0]['plan']) == 2
    assert len(goals[0]['acceptance_criteria']) == 2


# ── Self-assignment ─────────────────────────────────────────────────

def test_self_assign_disabled(tmp_path):
    config = {'enabled': False}
    task = self_assign_next_task(tmp_path / 'state', agent_id='AG-001',
                                 project='/tmp/p', config=config)
    assert task is None


def test_self_assign_from_confirmed_goal(tmp_path):
    state_dir = tmp_path / 'state'
    goal = propose_goal(state_dir, agent_id='AG-001', project='/tmp/p',
                        title='Build thing', acceptance_criteria=['It works'])
    confirm_goal(state_dir, project='/tmp/p', goal_id=goal['goal_id'])

    config = {'enabled': True}
    task = self_assign_next_task(state_dir, agent_id='AG-001',
                                 project='/tmp/p', config=config)
    assert task is not None
    assert task['task_type'] == 'goal_planning'
    assert 'Build thing' in task['instruction']


def test_self_assign_from_executing_goal_with_plan(tmp_path):
    state_dir = tmp_path / 'state'
    goal = propose_goal(state_dir, agent_id='AG-001', project='/tmp/p', title='Multi-step')
    confirm_goal(state_dir, project='/tmp/p', goal_id=goal['goal_id'])
    set_goal_plan(state_dir, project='/tmp/p', goal_id=goal['goal_id'], plan=[
        {'step': 1, 'description': 'Do first thing', 'status': 'pending'},
        {'step': 2, 'description': 'Do second thing', 'status': 'pending'},
    ])
    start_executing(state_dir, project='/tmp/p', goal_id=goal['goal_id'])

    config = {'enabled': True}
    task = self_assign_next_task(state_dir, agent_id='AG-001',
                                 project='/tmp/p', config=config)
    assert task is not None
    assert task['task_type'] == 'goal_step'
    assert 'first thing' in task['instruction']


def test_self_assign_nothing_when_no_goals(tmp_path):
    config = {'enabled': True}
    task = self_assign_next_task(tmp_path / 'state', agent_id='AG-001',
                                 project='/tmp/p', config=config)
    assert task is None


def test_self_assign_respects_time_budget(tmp_path):
    import time
    config = {
        'enabled': True,
        'time_budget_minutes': 1,
        '_autonomous_start_time': time.time() - 120,  # 2 minutes ago, budget is 1 min
    }
    state_dir = tmp_path / 'state'
    propose_goal(state_dir, agent_id='AG-001', project='/tmp/p', title='Over budget')
    confirm_goal(state_dir, project='/tmp/p',
                 goal_id=get_goals_by_status(state_dir, project='/tmp/p', status='proposed')[0]['goal_id']
                 if get_goals_by_status(state_dir, project='/tmp/p', status='proposed')
                 else get_goals_by_status(state_dir, project='/tmp/p', status='confirmed')[0]['goal_id'])

    # Confirm it first
    goals = get_goals_by_status(state_dir, project='/tmp/p', status='confirmed')
    if not goals:
        proposed = get_goals_by_status(state_dir, project='/tmp/p', status='proposed')
        if proposed:
            confirm_goal(state_dir, project='/tmp/p', goal_id=proposed[0]['goal_id'])

    task = self_assign_next_task(state_dir, agent_id='AG-001',
                                 project='/tmp/p', config=config)
    assert task is None  # budget exhausted


# ── Query helpers ───────────────────────────────────────────────────

def test_get_proposed_goals(tmp_path):
    state_dir = tmp_path / 'state'
    propose_goal(state_dir, agent_id='AG-001', project='/tmp/p', title='Goal A')
    propose_goal(state_dir, agent_id='AG-001', project='/tmp/p', title='Goal B')

    proposed = get_proposed_goals(state_dir, project='/tmp/p')
    assert len(proposed) == 2
