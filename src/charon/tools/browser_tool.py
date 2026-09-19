"""Browser tool — cleanroom Playwright-only browser automation.

Zero external deps beyond playwright. No telemetry. One local Chromium,
one dedicated event-loop thread, one file.

- Navigation, click, input, scroll, go_back, wait, screenshot, get_state
- Selector-based click/input/assert helpers (searched across every frame)
- Stable element refs: each interactive element is tagged in-page with an id
  (``e12``; ``f1.e4`` inside frame 1) that is bound to the DOM node, not to
  its position, so a ref survives reflow, lazy-loaded rows and list mutation.
  The legacy numeric ``index`` still works but is positional in the *last*
  listing — prefer ``ref``.
- Every frame (including cross-origin iframes) is walked, not just the top
  document; the snapshot is one round trip per frame and O(n).
- Dialogs (alert/confirm/prompt/beforeunload) never block: they are answered
  by policy (default: alerts accepted, everything else dismissed — the same
  outcome Playwright gave silently before) and *reported* in the next state so
  the agent can change policy and retry.
- Vision fallback: when the DOM walk finds nothing usable (canvas apps,
  closed shadow roots, custom widgets) a screenshot is described by the
  configured multimodal provider into a fixed JSON shape; the resulting
  ``v1``/``v2`` refs are clickable by coordinate.
"""
from __future__ import annotations

import asyncio
import base64
import importlib.util
import inspect
import json
import os
import re
import secrets
import threading
import time
from collections import deque
from pathlib import Path
from urllib.parse import urlsplit

from charon.tools import ToolContext, ToolResult
from charon.infra import config

try:
    from charon.infra.diagnostics import record as _diag
except Exception:  # diagnostics is best-effort and must never block import
    def _diag(*args, **kwargs):
        return None

# ── Dedicated browser thread ─────────────────────────────────────────────────

_loop: asyncio.AbstractEventLoop | None = None
_thread: threading.Thread | None = None
_ready = threading.Event()
_thread_start_lock = threading.Lock()


def _browser_thread_main():
    global _loop
    _loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_loop)
    # Signal readiness from inside the running loop.  Setting the event before
    # run_forever() leaves a small window where callers can submit work to a
    # loop that has not started yet.
    _loop.call_soon(_ready.set)
    _loop.run_forever()


def _ensure_thread() -> asyncio.AbstractEventLoop:
    global _loop, _thread

    # Multiple agents may invoke Browser at the same time.  Serialize only
    # thread creation; every caller still waits for the same readiness event.
    # Previously, a second caller could observe an alive thread and return
    # while `_loop` was still None, abandoning its newly-created coroutine.
    with _thread_start_lock:
        if _thread is None or not _thread.is_alive():
            _ready.clear()
            _loop = None
            _thread = threading.Thread(target=_browser_thread_main, daemon=True)
            _thread.start()

    if not _ready.wait(timeout=5):
        raise RuntimeError('Browser event loop did not start within 5 seconds.')

    loop = _loop
    if loop is None or loop.is_closed() or not loop.is_running():
        raise RuntimeError('Browser event loop is unavailable.')
    return loop


def _run(coro, timeout: int = 30):
    try:
        loop = _ensure_thread()
        future = asyncio.run_coroutine_threadsafe(coro, loop)
    except BaseException:
        # If startup/submission fails, the loop never owns this coroutine.
        # Close it explicitly so Python does not emit "was never awaited".
        close = getattr(coro, 'close', None)
        if callable(close):
            close()
        raise
    return future.result(timeout=timeout)


# ── Playwright state ──────────────────────────────────────────────────────────

_pw = None
_browser = None
_page = None
_context = None

# All public calls are serialized, including approval and session transitions.
_call_lock = threading.RLock()
_attached: dict | None = None
_cdp = None
_cdp_events: deque = deque(maxlen=100)
_console_contexts: set[int] = set()

# No caller-supplied protocol method, target ID, JS, or arbitrary parameters.
CDP_OPERATIONS = {
    'dom_document', 'network_status', 'console_messages', 'downloads',
    'dialogs', 'dialog_policy',
}
ATTACHED_READ_ACTIONS = {
    'get_state', 'assert_text', 'assert_selector', 'screenshot', 'vision', 'wait',
}


