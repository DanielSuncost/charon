# Libris F4 TUI Implementation Checklist

> Concrete implementation checklist for finishing the Libris room experience in the F4 multi-agent room view.
>
> Date: 2026-03-31
> Status: Partially Implemented
> Related:
> - `docs/plans/libris-f4-room-ui-spec.md`
> - `docs/plans/libris-f4-graph-viz-spec.md`
> - `docs/plans/libris-swarm-state-data-contract.md`

---

## 1. Current status

The backend already exposes Libris operations into the shared F4 room payload.

In `apps/tui/opentui/chat_backend.py`, Libris rooms are appended into `inter_agent_rooms` and now include the full set of fields the current OpenTUI frontend uses, including:
- `operation_id`
- `nodes`
- `edges`
- `topics`
- `team_grid_nodes`
- `non_shade_members`
- `views`
- `counts`
- `budget_status`
- `promising_sources`
- `final_selection_markdown`
- `executive_summary_markdown`
- `delivery_bundle`
- `events`

The F4 room list and Libris renderer live in:
- `apps/tui/opentui/src/index.ts`

As of the current checked-in implementation, F4 **does** render a dedicated Libris room body with:
- Team Grid mode
- Swarm Graph mode
- room-local selection state
- room-local detail tabs
- event/source/delivery presentation
- selection-aware Libris intervention target prefills

What remains is primarily polish and deeper graph/operator behavior, not first-pass frontend integration.

---

## 2. Main missing pieces

The following pieces are now implemented in the checked-in TUI source:

- dedicated Libris room renderer
- Team Grid mode for non-shade agents
- Swarm Graph mode for full Libris topology (text-mode MVP)
- room-local mode switching (`grid` / `graph`)
- node/topic selection inside the room
- Libris detail panel
- edge rendering using `activity_strength` / `active_now`
- room-local event/detail/source/final-delivery presentation
- selection-aware intervention target display and prefills
- Libris-specific `/inject-room` target routing for `coordinator`, `topic:<slug>`, `node:<agent-id>`, `researcher:<slug>`, `judge:<slug>`, and `shade:<agent-id>`

The main remaining gaps are:

- topology-aware graph navigation instead of linear/index-based cycling
- richer geometric/box-drawn graph layout closer to the graph viz spec
- stronger visual distinction between structural edges and active communication pulses
- tighter intervention UX polish beyond prefilled commands
- more robust handling of sparse/partial swarm payloads in edge cases

---

## 3. Primary implementation files

### Frontend
- `apps/tui/opentui/src/index.ts`

### Backend payload source
- `apps/tui/opentui/chat_backend.py`

### Reference contracts/specs
- `docs/plans/libris-swarm-state-data-contract.md`
- `docs/plans/libris-f4-room-ui-spec.md`
- `docs/plans/libris-f4-graph-viz-spec.md`

---

## 4. Step-by-step implementation plan

## Step 1 — extend frontend room state

### File
- `apps/tui/opentui/src/index.ts`

### Add room-local Libris state to `S`

Suggested additions:

```ts
roomViewMode: 'list' | 'grid' | 'graph'
roomNodeIdx: number
roomTopicIdx: number
roomDetailTab: 'node' | 'topic' | 'events' | 'sources' | 'delivery'
roomTargetMode: 'auto' | 'whole' | 'coordinator' | 'topic' | 'node'
roomGraphFocus: string | null
```

### Why
F4 currently tracks only:
- selected room index

That is not enough to support operating *inside* a Libris room.

---

## Step 2 — extend room typing

### File
- `apps/tui/opentui/src/index.ts`

### Expand `InterAgentRoom`

Add optional Libris-specific fields:

```ts
operation_id?: string
nodes?: any[]
edges?: any[]
topics?: any[]
team_grid_nodes?: any[]
non_shade_members?: any[]
views?: any
counts?: any
budget_status?: any
promising_sources?: any[]
final_selection_markdown?: string
executive_summary_markdown?: string
delivery_bundle?: any
```

### Why
The room type is currently too generic for the Libris room contract.

---

## Step 3 — ensure backend forwards all useful Libris room fields

### File
- `apps/tui/opentui/chat_backend.py`

### Confirm / add these fields when building Libris room objects

Required:
- `operation_id`
- `nodes`
- `edges`
- `topics`
- `budget_status`
- `promising_sources`
- `events`

Strongly recommended:
- `team_grid_nodes`
- `non_shade_members`
- `views`
- `counts`
- `executive_summary_markdown`
- `delivery_bundle`

### Why
The backend room object should contain everything the TUI needs so the frontend does not need to reconstruct Libris state itself.

---

## Step 4 — detect Libris rooms and route to a dedicated renderer

### File
- `apps/tui/opentui/src/index.ts`

### Current behavior
`buildRooms()` now has two render paths:
- generic room controls + metadata for normal rooms
- a dedicated Libris room body for `kind === 'libris'`

### Needed behavior
Keep the dedicated Libris room path and continue refining it rather than replacing it.

### Recommendation
Keep the room list visible in some form, but the selected Libris room should render:
- header
- main room view (`grid` or `graph`)
- side/detail panel

---

## Step 5 — build Team Grid mode

### Source fields
Use, in order:
1. `room.views?.grid?.nodes`
2. `room.team_grid_nodes`
3. `room.non_shade_members`
4. fallback: `room.nodes.filter(n => n.role !== 'shade')`

### Render
Grid cells for:
- coordinator
- researchers
- judges

