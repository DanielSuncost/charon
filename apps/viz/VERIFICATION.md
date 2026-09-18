# Verification — 2026-09-16

Base: `1eb528a78ed0ebb5ab5a24ddfa4926383e4ab35b`.
Branch: `a6ab5cb5ef`. Worktree: `/private/tmp/charon-a6ab5cb5ef`.
Main checkout was read only. No merge or push.

## Requested checks

| Check | Command / result | Exit |
| --- | --- | --- |
| a: render Charon | `.venv/bin/python scripts/charon_viz.py render --root /Users/doppo/Projects/charon --map /private/tmp/charon-a6ab5cb5ef/apps/viz/charon-example-map.json --out /private/tmp/charon-a6ab5cb5ef.html` | 0 |
| a: offline browser | Python Playwright, headless Chromium 145.0.7632.6, offline context, file URI; 5 nodes, 4 observed import relationships, evidence callback verified; no page errors; only request was the file URI | 0 |
| b: fixture suite | `.venv/bin/python -m pytest -q -s -p no:cacheprovider tests/test_viz.py` — 14 passed in 7.63s | 0 |
| c: stress | Included in b: 100 nodes / 100-level breadcrumbs; 10,000 stored nodes, 200 drawn, visible 9,800-node limit; cycle/missing/self-parent/schema/layout errors preserve previous render and state; observed selection time 0.20 ms | 0 |
| d: targeted | `.venv/bin/python -m pytest -q -p no:cacheprovider -k 'viz or system_map'` — 35 passed, 1562 deselected in 6.65s | 0 |
| e: lint | `.venv/bin/ruff check .` — All checks passed | 0 |
| e: full | `.venv/bin/python -m pytest -q -p no:cacheprovider` — 1590 passed, 7 skipped in 111.07s | 0 |
| module syntax | `node --input-type=module --check < apps/viz/charon-viz.js` | 0 |

Node is available at `/opt/homebrew/bin/node`; browser assertions use Python
Playwright and Chromium instead of a simulated Node DOM. The eight shared fixture
files all run through the real renderer. There are no skips in the viz suite.
The full suite's seven skipped tests remain unverified.

Sandbox targeted attempt: exit 1, 23 passed, 1 failed and 11 errors (loopback bind
and Chromium denied). Browser and loopback checks were rerun unrestricted.
Sandbox full attempt: interrupted with exit 2 after 1 failed / 16 passed; tmux
was denied and the process stalled in a Hugging Face HTTP call. The unrestricted
full rerun completed successfully. Initial fixture assertion failures involving
closed disclosures were corrected and all final checks above passed.

## Artifacts

- HTML: `/private/tmp/charon-a6ab5cb5ef.html`
- Visually inspected screenshot: `/private/tmp/charon-a6ab5cb5ef.png`
- Fixture log: `/private/tmp/viz-fixtures.log`
- Targeted log: `/private/tmp/viz-targeted-unrestricted.log`
- Full log: `/private/tmp/viz-full-unrestricted.log`

Offline evidence inspected: `src/charon/agents/shade_lifecycle.py:34`, root
`charon.repo`. The host displays references for opening in an editor; it has no
filesystem editor bridge.

## Contract clarifications / limits

1. Standard browsers block external ES module imports from `file://`. The exporter
   embeds the exact shared module and CSS inline, providing a working offline file
   without disabling browser security. No build step or runtime dependencies.
2. The fixed contract defines one system document, not a loaded-system collection.
   The several-systems fixture includes two independently mountable models.
   Cross-system references render explicitly unresolved. Combined loaded-system
   rendering needs an agreed input convention; no private alternative was invented.
3. View field names, claim field spelling, and edit-intent payloads were unspecified;
   the chosen conventions are documented in README.md for Acheron integration.
4. Charon has no root `system-map.json` at this base. The explicitly authored partial
   example map is passed with `--map`; it does not install or infer a project map.
   Unowned source and import/declaration disagreements remain visible diagnostics.
5. Packaging follows Graph Studio's source-checkout pattern. No wheel-asset
   packaging, Acheron transport, revision store, or editor bridge is added.
