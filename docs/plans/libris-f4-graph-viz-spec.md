# Libris F4 Graph Visualization Spec

> Visual design spec for rendering Libris research swarms inside the F4 agent chat-room view.
>
> Date: 2026-03-29
> Status: Proposed
> Related:
> - `docs/plans/libris-autonomous-research-operation.md`
> - `docs/plans/libris-implementation-architecture.md`

---

## 1. Purpose

The Libris graph view is a **live swarm topology overlay** for a research room.

It should help the user understand, at a glance:
- who exists in the swarm
- what role each agent plays
- what each agent is doing right now
- which topic each agent belongs to
- who is communicating with whom
- where active work is flowing
- which topics are progressing vs stalled

This graph is not a generic graph explorer. It is a **real-time operational diagram**.

---

## 2. Core design principle

Optimize for:
1. readability
2. role recognition
3. activity visibility
4. topic grouping
5. communication flow

Avoid overly abstract layouts that make it hard to see the coordinator → researcher → judge → shade structure.

---

## 3. Graph model

## Nodes

Nodes are agents:
- coordinator
- researchers
- judges
- shades

Each node comes from Libris swarm `nodes`.

## Edges

Edges are communication links:
- coordinator → researcher
- researcher ↔ judge
- researcher → shade
- shade → researcher

Each edge comes from Libris swarm `edges`.

---

## 4. Layout model

## Recommended layout: hierarchical clustered DAG

Use a stable topic-clustered layout.

## Top row
- **Coordinator** node centered at top

## Middle bands
For each topic cluster:
- **Researcher** on left/middle
- **Judge** on right/middle

## Bottom band
- shades under their parent researcher

### Example

```text
                    [ Coordinator ]

      ┌──────────── Topic A ────────────┐   ┌──────────── Topic B ────────────┐
      [ Researcher A ]  <---->  [ Judge A ] [ Researcher B ]  <----> [ Judge B ]
             |                                  |
        [Shade A1]                              [Shade B1]
        [Shade A2]                              [Shade B2]
        [Shade A3]
```

This should be the default Libris arrangement.

---

## 5. Topic clustering

Each topic should be rendered as a **visual cluster/lane/card group**.

Each topic cluster should show:
- topic title
- topic slug
- topic status
- topic phase
- checkpoint count
- best checkpoint if available

Possible presentation:
- a box boundary
- a subtle background tint
- a labeled section header

If there are many topics:
- allow collapsed clusters
- show only researcher/judge until expanded

---

## 6. Node design

Each node should render at least:

## Primary line
- agent name

## Secondary line
- role
- current phase

## Tertiary line
- topic or short goal

## Optional badge row
- status
- contract type for shades
- checkpoint count for judges/researchers

---

## 7. Node shape by role

### Coordinator
- largest node
- top center
- special color/accent
- may use hexagon, double-border, or emphasized styling

### Researcher
- main rectangular work node
- medium-large
- topic-colored accent

### Judge
- similarly prominent but visually distinct from researcher
- sharper border or orange accent works well

### Shade
- smaller subnode
- simpler box
- grouped beneath parent researcher

---

## 8. Color system

## Role colors
Suggested defaults:
- **Coordinator**: purple or gold
- **Researcher**: cyan/blue
- **Judge**: amber/orange
- **Shade**: slate/gray-blue

## Status overlay
Use border/background modifiers for:
- running → bright
- idle → muted
- waiting → blue
- failed → red
- stopped → dim red

## Topic coloring
Optionally tint a cluster with a subtle topic color so multiple topics are easier to distinguish.

---

## 9. Phase display

Phases should be visible on each node.

Examples:
- coordinator: `scouting`, `ranking`, `selecting_topics`, `selecting_final`
- researcher: `reviewing_leads`, `spawning_shades`, `drafting`, `revising`
- judge: `evaluating`
- shade: `procurement`, `extraction`, `summary`

If space is tight:
- render phase as a small badge under the role

Treat unknown phases as raw display strings.

---

## 10. Edge design

## Edge direction

Direction matters.

Recommended visual direction:
- coordinator → researcher
- researcher → judge
- judge → researcher
- researcher → shade
- shade → researcher

Show direction with one or more of:
- arrowheads
- tapered lines
- animated particle direction
- directional glyphs at line midpoint

---

## 11. Edge style by relationship

### Coordinator → Researcher
- assignment/control edge
- medium thickness
- stable structural connection

### Researcher ↔ Judge
- strongest/highest-visibility edge
- this is the core refinement loop
- should stand out visually

### Researcher → Shade
- thinner branch edges
- more numerous
- subordinate work channels

### Shade → Researcher
- return/result edges
- can brighten strongly when findings come back

---

## 12. Activity rendering

The graph should reflect live communication intensity.

Use edge fields:
- `activity_strength`
- `active_now`

## Visual mapping

### If `active_now == true`
- edge becomes bright
- optionally pulse or shimmer
- optionally animate directional flow

### If inactive
- edge remains visible but dim
- low alpha / muted color

## Suggested brightness mapping
- `1.0` → very bright, animated pulse
- `0.75` → bright
- `0.45` → medium
- `0.18` → faint structural line

Do not hide inactive edges completely.
Keep them dim so topology remains legible.

---

## 13. Communication pulse animation

