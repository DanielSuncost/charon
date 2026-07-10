"""Tests for idea capture, goal listing, and heartbeat."""
import json
import sys
import subprocess
from pathlib import Path

from charon.agents import goal_runtime

ROOT = Path(__file__).resolve().parents[1]


# ── Idea capture ────────────────────────────────────────────────────

def test_ingest_idea_creates_backlog_goal(tmp_path):
    state_dir = tmp_path / 'state'
    result = goal_runtime.ingest_idea(
        state_dir,
        agent_id='AG-001',
        project='/tmp/myproject',
        text='Add rate limiting to the API',
    )
    assert result['goal']['status'] == 'backlog'
    assert result['goal']['intent_type'] == 'idea'
    assert 'rate limiting' in result['goal']['title']
    assert result['goal']['goal_id'].startswith('goal-')


def test_ingest_idea_with_priority(tmp_path):
    state_dir = tmp_path / 'state'
    result = goal_runtime.ingest_idea(
        state_dir,
        agent_id='AG-001',
        project='/tmp/proj',
        text='Critical security fix',
        priority='high',
    )
    assert result['goal']['priority'] == 'high'
    assert result['goal']['status'] == 'backlog'


def test_multiple_ideas_accumulate(tmp_path):
    state_dir = tmp_path / 'state'
    goal_runtime.ingest_idea(state_dir, agent_id='AG-001', project='/tmp/p', text='Idea 1')
    goal_runtime.ingest_idea(state_dir, agent_id='AG-001', project='/tmp/p', text='Idea 2')
    goal_runtime.ingest_idea(state_dir, agent_id='AG-001', project='/tmp/p', text='Idea 3')

    goals = goal_runtime.list_goals(state_dir, project='/tmp/p')
    assert len(goals) == 3

    backlog = goal_runtime.list_goals(state_dir, project='/tmp/p', status='backlog')
    assert len(backlog) == 3


# ── Goal listing ────────────────────────────────────────────────────

def test_list_goals_filters_by_status(tmp_path):
    state_dir = tmp_path / 'state'

    # Create a mix of ideas and active goals
    goal_runtime.ingest_idea(state_dir, agent_id='AG-001', project='/tmp/p', text='Backlog item')
    goal_runtime.ingest_user_intent(
        state_dir, agent_id='AG-001', project='/tmp/p',
        session_id='ses-1', conversation_id='conv-1', text='Active task',
    )

    all_goals = goal_runtime.list_goals(state_dir, project='/tmp/p')
    assert len(all_goals) == 2

    backlog = goal_runtime.list_goals(state_dir, project='/tmp/p', status='backlog')
    assert len(backlog) == 1
    assert 'Backlog' in backlog[0]['title']

    active = goal_runtime.list_goals(state_dir, project='/tmp/p', status='active')
    assert len(active) == 1
    assert 'Active' in active[0]['title']


def test_list_goals_empty_project(tmp_path):
    goals = goal_runtime.list_goals(tmp_path / 'state', project='/tmp/nonexistent')
    assert goals == []


# ── Promote idea ────────────────────────────────────────────────────

def test_promote_idea_to_active(tmp_path):
    state_dir = tmp_path / 'state'
    result = goal_runtime.ingest_idea(
        state_dir, agent_id='AG-001', project='/tmp/p', text='Promote me',
    )
    goal_id = result['goal']['goal_id']

    promoted = goal_runtime.promote_idea(state_dir, project='/tmp/p', goal_id=goal_id)
    assert promoted is not None
    assert promoted['status'] == 'active'

    # Verify in listing
    active = goal_runtime.list_goals(state_dir, project='/tmp/p', status='active')
    assert len(active) == 1
    assert active[0]['goal_id'] == goal_id

    backlog = goal_runtime.list_goals(state_dir, project='/tmp/p', status='backlog')
    assert len(backlog) == 0


def test_promote_nonexistent_goal(tmp_path):
    state_dir = tmp_path / 'state'
    result = goal_runtime.promote_idea(state_dir, project='/tmp/p', goal_id='goal-nonexistent')
    assert result is None


# ── Heartbeat ───────────────────────────────────────────────────────

def test_heartbeat_emitted_in_loop(tmp_path):
    """Run the loop with a short heartbeat interval and verify heartbeat events."""
    state_dir = tmp_path / 'state'
    stop_file = tmp_path / 'STOP'

    SCRIPT = ROOT / 'src' / 'charon' / 'charon_loop.py'
    env = {
        'CHARON_HEARTBEAT_INTERVAL': '2',  # emit every 2 cycles
        'CHARON_STDOUT_EVENTS': '0',
        'PATH': '/usr/bin:/bin',
    }

    cmd = [
        sys.executable, str(SCRIPT),
        '--state-dir', str(state_dir),
        '--stop-file', str(stop_file),
        '--sleep-sec', '0.01',
        '--max-cycles', '10',
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10, env=env)
    assert proc.returncode == 0

    log_file = state_dir / 'run.log'
    assert log_file.exists()
    events = [json.loads(line) for line in log_file.read_text().splitlines() if line.strip()]
    event_types = [e['event'] for e in events]

    assert 'heartbeat' in event_types
    heartbeats = [e for e in events if e['event'] == 'heartbeat']
    assert len(heartbeats) >= 1
    assert 'uptime_seconds' in heartbeats[0]
    assert 'cycle' in heartbeats[0]
