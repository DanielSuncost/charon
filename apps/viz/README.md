# Charon visualization renderer 1.0.0

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
- `mode`: `overview`, `dependency`, or `data-flow`. Overview additionally draws
  containment links. Dependency/data-flow draw the supplied typed relationships;
  use `relationshipKinds` to choose the edges, without inferring their meaning.
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
All eight shared JSON fixtures are checked, plus transactional errors, stable/pinned
positions, 100-level breadcrumbs, 10,000-node bounds, HTML injection, evidence,
SVG/text exports, lazy expansion success/rejection/destroy, theme inheritance,
edit intents, extraction preservation, and the loopback-only snapshot server.
No browser skip masks failure; install the optional browser dependency and Chromium
before running this suite. The full repo already uses Playwright browser fixtures.