Recommended behavior:
- when a communication event occurs, line flashes bright
- a traveling dot/particle can move from source node to target node
- after a short interval, line decays back to dim

This is especially valuable for:
- researcher → judge
- judge → researcher
- shade → researcher

---

## 14. Message-kind styling

Optional but recommended.

Use `message_kind` to tint pulses:
- `topic_assignment` → purple pulse
- `draft_for_review` → cyan pulse toward judge
- `critique_returned` → amber pulse back to researcher
- `source_procurement` → blue pulse toward shade
- `shade_result_returned` → green pulse back to researcher
- `shade_failed` → red flash
- `contract_progress` → soft blue pulse
- `contract_return` → green pulse

---

## 15. Node activity highlighting

Nodes should react to active communication.

When an edge is bright:
- source and target nodes get a subtle glow/ring
- optionally highlight node border
- optionally show last message kind briefly

Examples:
- researcher border glows cyan while sending draft
- judge glows amber while critiquing
- shade glows blue while procuring

---

## 16. Shade cluster rendering

When many shades exist, raw separate nodes can get messy.

## Expanded mode
Show every shade node individually.

Good when:
- 1–4 shades
- zoomed into one topic

## Collapsed mode
Show a **shade tray** beneath the researcher:
- `3 active shades`
- expandable on selection/zoom

This is likely best for default multi-topic view.

---

## 17. Zoom/focus behavior

## Room-level default
Show full swarm topology.

## Focused topic mode
When the user selects a topic:
- enlarge that topic cluster
- expand its shades
- highlight only its active edges
- dim other topic clusters

## Node focus
When a node is selected:
- show detail panel with:
  - role
  - phase
  - goal
  - phase summary
  - contract info if shade
  - recent events/messages

---

## 18. Side panels / details

Depending on selection:

## Coordinator selected
Show:
- prompt
- budget usage
- operation status
- selected topics
- final selection state

## Researcher selected
Show:
- topic
- phase
- draft path
- latest checkpoint
- promising sources

## Judge selected
Show:
- topic
- checkpoint count
- latest critique summary
- score info if available

## Shade selected
Show:
- contract type
- current phase
- expected outputs
- contract metadata
- latest contract event

---

## 19. Graph vs chat-room integration

F4 is a room view, so the Libris graph should be one layer of the room, not the whole room.

Recommended composition:

## Left/center
- graph visualization

## Right/bottom
- room message stream / event stream / selected-node details

This allows the user to:
- watch the topology
- read the conversation/handoffs
- intervene when desired

---

## 20. Intervention UX

When the user intervenes:
- highlight target node(s)
- briefly flash incoming user edge to selected agent
- show intervention in room log
- optionally use a special color edge:
  - user → coordinator
  - user → selected researcher
  - user → whole-room broadcast

This makes interventions feel tangible.

---

## 21. Minimal viable implementation

## MVP graph
- stable hierarchical layout
- nodes with:
  - name
  - role
  - phase
- edges with:
  - dim baseline
  - bright when `active_now`
- topic grouping
- selected-node detail panel

## Phase 2
- animated pulses
- collapsed/expanded shades
- message-kind colors
- final-selection and checkpoint overlays

---

## 22. Concrete rendering rules

## Node label format

```text
[Name]
role • phase
topic / short goal
```

## Shade label format

```text
[shade-name]
shade • extraction
libris_source_procurement
```

## Topic header

```text
Vision-language model improvements
status: judging   checkpoints: 2
```

## Edge tooltip/detail

```text
researcher → judge
draft_for_review
last active: 3s ago
```

---

## 23. Backend fields to consume

For Libris graph rendering, use primarily:

## Required
- `nodes`
- `edges`
- `topics`
- `status`
- `budget_status`

## Strongly useful
- `events_tail`
- `promising_sources`
- `final_selection_markdown`

---

## 24. Suggested refresh model

For now:
- poll `get_swarm_state(operation_id)` every 1–2 seconds

Later:
- switch to incremental event streaming

Because `activity_strength` is time-sensitive, polling every ~1s is sufficient for the graph to feel alive.

---

## 25. Suggested Rust-side rendering rules

## Node title
- `name`

## Node subtitle
- `role • phase`

## Node tertiary
- `topic_slug` or shortened title

## Border color by status
- running: bright
- idle: muted
- waiting: blue
- failed: red
- stopped: dim red

## Badge by role
- coordinator: purple/gold
- researcher: cyan
- judge: orange
- shade: gray/blue

## Edge color by message kind
Optional mapping:
- topic_assignment: purple
- draft_for_review: cyan
- critique_returned: orange
- source_procurement: blue
- shade_result_returned: green

Multiply brightness by `activity_strength`.

---

## 26. Final summary

The Libris graph view in F4 should render the research swarm as a topic-clustered hierarchical graph. A coordinator sits at the top, each topic is a cluster containing a researcher–judge pair, and shades appear beneath their parent researcher. Nodes are role-colored and display live phase/status labels. Edges represent semantic communication events such as topic assignment, draft review, critique return, source procurement, and shade result return. Every edge remains dimly visible as a structural relationship, but becomes bright and optionally animated when active, using backend `activity_strength` and `active_now` fields. The graph should support collapsing shades, focusing one topic, and pairing the visual topology with a room log/detail panel so the user can watch, inspect, and intervene in the swarm in real time.