def _origin(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('Only HTTP(S) origins without credentials are allowed.')
    host = parsed.hostname.lower()
    if ':' in host:
        host = f'[{host}]'
    port = parsed.port if parsed.port is not None else (443 if parsed.scheme == 'https' else 80)
    return f'{parsed.scheme}://{host}:{port}'


def _allowed_origins() -> frozenset[str]:
    # Operator configuration, never model-supplied tool arguments.
    values = os.environ.get('CHARON_BROWSER_ALLOWED_ORIGINS', '').split(',')
    origins = set()
    for value in values:
        value = value.strip()
        if not value:
            continue
        parsed = urlsplit(value)
        if parsed.path not in ('', '/') or parsed.query or parsed.fragment:
            raise ValueError('CHARON_BROWSER_ALLOWED_ORIGINS must contain exact origins.')
        origins.add(_origin(value))
    if not origins:
        raise ValueError('Set CHARON_BROWSER_ALLOWED_ORIGINS before attaching.')
    return frozenset(origins)


def _url_allowed(url: str, origins) -> bool:
    try:
        return _origin(url) in origins
    except ValueError:
        return False


def attached_scope_error(params: dict, ctx: ToolContext) -> str | None:
    """Trusted context grants cannot be supplied through Browser arguments."""
    action = str(params.get('action', '')).strip().lower()
    scoped = bool(ctx.scope or ctx.frozen or ctx.parent_agent_id or ctx.topology_depth)
    if action == 'attach' and scoped:
        return 'Scope violation: shades cannot attach; the owner must grant an existing browser session.'
    if _attached:
        session = _attached['session_id']
        granted = session in (ctx.browser_session_grants or [])
        if (scoped and not granted) or (not scoped and ctx.agent_id != _attached['owner'] and not granted):
            return 'Scope violation: attached browser session was not granted to this caller.'
        if params.get('session_id') != session:
            return 'Scope violation: attached calls require the current browser session_id.'
    return None


def attached_action_requires_approval(params: dict) -> bool:
    action = str(params.get('action', '')).strip().lower()
    if action == 'attach':
        return True
    if not _attached:
        return False
    if action == 'cdp':
        return params.get('operation') == 'dialog_policy'
    return action not in ATTACHED_READ_ACTIONS


def _guard_attached_page(page) -> None:
    if not _attached:
        return
    if page.is_closed() or not _browser or not _browser.is_connected():
        raise ValueError('Attached page disconnected; detach before launching or reattaching.')
    # Fail closed on mixed-origin pages, including screenshots and vision.
    for frame in page.frames:
        if not frame.is_detached() and not _url_allowed(frame.url, _attached['origins']):
            raise ValueError('Domain allowlist refused the page or one of its frames.')


async def _attached_request(event: dict) -> None:
    client = _cdp
    if client is None:
        return
    method = 'Fetch.continueRequest' if _attached and _url_allowed(event['request']['url'], _attached['origins']) else 'Fetch.failRequest'
    args = {'requestId': event['requestId']}
    if method == 'Fetch.failRequest':
        args['errorReason'] = 'BlockedByClient'
    try:
        await client.send(method, args)
    except Exception:
        pass  # Target may have closed while a request was paused.


def _console_context(event: dict) -> None:
    context = event['context']
    if _attached and _url_allowed(context.get('origin', ''), _attached['origins']):
        _console_contexts.add(context['id'])


def _console_message(event: dict) -> None:
    if event.get('executionContextId') in _console_contexts:
        values = [str(arg.get('value', ''))[:500] for arg in event.get('args', [])[:10]
                  if arg.get('type') in {'string', 'number', 'boolean'}]
        _on_cdp_event('console', {'source': 'console-api', 'level': event.get('type', ''), 'text': ' '.join(values)})


def _on_cdp_event(kind: str, event: dict) -> None:
    try:
        _guard_attached_page(_page)
        entry = event.get('entry', event)
        url = entry.get('url')
        if url and not _url_allowed(url, _attached['origins']):
            return
        # Bounded metadata only. No remote objects, stacks, bodies or headers.
        keys = {
            'console': ('source', 'level', 'text'),
            'download': ('suggestedFilename',),
            'dialog': ('type', 'message'),
        }[kind]
        _cdp_events.append({'kind': kind, **{k: str(entry[k])[:1000] for k in keys if k in entry}})
    except Exception:
        return


async def _attach(params: dict, ctx: ToolContext) -> str:
    global _pw, _browser, _context, _page, _attached, _cdp
    if _browser is not None or _attached:
        raise ValueError('Close or detach the current browser before attaching.')
    origins = _allowed_origins()
    endpoint = str(params.get('endpoint', ''))
    parsed = urlsplit(endpoint)
    # Local CDP endpoints only; do not send credentials or allow arbitrary SSRF.
    if parsed.scheme != 'http' or parsed.hostname not in ('localhost', '127.0.0.1', '::1') or parsed.username or parsed.password or parsed.path not in ('', '/') or parsed.query or parsed.fragment:
        raise ValueError('endpoint must be a loopback HTTP CDP endpoint without credentials or path.')
    target_url = str(params.get('url', ''))
    if not _url_allowed(target_url, origins):
        raise ValueError('Domain allowlist refused the requested page.')
    from playwright.async_api import async_playwright
    _pw = await async_playwright().start()
    try:
        _browser = await _pw.chromium.connect_over_cdp(endpoint, timeout=15000)
        matches = [(context, page) for context in _browser.contexts for page in context.pages if page.url == target_url]
        if len(matches) != 1:
            raise ValueError('url must identify exactly one existing page; no page was selected.')
        _context, _page = matches[0]
        _attached = {'session_id': secrets.token_hex(16), 'owner': ctx.agent_id, 'origins': origins,
                     'previous_policy': dict(_dialog_policy)}
        _guard_attached_page(_page)
        _cdp = await _context.new_cdp_session(_page)
        _cdp.on('Fetch.requestPaused', _attached_request)
        await _cdp.send('Fetch.enable', {'patterns': [{'urlPattern': '*', 'requestStage': 'Request'}]})
        _cdp.on('Runtime.executionContextCreated', _console_context)
        _cdp.on('Runtime.executionContextDestroyed', lambda event: _console_contexts.discard(event['executionContextId']))
        _cdp.on('Runtime.executionContextsCleared', lambda _: _console_contexts.clear())
        _cdp.on('Runtime.consoleAPICalled', _console_message)
        await _cdp.send('Runtime.enable')
        _cdp.on('Log.entryAdded', lambda event: _on_cdp_event('console', event))
        _cdp.on('Page.downloadWillBegin', lambda event: _on_cdp_event('download', event))
        _cdp.on('Page.javascriptDialogOpening', lambda event: _on_cdp_event('dialog', event))
        await _cdp.send('Log.enable')
        await _cdp.send('Page.enable')
        _reset_identity()
        _dialog_log.clear()
        _dialogs_unreported.clear()
        _cdp_events.clear()
        _set_dialog_policy('dismiss', '')
        _page.on('dialog', _on_dialog)
        return json.dumps({'session_id': _attached['session_id'], 'origin': _origin(_page.url), 'mode': 'attached'})
    except BaseException:
        await _detach()
        raise


async def _detach() -> None:
    global _pw, _browser, _context, _page, _attached, _cdp
    try:
        if _page is not None:
            _page.remove_listener('dialog', _on_dialog)
        if _cdp is not None:
            await _cdp.send('Fetch.disable')
            await _cdp.detach()
    except Exception as exc:
        _diag('browser_tool', 'attached target already disconnected during detach',
              state_dir=_state_dir_ctx, error=type(exc).__name__)
    finally:
        # Stopping our Playwright transport disconnects; never close the user's
        # context, tabs or browser. Also used to clean up failed attachment.
        try:
            if _pw is not None:
                await _pw.stop()
        finally:
            if _attached:
                _dialog_policy.clear()
                _dialog_policy.update(_attached['previous_policy'])
            _pw = _browser = _context = _page = _attached = _cdp = None
            _console_contexts.clear()
            _reset_identity()
            _cdp_events.clear()
            _dialog_log.clear()
            _dialogs_unreported.clear()


async def _do_cdp(params: dict) -> str:
    if not _attached or _cdp is None:
        raise ValueError('CDP requires an explicitly attached session.')
    _guard_attached_page(_page)
    operation = params.get('operation')
    if operation not in CDP_OPERATIONS:
        raise ValueError('CDP operation is not allowlisted.')
    allowed_keys = {'action', 'session_id', 'operation'}
    if operation == 'dialog_policy':
        allowed_keys |= {'policy', 'text'}
    if set(params) - allowed_keys:
        raise ValueError('Unexpected CDP arguments; arbitrary protocol parameters are refused.')
    if operation == 'dom_document':
        result = await _cdp.send('DOM.getDocument', {'depth': 1, 'pierce': False})
    elif operation == 'network_status':
        result = await _cdp.send('Network.getSecurityIsolationStatus')
    elif operation == 'dialog_policy':
        return _set_dialog_policy(str(params.get('policy', '')), str(params.get('text', '')))
    else:
        kind = {'console_messages': 'console', 'downloads': 'download', 'dialogs': 'dialog'}[operation]
        result = [e for e in _cdp_events if e['kind'] == kind]
    _guard_attached_page(_page)
    return json.dumps(result)[:15000]


# ── Browser visibility context (set per execute_browser call) ─────────────────
_session_id_ctx: str = ''
_state_dir_ctx = None

_BROWSER_INSTALL_ERROR = (
    "Browser is unavailable because Playwright is not installed. "
    "Install it with `pip install 'charon[browser]'`, then run "
    "`playwright install chromium`."
)


def _playwright_available() -> bool:
    try:
        return importlib.util.find_spec('playwright') is not None
    except (ImportError, ValueError):
        return False

# ── Element identity ──────────────────────────────────────────────────────────
# ref ('e12' / 'f1.e4') → {'frame': Frame, 'id': 'e12', 'token': doc token, ...}
_refs: dict[str, dict] = {}
# Legacy positional map: int → the same entries, in last-listing order.
_element_index: dict[int, dict] = {}
# Frame → small stable number (1, 2, …) for the lifetime of the Frame object.
_frame_numbers: dict = {}
_frame_counter = 0
# Next element id for a *new* document, so 'e1' never means two things in one session.
_ref_seed = 1
# Per-frame snapshot cache: frame → {'token', 'scroll_y', 'data'}
_snapshot_cache: dict = {}
# Monotonic counters so tests and the bench can tell a cache hit from a walk.
_stats: dict[str, int] = {'walks': 0, 'cache_hits': 0}

# ── Dialogs ───────────────────────────────────────────────────────────────────
_DIALOG_KINDS = ('confirm', 'prompt', 'beforeunload')
_dialog_policy: dict = {'confirm': 'dismiss', 'prompt': 'dismiss', 'beforeunload': 'dismiss', 'prompt_text': ''}
_dialog_log: deque = deque(maxlen=20)
_dialogs_unreported: list[dict] = []

# ── Vision fallback ───────────────────────────────────────────────────────────
# Injectable: (png_bytes, question, schema, state_dir) -> dict, sync or async.
# None means "use the configured provider" (see _provider_vision_backend).
_vision_backend = None
_vision_refs: dict[str, dict] = {}   # 'v1' → {'x', 'y', 'label', 'kind'}

VISION_SCHEMA = {
    'type': 'object',
    'properties': {
        'summary': {'type': 'string', 'description': 'One or two sentences: what the page shows.'},
        'answer': {'type': 'string', 'description': 'Answer to the question, if one was asked.'},
        'elements': {
            'type': 'array',
            'items': {
                'type': 'object',
                'properties': {
                    'label': {'type': 'string'},
                    'kind': {'type': 'string', 'description': 'button, link, input, tab, menu, canvas-control, text, other'},
                    'x': {'type': 'number', 'description': 'centre x in image pixels'},
                    'y': {'type': 'number', 'description': 'centre y in image pixels'},
                },
                'required': ['label', 'x', 'y'],
            },
        },
    },
    'required': ['summary', 'elements'],
}


class VisionUnavailable(RuntimeError):
    """The vision fallback could not run; the reason is the message."""


async def _on_dialog(dialog) -> None:
    kind = dialog.type
    choice = 'accept' if kind == 'alert' else str(_dialog_policy.get(kind, 'dismiss'))
    prompt_text = _dialog_policy.get('prompt_text') or ''
    try:
        if choice == 'accept':
            if kind == 'prompt':
                await dialog.accept(prompt_text)
            else:
                await dialog.accept()
        else:
            await dialog.dismiss()
    except Exception as e:
        _diag('browser_tool', 'dialog auto-response failed; page JS may stay blocked', error=e, kind=kind)
    entry = {'type': kind, 'message': dialog.message, 'response': choice,
             'prompt_text': prompt_text if (kind == 'prompt' and choice == 'accept') else '',
             'ts': time.time()}
    _dialog_log.append(entry)
    _dialogs_unreported.append(entry)


async def _ensure_page():
    global _pw, _browser, _page, _context

    if _attached:
        _guard_attached_page(_page)
        return _page

    if _page is not None and not _page.is_closed() and _browser is not None and _browser.is_connected():
        return _page

    from playwright.async_api import async_playwright
    try:
        from charon.providers.browser_settings import should_show_browser
        headless = not should_show_browser(_session_id_ctx, _state_dir_ctx)
    except Exception as e:
        _diag('browser_tool', 'headless-flag resolution failed; falling back to config default', error=e)
        headless = config.browser_headless()

    if _pw is None:
        _pw = await async_playwright().start()
    if _browser is None or not _browser.is_connected():
        _browser = await _pw.chromium.launch(headless=headless)
        _context = None
    if _context is None:
        _context = await _browser.new_context(
            viewport={'width': 1280, 'height': 800},
            user_agent=(
                'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 '
                '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            ),
        )
    _page = await _context.new_page()
    _page.on('dialog', _on_dialog)
    _reset_identity()
    return _page


async def _shutdown() -> None:
    """Close the browser and forget every ref. Safe to call when nothing is open."""
    global _pw, _browser, _page, _context
    if _attached:
        await _detach()
        return
    for closer in ((_context, 'close'), (_browser, 'close'), (_pw, 'stop')):
        obj, meth = closer
        if obj is not None:
            try:
                await getattr(obj, meth)()
            except Exception as e:
                _diag('browser_tool', f'browser shutdown step {meth} failed', error=e)
    _pw = _browser = _page = _context = None
    _reset_identity()


def _reset_identity() -> None:
    global _frame_counter, _ref_seed
    _refs.clear()
    _element_index.clear()
    _frame_numbers.clear()
    _snapshot_cache.clear()
    _vision_refs.clear()
    _frame_counter = 0
    _ref_seed = 1


# ── Interactive element snapshot ─────────────────────────────────────────────

_SELECTORS_JS = (
    'a[href], button, input:not([type="hidden"]), select, textarea, [role="button"], '
    '[role="link"], [role="checkbox"], [role="radio"], [role="combobox"], [role="menuitem"], '
    '[role="tab"], [tabindex]:not([tabindex="-1"])'
)

# One evaluate per frame. Installs (once per document) a registry that binds
# ids to element objects — a WeakMap for element→id and WeakRefs for id→element,
# so ids are stable across reflow/mutation and detached nodes are collectable —
# plus a MutationObserver that marks the document dirty, which is what lets
# get_state skip the walk when nothing changed.
_SNAPSHOT_JS = """
(opts) => {
    const SELECTORS = %s;
    const W = window;
    let reg = W.__charon_reg;
    if (!reg || reg.doc !== document) {
        reg = W.__charon_reg = {
            doc: document,
            token: Math.random().toString(36).slice(2, 10),
            next: opts.seed || 1,
            byEl: new WeakMap(),
            byId: new Map(),
            dirty: true,
            root: document.documentElement,
            href: location.href,
        };
        try {
            // Observe the Document node, not documentElement: document.open()/
            // set_content replace the root while keeping window and document.
            const mo = new MutationObserver(() => { reg.dirty = true; });
            mo.observe(document, {childList: true, subtree: true, attributes: true, characterData: true});
            reg.observer = mo;
        } catch (e) {}
    }
    const vw = W.innerWidth, vh = W.innerHeight;

    function getLabel(el) {
        if (el.getAttribute('aria-label')) return el.getAttribute('aria-label').trim();
        if (el.getAttribute('title')) return el.getAttribute('title').trim();
        if (el.getAttribute('placeholder')) return el.getAttribute('placeholder').trim();
        if (el.getAttribute('alt')) return el.getAttribute('alt').trim();
        if (el.id) {
            const lbl = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
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
        return role ? role : tag;
    }
    function isVisible(el) {
        const style = W.getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden') return false;
        if (parseFloat(style.opacity) < 0.1) return false;
        return true;
    }

    const seen = new Set();
    const results = [];
    const nodes = document.querySelectorAll(SELECTORS);
    for (const el of nodes) {
        if (!isVisible(el)) continue;
        const rect = el.getBoundingClientRect();
        if (rect.width < 2 || rect.height < 2) continue;
        if (rect.bottom < 0 || rect.top > vh || rect.right < 0 || rect.left > vw) continue;
        const key = el.tagName + '|' + Math.round(rect.top) + '|' + Math.round(rect.left);
        if (seen.has(key)) continue;
        seen.add(key);
        let id = reg.byEl.get(el);
        if (!id) {
            id = 'e' + (reg.next++);
            reg.byEl.set(el, id);
            reg.byId.set(id, new WeakRef(el));
        }
        results.push({
            id, type: getType(el), label: getLabel(el),
            href: (el.href || '').slice(0, 120), value: (el.value || '').slice(0, 80),
            tag: el.tagName.toLowerCase(),
            rect: {top: Math.round(rect.top), left: Math.round(rect.left), w: Math.round(rect.width), h: Math.round(rect.height)},
        });
        if (results.length >= opts.max) break;
    }
    for (const [id, ref] of reg.byId) {
        const el = ref.deref();
        if (!el || !el.isConnected) reg.byId.delete(id);
    }
    let canvasRatio = 0;
    for (const c of document.querySelectorAll('canvas')) {
        const r = c.getBoundingClientRect();
        canvasRatio += Math.max(0, Math.min(r.right, vw) - Math.max(r.left, 0)) * Math.max(0, Math.min(r.bottom, vh) - Math.max(r.top, 0));
    }
    canvasRatio = vw * vh ? canvasRatio / (vw * vh) : 0;
    reg.dirty = false;
    reg.root = document.documentElement;
    reg.href = location.href;
    return {
        token: reg.token,
        next: reg.next,
        title: document.title,
        text: (document.body ? document.body.innerText : '').trim().slice(0, opts.textLimit),
        elements: results,
        scroll_y: W.scrollY,
        canvas_ratio: canvasRatio,
    };
}
""" % json.dumps(_SELECTORS_JS)

# Cheap freshness probe: is the last snapshot of this document still valid?
_PROBE_JS = """
() => {
    const r = window.__charon_reg;
    if (!r || r.doc !== document) return null;
    const changed = !!r.dirty || r.root !== document.documentElement || r.href !== location.href;
    return {token: r.token, dirty: changed, scroll_y: window.scrollY};
}
"""

_RESOLVE_JS = """
(id) => {
    const r = window.__charon_reg;
    const w = r && r.byId.get(id);
    const el = w && w.deref();
    return (el && el.isConnected) ? el : null;
}
"""

_MAX_ELEMENTS = 80
_TEXT_LIMIT = 6000
_FRAME_TEXT_LIMIT = 1500


def _frame_number(frame) -> int:
    global _frame_counter
    n = _frame_numbers.get(frame)
    if n is None:
        _frame_counter += 1
        n = _frame_counter
        _frame_numbers[frame] = n
    return n


async def _snapshot_frame(frame, *, is_main: bool, fresh: bool) -> dict | None:
    """Snapshot one frame, reusing the cached walk when the document is unchanged."""
    global _ref_seed
    text_limit = _TEXT_LIMIT if is_main else _FRAME_TEXT_LIMIT
    cached = _snapshot_cache.get(frame)
    if cached and not fresh:
        try:
            probe = await frame.evaluate(_PROBE_JS)
        except Exception:
            probe = None
        if probe and probe['token'] == cached['token'] and not probe['dirty'] and probe['scroll_y'] == cached['scroll_y']:
            _stats['cache_hits'] += 1
            return cached['data']
    try:
        data = await frame.evaluate(_SNAPSHOT_JS, {'max': _MAX_ELEMENTS, 'textLimit': text_limit, 'seed': _ref_seed})
    except Exception as exc:
        if is_main:
            _diag('browser_tool', 'interactive-element collection failed; page state lists no elements', error=exc)
        return None
    _ref_seed = max(_ref_seed, int(data.get('next') or 1))
    _stats['walks'] += 1
    _snapshot_cache[frame] = {'token': data['token'], 'scroll_y': data['scroll_y'], 'data': data}
    return data


async def _collect(page, *, fresh: bool = False) -> dict:
    """Walk every frame; rebuild the ref maps; return {'main', 'frames'}."""
    _guard_attached_page(page)
    main = page.main_frame
    live_frames = set()
    result = {'main': None, 'frames': []}
    _refs.clear()
    _element_index.clear()
    order = 0
    for frame in page.frames:
        if frame.is_detached():
            continue
        _guard_attached_page(page)
        is_main = frame is main
        data = await _snapshot_frame(frame, is_main=is_main, fresh=fresh)
        live_frames.add(frame)
        if data is None:
            continue
        prefix = '' if is_main else f'f{_frame_number(frame)}.'
        if not is_main and not data['elements'] and not data['text']:
            continue
        for el in data['elements']:
            ref = prefix + el['id']
            entry = {'frame': frame, 'id': el['id'], 'token': data['token'], 'ref': ref, **el}
            _refs[ref] = entry
            _element_index[order] = entry
            order += 1
        if is_main:
            result['main'] = data
        else:
            result['frames'].append({'number': _frame_number(frame), 'url': frame.url, 'data': data, 'prefix': prefix})
    for frame in list(_snapshot_cache):
        if frame not in live_frames:
            _snapshot_cache.pop(frame, None)
            _frame_numbers.pop(frame, None)
    _guard_attached_page(page)
    return result


def _format_element(ref: str, el: dict) -> str:
    t = el['type']
    label = el['label']
    if t == 'link':
        part = f'[{ref}] link "{label}"'
        if el.get('href'):
            part += f' → {el["href"]}'
    elif t.startswith('input/'):
        part = f'[{ref}] input ({t.split("/")[1]})'
        if label:
            part += f' "{label}"'
        if el.get('value'):
            part += f' value="{el["value"]}"'
    else:
        part = f'[{ref}] {t} "{label}"'
    return part


def _format_dialogs() -> list[str]:
    if not _dialogs_unreported:
        return []
    lines = ['', '--- Dialogs (auto-handled) ---']
    for d in _dialogs_unreported:
        note = f'{d["type"]} "{d["message"][:120]}" → {d["response"]}ed'
        if d['prompt_text']:
            note += f' with {d["prompt_text"]!r}'
        if d['type'] in _DIALOG_KINDS:
            note += ' (policy). Use action dialog_policy to change this and retry.'
        lines.append(note)
    _dialogs_unreported.clear()
    return lines


async def _page_state(page, *, fresh: bool = False, allow_vision: bool = True) -> str:
    snap = await _collect(page, fresh=fresh)
    main = snap['main'] or {'title': '', 'text': '', 'elements': [], 'canvas_ratio': 0}
    parts = [f'URL: {page.url}', f'Title: {main["title"]}', '', main['text']]

    elem_lines = [_format_element(ref, el) for ref, el in _refs.items() if el['frame'] is page.main_frame]
    for fr in snap['frames']:
        tail = fr['url'][-60:]
        if fr['data']['text']:
            parts += ['', f'--- Frame f{fr["number"]} ({tail}) ---', fr['data']['text']]
        for el in fr['data']['elements']:
            elem_lines.append(_format_element(fr['prefix'] + el['id'], el) + f'  ⟨frame f{fr["number"]}⟩')
    if elem_lines:
        parts += ['', '--- Interactive Elements ---', *elem_lines]

    if allow_vision and not _refs and page.url != 'about:blank' and _vision_enabled():
        parts += ['', '--- Vision fallback (no interactive elements found in the DOM) ---']
        parts += await _vision_lines(page, question='')
    elif allow_vision and main.get('canvas_ratio', 0) >= 0.5 and _vision_enabled():
        parts += ['', f'--- Vision fallback (canvas covers {int(main["canvas_ratio"] * 100)}% of the viewport) ---']
        parts += await _vision_lines(page, question='')

    parts += _format_dialogs()
    return '\n'.join(parts)


# ── Ref resolution ────────────────────────────────────────────────────────────

def _normalise_ref(ref) -> str:
    return str(ref).strip().lstrip('@').strip('[]').strip()


def _lookup(params: dict) -> tuple[dict | None, str | None, str]:
    """Return (entry, vision_ref, error). Accepts ref (preferred) or legacy index."""
    ref = params.get('ref')
    if ref not in (None, ''):
        ref = _normalise_ref(ref)
        if ref in _vision_refs:
            return None, ref, ''
        entry = _refs.get(ref)
        if entry is None:
            return None, None, f'Error: unknown ref [{ref}]. Run get_state and use a ref from the listing.'
        return entry, None, ''
    index = params.get('index')
    if index is None:
        return None, None, 'Error: ref (or legacy index) is required.'
    try:
        entry = _element_index.get(int(index))
    except (TypeError, ValueError):
        return None, None, f'Error: index must be a number, got {index!r}.'
    if entry is None:
        return None, None, f'Error: no element at index [{index}]. Use get_state to refresh (prefer ref over index).'
    return entry, None, ''


async def _resolve_handle(entry: dict):
    frame = entry['frame']
    if frame.is_detached():
        return None, f'Error: ref [{entry["ref"]}] belongs to a frame that is gone. Run get_state.'
    try:
        handle = await frame.evaluate_handle(_RESOLVE_JS, entry['id'])
    except Exception as e:
        return None, f'Error: could not resolve ref [{entry["ref"]}] ({e}). Run get_state.'
    el = handle.as_element()
    if el is None:
        try:
            probe = await frame.evaluate(_PROBE_JS)
        except Exception:
            probe = None
        navigated = probe is None or probe.get('token') != entry['token']
        why = 'the page navigated' if navigated else 'the element left the DOM'
        return None, f'Error: ref [{entry["ref"]}] is stale — {why}. Run get_state for fresh refs.'
    return el, ''


# ── Vision ────────────────────────────────────────────────────────────────────

def _vision_enabled() -> bool:
    import os
    return os.environ.get('CHARON_BROWSER_VISION', '1') != '0'


def _extract_json(text: str) -> dict | None:
    text = text.strip()
    m = re.search(r'\{.*\}', text, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


async def _provider_vision_backend(png: bytes, question: str, schema: dict, state_dir) -> dict:
    """Describe a screenshot with the configured provider into `schema`.

    Gated on the adapter's own `supports_image_input` capability, not its
    family: an adapter that advertises it carries the image block below to
    the model as an image. One that does not would ship the base64 as prose,
    so refuse loudly rather than do that.
    """
    from charon.providers import Message
    from charon.providers.provider_bridge import create_provider_and_model
    from charon.providers.structured_output import schema_validation_errors

    provider, model, ready = create_provider_and_model(state_dir)
    if not ready:
        raise VisionUnavailable('no provider configured')
    if not getattr(provider, 'supports_image_input', False):
        raise VisionUnavailable(
            f'{type(provider).__name__} cannot carry images (it does not advertise '
            'supports_image_input); the vision fallback needs a provider that does'
        )
    model.supports_images = True

    ask = question.strip() or 'List every clickable or typeable control you can see.'
    prompt = (
        f'{ask}\n\nRespond with ONLY a JSON object matching this schema (coordinates are pixel '
        f'centres in the attached image):\n{json.dumps(schema)}'
    )
    messages = [Message(role='user', content=[
        {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png',
                                     'data': base64.b64encode(png).decode('ascii')}},
        {'type': 'text', 'text': prompt},
    ])]
    chunks: list[str] = []
    async for delta in provider.stream(
        messages=messages, model=model,
        system_prompt='You describe browser screenshots for an automation agent. Output ONLY JSON.',
        max_tokens=1500,
    ):
        if getattr(delta, 'type', '') == 'text':
            chunks.append(delta.text)
        elif getattr(delta, 'type', '') == 'error':
            raise VisionUnavailable(delta.error or 'provider error')
    data = _extract_json(''.join(chunks))
    if data is None:
        raise VisionUnavailable('provider returned no JSON object')
    errors = schema_validation_errors(data, schema)
    if errors:
        raise VisionUnavailable('vision answer did not match schema: ' + '; '.join(errors[:3]))
    return data


async def _run_vision(page, question: str) -> dict:
    png = await page.screenshot(full_page=False)
    dpr = await page.evaluate('() => window.devicePixelRatio || 1')
    backend = _vision_backend or _provider_vision_backend
    result = backend(png, question, VISION_SCHEMA, _state_dir_ctx)
    if inspect.isawaitable(result):
        result = await result
    if not isinstance(result, dict):
        raise VisionUnavailable('vision backend returned a non-dict')
    _vision_refs.clear()
    for i, el in enumerate(result.get('elements') or [], start=1):
        try:
            x, y = float(el['x']) / dpr, float(el['y']) / dpr
        except (KeyError, TypeError, ValueError):
            continue
        _vision_refs[f'v{i}'] = {'x': x, 'y': y, 'label': str(el.get('label', ''))[:80],
                                 'kind': str(el.get('kind', 'element'))[:30]}
    return result


async def _vision_lines(page, question: str) -> list[str]:
    try:
        result = await _run_vision(page, question)
    except VisionUnavailable as e:
        _diag('browser_tool', 'vision fallback unavailable; state has DOM info only', error=e)
        return [f'(vision fallback unavailable: {e})']
    except Exception as e:
        _diag('browser_tool', 'vision fallback failed; state has DOM info only', error=e)
        return [f'(vision fallback failed: {e})']
    lines = [str(result.get('summary', '')).strip()]
    if result.get('answer'):
        lines.append(f'Answer: {str(result["answer"]).strip()}')
    for ref, el in _vision_refs.items():
        lines.append(f'[{ref}] {el["kind"]} "{el["label"]}" at ({int(el["x"])}, {int(el["y"])}) — click with ref {ref}')
    if not _vision_refs:
        lines.append('(no clickable elements identified in the screenshot)')
    return lines


# ── Actions ───────────────────────────────────────────────────────────────────

async def _do_navigate(url: str) -> str:
    page = await _ensure_page()
    await page.goto(url, wait_until='domcontentloaded', timeout=20000)
    await page.wait_for_timeout(800)
    return await _page_state(page, fresh=True)


async def _do_get_state() -> str:
    page = await _ensure_page()
    return await _page_state(page)


async def _do_click(params: dict) -> str:
    page = await _ensure_page()
    entry, vref, err = _lookup(params)
    if err:
        return err
    if vref:
        v = _vision_refs[vref]
        await page.mouse.click(v['x'], v['y'])
        await page.wait_for_timeout(800)
        return f'Clicked [{vref}] {v["kind"]} "{v["label"]}" at ({int(v["x"])}, {int(v["y"])}).\n\n' + await _page_state(page, fresh=True)

    el, err = await _resolve_handle(entry)
    if err:
        return err
    how = ''
    try:
        await el.scroll_into_view_if_needed(timeout=3000)
        await el.click(timeout=5000)
    except Exception as e:
        # Playwright's actionability checks refused (covered, off-screen, detached
        # mid-way). Fall back to a DOM click so overlays and odd widgets still work.
        _diag('browser_tool', 'playwright click refused; used DOM click fallback', error=e, ref=entry['ref'])
        try:
            await el.evaluate('el => { el.scrollIntoView({block: "center", behavior: "instant"}); el.focus && el.focus(); el.click(); }')
            how = ' (DOM click fallback)'
        except Exception as e2:
            return f'Error: could not click [{entry["ref"]}]: {e2}'
    await page.wait_for_timeout(800)
    return f'Clicked [{entry["ref"]}] {entry["type"]} "{entry["label"]}"{how}.\n\n' + await _page_state(page, fresh=True)


async def _do_input(params: dict, text: str) -> str:
    page = await _ensure_page()
    entry, vref, err = _lookup(params)
    if err:
        return err
    if vref:
        v = _vision_refs[vref]
        await page.mouse.click(v['x'], v['y'])
        await page.keyboard.type(text, delay=20)
        await page.wait_for_timeout(300)
        return f'Typed "{text[:60]}" at [{vref}] ({int(v["x"])}, {int(v["y"])}).'
    el, err = await _resolve_handle(entry)
    if err:
        return err
    try:
        if entry['tag'] == 'select':
            await el.select_option(label=text)
        else:
            await el.fill(text, timeout=5000)
    except Exception as e:
        _diag('browser_tool', 'fill refused; used focus+type fallback', error=e, ref=entry['ref'])
        await el.evaluate('el => { el.scrollIntoView({block: "center", behavior: "instant"}); el.focus(); if ("value" in el) { el.value = ""; el.dispatchEvent(new Event("input", {bubbles: true})); } }')
        await page.keyboard.type(text, delay=20)
    await page.wait_for_timeout(300)
    return f'Typed "{text[:60]}" into [{entry["ref"]}] {entry["type"]} "{entry["label"]}".'


async def _do_click_at(x: float, y: float) -> str:
    page = await _ensure_page()
    await page.mouse.click(x, y)
    await page.wait_for_timeout(800)
    return f'Clicked at ({int(x)}, {int(y)}).\n\n' + await _page_state(page, fresh=True)


async def _do_vision(question: str) -> str:
    page = await _ensure_page()
    lines = await _vision_lines(page, question)
    return f'URL: {page.url}\n\n--- Vision ---\n' + '\n'.join(lines)


async def _do_screenshot() -> bytes:
    page = await _ensure_page()
    return await page.screenshot(full_page=False)


async def _find_locator(page, selector: str):
    """First frame (main first) where the selector matches; None if nowhere."""
    for frame in page.frames:
        if frame.is_detached():
            continue
        try:
            loc = frame.locator(selector).first
            if await loc.count() > 0:
                return loc, frame
        except Exception:
            continue
    return None, None


async def _do_click_selector(selector: str) -> str:
    page = await _ensure_page()
    locator, frame = await _find_locator(page, selector)
    if locator is None:
        locator = page.locator(selector).first   # let Playwright wait for it to appear
    await locator.wait_for(state='visible', timeout=10000)
    await locator.scroll_into_view_if_needed(timeout=5000)
    await locator.click(timeout=10000)
    await page.wait_for_timeout(800)
    where = '' if (frame is None or frame is page.main_frame) else f' (frame f{_frame_number(frame)})'
    return f'Clicked selector {selector}{where}.\n\n' + await _page_state(page, fresh=True)


async def _do_input_selector(selector: str, text: str) -> str:
    page = await _ensure_page()
    locator, _frame = await _find_locator(page, selector)
    if locator is None:
        locator = page.locator(selector).first
    await locator.wait_for(state='visible', timeout=10000)
    await locator.scroll_into_view_if_needed(timeout=5000)
    await locator.fill(text, timeout=10000)
    await page.wait_for_timeout(300)
    return f'Filled selector {selector}.'


async def _do_assert_text(expected_text: str) -> str:
    page = await _ensure_page()
    state = await _page_state(page, allow_vision=False)
    if expected_text and expected_text in state:
        return f'Assertion passed: found text {expected_text!r}.\n\n' + state
    return f'Error: expected text not found: {expected_text!r}.\n\n' + state


async def _do_assert_selector(selector: str) -> str:
    page = await _ensure_page()
    locator, _frame = await _find_locator(page, selector)
    if locator is None:
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
    return 'Went back.\n\n' + await _page_state(page, fresh=True)


def _set_dialog_policy(policy: str, prompt_text: str) -> str:
    policy = policy.strip().lower()
    if policy not in ('accept', 'dismiss'):
        return "Error: policy must be 'accept' or 'dismiss'."
    for kind in _DIALOG_KINDS:
        _dialog_policy[kind] = policy
    _dialog_policy['prompt_text'] = prompt_text
    extra = f' Prompts will be answered with {prompt_text!r}.' if (policy == 'accept' and prompt_text) else ''
    return f'Dialog policy: confirm/prompt/beforeunload → {policy}. Alerts are always accepted.{extra}'


# ── Tool definition ───────────────────────────────────────────────────────────

BROWSER_TOOL_DEF = {
    'name': 'Browser',
    'description': (
        'Control a web browser with full JavaScript rendering. Navigate pages, click elements, '
        'type text, take screenshots. Uses Playwright Chromium. '
        'After navigating, interactive elements are listed with stable refs like [e12] '
        '(or [f1.e3] inside frame 1); pass them as ref to click/input. Refs stay valid while the '
        'element exists, even if the page reflows or new rows are inserted. '
        'Elements inside iframes are included. Dialogs (confirm/prompt) are auto-answered by '
        'policy and reported; use dialog_policy to change it. When the DOM exposes nothing '
        'usable (canvas apps, custom widgets) a vision fallback describes the screenshot and '
        'gives coordinate refs [v1], [v2]. '
        'Actions: navigate, click, input, screenshot, scroll, go_back, wait, get_state, '
        'click_selector, input_selector, assert_text, assert_selector, vision, click_at, '
        'dialog_policy, close, attach, detach, cdp. Attach requires operator-configured '
        'CHARON_BROWSER_ALLOWED_ORIGINS, endpoint and exact existing page url. Attached calls '
        'require the returned session_id; CDP accepts only named operations.'
    ),
    'input_schema': {
        'type': 'object',
        'properties': {
            'action': {
                'type': 'string',
                'enum': ['navigate', 'click', 'input', 'screenshot',
                         'scroll', 'go_back', 'wait', 'get_state',
                         'click_selector', 'input_selector', 'assert_text', 'assert_selector',
                         'vision', 'click_at', 'dialog_policy', 'close', 'attach', 'detach', 'cdp'],
            },
            'url': {'type': 'string', 'description': 'URL for navigate or exact existing page URL for attach.'},
            'endpoint': {'type': 'string', 'description': 'attach: loopback HTTP CDP endpoint.'},
            'session_id': {'type': 'string', 'description': 'Required on every attached-session call.'},
            'operation': {'type': 'string', 'enum': sorted(CDP_OPERATIONS)},
            'ref': {'type': 'string', 'description': 'Element ref from the listing, e.g. "e12", "f1.e3" or "v2" (vision).'},
            'index': {'type': 'number', 'description': 'Legacy positional index from the last listing; prefer ref.'},
            'selector': {'type': 'string', 'description': 'CSS selector for selector-based actions (searched in every frame).'},
            'text': {'type': 'string', 'description': 'Text for input/assert_text; the question for vision; prompt answer for dialog_policy.'},
            'direction': {'type': 'string', 'enum': ['up', 'down']},
            'seconds': {'type': 'number', 'description': 'Wait duration.'},
            'x': {'type': 'number', 'description': 'click_at: x in CSS pixels.'},
            'y': {'type': 'number', 'description': 'click_at: y in CSS pixels.'},
            'policy': {'type': 'string', 'enum': ['accept', 'dismiss'],
                       'description': 'dialog_policy: how confirm/prompt/beforeunload dialogs are answered.'},
        },
        'required': ['action'],
    },
}


def _execute_browser(params: dict, ctx: ToolContext) -> ToolResult:
    """Execute a browser action."""
    action = str(params.get('action', '')).strip().lower()

    # The registry normally hides Browser when its extra is absent. Keep this
    # guard for direct imports and long-lived processes whose environment
    # changed after the registry was built.
    if not _playwright_available():
        return ToolResult(content=_BROWSER_INSTALL_ERROR, is_error=True)

    # Update module-level session context so _ensure_page picks up the right settings
    global _session_id_ctx, _state_dir_ctx
    _session_id_ctx = ctx.agent_id or ''
    _state_dir_ctx = ctx.state_dir

    try:
        if action == 'attach':
            return ToolResult(content=_run(_attach(params, ctx), timeout=60))
        if action == 'detach':
            if not _attached:
                return ToolResult(content='No attached browser.', is_error=True)
            _run(_detach())
            return ToolResult(content='Browser detached; user tabs remain open.')
        if action == 'cdp':
            content = _run(_do_cdp(params))
            return ToolResult(content=content, is_error=content.startswith('Error:'))
        if action == 'navigate':
            url = str(params.get('url', '')).strip()
            if not url:
                return ToolResult(content='Error: url is required.', is_error=True)
            result = _run(_do_navigate(url), timeout=60)
            return ToolResult(content=result[:15000])

        if action == 'get_state':
            return ToolResult(content=_run(_do_get_state(), timeout=60)[:15000])

        if action == 'click':
            if params.get('ref') in (None, '') and params.get('index') is None:
                return ToolResult(content='Error: ref (or legacy index) is required.', is_error=True)
            content = _run(_do_click(params), timeout=60)
            return ToolResult(content=content[:12000], is_error=content.startswith('Error:'))

        if action == 'click_selector':
            selector = str(params.get('selector', '')).strip()
            if not selector:
                return ToolResult(content='Error: selector is required.', is_error=True)
            return ToolResult(content=_run(_do_click_selector(selector), timeout=60)[:12000])

        if action == 'input':
            if params.get('ref') in (None, '') and params.get('index') is None:
                return ToolResult(content='Error: ref (or legacy index) is required.', is_error=True)
            content = _run(_do_input(params, str(params.get('text', ''))))
            return ToolResult(content=content, is_error=content.startswith('Error:'))

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

        if action == 'vision':
            return ToolResult(content=_run(_do_vision(str(params.get('text', ''))), timeout=120)[:12000])

        if action == 'click_at':
            x, y = params.get('x'), params.get('y')
            if x is None or y is None:
                return ToolResult(content='Error: x and y are required.', is_error=True)
            return ToolResult(content=_run(_do_click_at(float(x), float(y)), timeout=60)[:12000])

        if action == 'dialog_policy':
            policy = str(params.get('policy') or params.get('text') or '')
            prompt_text = str(params.get('text', '')) if params.get('policy') else ''
            content = _set_dialog_policy(policy, prompt_text)
            return ToolResult(content=content, is_error=content.startswith('Error:'))

        if action == 'scroll':
            direction = str(params.get('direction', 'down')).lower()
            return ToolResult(content=_run(_do_scroll(direction), timeout=60)[:12000])

        if action == 'go_back':
            return ToolResult(content=_run(_do_go_back(), timeout=60)[:12000])

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

        if action == 'close':
            _run(_shutdown())
            return ToolResult(content='Browser closed.')

        return ToolResult(content=f'Unknown action: {action}', is_error=True)

    except ImportError as e:
        if 'playwright' in str(e).lower() or str(getattr(e, 'name', '')).startswith('playwright'):
            return ToolResult(content=_BROWSER_INSTALL_ERROR, is_error=True)
        return ToolResult(content=f'Browser error: {e}', is_error=True)
    except Exception as e:
        return ToolResult(content=f'Browser error: {e}', is_error=True)


def execute_browser(params: dict, ctx: ToolContext) -> ToolResult:
    """Enforce attachment privileges even for direct executor calls."""
    with _call_lock:
        action = str(params.get('action', '')).strip().lower()
        audited = action in {'attach', 'cdp', 'detach'}
        audit_session = (_attached or {}).get('session_id')
        result = ToolResult(content='Browser call failed.', is_error=True)
        try:
            if not _playwright_available():
                result = ToolResult(content=_BROWSER_INSTALL_ERROR, is_error=True)
                return result
            error = attached_scope_error(params, ctx)
            if error:
                result = ToolResult(content=error, is_error=True)
                return result
            if attached_action_requires_approval(params):
                from charon.infra.tool_approval import request_attached_browser_approval
                if not request_attached_browser_approval(params, ctx):
                    result = ToolResult(content='Blocked: attached browser action requires approval.', is_error=True)
                    return result
            if _attached and action not in {'detach', 'close'}:
                _guard_attached_page(_page)
                if action == 'navigate' and not _url_allowed(str(params.get('url', '')), _attached['origins']):
                    raise ValueError('Domain allowlist refused navigation.')
            result = _execute_browser(params, ctx)
            return result
        except Exception as exc:
            result = ToolResult(content=f'Browser error: {exc}', is_error=True)
            return result
        finally:
            if audited:
                _diag('browser_audit', 'browser operation', state_dir=ctx.state_dir,
                      action=action, operation=str(params.get('operation', ''))[:80],
                      actor=ctx.agent_id, session_id=(_attached or {}).get('session_id') or audit_session,
                      outcome='refused_or_failed' if result.is_error else 'success')
