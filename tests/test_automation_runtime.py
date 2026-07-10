import time

from datetime import datetime, timezone

from charon.automation.automation_runtime import create_automation, get_automation_state, pause_automation, resume_automation, request_stop_automation, compute_next_run, cron_matches_dt
from charon.automation.automation_scheduler import run_due_automations_once


def test_automation_lifecycle_and_scheduler_run(tmp_path, monkeypatch):
    state_dir = tmp_path / 'state'
    project_root = tmp_path / 'project'
    project_root.mkdir()

    doc = create_automation(
        state_dir,
        project_root,
        title='Hourly website check',
        goal='Every hour check https://example.com',
        kind='http_check',
        mode='scheduled',
        schedule={'interval_seconds': 1},
        action={'url': 'https://example.com'},
    )
    assert doc['status'] == 'active'
    assert doc['kind'] == 'http_check'

    from charon.automation import automation_scheduler
    monkeypatch.setattr(automation_scheduler, '_http_check', lambda action: (True, 'HTTP 200 from example', {'url': action['url']}, ''))

    started = run_due_automations_once(state_dir, now_ts=time.time() + 5)
    assert doc['automation_id'] in started

    deadline = time.time() + 2
    latest = {}
    while time.time() < deadline:
        latest = get_automation_state(state_dir, doc['automation_id'])
        if latest.get('runs_tail') and latest.get('health') == 'healthy':
            break
        time.sleep(0.02)

    assert latest['health'] == 'healthy'
    assert latest['runs_tail'][-1]['ok'] is True
    assert latest['next_run_ts'] > 0


def test_cron_schedule_computation():
    dt = datetime(2026, 3, 30, 9, 0, tzinfo=timezone.utc)
    assert cron_matches_dt('0 9 * * 1-5', dt) is True
    next_ts, _ = compute_next_run(datetime(2026, 3, 30, 8, 15, tzinfo=timezone.utc).timestamp(), 'scheduled', {'type': 'cron', 'cron': '0 9 * * 1-5'})
    next_dt = datetime.fromtimestamp(next_ts, tz=timezone.utc)
    assert next_dt.hour == 9
    assert next_dt.minute == 0


def test_browser_check_automation(tmp_path, monkeypatch):
    state_dir = tmp_path / 'state'
    project_root = tmp_path / 'project'
    project_root.mkdir()

    doc = create_automation(
        state_dir,
        project_root,
        title='Browser functional monitor',
        goal='Browser check https://example.com',
        kind='browser_check',
        mode='scheduled',
        schedule={'type': 'interval', 'interval_seconds': 1},
        action={'url': 'https://example.com', 'expected_text': 'Example Domain', 'screenshot_on_failure': True},
    )

    import charon.tools.browser_tool as browser_tool

    class FakeResult:
        def __init__(self, content, is_error=False):
            self.content = content
            self.is_error = is_error

    def fake_execute_browser(params, ctx):
        if params.get('action') == 'navigate':
            return FakeResult('URL: https://example.com\nTitle: Example Domain\n\nExample Domain')
        if params.get('action') == 'screenshot':
            return FakeResult('Screenshot saved: /tmp/fake.png (123 bytes)')
        return FakeResult('ok')

    monkeypatch.setattr(browser_tool, 'execute_browser', fake_execute_browser)

    started = run_due_automations_once(state_dir, now_ts=time.time() + 5)
    assert doc['automation_id'] in started

    deadline = time.time() + 2
    latest = {}
    while time.time() < deadline:
        latest = get_automation_state(state_dir, doc['automation_id'])
        if latest.get('runs_tail') and latest.get('health') == 'healthy':
            break
        time.sleep(0.02)

    assert latest['runs_tail'][-1]['ok'] is True
    assert latest['health'] == 'healthy'


def test_continuous_automation_runs_multiple_iterations(tmp_path, monkeypatch):
    state_dir = tmp_path / 'state'
    project_root = tmp_path / 'project'
    project_root.mkdir()

    doc = create_automation(
        state_dir,
        project_root,
        title='Always-on website check',
        goal='Continuously check https://example.com',
        kind='http_check',
        mode='continuous',
        schedule={'type': 'continuous', 'poll_seconds': 1},
        action={'url': 'https://example.com'},
    )

    from charon.automation import automation_scheduler
    monkeypatch.setattr(automation_scheduler, '_http_check', lambda action: (True, 'loop ok', {'url': action['url']}, ''))

    started = run_due_automations_once(state_dir, now_ts=time.time())
    assert doc['automation_id'] in started

    deadline = time.time() + 2.5
    latest = {}
    while time.time() < deadline:
        latest = get_automation_state(state_dir, doc['automation_id'])
        if len(latest.get('runs_tail') or []) >= 2:
            break
        time.sleep(0.05)

    assert len(latest.get('runs_tail') or []) >= 2
    assert latest['status'] == 'active'
    stopped = request_stop_automation(state_dir, doc['automation_id'])
    assert stopped['stop_requested'] is True


def test_automation_pause_resume_and_stop(tmp_path):
    state_dir = tmp_path / 'state'
    project_root = tmp_path / 'project'
    project_root.mkdir()

    doc = create_automation(
        state_dir,
        project_root,
        title='Daily monitor',
        goal='Check site daily',
        kind='http_check',
        mode='scheduled',
        schedule={'interval_seconds': 86400},
        action={'url': 'https://example.com'},
    )

    paused = pause_automation(state_dir, doc['automation_id'])
    assert paused['status'] == 'paused'

    resumed = resume_automation(state_dir, doc['automation_id'])
    assert resumed['status'] == 'active'

    stopping = request_stop_automation(state_dir, doc['automation_id'])
    assert stopping['status'] == 'stopping'
    assert stopping['stop_requested'] is True
