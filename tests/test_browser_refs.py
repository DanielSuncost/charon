"""Stable element refs: identity is bound to the DOM node, not to its position.

Runs against the tool's own headless Chromium on a local page; the module is
skipped (not failed) where Playwright has no browser binary.
"""
from __future__ import annotations

import pytest

from charon.tools import browser_tool
from browser_support import LocalSite, act, ensure_browser_or_skip, js, reset_tool_state, shutdown_browser

LIST_PAGE = """<!doctype html><html><head><title>refs</title></head><body>
<h1>List</h1>
<ul id="list">
  <li><button id="b-alpha" onclick="window.__clicked='alpha'; window.__trusted=event.isTrusted">alpha</button></li>
  <li><button id="b-beta" onclick="window.__clicked='beta'">beta</button></li>
  <li><button id="b-gamma" onclick="window.__clicked='gamma'">gamma</button></li>
</ul>
<a id="lnk" href="#bottom">jump</a>
<div style="height: 2400px"></div>
<button id="b-bottom" onclick="window.__clicked='bottom'">bottom</button>
</body></html>"""

OTHER_PAGE = """<!doctype html><html><head><title>other</title></head><body>
<button onclick="window.__clicked='other-one'">one</button>
<button onclick="window.__clicked='other-two'">two</button>
</body></html>"""

PREPEND_ROWS = """() => {
    const u = document.getElementById('list');
    for (let i = 0; i < 5; i++) {
        const li = document.createElement('li');
        li.innerHTML = '<button onclick="window.__clicked=\\'row' + i + '\\'">row' + i + '</button>';
        u.prepend(li);
    }
}"""


@pytest.fixture(scope='module')
def site():
    s = LocalSite({'/list': LIST_PAGE, '/other': OTHER_PAGE}).start()
    yield s
    s.stop()


@pytest.fixture(scope='module')
def browser(tmp_path_factory):
    ctx = ensure_browser_or_skip(tmp_path_factory.mktemp('browser-refs'))
    yield ctx
    shutdown_browser()


@pytest.fixture(autouse=True)
def _reset():
    reset_tool_state()
    yield
    reset_tool_state()


def _ref(label: str) -> str:
    return next(e['ref'] for e in browser_tool._refs.values() if e['label'] == label)


def _num(ref: str) -> int:
    return int(ref.rsplit('e', 1)[1])


def test_browser_ref_survives_mutation_that_shifts_positional_indices(site, browser):
    state = act(browser, action='navigate', url=site.url('/list')).content
    gamma = _ref('gamma')
    assert f'[{gamma}] button "gamma"' in state

    # Five rows inserted *above* gamma: every positional index moves by five.
    js(PREPEND_ROWS)

    result = act(browser, action='click', ref=gamma)
    assert not result.is_error
    assert result.content.startswith(f'Clicked [{gamma}] button "gamma"')
    assert js('() => window.__clicked') == 'gamma'

    listing = result.content
    row4, alpha = _ref('row4'), _ref('alpha')
    # New rows got *new* ids and sit first in DOM order; old ids are unchanged.
    assert _num(row4) > _num(gamma)
    assert listing.index(f'[{row4}] button "row4"') < listing.index(f'[{alpha}] button "alpha"')
    assert f'[{gamma}] button "gamma"' in listing
    # The legacy positional index 2 no longer means gamma — that was the bug.
    assert browser_tool._element_index[2]['label'] != 'gamma'


def test_browser_legacy_index_is_positional_and_still_clicks(site, browser):
    act(browser, action='navigate', url=site.url('/list'))
    assert not act(browser, action='click', index=0).is_error
    assert js('() => window.__clicked') == 'alpha'

    js(PREPEND_ROWS)
    act(browser, action='get_state')
    act(browser, action='click', index=0)
    # Same index, different element: index is a position in the last listing.
    assert js('() => window.__clicked') == 'row4'


def test_browser_ref_click_dispatches_trusted_pointer_events(site, browser):
    act(browser, action='navigate', url=site.url('/list'))
    act(browser, action='click', ref=_ref('alpha'))
    # The old JS el.click() produced isTrusted === false; a Playwright click is real input.
    assert js('() => window.__trusted') is True


