"""Dialogs never block and are reported; every frame is walked and clickable.

Note on the brief: an *unhandled* confirm() does not hang Playwright — it is
auto-dismissed in ~50 ms, so the old failure mode was a silent cancel. These
tests pin the new behaviour: answered by policy, and always reported.
"""
from __future__ import annotations

import time

import pytest

from charon.tools import browser_tool
from browser_support import LocalSite, act, ensure_browser_or_skip, frames, js, reset_tool_state, shutdown_browser

DIALOG_PAGE = """<!doctype html><html><head><title>dialogs</title></head><body>
<button onclick="window.__result = confirm('Delete?') ? 'confirmed' : 'cancelled'">confirm</button>
<button onclick="window.__result = prompt('Name?', 'dflt')">prompt</button>
<button onclick="alert('hi'); window.__result = 'after-alert'">alert</button>
</body></html>"""

INNER_PAGE = """<!doctype html><html><body>
<button id="ib" onclick="window.__inner='clicked'">inner btn</button>
<input id="iin" placeholder="inner field">
<p>inner text here</p></body></html>"""

OUTER_PAGE = """<!doctype html><html><head><title>outer</title></head><body>
<button onclick="window.__outer='clicked'">outer btn</button>
<iframe id="fr" src="/inner" width="500" height="150"></iframe>
</body></html>"""

# Served from localhost, embeds 127.0.0.1 → a different origin for the browser.
XORIGIN_PAGE = """<!doctype html><html><head><title>xorigin</title></head><body>
<button onclick="window.__outer='clicked'">outer btn</button>
<iframe id="fr" src="http://127.0.0.1:{port}/inner" width="500" height="150"></iframe>
</body></html>"""


@pytest.fixture(scope='module')
def site():
    s = LocalSite({'/dialogs': DIALOG_PAGE, '/inner': INNER_PAGE,
                   '/outer': OUTER_PAGE, '/xorigin': XORIGIN_PAGE}).start()
    yield s
    s.stop()


@pytest.fixture(scope='module')
def browser(tmp_path_factory):
    ctx = ensure_browser_or_skip(tmp_path_factory.mktemp('browser-dialogs'))
    yield ctx
    shutdown_browser()


@pytest.fixture(autouse=True)
def _reset():
    reset_tool_state()
    yield
    reset_tool_state()


def _ref(label: str) -> str:
    return next(e['ref'] for e in browser_tool._refs.values() if e['label'] == label)


def _inner_frame():
    return next(f for f in frames() if f.url.endswith('/inner'))


# ── dialogs ──────────────────────────────────────────────────────────────────

def test_browser_dialog_confirm_does_not_hang_and_is_reported(site, browser):
    act(browser, action='navigate', url=site.url('/dialogs'))
    started = time.perf_counter()
    result = act(browser, action='click', ref=_ref('confirm'))
    assert time.perf_counter() - started < 15
    assert not result.is_error
    assert js('() => window.__result') == 'cancelled'
    assert 'confirm "Delete?" → dismissed (policy)' in result.content
    assert 'dialog_policy' in result.content
    # reported once, not on every later state
    assert 'Dialogs' not in act(browser, action='get_state').content


def test_browser_dialog_policy_accept_confirms_and_answers_prompt(site, browser):
    act(browser, action='navigate', url=site.url('/dialogs'))
    policy = act(browser, action='dialog_policy', policy='accept', text='Ada')
    assert not policy.is_error and 'accept' in policy.content

    act(browser, action='click', ref=_ref('confirm'))
    assert js('() => window.__result') == 'confirmed'

    result = act(browser, action='click', ref=_ref('prompt'))
    assert js('() => window.__result') == 'Ada'
    assert 'prompt "Name?" → accepted with \'Ada\'' in result.content


def test_browser_dialog_prompt_dismissed_by_default_returns_null(site, browser):
    act(browser, action='navigate', url=site.url('/dialogs'))
    act(browser, action='click', ref=_ref('prompt'))
    assert js('() => window.__result') is None


def test_browser_dialog_alert_is_accepted_under_any_policy(site, browser):
    act(browser, action='navigate', url=site.url('/dialogs'))
    result = act(browser, action='click', ref=_ref('alert'))
    assert js('() => window.__result') == 'after-alert'
    assert 'alert "hi" → accepted' in result.content


def test_browser_dialog_policy_rejects_unknown_value(browser):
    result = act(browser, action='dialog_policy', policy='maybe')
    assert result.is_error


# ── frames ───────────────────────────────────────────────────────────────────

def test_browser_iframe_elements_listed_and_clickable_by_ref(site, browser):
    state = act(browser, action='navigate', url=site.url('/outer')).content
    n = browser_tool._frame_number(_inner_frame())
    assert f'--- Frame f{n} (' in state and 'inner text here' in state
    ref = _ref('inner btn')
    assert ref.startswith(f'f{n}.') and f'[{ref}] button "inner btn"  ⟨frame f{n}⟩' in state

    result = act(browser, action='click', ref=ref)
    assert not result.is_error and result.content.startswith(f'Clicked [{ref}]')
    assert js('() => window.__inner', frame=_inner_frame()) == 'clicked'
    assert js('() => window.__outer') is None                      # the outer button was not touched

    assert not act(browser, action='input', ref=_ref('inner field'), text='hello').is_error
    assert js('() => document.getElementById("iin").value', frame=_inner_frame()) == 'hello'


def test_browser_cross_origin_iframe_oopif_walked_and_clickable(site, browser):
    """Outer document on localhost, iframe on 127.0.0.1: a different origin.

    Whether Chromium hosts it out-of-process depends on site-isolation flags
    (loopback hosts are one site unless --site-per-process); Playwright's frame
    tree abstracts both cases and the tool only ever goes through it.
    """
    state = act(browser, action='navigate', url=site.alt_url('/xorigin')).content
    inner = _inner_frame()
    assert inner.url.startswith('http://127.0.0.1:') and browser_tool._page.url.startswith('http://localhost:')
    assert 'inner text here' in state
    ref = _ref('inner btn')
    assert not act(browser, action='click', ref=ref).is_error
    assert js('() => window.__inner', frame=inner) == 'clicked'


def test_browser_frame_selector_actions_search_every_frame(site, browser):
    act(browser, action='navigate', url=site.url('/outer'))
    assert not act(browser, action='assert_selector', selector='#ib').is_error
    result = act(browser, action='click_selector', selector='#ib')
    n = browser_tool._frame_number(_inner_frame())
    assert not result.is_error and f'(frame f{n})' in result.content
    assert js('() => window.__inner', frame=_inner_frame()) == 'clicked'
    assert not act(browser, action='input_selector', selector='#iin', text='via selector').is_error
    assert js('() => document.getElementById("iin").value', frame=_inner_frame()) == 'via selector'


def test_browser_frame_removed_ref_reports_gone_not_hang(site, browser):
    act(browser, action='navigate', url=site.url('/outer'))
    ref = _ref('inner btn')
    js('() => document.getElementById("fr").remove()')
    started = time.perf_counter()
    result = act(browser, action='click', ref=ref)
    assert time.perf_counter() - started < 10
    assert result.is_error and ('frame that is gone' in result.content or 'stale' in result.content)
    state = act(browser, action='get_state').content
    assert '--- Frame f' not in state and 'inner btn' not in state
