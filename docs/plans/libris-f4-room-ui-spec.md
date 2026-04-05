# Libris F4 Room UI Spec

> Room-level UX spec for Libris inside the generalized F4 multi-agent chat-room view.
>
> Date: 2026-03-29
> Status: Active
> Related:
> - `docs/plans/libris-f4-graph-viz-spec.md`
> - `docs/plans/libris-swarm-state-data-contract.md`

---

## 0. Implementation note

The current checked-in OpenTUI implementation in `apps/tui/opentui/src/index.ts` now includes a working Libris-specific F4 room renderer with:
- Team Grid mode
- Swarm Graph mode
- a shared detail panel
- room-local event/source/delivery views
- selection-aware intervention targeting and prefills

This document should now be read as the target UX and refinement guide for that implementation, not as a description of a completely missing feature.

## 1. Purpose

F4 is a generalized multi-agent room view.

A Libris room should support two primary operator modes inside the same room:
1. **Team Grid** — practical cell/grid view for non-shade agents
2. **Swarm Graph** — topology view with shades and communication edges

The user should be able to switch between these instantly while staying in the same Libris room.

---

## 2. Room identity

A Libris room corresponds to one research operation.

Room identity:
- `room_kind = libris`
- `room_id = operation_id`

Room title suggestions:
- prompt-derived short title
- or `Libris • <operation_id>`

Subtitle suggestions:
- current status
- topic count
- active agents
- budget state

---

## 3. Core room layout

Recommended composition:

```text
┌──────────────────────── F4 Libris Room ────────────────────────┐
│ Room header: title • status • budget • topic count • view mode │
├───────────────────────┬─────────────────────────────────────────┤
│ Main panel            │ Side panel                              │
│                       │                                         │
│ Team Grid OR          │ Selected node/topic details            │
│ Swarm Graph           │ + recent event log                     │
│                       │                                         │
├───────────────────────┴─────────────────────────────────────────┤
│ Optional bottom composer / intervention bar                    │
└─────────────────────────────────────────────────────────────────┘
```

---

## 4. View modes

## A. Team Grid mode

Purpose:
- quickly watch major non-shade agents
- switch focus between coordinator, researchers, and judges
- enter/inspect active sessions

Use:
- `views.grid.nodes`
- fallback to `team_grid_nodes`

Grid includes:
- coordinator
- one researcher cell per topic
- one judge cell per topic

Grid excludes:
- shades by default

Each cell should show:
- agent name
- role
- phase
- status
- topic
- live line / short current action

This mode should feel similar to your existing session grid.

---

## B. Swarm Graph mode

Purpose:
- understand topology and active interactions
- see shades and communication flow
- inspect topic-level organization

Use:
- `topics`
- `nodes`
- `edges`

Graph includes:
- coordinator
- researchers
- judges
- shades
- communication edges
- topic clusters

This mode uses the graph spec in `libris-f4-graph-viz-spec.md`.

---

## 5. Mode switching

Recommended keys:
- `g` or `2` → graph mode
- `v` or `tab` → cycle between room views
- `1` → team grid
- `2` → swarm graph
- `m` → cycle intervention target mode (`auto` / `whole` / `coordinator` / `topic` / `node`)

The current mode should be visible in the header.

Example:
- `View: Team Grid`
- `View: Swarm Graph`

Selection state should persist where possible when switching.

---

## 6. Team Grid behavior

Grid ordering should be stable:
1. coordinator
2. researcher(topic1)
3. judge(topic1)
4. researcher(topic2)
5. judge(topic2)
6. ...

Each cell should expose:
- selection/focus
- quick jump into room transcript relevant to that agent
- optional direct intervention target

If a researcher has many shades, do not create many new grid cells.
Instead, show a badge like:
- `2 shades active`
- `3 procurement contracts`

---

## 7. Graph behavior

The graph should be the richer visualization mode.

It should support:
- topic clustering
- bright/dim communication lines
- expanding/collapsing shades
- selecting a node or topic
- seeing current phase and live-line text

Recommended defaults:
- coordinator at top
- researcher/judge pairs in topic lanes
- shades below their parent researcher

---

## 8. Side panel behavior

The side panel should be shared by both modes.

When selecting a node, show:
- agent name / role / status
- topic slug
- current phase
- phase summary
- live line
- goal
- tmux/session availability

When selecting a topic, show:
- topic title
- topic status
- checkpoint count
- best checkpoint id
- draft report path
- contract summaries
- top promising sources

When nothing is selected, show:
- room summary
- operation prompt
- budget state
- final selection if available

---

## 9. Event log

The room should show a recent event log using `events_tail`.

This can live in:
- side panel lower half
- or a toggleable bottom drawer

The log should prioritize events like:
- phase changes
- topic assignments
- draft handoffs
- judge critiques
- shade progress/returns
- final selection

---

## 10. Intervention UX

The user should be able to intervene from either mode.

Targets:
- whole room
- coordinator
- selected topic
- selected researcher
- selected judge
- selected shade
- selected node in graph

Current checked-in target forms supported by `/inject-room` for Libris rooms:
- `whole`
- `coordinator`
- `topic:<slug>`
- `researcher:<slug>`
- `judge:<slug>`
- `shade:<agent-id>`
- `node:<agent-id>`

Intervention types:
- suggestion
- clarification
- steering question
- stop/pause request

Visual behavior:
- show the intervention in room log
- briefly highlight the target node/cell
- optionally draw a user → agent pulse in graph mode

---

## 11. Recommended badges

## Node/cell badges
- role badge
- status badge
- phase badge
- topic badge
- shade-count badge for researchers
- checkpoint-count badge for judges/topics

## Room header badges
- operation status
- budget status
- topic count
- agent count
- active edge count (optional)

---

## 12. Refresh model

Current recommendation:
- poll room state every 1–2 seconds

Both grid and graph should update from the same room payload.

That means:
- no separate data fetch for graph vs grid
- one shared Libris room state store

---

## 13. Room-selection model

F4 overall may contain many rooms:
- Libris research rooms
- development-team rooms
- direct agent conversation rooms

For Libris rooms, room switching should show:
- room title
- status
- active member count
- last activity time
- maybe prompt preview

Once a Libris room is selected, the user can switch between grid and graph locally within that room.

---

## 14. MVP implementation

The first working Libris F4 room should support:
- room header
- team grid mode
- graph mode
- view switching
- selected-node detail panel
- recent event list
- polling swarm state

That is enough for a compelling first end-to-end demo.

---

## 15. Final summary

A Libris room in F4 should behave like a generalized agent chat room with two synchronized representations of the same operation:
- a **Team Grid** for practical monitoring of coordinator/researcher/judge sessions
- a **Swarm Graph** for seeing overall topology, shades, and communication dynamics

The user should stay in the same room while switching between these modes, reading the event log, and intervening in the swarm as needed.