def test_browser_ref_stable_across_scroll_and_reflow(site, browser):
    state_top = act(browser, action='navigate', url=site.url('/list')).content
    alpha = _ref('alpha')
    assert f'[{alpha}] button "alpha"' in state_top
    assert 'button "bottom"' not in state_top          # below the fold → not listed

    js('() => window.scrollTo(0, document.body.scrollHeight)')
    state_bottom = act(browser, action='get_state').content
    bottom = _ref('bottom')
    assert _num(bottom) > _num(alpha)                          # first seen now → a fresh id
    assert f'[{bottom}] button "bottom"' in state_bottom
    assert f'[{alpha}] button "alpha"' not in state_bottom      # scrolled out of view

    js('() => window.scrollTo(0, 0)')
    state_again = act(browser, action='get_state').content
    assert f'[{alpha}] button "alpha"' in state_again           # same node, same ref

    js('() => window.scrollTo(0, document.body.scrollHeight)')
    assert f'[{bottom}] button "bottom"' in act(browser, action='get_state').content


def test_browser_ref_from_previous_page_is_unknown_after_navigation(site, browser):
    act(browser, action='navigate', url=site.url('/list'))
    alpha = _ref('alpha')
    state = act(browser, action='navigate', url=site.url('/other')).content
    # Numbering continues across documents, so an old ref can never alias a new element.
    assert f'[{alpha}]' not in state and 'button "one"' in state
    assert _num(_ref('one')) > _num(alpha)
    result = act(browser, action='click', ref=alpha)
    assert result.is_error and 'unknown ref' in result.content


def test_browser_ref_stale_after_reload_reports_navigation(site, browser):
    act(browser, action='navigate', url=site.url('/list'))
    alpha = _ref('alpha')
    browser_tool._run(browser_tool._page.reload(wait_until='domcontentloaded'))
    result = act(browser, action='click', ref=alpha)
    assert result.is_error
    assert 'stale' in result.content and 'navigated' in result.content


def test_browser_ref_removed_element_reports_stale_not_wrong_click(site, browser):
    act(browser, action='navigate', url=site.url('/list'))
    beta = _ref('beta')
    js('() => document.getElementById("b-beta").remove()')
    result = act(browser, action='click', ref=beta)
    assert result.is_error and 'left the DOM' in result.content
    assert js('() => window.__clicked') is None


def test_browser_get_state_reuses_snapshot_when_dom_unchanged(site, browser):
    act(browser, action='navigate', url=site.url('/list'))
    walks, hits = browser_tool._stats['walks'], browser_tool._stats['cache_hits']

    act(browser, action='get_state')                       # nothing changed → probe only
    assert browser_tool._stats['cache_hits'] == hits + 1
    assert browser_tool._stats['walks'] == walks

    beta = _ref('beta')
    js('() => document.getElementById("b-beta").textContent = "beta2"')
    state = act(browser, action='get_state').content        # mutation → full walk
    assert browser_tool._stats['walks'] == walks + 1
    assert f'[{beta}] button "beta2"' in state


def test_browser_get_state_walks_again_when_document_root_is_replaced(site, browser):
    # document.open()/set_content keep window *and* document but swap the root
    # element, so an observer on the old documentElement never fires. The probe
    # must still notice.
    act(browser, action='navigate', url=site.url('/list'))
    walks = browser_tool._stats['walks']
    browser_tool._run(browser_tool._page.set_content('<button>fresh</button>'))
    state = act(browser, action='get_state').content
    assert browser_tool._stats['walks'] == walks + 1
    assert 'button "fresh"' in state and 'alpha' not in state


def test_browser_ref_input_fills_field(site, browser):
    js_page = '<input id="name" placeholder="Your name"><select id="pick"><option>red</option><option>blue</option></select>'
    browser_tool._run(browser_tool._page.set_content(js_page))
    state = act(browser, action='get_state').content
    name_ref = next(e['ref'] for e in browser_tool._refs.values() if e['label'] == 'Your name')
    pick_ref = next(e['ref'] for e in browser_tool._refs.values() if e['tag'] == 'select')
    assert name_ref in state and pick_ref in state

    assert not act(browser, action='input', ref=name_ref, text='Ada').is_error
    assert js('() => document.getElementById("name").value') == 'Ada'
    assert not act(browser, action='input', ref=pick_ref, text='blue').is_error
    assert js('() => document.getElementById("pick").value') == 'blue'
