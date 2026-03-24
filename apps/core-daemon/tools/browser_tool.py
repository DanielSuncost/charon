"""Browser tool — web browser automation.

Primary: browser-use via CDP connection to a managed Chrome instance.
Fallback: Playwright async API if browser-use fails.

Both run on a dedicated thread with their own event loop to avoid
conflicts with the chat engine's asyncio loop.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import threading
import time
from pathlib import Path
from typing import Any

from tools import ToolContext, ToolResult

# ── Dedicated browser thread ────────────────────────────────────────

_loop = None
_thread = None
_ready = threading.Event()
_backend = None  # 'browser_use' or 'playwright'


def _browser_thread_main():
    global _loop
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)
    _ready.set()
    _loop.run_forever()


def _ensure_thread():
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _ready.clear()
    _thread = threading.Thread(target=_browser_thread_main, daemon=True)
    _thread.start()
    _ready.wait(timeout=5)


def _run(coro):
    _ensure_thread()
    future = asyncio.run_coroutine_threadsafe(coro, _loop)
    return future.result(timeout=30)


# ── Chrome process management ───────────────────────────────────────

_chrome_proc = None

def _find_chrome() -> str | None:
    """Find a Chrome/Chromium binary."""
    import shutil
    for name in ('google-chrome', 'chromium', 'chromium-browser', 'chrome'):
        path = shutil.which(name)
        if path:
            return path
    # Check Playwright's Chromium
    pw_chrome = Path.home() / '.cache' / 'ms-playwright'
    if pw_chrome.exists():
        for chrome in sorted(pw_chrome.glob('*/chrome-linux*/chrome'), reverse=True):
            if chrome.exists():
                return str(chrome)
    return None


def _ensure_chrome_cdp(port: int = 9222) -> str:
    """Launch Chrome with remote debugging if not running."""
    global _chrome_proc
    import httpx

    cdp_url = f'http://127.0.0.1:{port}'

    # Check if already running
    try:
        resp = httpx.get(f'{cdp_url}/json/version', timeout=2)
        if resp.status_code == 200:
            return cdp_url
    except Exception:
        pass

    # Launch
    chrome = _find_chrome()
    if not chrome:
        return ''

    _chrome_proc = subprocess.Popen([
        chrome,
        '--headless=new' if os.environ.get('CHARON_BROWSER_HEADLESS', '1') != '0' else '--headless=false',
        f'--remote-debugging-port={port}',
        '--no-sandbox',
        '--disable-gpu',
        '--disable-dev-shm-usage',
        '--no-first-run',
        '--no-default-browser-check',
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # Wait for it to be ready
    for _ in range(20):
        time.sleep(0.3)
        try:
            resp = httpx.get(f'{cdp_url}/json/version', timeout=2)
            if resp.status_code == 200:
                return cdp_url
        except Exception:
            pass

    return ''


# ── browser-use backend ─────────────────────────────────────────────

_bu_session = None
_bu_controller = None
_bu_action_cls = None


async def _init_browser_use(cdp_url: str):
    global _bu_session, _bu_controller, _bu_action_cls
    if _bu_session is not None:
        return True

    try:
        from browser_use.browser.session import BrowserSession
        from browser_use.controller import Controller

        _bu_controller = Controller()
        _bu_action_cls = _bu_controller.registry.create_action_model()
        _bu_session = BrowserSession(cdp_url=cdp_url)
        await _bu_session.start()
        return True
    except Exception:
        _bu_session = None
        return False


async def _bu_navigate(url: str) -> str:
    await _bu_session.navigate_to(url)
    return await _bu_session.get_state_as_text()


async def _bu_get_state() -> str:
    return await _bu_session.get_state_as_text()


async def _bu_get_url() -> str:
    return await _bu_session.get_current_page_url()


async def _bu_act(action_dict: dict):
    action = _bu_action_cls.model_validate(action_dict)
    return await _bu_controller.act(action, _bu_session)


async def _bu_screenshot() -> bytes:
    return await _bu_session.take_screenshot()


# ── Playwright fallback ─────────────────────────────────────────────

_pw = None
_pw_browser = None
_pw_page = None


async def _init_playwright():
    global _pw, _pw_browser, _pw_page
    if _pw_page is not None:
        return True

    try:
        from playwright.async_api import async_playwright
        _pw = await async_playwright().start()
        headless = os.environ.get('CHARON_BROWSER_HEADLESS', '1') != '0'
        _pw_browser = await _pw.chromium.launch(headless=headless)
        _pw_page = await _pw_browser.new_page()
        return True
    except Exception:
        return False


async def _pw_navigate(url: str) -> str:
    await _pw_page.goto(url, wait_until='domcontentloaded', timeout=15000)
    await _pw_page.wait_for_timeout(1000)
    return await _pw_page_summary()


async def _pw_page_summary() -> str:
    url = _pw_page.url
    title = await _pw_page.title()
    try:
        text = await _pw_page.evaluate('() => document.body.innerText')
    except Exception:
        text = ''

    elements = []
    try:
        interactives = await _pw_page.evaluate('''() => {
            const els = document.querySelectorAll('a, button, input, select, textarea, [role="button"], [onclick]');
            return Array.from(els).slice(0, 50).map((el, i) => {
                const tag = el.tagName.toLowerCase();
                const text = (el.innerText || el.value || el.placeholder || el.getAttribute('aria-label') || '').trim().slice(0, 60);
                const href = el.href || '';
                return {i, tag, text, href: href.slice(0, 80)};
            });
        }''')
        for el in interactives:
            parts = [f'[{el["i"]}] {el["tag"]}']
            if el.get('text'):
                parts.append(f'"{el["text"]}"')
            if el.get('href'):
                parts.append(f'→ {el["href"]}')
            elements.append(' '.join(parts))
    except Exception:
        pass

    lines = [f'URL: {url}', f'Title: {title}', '', text[:8000] if text else '']
    if elements:
        lines.append('\n--- Interactive Elements ---')
        lines.extend(elements)
    return '\n'.join(lines)


# ── Unified dispatch ────────────────────────────────────────────────

async def _ensure_backend():
    """Initialize browser-use or fall back to Playwright."""
    global _backend

    if _backend:
        return _backend

    # Try browser-use first
    cdp_url = _ensure_chrome_cdp()
    if cdp_url:
        ok = await _init_browser_use(cdp_url)
        if ok:
            _backend = 'browser_use'
            return _backend

    # Fall back to Playwright
    ok = await _init_playwright()
    if ok:
        _backend = 'playwright'
        return _backend

    raise RuntimeError('No browser backend available. Install playwright: uv pip install playwright && playwright install chromium')


async def _do_navigate(url: str) -> str:
    backend = await _ensure_backend()
    if backend == 'browser_use':
        state = await _bu_navigate(url)
        page_url = await _bu_get_url()
        return f'URL: {page_url}\n\n{state}'
    return await _pw_navigate(url)


async def _do_get_state() -> str:
    backend = await _ensure_backend()
    if backend == 'browser_use':
        state = await _bu_get_state()
        page_url = await _bu_get_url()
        return f'URL: {page_url}\n\n{state}'
    return await _pw_page_summary()


async def _do_click(index: int) -> str:
    backend = await _ensure_backend()
    if backend == 'browser_use':
        await _bu_act({'click': {'index': index}})
        state = await _bu_get_state()
        return f'Clicked [{index}].\n\n{state}'
    await _pw_page.evaluate(f'''() => {{
        const els = document.querySelectorAll('a, button, input, select, textarea, [role="button"], [onclick]');
        if (els[{index}]) els[{index}].click();
    }}''')
    await _pw_page.wait_for_timeout(1000)
    return f'Clicked [{index}].\n\n' + await _pw_page_summary()


async def _do_input(index: int, text: str) -> str:
    backend = await _ensure_backend()
    if backend == 'browser_use':
        await _bu_act({'input': {'index': index, 'text': text, 'clear': True}})
        return f'Input "{text[:50]}" into [{index}].'
    await _pw_page.evaluate(f'''() => {{
        const els = document.querySelectorAll('a, button, input, select, textarea, [role="button"], [onclick]');
        const el = els[{index}];
        if (el) {{ el.focus(); el.value = {json.dumps(text)}; }}
    }}''')
    return f'Input "{text[:50]}" into [{index}].'


async def _do_screenshot() -> bytes:
    backend = await _ensure_backend()
    if backend == 'browser_use':
        return await _bu_screenshot()
    return await _pw_page.screenshot(full_page=False)


async def _do_scroll(direction: str) -> str:
    backend = await _ensure_backend()
    if backend == 'browser_use':
        await _bu_act({'scroll': {'direction': direction}})
        return f'Scrolled {direction}.\n\n' + await _bu_get_state()
    delta = 500 if direction == 'down' else -500
    await _pw_page.evaluate(f'window.scrollBy(0, {delta})')
    await _pw_page.wait_for_timeout(500)
    return f'Scrolled {direction}.\n\n' + await _pw_page_summary()


async def _do_go_back() -> str:
    backend = await _ensure_backend()
    if backend == 'browser_use':
        await _bu_act({'go_back': {}})
        return 'Went back.\n\n' + await _bu_get_state()
    await _pw_page.go_back(wait_until='domcontentloaded', timeout=10000)
    await _pw_page.wait_for_timeout(1000)
    return 'Went back.\n\n' + await _pw_page_summary()


# ── Tool definition ─────────────────────────────────────────────────

BROWSER_TOOL_DEF = {
    'name': 'Browser',
    'description': (
        'Control a web browser with full JavaScript rendering. Navigate pages, click elements, '
        'type text, take screenshots. Uses browser-use (CDP) or Playwright as fallback. '
        'After navigating, interactive elements are listed with indices [0], [1], etc. '
        'Actions: navigate, click, input, screenshot, scroll, go_back, wait, get_state.'
    ),
    'input_schema': {
        'type': 'object',
        'properties': {
            'action': {
                'type': 'string',
                'enum': ['navigate', 'click', 'input', 'screenshot',
                         'scroll', 'go_back', 'wait', 'get_state'],
            },
            'url': {'type': 'string', 'description': 'URL for navigate.'},
            'index': {'type': 'number', 'description': 'Element index for click/input.'},
            'text': {'type': 'string', 'description': 'Text for input action.'},
            'direction': {'type': 'string', 'enum': ['up', 'down']},
            'seconds': {'type': 'number', 'description': 'Wait duration.'},
        },
        'required': ['action'],
    },
}


def execute_browser(params: dict, ctx: ToolContext) -> ToolResult:
    """Execute a browser action."""
    action = str(params.get('action', '')).strip().lower()

    try:
        if action == 'navigate':
            url = str(params.get('url', '')).strip()
            if not url:
                return ToolResult(content='Error: url is required.', is_error=True)
            result = _run(_do_navigate(url))
            return ToolResult(content=result[:15000])

        if action == 'get_state':
            return ToolResult(content=_run(_do_get_state())[:15000])

        if action == 'click':
            index = params.get('index')
            if index is None:
                return ToolResult(content='Error: index is required.', is_error=True)
            return ToolResult(content=_run(_do_click(int(index)))[:12000])

        if action == 'input':
            index = params.get('index')
            if index is None:
                return ToolResult(content='Error: index is required.', is_error=True)
            return ToolResult(content=_run(_do_input(int(index), str(params.get('text', '')))))

        if action == 'screenshot':
            img_bytes = _run(_do_screenshot())
            import tempfile
            tmp = tempfile.NamedTemporaryFile(suffix='.png', prefix='charon-ss-', delete=False, dir='/tmp')
            tmp.write(img_bytes)
            tmp.close()
            return ToolResult(content=f'Screenshot saved: {tmp.name} ({len(img_bytes)} bytes)')

        if action == 'scroll':
            return ToolResult(content=_run(_do_scroll(str(params.get('direction', 'down'))))[:12000])

        if action == 'go_back':
            return ToolResult(content=_run(_do_go_back())[:12000])

        if action == 'wait':
            seconds = min(int(params.get('seconds', 2)), 30)
            async def _w():
                await asyncio.sleep(seconds)
            _run(_w())
            return ToolResult(content=f'Waited {seconds} seconds.')

        return ToolResult(content=f'Unknown action: {action}', is_error=True)

    except Exception as e:
        return ToolResult(content=f'Browser error: {e}', is_error=True)
