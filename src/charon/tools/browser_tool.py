"""Browser tool — cleanroom Playwright-only browser automation.

Zero external deps beyond playwright. No telemetry.

Uses Playwright Chromium with CDP for:
- Navigation, click, input, scroll, go_back, wait, screenshot, get_state
- Selector-based click/input/assert_text helpers for stable workflow automation
- Proper interactive element indexing via JS DOM walk + bounding box filtering
"""
from __future__ import annotations

import asyncio
import secrets
import threading
from pathlib import Path

from charon.tools import ToolContext, ToolResult
from charon.infra import config

# ── Dedicated browser thread ─────────────────────────────────────────────────

_loop: asyncio.AbstractEventLoop | None = None
_thread: threading.Thread | None = None
_ready = threading.Event()


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


def _run(coro, timeout: int = 30):
    _ensure_thread()
    future = asyncio.run_coroutine_threadsafe(coro, _loop)
    return future.result(timeout=timeout)


# ── Playwright state ──────────────────────────────────────────────────────────

_pw = None
_browser = None
_page = None
_context = None

# ── Browser visibility context (set per execute_browser call) ─────────────────
_session_id_ctx: str = ''
_state_dir_ctx = None

# Index map: int → xpath or element handle info for clicking
_element_index: dict[int, dict] = {}


