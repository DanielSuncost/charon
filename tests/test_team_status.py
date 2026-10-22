"""The chat's team band: live count, honest stall/lost states, real countdown."""
from __future__ import annotations

import json
import threading
import time

from backend.team_status import TeamTracker, collect_team
from charon.shade import shade_activity
from charon.shade.shade_activity import ActivityRecorder, register_thread
from charon.shade.shade_orchestrator import create_contract, get_contract, mark_phase_completed


def _spawn(state_dir, role, owner='session-1'):
    c = create_contract(state_dir, parent_task_id='', parent_agent_id=owner, shade_agent_id=f'AG-{role}',
                        conversation_id='x', project='/p', goal=f'{role} work', metadata={'role': role})
    stop = threading.Event()
    t = threading.Thread(target=stop.wait, daemon=True)
    t.start()
    register_thread(c['id'], t)
    rec = ActivityRecorder(state_dir, c['id'])
    rec.note('tool_call', {'tool_name': 'Edit', 'arguments': {'path': '/p/RecitationView.swift'}})
    return c['id'], stop, rec


def test_working_team_has_count_doing_and_a_real_countdown(tmp_path, monkeypatch):
    monkeypatch.setenv('CHARON_SHADE_CONTRACT_STALL_SECONDS', '3600')
    a, sa, _ = _spawn(tmp_path, 'engine')
    _spawn(tmp_path, 'ui')
    _spawn(tmp_path, 'qa', owner='someone-else')
    tracker = TeamTracker(interval=300)
    now = time.time()
    status, lines = tracker.tick(tmp_path, 'session-1', now)
    assert status['state'] == 'working' and len(status['agents']) == 2
    assert status['agents'][0]['doing'] == 'Edit RecitationView.swift'
    assert status['next_update_at'] == now + 300 and lines == []
    # The scheduled update fires on time and reschedules.
    status, lines = tracker.tick(tmp_path, 'session-1', now + 301)
    assert lines[0] == 'Team update · 2 working on your task'
    assert status['next_update_at'] == now + 601


def test_quiet_agent_is_flagged_not_shown_healthy(tmp_path):
    cid, _, rec = _spawn(tmp_path, 'engine')
    tracker = TeamTracker(interval=300)
    now = time.time()
    tracker.tick(tmp_path, 'session-1', now)
    status, lines = tracker.tick(tmp_path, 'session-1', now + 400)   # stall window is 300s
    assert status['agents'][0]['health'] == 'quiet'
    assert lines[0].startswith('⚠ engine has produced nothing for')


def test_dead_worker_stops_the_band_and_fails_the_contract(tmp_path):
    cid, stop, _ = _spawn(tmp_path, 'engine')
    tracker = TeamTracker(interval=300)
    tracker.tick(tmp_path, 'session-1')
    stop.set()
    time.sleep(0.05)                                   # thread exits without finishing the contract
    status, lines = tracker.tick(tmp_path, 'session-1')
    assert status['state'] == 'stopped' and status['next_update_at'] is None
    assert any('worker exited' in line for line in lines)
    assert get_contract(tmp_path, cid)['status'] == 'failed'


def test_finishing_clears_the_band_with_a_summary(tmp_path):
    cid, _, _ = _spawn(tmp_path, 'engine')
    tracker = TeamTracker(interval=300)
    tracker.tick(tmp_path, 'session-1')
    for phase in get_contract(tmp_path, cid)['phases']:
        mark_phase_completed(tmp_path, cid, phase['phase_id'], task_id='AG-engine', summary='Implemented and tested the engine.')
    status, lines = tracker.tick(tmp_path, 'session-1')
    assert status['state'] == 'idle'
    assert lines[0].startswith('✓ engine finished') and lines[-1].startswith('✓ Team done — 1/1')


def test_heartbeat_from_another_process_that_exited_is_lost(tmp_path):
    c = create_contract(tmp_path, parent_task_id='', parent_agent_id='s', shade_agent_id='AG-1',
                        conversation_id='x', project='/p', goal='g')
    path = tmp_path / 'shade_activity' / f"{c['id']}.json"
    path.parent.mkdir()
    path.write_text(json.dumps({'pid': 999999, 'last_event_at': time.time(), 'doing': 'Bash xcodebuild'}))
    assert collect_team(tmp_path, 's')[0]['health'] == 'lost'
    assert shade_activity.thread_alive(c['id']) is None
