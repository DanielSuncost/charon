# Charon visualization renderer 1.1.0

Vendor `charon-viz.js`, `charon-viz.css`, and `fixtures/` together. The module exports
`mount`, `RENDERER_VERSION`, and `SCHEMA_VERSIONS` (`[2]`). No dependencies or build.
The source-checkout packaging follows Graph Studio: `apps/` assets, a Python host
under `src/charon`, and a launcher under `scripts/`.

```sh
.venv/bin/python scripts/charon_viz.py render --root /your/project --out /tmp/project.html
.venv/bin/python scripts/charon_viz.py serve --root /your/project --port 4318
```

The project must have a valid V1 `system-map.json`. Reading never migrates or writes
it. `--map` accepts an explicit map path. This checkout has no root map; the partial
`charon-example-map.json` provides four explicitly authored components for a demo:

```sh
.venv/bin/python scripts/charon_viz.py render --root /Users/doppo/Projects/charon \
  --map "$PWD/apps/viz/charon-example-map.json" --out /tmp/charon-viz.html
```

Gaps are deliberately retained. The adapter calls the same `validate_map`,
`extract_facts`, and `report_table` functions as the SystemMap validate/extract/query
tool. Observed imports and declared dependencies have distinct stable IDs. Authored
nodes/descriptions are untouched. Authored freshness is unknown; imports extracted
in this run are fresh as of `extracted_at`. Source revision, declaration digest,
validation, gaps, and conflicts appear in a versioned extension's readable fallback.
No import observation claims runtime data flow. Freshness describes the snapshot;
an offline file does not monitor subsequent filesystem changes.

## Host integration

```js
import {mount, RENDERER_VERSION, SCHEMA_VERSIONS} from './charon-viz.js';
const handle = mount(element, {model, view, theme, callbacks});
```

Include the CSS in your host. The standalone host embeds that same module and CSS
in one HTML file, using an inline `type="module"` script. Standard browsers block
external ES-module imports from `file://`; inline embedding is the portable offline
consumption path. There are no fetches, remote fonts, or network dependencies.

### View field conventions

The upstream contract specifies semantics but does not assign view field names.
This implementation uses:

- `focusNode`: node ID or null; `visibleDepth`: positive integer, default 1.
- `maxNodes`: 1–200, default 200; relationships are separately bounded at 500.
- `relationshipKinds`: array of kinds (empty means all).
- `mode`: `overview`, `dependency`, or `data-flow`. Overview and dependency are structural views with subsystem lanes and dependency-depth
  columns. Data-flow includes reads/writes/calls/produces; structural views exclude
  those flow kinds. `relationshipKinds` further restricts the active mode. Counts
  identify relationships excluded by mode or filter.
- `pinnedPositions`: `{nodeId: {x, y}}`; `collapsedGroups`: array of node IDs.

Claims use `claim`; freshness uses `freshness`. Both have explicit text badges.
`update({model, view})` replaces whichever document is supplied, returning true
on success and false on error. Omitted documents and local camera/selection/
expansion are retained. `getState()` returns `{camera: {x,y,zoom}, selection,
expandedIds, focusNode}`. Node IDs and relationship IDs must be globally unique
within a document. Focus null returns to roots. Search covers all loaded nodes.

`onSelect` receives an ID or null. `onFocus` gets ID and `{id,name}[]` breadcrumbs.
`onRequestChildren` resolves to `{nodes, relationships}`; duplicate IDs replace
previous records only in the renderer's local snapshot. Loading/errors are visible;
responses arriving after an update or destroy are ignored. Evidence callbacks get
the original reference data. No URL/path is opened by the renderer. The standalone
host displays the reference and editor instructions because it has no editor bridge.
Callback throws/rejections appear as readable errors.

Optional edit controls only emit intent. Label/description/note intent shape is
`{type, targetId, value}`, pin is `{type:'pin', nodeId, position:{x,y}}`, collapse is
`{type:'collapse', nodeId, collapsed}`. A host validates/commits and sends an update.
Auto-layout explicitly clears renderer pins; hosts can persist positions via their
own view revision workflow. Unchanged nodes retain their coordinates on updates.