async def _ensure_page():
    global _pw, _browser, _page, _context

    if _page is not None:
        return _page

    from playwright.async_api import async_playwright
    try:
        from charon.providers.browser_settings import should_show_browser
        headless = not should_show_browser(_session_id_ctx, _state_dir_ctx)
    except Exception:
        headless = config.browser_headless()

    _pw = await async_playwright().start()
    _browser = await _pw.chromium.launch(headless=headless)
    _context = await _browser.new_context(
        viewport={'width': 1280, 'height': 800},
        user_agent=(
            'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
            '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        ),
    )
    _page = await _context.new_page()
    return _page


# ── Interactive element indexer ───────────────────────────────────────────────

# JS that collects all visible, in-viewport interactive elements with metadata.
_COLLECT_ELEMENTS_JS = """
() => {
    const SELECTORS = 'a[href], button, input:not([type="hidden"]), select, textarea, [role="button"], [role="link"], [role="checkbox"], [role="radio"], [role="combobox"], [role="menuitem"], [role="tab"], [tabindex]:not([tabindex="-1"])';
    const vw = window.innerWidth;
    const vh = window.innerHeight;

    function getLabel(el) {
        // Try various label sources
        if (el.getAttribute('aria-label')) return el.getAttribute('aria-label').trim();
        if (el.getAttribute('title')) return el.getAttribute('title').trim();
        if (el.getAttribute('placeholder')) return el.getAttribute('placeholder').trim();
        if (el.getAttribute('alt')) return el.getAttribute('alt').trim();
        // For label elements
        if (el.id) {
            const lbl = document.querySelector('label[for="' + el.id + '"]');
            if (lbl) return lbl.innerText.trim();
        }
        const txt = (el.innerText || el.textContent || '').trim().replace(/\\s+/g, ' ');
        return txt.slice(0, 80);
    }

    function getType(el) {
        const tag = el.tagName.toLowerCase();
        if (tag === 'a') return 'link';
        if (tag === 'button') return 'button';
        if (tag === 'select') return 'select';
        if (tag === 'textarea') return 'textarea';
        if (tag === 'input') return 'input/' + (el.type || 'text');
        const role = el.getAttribute('role');
        if (role) return role;
        return tag;
    }

    function isVisible(el) {
        const style = window.getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden') return false;
        if (parseFloat(style.opacity) < 0.1) return false;
        return true;
    }

    const seen = new Set();
    const results = [];

    document.querySelectorAll(SELECTORS).forEach(el => {
        if (!isVisible(el)) return;
        const rect = el.getBoundingClientRect();
        // Must have size and be at least partially in viewport
        if (rect.width < 2 || rect.height < 2) return;
        if (rect.bottom < 0 || rect.top > vh || rect.right < 0 || rect.left > vw) return;

        // Deduplicate by position+tag
        const key = el.tagName + '|' + Math.round(rect.top) + '|' + Math.round(rect.left);
        if (seen.has(key)) return;
        seen.add(key);

        const type = getType(el);
        const label = getLabel(el);
        const href = el.href || '';
        const value = el.value || '';

        // Build a stable selector to find this element again
        // Use index in querySelectorAll result as a positional reference
        const allMatching = Array.from(document.querySelectorAll(SELECTORS));
        const posIndex = allMatching.indexOf(el);

        results.push({
            type,
            label,
            href: href.slice(0, 120),
            value: value.slice(0, 80),
            tag: el.tagName.toLowerCase(),
            pos_index: posIndex,
            rect: { top: Math.round(rect.top), left: Math.round(rect.left), w: Math.round(rect.width), h: Math.round(rect.height) }
        });

        if (results.length >= 80) return;
    });

    return results;
}
"""

_CLICK_BY_POS_JS = """
(pos_index) => {
    const SELECTORS = 'a[href], button, input:not([type="hidden"]), select, textarea, [role="button"], [role="link"], [role="checkbox"], [role="radio"], [role="combobox"], [role="menuitem"], [role="tab"], [tabindex]:not([tabindex="-1"])';
    const els = document.querySelectorAll(SELECTORS);
    const el = els[pos_index];
    if (!el) return false;
    el.scrollIntoView({block: 'center', behavior: 'instant'});
    el.focus();
    el.click();
    return true;
}
"""

_INPUT_BY_POS_JS = """
([pos_index, text]) => {
    const SELECTORS = 'a[href], button, input:not([type="hidden"]), select, textarea, [role="button"], [role="link"], [role="checkbox"], [role="radio"], [role="combobox"], [role="menuitem"], [role="tab"], [tabindex]:not([tabindex="-1"])';
    const els = document.querySelectorAll(SELECTORS);
    const el = els[pos_index];
    if (!el) return false;
    el.scrollIntoView({block: 'center', behavior: 'instant'});
    el.focus();
    el.value = '';
    el.dispatchEvent(new Event('input', {bubbles: true}));
    return true;
}
"""


async def _collect_elements(page) -> list[dict]:
    global _element_index
    try:
        elements = await page.evaluate(_COLLECT_ELEMENTS_JS)
        _element_index = {i: e for i, e in enumerate(elements)}
        return elements
    except Exception:
        _element_index = {}
        return []


def _format_elements(elements: list[dict]) -> str:
    if not elements:
        return ''
    lines = ['', '--- Interactive Elements ---']
    for i, el in enumerate(elements):
        t = el['type']
        label = el['label']
        href = el.get('href', '')
        value = el.get('value', '')

        if t == 'link':
            part = f'[{i}] link "{label}"'
            if href:
                part += f' → {href}'
        elif t.startswith('input/'):
            itype = t.split('/')[1]
            part = f'[{i}] input ({itype})'
            if label:
                part += f' "{label}"'
            if value:
                part += f' value="{value}"'
        elif t == 'select':
            part = f'[{i}] select "{label}"'
        elif t == 'textarea':
            part = f'[{i}] textarea "{label}"'
        else:
            part = f'[{i}] {t} "{label}"'
        lines.append(part)
    return '\n'.join(lines)


async def _page_state(page) -> str:
    url = page.url
    title = await page.title()

    try:
        text = await page.evaluate('() => document.body ? document.body.innerText : ""')
        text = text.strip()[:6000]
    except Exception:
        text = ''

    elements = await _collect_elements(page)
    elem_str = _format_elements(elements)

    parts = [f'URL: {url}', f'Title: {title}', '', text]
    if elem_str:
        parts.append(elem_str)
    return '\n'.join(parts)


# ── Actions ───────────────────────────────────────────────────────────────────

async def _do_navigate(url: str) -> str:
    page = await _ensure_page()
    await page.goto(url, wait_until='domcontentloaded', timeout=20000)
    await page.wait_for_timeout(800)
    return await _page_state(page)


async def _do_get_state() -> str:
    page = await _ensure_page()
    return await _page_state(page)


async def _do_click(index: int) -> str:
    page = await _ensure_page()
    el = _element_index.get(index)
    if el is None:
        return f'Error: no element at index [{index}]. Use get_state to refresh.'

    ok = await page.evaluate(_CLICK_BY_POS_JS, el['pos_index'])
    if not ok:
        return f'Error: element [{index}] not found in DOM (page may have changed).'

    await page.wait_for_timeout(800)
    return f'Clicked [{index}].\n\n' + await _page_state(page)


async def _do_input(index: int, text: str) -> str:
    page = await _ensure_page()
    el = _element_index.get(index)
    if el is None:
        return f'Error: no element at index [{index}]. Use get_state to refresh.'

    # Focus + clear via JS, then type via keyboard
    await page.evaluate(_INPUT_BY_POS_JS, [el['pos_index'], text])
    await page.keyboard.type(text, delay=20)
    await page.wait_for_timeout(300)
    return f'Typed "{text[:60]}" into [{index}].'


async def _do_screenshot() -> bytes:
    page = await _ensure_page()
    return await page.screenshot(full_page=False)


async def _do_click_selector(selector: str) -> str:
    page = await _ensure_page()
    locator = page.locator(selector).first
    await locator.wait_for(state='visible', timeout=10000)
    await locator.scroll_into_view_if_needed(timeout=5000)
    await locator.click(timeout=10000)
    await page.wait_for_timeout(800)
    return f'Clicked selector {selector}.\n\n' + await _page_state(page)


async def _do_input_selector(selector: str, text: str) -> str:
    page = await _ensure_page()
    locator = page.locator(selector).first
    await locator.wait_for(state='visible', timeout=10000)
    await locator.scroll_into_view_if_needed(timeout=5000)
    await locator.fill(text, timeout=10000)
    await page.wait_for_timeout(300)
    return f'Filled selector {selector}.'


async def _do_assert_text(expected_text: str) -> str:
    page = await _ensure_page()
    state = await _page_state(page)
    if expected_text and expected_text in state:
        return f'Assertion passed: found text {expected_text!r}.\n\n' + state
    return f'Error: expected text not found: {expected_text!r}.\n\n' + state


async def _do_assert_selector(selector: str) -> str:
    page = await _ensure_page()
    locator = page.locator(selector).first
    count = await locator.count()
    if count <= 0:
        return f'Error: selector not found: {selector}'
    await locator.wait_for(state='attached', timeout=5000)
    return f'Assertion passed: selector exists {selector}.'


async def _do_scroll(direction: str) -> str:
    page = await _ensure_page()
    delta = 600 if direction == 'down' else -600
    await page.evaluate(f'window.scrollBy({{top: {delta}, behavior: "smooth"}})')
    await page.wait_for_timeout(600)
    return f'Scrolled {direction}.\n\n' + await _page_state(page)


async def _do_go_back() -> str:
    page = await _ensure_page()
    await page.go_back(wait_until='domcontentloaded', timeout=15000)
    await page.wait_for_timeout(800)
    return 'Went back.\n\n' + await _page_state(page)


# ── Tool definition ───────────────────────────────────────────────────────────

BROWSER_TOOL_DEF = {
    'name': 'Browser',
    'description': (
        'Control a web browser with full JavaScript rendering. Navigate pages, click elements, '
        'type text, take screenshots. Uses Playwright Chromium. '
        'After navigating, interactive elements are listed with indices [0], [1], etc. '
        'Actions: navigate, click, input, screenshot, scroll, go_back, wait, get_state.'
    ),
    'input_schema': {
        'type': 'object',
        'properties': {
            'action': {
                'type': 'string',
                'enum': ['navigate', 'click', 'input', 'screenshot',
                         'scroll', 'go_back', 'wait', 'get_state',
                         'click_selector', 'input_selector', 'assert_text', 'assert_selector'],
            },
            'url': {'type': 'string', 'description': 'URL for navigate.'},
            'index': {'type': 'number', 'description': 'Element index for click/input.'},
            'selector': {'type': 'string', 'description': 'CSS selector for selector-based actions.'},
            'text': {'type': 'string', 'description': 'Text for input/assert action.'},
            'direction': {'type': 'string', 'enum': ['up', 'down']},
            'seconds': {'type': 'number', 'description': 'Wait duration.'},
        },
        'required': ['action'],
    },
}


def execute_browser(params: dict, ctx: ToolContext) -> ToolResult:
    """Execute a browser action."""
    action = str(params.get('action', '')).strip().lower()

    # Update module-level session context so _ensure_page picks up the right settings
    global _session_id_ctx, _state_dir_ctx
    _session_id_ctx = ctx.agent_id or ''
    _state_dir_ctx = ctx.state_dir

    try:
        if action == 'navigate':
            url = str(params.get('url', '')).strip()
            if not url:
                return ToolResult(content='Error: url is required.', is_error=True)
            result = _run(_do_navigate(url), timeout=25)
            return ToolResult(content=result[:15000])

        if action == 'get_state':
            return ToolResult(content=_run(_do_get_state())[:15000])

        if action == 'click':
            index = params.get('index')
            if index is None:
                return ToolResult(content='Error: index is required.', is_error=True)
            return ToolResult(content=_run(_do_click(int(index)))[:12000])

        if action == 'click_selector':
            selector = str(params.get('selector', '')).strip()
            if not selector:
                return ToolResult(content='Error: selector is required.', is_error=True)
            return ToolResult(content=_run(_do_click_selector(selector))[:12000])

        if action == 'input':
            index = params.get('index')
            if index is None:
                return ToolResult(content='Error: index is required.', is_error=True)
            return ToolResult(content=_run(_do_input(int(index), str(params.get('text', '')))))

        if action == 'input_selector':
            selector = str(params.get('selector', '')).strip()
            if not selector:
                return ToolResult(content='Error: selector is required.', is_error=True)
            return ToolResult(content=_run(_do_input_selector(selector, str(params.get('text', '')))))

        if action == 'screenshot':
            img_bytes = _run(_do_screenshot())
            tag = secrets.token_hex(4)
            tmp = Path(f'/tmp/charon-ss-{tag}.png')
            tmp.write_bytes(img_bytes)
            return ToolResult(content=f'Screenshot saved: {tmp} ({len(img_bytes)} bytes)')

        if action == 'scroll':
            direction = str(params.get('direction', 'down')).lower()
            return ToolResult(content=_run(_do_scroll(direction))[:12000])

        if action == 'go_back':
            return ToolResult(content=_run(_do_go_back())[:12000])

        if action == 'wait':
            seconds = min(int(params.get('seconds', 2)), 30)
            _run(asyncio.sleep(seconds), timeout=seconds + 5)
            return ToolResult(content=f'Waited {seconds}s.')

        if action == 'assert_text':
            text = str(params.get('text', '')).strip()
            if not text:
                return ToolResult(content='Error: text is required.', is_error=True)
            content = _run(_do_assert_text(text))
            return ToolResult(content=content[:15000], is_error=content.lower().startswith('error:'))

        if action == 'assert_selector':
            selector = str(params.get('selector', '')).strip()
            if not selector:
                return ToolResult(content='Error: selector is required.', is_error=True)
            content = _run(_do_assert_selector(selector))
            return ToolResult(content=content[:12000], is_error=content.lower().startswith('error:'))

        return ToolResult(content=f'Unknown action: {action}', is_error=True)

    except Exception as e:
        return ToolResult(content=f'Browser error: {e}', is_error=True)