### Each cell should show
- name
- role
- phase
- status
- topic slug
- live line

### Example cell
```text
researcher-rl
researcher • drafting
recent-rl-papers
→ sending draft for review
```

### Important
Do not give shades persistent cells in Team Grid mode.

---

## Step 6 — build Swarm Graph mode

### Source fields
Use:
- `room.topics`
- `room.nodes`
- `room.edges`

### MVP graph mode
A first acceptable version can be text/spatial rather than perfect line art:
- coordinator at top
- topic clusters below
- researcher/judge pairs grouped by topic
- shades beneath researcher
- active edges represented visually or in an edge activity list

### Better version
Implement the graph layout from:
- `docs/plans/libris-f4-graph-viz-spec.md`

That means:
- coordinator top-center
- researcher/judge in each topic cluster
- shades below researchers
- dim structural edges
- bright active edges

---

## Step 7 — use activity fields in graph mode

### Edge fields to consume
- `activity_strength`
- `active_now`

### Minimum rendering rule
- if `active_now == true` → bright edge color
- otherwise → dim edge color

### Better rendering rule
Map brightness from `activity_strength`.

Example:
- `1.0` → brightest
- `0.75` → bright
- `0.45` → medium
- `0.18` → faint

---

## Step 8 — add selection behavior inside the Libris room

### Needed
When inside a Libris room:
- arrow keys / hjkl should move selection across nodes/topics

### Track
- selected node
- selected topic

### Why
This is needed for:
- details panel
- interventions later
- graph focus behavior

---

## Step 9 — implement the side/detail panel

### When a node is selected
Show:
- name
- role
- status
- phase
- phase summary
- live line
- topic slug
- contract details if shade

### When a topic is selected
Show:
- title
- slug
- status
- checkpoint count
- best checkpoint id
- contract summaries
- top promising sources

### When nothing is selected
Show:
- prompt
- budget state
- final selection markdown
- executive summary markdown
- delivery bundle overview

### Source fields
Use:
- `promising_sources`
- `final_selection_markdown`
- `executive_summary_markdown`
- `delivery_bundle`

---

## Step 10 — show room event log

### Source field
- `room.events`

### Recommendation
Show the recent event log either:
- below the detail panel
- or in a lower split panel

### Why
The graph alone is not enough to understand what the swarm is doing.

---

## Step 11 — add room-local mode switching keys

### File
- `apps/tui/opentui/src/index.ts`

### Add when selected room is Libris
Suggested controls:
- `1` → Team Grid
- `2` → Swarm Graph
- `tab` → cycle room-local subviews
- `e` → focus events tab
- `s` → focus sources tab
- `d` → focus delivery tab

### Current F4 keys only support
- select room
- refresh
- say to room
- inject
- pause/resume
- delete

These should continue to work, but Libris rooms need their own inner controls too.

---

## Step 12 — preserve generic room support

Not every room is a Libris room.

So F4 should keep two render paths:
- generic room renderer for normal rooms
- Libris room renderer for `kind === 'libris'`

Do not break existing generic room behavior.

---

## 4A. Current implementation snapshot

Implemented now in `apps/tui/opentui/src/index.ts`:
- dedicated Libris room body for `kind === 'libris'`
- split header/main/detail composition in text mode
- Team Grid ordering based on coordinator + researcher/judge topic order
- Swarm Graph topic lanes with coordinator/topic/researcher/judge/shade grouping
- focusable topic mode via `roomGraphFocus`
- detail tabs: `node`, `topic`, `events`, `sources`, `delivery`
- intervention target modes: `auto`, `whole`, `coordinator`, `topic`, `node`
- selection-aware Enter / `i` prefills for Libris room interventions

Implemented now in `apps/tui/opentui/chat_backend.py`:
- Libris room payload forwarding for `team_grid_nodes`, `non_shade_members`, `views`, `counts`, `executive_summary_markdown`, `delivery_bundle`
- Libris-specific `/say-room` / `/inject-room` handling for synthetic `libris-<operation_id>` room ids
- intervention routing through live agent session steering for Libris targets

## 5. Minimum viable acceptance criteria

Libris F4 is working when all of the following are true:

1. user can open F4
2. user can select a room with `kind === 'libris'`
3. selected Libris room renders a custom room body
4. pressing `1` shows Team Grid mode
5. pressing `2` shows Swarm Graph mode
6. Team Grid uses non-shade nodes only
7. Graph mode uses nodes/edges/topics
8. active edges become visibly brighter than inactive ones
9. selected node/topic updates the detail panel
10. recent room events are visible
11. Enter / `i` prefill a Libris-aware intervention target
12. `/inject-room` can address Libris-specific targets beyond `whole`

As of the current checked-in code, these MVP acceptance criteria are met.

---

## 6. Recommended implementation order

### Phase 1
- room-local Libris state
- Libris room detection
- Team Grid mode
- detail panel

### Phase 2
- Swarm Graph mode
- active edge brightness
- topic grouping

### Phase 3
- improved graph layout
- collapsible shades
- node/topic focus states
- intervention polish

---

## 7. Final summary

The backend is already doing the hard part: Libris operations are exposed as F4 rooms with nodes, edges, topics, events, and delivery artifacts. The current checked-in OpenTUI source now consumes that contract and renders a dedicated Libris room with Team Grid and Swarm Graph modes, a shared detail panel, an event log, and selection-aware intervention targeting.

The remaining work is now refinement rather than initial integration: improve topology-aware navigation, push the graph layout closer to the full visualization spec, and further polish the intervention/operator loop.
