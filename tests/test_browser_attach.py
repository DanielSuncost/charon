"""Attachment capabilities: real external Chromium plus unit security checks."""
from __future__ import annotations

import json
import subprocess
import time

import pytest

from charon.infra import diagnostics, tool_approval
from charon.tools import ToolContext, _check_scope, browser_tool as b
from tests.browser_support import LocalSite


@pytest.fixture
def clean_browser():
    b._run(b._shutdown())
    yield
    b._run(b._shutdown())


@pytest.fixture
def external_browser(tmp_path, clean_browser):
    pytest.importorskip('playwright')
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        executable = pw.chromium.executable_path
    profile = tmp_path / 'profile'
    profile.mkdir()
    (profile / 'Default').mkdir()
    (profile / 'Default' / 'Preferences').write_text(json.dumps({
        'download': {'default_directory': str(tmp_path / 'downloads'), 'prompt_for_download': False},
    }))
    site = LocalSite({'/': '<title>External</title><button onclick="this.textContent=\'done\'">Click</button>',
                      '/other': '<title>Other</title><p>other</p>'}).start()
    process = subprocess.Popen([executable, '--headless', '--no-sandbox', '--remote-debugging-port=0',
                                '--disable-background-timer-throttling', '--disable-renderer-backgrounding',
                                '--disable-backgrounding-occluded-windows', '--no-first-run',
                                '--disable-features=PaintHolding', '--enable-automation',
                                '--allow-pre-commit-input', '--use-mock-keychain',
                                f'--user-data-dir={profile}', site.url('/')],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        deadline = time.monotonic() + 20
        port_file = profile / 'DevToolsActivePort'
        while not port_file.exists() and time.monotonic() < deadline:
            assert process.poll() is None, 'External Chromium exited before CDP became ready'
            time.sleep(.05)
        assert port_file.exists(), 'External Chromium did not publish its CDP port'
        endpoint = f'http://127.0.0.1:{port_file.read_text().splitlines()[0]}'
        # Ensure startup navigation completed through an independent connection.
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(endpoint)
            page = browser.contexts[0].pages[0]
            page.wait_for_url(site.url('/'))
            page.wait_for_load_state('domcontentloaded')
        yield endpoint, site, process
    finally:
        b._run(b._shutdown())
        process.terminate()
        process.wait(timeout=15)
        site.stop()


def test_real_attach_cdp_and_rails(external_browser, tmp_path, monkeypatch):
    endpoint, site, process = external_browser
    ctx = ToolContext(project_root=tmp_path, state_dir=tmp_path / 'state', agent_id='owner')
    approvals = []

    def approve(*args):
        approvals.append(args)
        return True

    monkeypatch.setattr(tool_approval, 'request_attached_browser_approval', approve)
    monkeypatch.setenv('CHARON_BROWSER_ALLOWED_ORIGINS', site.url('/'))
    attached = b.execute_browser({'action': 'attach', 'endpoint': endpoint, 'url': site.url('/')}, ctx)
    assert not attached.is_error, attached.content
    session_id = json.loads(attached.content)['session_id']

    def call(action, **params):
        return b.execute_browser({'action': action, 'session_id': session_id, **params}, ctx)

    assert len(approvals) == 1
    assert 'External' in call('get_state').content
    allowed = call('cdp', operation='dom_document')
    assert not allowed.is_error, allowed.content
    assert 'root' in json.loads(allowed.content)
    status = call('cdp', operation='network_status')
    assert not status.is_error, status.content
    assert call('cdp', operation='Runtime.evaluate').is_error
    assert call('cdp', operation='dom_document', method='Runtime.evaluate').is_error
    for operation in ('console_messages', 'downloads', 'dialogs'):
        assert not call('cdp', operation=operation).is_error
    assert len(approvals) == 1  # Inspection does not prompt.
    assert call('navigate', url=site.alt_url('/other')).is_error

    shade = ToolContext(project_root=tmp_path, agent_id='shade', scope=['src'])
    assert 'not granted' in _check_scope('Browser', {'action': 'get_state', 'session_id': session_id}, shade)
    assert b.execute_browser({'action': 'get_state', 'session_id': session_id}, shade).is_error
    shade.browser_session_grants = [session_id]
    assert _check_scope('Browser', {'action': 'get_state', 'session_id': session_id}, shade) is None
    assert not b.execute_browser({'action': 'get_state', 'session_id': session_id}, shade).is_error
    assert _check_scope('Browser', {'action': 'attach'}, shade)
    assert b.execute_browser({'action': 'get_state'}, ctx).is_error

    monkeypatch.setattr(tool_approval, 'request_attached_browser_approval', lambda *args: False)
    assert call('click_selector', selector='button').is_error
    assert 'done' not in call('get_state').content
    assert call('cdp', operation='dialog_policy', policy='accept').is_error
    monkeypatch.setattr(tool_approval, 'request_attached_browser_approval', approve)
    # External full Chromium on macOS can stop animation frames in headless
    # mode. Exercise the existing ref click's actionability/DOM fallback.
    ref = next(ref for ref, entry in b._refs.items() if entry['label'] == 'Click')
    clicked = call('click', ref=ref)
    assert not clicked.is_error, clicked.content
    assert 'done' in call('get_state').content
    b._run(b._page.evaluate("console.log('attach-console'); alert('attach-dialog')"))
    assert 'attach-console' in call('cdp', operation='console_messages').content
    assert 'attach-dialog' in call('cdp', operation='dialogs').content
    b._run(b._page.evaluate("""() => {
        const link = document.createElement('a');
        link.href = '/other'; link.download = 'fixture.txt';
        document.body.appendChild(link); link.click();
    }"""))
    b._run(b._page.wait_for_timeout(300))
    assert 'fixture.txt' in call('cdp', operation='downloads').content
    assert not call('detach').is_error
    assert process.poll() is None
    assert not b._refs and b._attached is None
    # Verify the original page/session survives detachment, without a tool launch.
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(endpoint)
        assert browser.contexts[0].pages[0].locator('button').inner_text() == 'done'
    records = diagnostics.read_recent(ctx.state_dir, 100)
    audit = [r for r in records if r['component'] == 'browser_audit']
    assert any(r['action'] == 'attach' and r['outcome'] == 'success' for r in audit)
    assert any(r['operation'] == 'dom_document' and r['outcome'] == 'success' for r in audit)
    assert any(r['operation'] == 'Runtime.evaluate' and r['outcome'] == 'refused_or_failed' for r in audit)
    assert endpoint not in (ctx.state_dir / 'diagnostics.jsonl').read_text()
    # A new attachment revokes old session identities. Closing its page must
    # fail closed rather than silently launching a replacement browser.
    attached = b.execute_browser({'action': 'attach', 'endpoint': endpoint, 'url': site.url('/')}, ctx)
    assert not attached.is_error, attached.content
    new_session = json.loads(attached.content)['session_id']
    assert new_session != session_id
    assert call('get_state').is_error
    session_id = new_session
    b._run(b._page.close())
    assert call('get_state').is_error
    assert not call('detach').is_error
    assert b._attached is None and process.poll() is None


def test_domain_config_and_gate(tmp_path, monkeypatch, clean_browser):
    monkeypatch.delenv('CHARON_BROWSER_ALLOWED_ORIGINS', raising=False)
    with pytest.raises(ValueError, match='Set CHARON'):
        b._allowed_origins()
    monkeypatch.setenv('CHARON_BROWSER_ALLOWED_ORIGINS', 'https://example.com')
    origins = b._allowed_origins()
    assert b._url_allowed('https://example.com/path', origins)
    for url in ('https://example.com.evil', 'https://example.com:444/', 'https://example.com:0/', 'http://example.com',
                'file:///etc/passwd', 'https://user@example.com', 'https://sub.example.com'):
        assert not b._url_allowed(url, origins)
    monkeypatch.setattr(tool_approval, 'is_approval_skipped', lambda: False)
    monkeypatch.setattr('charon.tools._request_interactive_approval', lambda *args: (False, 'denied'))
    ctx = ToolContext(project_root=tmp_path, state_dir=tmp_path)
    result = b.execute_browser({'action': 'attach', 'endpoint': 'http://127.0.0.1:1', 'url': 'https://example.com'}, ctx)
    assert result.is_error and 'approval' in result.content
    assert diagnostics.read_recent(tmp_path)[-1]['outcome'] == 'refused_or_failed'


def test_disallowed_existing_page(external_browser, tmp_path, monkeypatch):
    endpoint, site, process = external_browser
    monkeypatch.setenv('CHARON_BROWSER_ALLOWED_ORIGINS', 'https://example.com')
    monkeypatch.setattr(tool_approval, 'request_attached_browser_approval', lambda *args: True)
    ctx = ToolContext(project_root=tmp_path, state_dir=tmp_path)
    result = b.execute_browser({'action': 'attach', 'endpoint': endpoint, 'url': site.url('/')}, ctx)
    assert result.is_error and 'allowlist' in result.content
    assert b._attached is None and process.poll() is None


def test_mixed_origin_attach_fails_without_closing_user_page(external_browser, tmp_path, monkeypatch):
    endpoint, site, process = external_browser
    site.pages['/mixed'] = '<iframe src="' + site.alt_url('/other') + '"></iframe>'
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(endpoint)
        page = browser.contexts[0].pages[0]
        page.goto(site.url('/mixed'))
        assert len(page.frames) == 2
    monkeypatch.setenv('CHARON_BROWSER_ALLOWED_ORIGINS', site.url('/'))
    monkeypatch.setattr(tool_approval, 'request_attached_browser_approval', lambda *args: True)
    ctx = ToolContext(project_root=tmp_path, state_dir=tmp_path)
    result = b.execute_browser({'action': 'attach', 'endpoint': endpoint, 'url': site.url('/mixed')}, ctx)
    assert result.is_error and 'allowlist' in result.content
    assert b._attached is None and process.poll() is None
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(endpoint)
        assert browser.contexts[0].pages[0].url == site.url('/mixed')
