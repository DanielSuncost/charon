import time

from charon.automation.automation_runtime import (
    create_automation,
    get_automation_state,
    reconcile_stale_automation_runs,
    set_automation_webhook,
)
from charon.automation.automation_scheduler import run_due_automations_once
from chat_backend import ChatBackend


def test_browser_workflow_automation_runs_steps(tmp_path, monkeypatch):
    state_dir = tmp_path / 'state'
    project_root = tmp_path / 'project'
    project_root.mkdir()

    doc = create_automation(
        state_dir,
        project_root,
        title='Browser workflow test',
        goal='Workflow',
        kind='browser_workflow',
        mode='scheduled',
        schedule={'type': 'interval', 'interval_seconds': 1},
        action={
            'steps': [
                {'action': 'navigate', 'url': 'https://example.com/login'},
                {'action': 'input', 'index': 0, 'text': 'alice@example.com'},
                {'action': 'click', 'index': 1},
                {'action': 'assert_text', 'text': 'Dashboard'},
            ],
            'screenshot_on_failure': True,
        },
    )

    import charon.tools.browser_tool as browser_tool

    class FakeResult:
        def __init__(self, content, is_error=False):
            self.content = content
            self.is_error = is_error

    def fake_execute_browser(params, ctx):
        action = params.get('action')
        if action == 'navigate':
            return FakeResult('URL: https://example.com/login\nTitle: Login')
        if action == 'input':
            return FakeResult('Typed into field')
        if action == 'click':
            return FakeResult('Clicked [1]')
        if action == 'get_state':
            return FakeResult('URL: https://example.com/app\nTitle: Dashboard\n\nDashboard')
        if action == 'screenshot':
            return FakeResult('Screenshot saved: /tmp/workflow.png')
        return FakeResult('ok')

    monkeypatch.setattr(browser_tool, 'execute_browser', fake_execute_browser)

    started = run_due_automations_once(state_dir, now_ts=time.time() + 5)
    assert doc['automation_id'] in started

    # Background daemon thread + two-phase finalize_run write: generous
    # deadline (was 2s — same race class as the webhook test fixed below).
    deadline = time.time() + 10
    latest = {}
    while time.time() < deadline:
        latest = get_automation_state(state_dir, doc['automation_id'])
        if latest.get('runs_tail') and latest.get('health') == 'healthy':
            break
        time.sleep(0.02)

    assert latest.get('runs_tail'), 'automation run did not record in time'
    assert latest['runs_tail'][-1]['ok'] is True
    steps = latest['runs_tail'][-1]['details']['steps']
    assert len(steps) == 4


def test_webhook_alert_delivery_and_recovery_reconcile(tmp_path, monkeypatch):
    state_dir = tmp_path / 'state'
    project_root = tmp_path / 'project'
    project_root.mkdir()

    doc = create_automation(
        state_dir,
        project_root,
        title='Webhook monitor',
        goal='Monitor with webhook',
        kind='http_check',
        mode='scheduled',
        schedule={'type': 'interval', 'interval_seconds': 1},
        action={'url': 'https://example.com'},
    )
    set_automation_webhook(state_dir, doc['automation_id'], 'https://hooks.example.test/alert')

    from charon.automation import automation_runtime
    from charon.automation import automation_scheduler

    delivered = {}
    def fake_deliver(url, payload):
        delivered['payload'] = payload
        return True, ''

    monkeypatch.setattr(automation_runtime, '_deliver_webhook', fake_deliver)
    monkeypatch.setattr(automation_scheduler, '_http_check', lambda action: (False, 'site failed', {'url': action['url']}, 'down'))

    run_due_automations_once(state_dir, now_ts=time.time() + 5)

    # The automation runs on a background daemon thread and delivers the webhook
    # as part of that run. Wait for BOTH the run record and the delivery before
    # asserting — breaking on runs_tail alone races the webhook thread, and a
    # tight deadline is flaky under full-suite CPU contention (was 2s → CI flake).
    deadline = time.time() + 10
    latest = {}
    while time.time() < deadline:
        latest = get_automation_state(state_dir, doc['automation_id'])
        if latest.get('runs_tail') and delivered.get('payload'):
            break
        time.sleep(0.02)

    assert latest.get('runs_tail'), 'automation run did not record in time'
    assert latest['runs_tail'][-1]['ok'] is False
    assert delivered.get('payload'), 'webhook was not delivered in time'
    assert delivered['payload']['automation_id'] == doc['automation_id']
    assert delivered['payload']['state'] == 'failure'

    stale = create_automation(
        state_dir,
        project_root,
        title='Continuous stale',
        goal='Continuous stale',
        kind='http_check',
        mode='continuous',
        schedule={'type': 'continuous', 'poll_seconds': 30},
        action={'url': 'https://example.com'},
    )
    from charon.automation.automation_runtime import update_automation_doc
    update_automation_doc(
        state_dir,
        stale['automation_id'],
        lambda d: d.update({'active_run_count': 1, 'current_run_started_at': '2020-01-01T00:00:00+00:00'})
    )
    recovered = reconcile_stale_automation_runs(state_dir, stale_after_seconds=1)
    assert stale['automation_id'] in recovered
    stale_state = get_automation_state(state_dir, stale['automation_id'])
    assert stale_state['active_run_count'] == 0
    assert stale_state['status'] == 'active'


def test_automate_webhook_and_browser_workflow_commands(monkeypatch, tmp_path):
    backend = ChatBackend()
    captured = []

    def fake_emit(event):
        captured.append(event)

    from backend import common
    monkeypatch.setattr(common, 'emit', fake_emit)
    monkeypatch.setattr(common, 'STATE_DIR', tmp_path / 'state')
    monkeypatch.setattr(backend, '_get_refresh_payload', lambda: {})

    backend.handle_command('/automate browser-workflow every 5 minutes steps [{"action":"navigate","url":"https://example.com"},{"action":"assert_text","text":"Example"}]', 'req-wf')
    assert any('Started browser workflow automation' in e.get('message', '') for e in captured if e.get('type') == 'status')
