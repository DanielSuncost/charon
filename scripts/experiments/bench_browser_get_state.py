#!/usr/bin/env python3
"""Time Browser get_state before/after the stable-ref snapshot (cap-002).

    ./.venv/bin/python scripts/experiments/bench_browser_get_state.py --before <old browser_tool.py> [--runs 20]

Method
------
One headless Chromium (the tool's own thread). A synthetic page with N
interactive elements, V of them laid out inside the 1280x800 viewport (the
rest below a 3000px spacer). Both versions' `_page_state(page)` are called on
the *same* page object; timing is taken with time.perf_counter() inside the
browser event loop, around the awaited call, so the thread hop is excluded
for both. "before" is the pre-change module loaded from --before (e.g. the
output of `git show <base-sha>:src/charon/tools/browser_tool.py`).

  cold  the DOM is dirtied before every call (a data-attribute toggle), so the
        new version cannot use its snapshot cache
  warm  the DOM is untouched between calls (new version: probe + cache hit;
        old version: has no cache and re-walks)

Reports median and p90 over --runs calls, in milliseconds.
"""
from __future__ import annotations

import argparse
import importlib.util
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))

from charon.tools import browser_tool as after  # noqa: E402


def _load_before(path: Path):
    spec = importlib.util.spec_from_file_location('browser_tool_before', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _page_html(n: int, visible: int) -> str:
    def el(i: int) -> str:
        kind = i % 3
        if kind == 0:
            return f'<button>b{i}</button>'
        if kind == 1:
            return f'<a href="#x{i}">l{i}</a>'
        return f'<input placeholder="p{i}">'
    style = ('<style>body{margin:0} .grid{display:flex;flex-wrap:wrap;width:1280px} '
             '.grid>*{width:60px;height:20px;margin:1px;box-sizing:border-box;font-size:10px;overflow:hidden}</style>')
    top = ''.join(el(i) for i in range(visible))
    rest = ''.join(el(i) for i in range(visible, n))
    return (f'<!doctype html><html><head><title>bench</title>{style}</head><body>'
            f'<div class="grid">{top}</div><div style="height:3000px"></div><div class="grid">{rest}</div></body></html>')


async def _timed(coro):
    t0 = time.perf_counter()
    await coro
    return (time.perf_counter() - t0) * 1000.0


def _bench(label: str, make_coro, runs: int, dirty_each: bool, page) -> tuple[float, float]:
    samples = []
    for i in range(runs):
        if dirty_each:
            after._run(page.evaluate(f'() => document.body.setAttribute("data-tick", "{i}")'))
        samples.append(after._run(_timed(make_coro()), timeout=120))
    samples.sort()
    med = statistics.median(samples)
    p90 = samples[min(len(samples) - 1, int(round(0.9 * (len(samples) - 1))))]
    print(f'  {label:<34} median {med:8.1f} ms   p90 {p90:8.1f} ms')
    return med, p90


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--before', type=Path, required=True, help='path to the pre-change browser_tool.py')
    ap.add_argument('--runs', type=int, default=20)
    ap.add_argument('--cases', default='300:120,2000:600', help='comma list of N:V')
    args = ap.parse_args()

    before = _load_before(args.before)
    page = after._run(after._ensure_page(), timeout=90)
    print(f'chromium {after._browser.version}, runs={args.runs}, viewport 1280x800')
    for case in args.cases.split(','):
        n, v = (int(x) for x in case.split(':'))
        after._run(page.set_content(_page_html(n, v)))
        after._run(page.wait_for_timeout(200))
        listed = after._run(page.evaluate('() => document.querySelectorAll("button,a,input").length'))
        print(f'\nN={n} interactive elements ({v} in viewport, {listed} in DOM)')
        _bench('before  _page_state (3 round trips)', lambda: before._page_state(page), args.runs, True, page)
        _bench('after   _page_state cold (walk)', lambda: after._page_state(page, fresh=True), args.runs, True, page)
        _bench('after   _page_state warm (cache)', lambda: after._page_state(page), args.runs, False, page)
    after._run(after._shutdown())
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
