import json
import time

from charon.automation.automation_runtime import create_automation, get_automation_state
from charon.automation.automation_scheduler import run_due_automations_once
from chat_backend import ChatBackend


def test_browser_workflow_selector_steps(tmp_path, monkeypatch):
    state_dir = tmp_path / 'state'
    project_root = tmp_path / 'project'
    project_root.mkdir()

    doc = create_automation(
        state_dir,
        project_root,
        title='Selector workflow',
        goal='Selector workflow',
        kind='browser_workflow',
        mode='scheduled',
        schedule={'type': 'interval', 'interval_seconds': 1},
        action={
            'steps': [
                {'action': 'navigate', 'url': 'https://example.com/login'},
                {'action': 'input_selector', 'selector': '#email', 'text': 'alice@example.com'},
                {'action': 'click_selector', 'selector': 'button[type="submit"]'},
                {'action': 'assert_selector', 'selector': '#dashboard'},
            ],
            'screenshot_on_failure': True,
        },
    )

    import charon.tools.browser_tool as browser_tool

    class FakeResult:
        def __init__(self, content, is_error=False):
            self.content = content
            self.is_error = is_error

    calls = []

    def fake_execute_browser(params, ctx):
        calls.append(dict(params))
        action = params.get('action')
        if action == 'assert_selector':
            return FakeResult('Assertion passed: selector exists #dashboard.')
        return FakeResult(f'{action} ok')

    monkeypatch.setattr(browser_tool, 'execute_browser', fake_execute_browser)

    started = run_due_automations_once(state_dir, now_ts=time.time() + 5)
    assert doc['automation_id'] in started

    deadline = time.time() + 2
    latest = {}
    while time.time() < deadline:
        latest = get_automation_state(state_dir, doc['automation_id'])
        if latest.get('runs_tail'):
            break
        time.sleep(0.02)

    assert latest['runs_tail'][-1]['ok'] is True
    assert any(c.get('action') == 'input_selector' and c.get('selector') == '#email' for c in calls)
    assert any(c.get('action') == 'click_selector' and c.get('selector') == 'button[type="submit"]' for c in calls)
    assert any(c.get('action') == 'assert_selector' and c.get('selector') == '#dashboard' for c in calls)


def test_browser_workflow_from_file_command(monkeypatch, tmp_path):
    backend = ChatBackend()
    captured = []

    workflow_dir = tmp_path / 'project' / 'workflows'
    workflow_dir.mkdir(parents=True)
    workflow_file = workflow_dir / 'login-check.json'
    workflow_file.write_text(json.dumps([
        {'action': 'navigate', 'url': 'https://example.com/login'},
        {'action': 'assert_text', 'text': 'Login'},
    ]))

    from backend import common
    monkeypatch.setattr(common, 'STATE_DIR', tmp_path / 'state')
    monkeypatch.setattr(common, 'emit', lambda event: captured.append(event))
    monkeypatch.setattr(backend, '_devop_project_root', lambda: str(tmp_path / 'project'))
    monkeypatch.setattr(backend, '_get_refresh_payload', lambda: {})

    backend.handle_command('/automate browser-workflow every 10 minutes from workflows/login-check.json', 'req-file')

    status_msgs = [e.get('message', '') for e in captured if e.get('type') == 'status']
    assert any('Started browser workflow automation' in msg for msg in status_msgs)
    assert any('Source: workflows/login-check.json' in msg for msg in status_msgs)
