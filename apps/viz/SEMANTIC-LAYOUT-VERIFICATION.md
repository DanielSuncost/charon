# Semantic layout follow-up — 2026-09-17

Branch `a6ab5cb5ef`, worktree `/private/tmp/charon-a6ab5cb5ef`, on top of
`f80537263b156cdc8a254e383f4f5aa7d5120a40`. Renderer version is now 1.1.0.

## Acceptance evidence

1. **Meaningful structural axis:** ported dependency-depth columns and stable
   declaration ordering from Acheron `src/overseer/mapview.js` (`layerNodes` and
   `layoutGraph`). The header and SVG export explicitly state “left → right:
   depends on.” Structural edges retain declared versus observed status.
2. **Containment:** labelled subsystem regions. Cross-lane edges enter/leave via
   lane-top gutters and an outer rail instead of passing through unrelated nodes.
   The two-lane browser test verifies both regions and the boundary-routing path.
3. **Flow:** the existing `data-flow` view now layers reads, writes, calls, produces
   by source-to-consumer order. Reads run resource → reader; others actor → target.
   The shared flow fixture asserts all five stages appear in increasing columns.
   This is relation order, not an inferred runtime trace. Charon's import-only
   example correctly has no flow edges.
4. **Stability:** deterministic cycle breaking by declaration order; feedback
   edges retain their actual direction, use a compound dash, and display a
   “↶ feedback” label. Tests compare identical layout outputs, add an unrelated
   node without changing existing positions, and check feedback rendering.
5. **Crossing reduction:** four alternating barycenter sweeps within each
   subsystem/column, retaining the best result, with declaration-order ties.
   `fixtures/crossing-reduction.json`: **6 → 0 crossings (100% reduction)**.
   Metric: endpoint-order inversions between forward edges joining the same pair
   of columns, excluding shared endpoints. This measures layer ordering, not
   geometric intersections after routing. No claim of globally optimal layout.
6. **Pins:** explicit coordinates take precedence; tests verify them through
   updates and mode switches. Only explicit Auto-layout clears them. Layout
   caches are per mode. Retained/pinned positions can depart from new dependency
   depths after topology changes until the user requests Auto-layout.

## Checks

- `.venv/bin/python -m pytest -q -s -p no:cacheprovider tests/test_viz.py`:
  **18 passed in 7.91s, exit 0**, including the original stress/contract fixtures.
- `.venv/bin/ruff check .`: **All checks passed, exit 0**.
- `node --input-type=module --check < apps/viz/charon-viz.js`: **exit 0**.
- `.venv/bin/python -m pytest -q -p no:cacheprovider`: **1594 passed,
  7 skipped in 129.44s, exit 0**. The seven skipped tests remain unverified.
- HTML generation and offline screenshot/evidence check: **exit 0**.
  Python Playwright / Chromium 145.0.7632.6, offline context, file URI, no
  JavaScript errors, no network requests, 5 nodes and 4 observed edges.
  Browser/full tests run unrestricted because this environment's sandbox blocks
  Chromium and loopback fixtures (established in the initial implementation).

## Visual review and artifacts

- HTML: `/private/tmp/charon-semantic-layout.html`
- Inspected screenshot: `/private/tmp/charon-semantic-layout.png`
- Fixture log: `/private/tmp/viz-semantic-tests.log`
- Full log: `/private/tmp/viz-semantic-full.log`

The Charon picture now reads left-to-right: Tools → Workspace / Agents →
Orchestration, in the Runtime region. Runtime's context node sits at the lane's
left edge. The axis is named above the graph, and arrows align with dependency
columns. File:line evidence remains readable in the inspector. This is a partial
architecture using the same explicit example map as the prior verification;
there is still no root system-map.json to silently fill in.

## Coordination / limits

No changes to schema version, mount/handle methods, callback shapes, or CSS token
contract. The renderer adds the pure `layoutSystem` diagnostic export. Existing
mode values now have semantic filtering: structural excludes the four flow kinds;
flow includes them. Excluded counts remain visible. Acheron should vendor version
1.1.0 and the updated fixtures/CSS together; no planning docs in Acheron were edited.

Arbitrary pinned overlaps can obstruct lanes/routing; pins are intentionally
preserved. Incremental lane overflow can produce labelled continuation regions.
The crossing measurement is a small reproducible benchmark, not a claim about
all models or all routed-edge intersections.