### Themes and accessibility

All contract tokens are consumed as CSS custom properties inherited from the mount
element. `theme` accepts overrides keyed by the full `--viz-*` property name.
Defaults are inline fallbacks in the stylesheet: light surfaces, dark text,
system sans-serif/monospace fonts, and 8px radii. Dark hosts supply their own token
values. `--viz-gap` optionally controls spacing (default 12px).

Nodes and edges are keyboard-selectable. Enter selects, Right drills into a node;
breadcrumb buttons navigate back. Focus the SVG and use arrow keys to pan, or drag
the background. Zoom buttons and Fit are keyboard-accessible. A readable tree and
relationship list provide text alternatives. The inspector exposes full descriptions
and code/evidence references. Long graph labels are abbreviated with full titles.

### Multi-system boundary

The fixed input contract contains one `system`, with no defined loaded-system
collection or cross-system node-key format. Each document mounts independently.
`several-systems.json` supplies `systems[]` as a **fixture container**, containing
two independently mountable documents; `model` is the first. Cross-system links
remain explicit unresolved references rather than invented local nodes. A combined
loaded-system input convention needs agreement with Acheron before implementation.

### Tests

```sh
.venv/bin/python -m pytest -q -s -p no:cacheprovider tests/test_viz.py
```

Tests use Python Playwright and real headless Chromium (Node is not needed by the
test harness). Each browser context is offline and opens a self-contained file URI.
All ten shared JSON fixtures are checked, plus transactional errors, stable/pinned
positions, 100-level breadcrumbs, 10,000-node bounds, HTML injection, evidence,
SVG/text exports, lazy expansion success/rejection/destroy, theme inheritance,
edit intents, extraction preservation, and the loopback-only snapshot server.
No browser skip masks failure; install the optional browser dependency and Chromium
before running this suite. The full repo already uses Playwright browser fixtures.


## Semantic layout (1.1.0)

The layering and lane approach is ported from Acheron's
`src/overseer/mapview.js` (`layerNodes`, `layoutGraph`). No schema or mount/handle
contract change. The extra pure `layoutSystem` export supports diagnostic tests.

Structural columns use reverse dependency depth: dependents left, sinks/external
nodes right. When no sink is available, the earliest declared unresolved node
breaks the cycle. Feedback edges keep their actual direction, with a distinct
compound dash and a visible `↶ feedback` label. Declared and observed edges remain
separate; unlike the legacy classifier, this renderer does not collapse their
claims into one edge.

Flow uses the same layering engine over flow relations: reads point from resource
to reader, writes/calls/produces from actor to target. This gives source-to-consumer
ordering, not a claim about execution time. Imports are not promoted into flow.
Charon's example map contains only imports, so its flow view has no flow edges;
the shared flow fixture demonstrates the four supported relations.

Within each subsystem lane and column, four alternating barycenter sweeps use
neighbor vertical positions, with declaration-order tie breaking. The best pass
is retained. The crossing metric counts endpoint-order inversions for forward
edges between the same pair of columns, excluding shared endpoints; it does not
count geometric intersections of final routed polylines. The crossing fixture
measures six inversions before ordering and zero afterward.

Cross-lane, long, and feedback edges route via lane-top gutters and outer region
rails. Adjacent same-lane edges use inter-column gutters. Pins may deliberately
place nodes across these routing gutters; their coordinates take precedence over
computed geometry. The renderer does not promise obstacle avoidance for arbitrary
user-pinned overlapping nodes.

Initial and explicit auto-layout are deterministic. Incremental updates retain
existing coordinates and allocate new nodes in their semantic column; overflowing
lanes can gain labelled continuation regions. Pins override computed positions.
Each mode has its own retained coordinates so explicit switching can show the
other ordering without losing previous slots. Only explicit Auto-layout clears
pins and reorders an existing mode. Consequently, retained/pinned positions may
not reflect changed topology until the user requests Auto-layout.
