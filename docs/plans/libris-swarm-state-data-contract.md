# Libris Swarm State Data Contract

> Data contract for `Research(action="get_swarm_state")` / `get_libris_swarm_state(...)`.
>
> Date: 2026-03-29
> Status: Active
> Related:
> - `docs/plans/libris-f4-graph-viz-spec.md`
> - `docs/plans/libris-remaining-work-roadmap.md`

---

## 1. Purpose

This document defines the payload shape consumed by the F4 room UI for Libris.

The same room should support two main Libris views:
- **team grid** view for non-shade agents
- **graph** view for full swarm topology

The backend should return one room payload that supports both views.

---

## 2. Top-level shape

`get_swarm_state(operation_id)` returns an object with fields like:

```json
{
  "operation_id": "rop_...",
  "prompt": "research the newest RL papers",
  "status": "running",
  "budget_status": {},
  "coordinator": {},
  "topics": [],
  "members": [],
  "nodes": [],
  "non_shade_members": [],
  "team_grid_nodes": [],
  "views": {
    "grid": {},
    "graph": {}
  },
  "counts": {},
  "edges": [],
  "events_tail": [],
  "promising_sources": [],
  "final_selection_markdown": "...",
  "executive_summary_markdown": "...",
  "delivery_bundle": {}
}
```

---

## 3. Top-level fields

## Required

### `operation_id: string`
Unique Libris operation id.

### `prompt: string`
Original user research prompt.

### `status: string`
Operation status.
Examples:
- `running`
- `stopped`
- `delivered`
- `failed`

### `budget_status: object`
Structured budget/usage snapshot.
Expected keys include:
- `continue_running`
- `reasons`
- `advisory_reasons`
- `budget`
- `usage`
- `wall_hours_elapsed`

### `topics: TopicCard[]`
Topic clusters.

### `members: Node[]`
All visible Libris agents for the operation, including shades.

### `nodes: Node[]`
Alias for `members` for graph clients.

### `edges: Edge[]`
Communication graph edges.

### `events_tail: object[]`
Recent operation events, newest at end.

### `promising_sources: object[]`
Promising source shortlist for the operation.

### `final_selection_markdown: string`
Final selection / delivery summary if available.

### `executive_summary_markdown: string`
Operation-level executive summary markdown if generated.

### `delivery_bundle: object`
Structured ranked delivery bundle if generated.

---

## 4. Team-grid-specific fields

These exist specifically so F4 can switch between grid and graph without inventing its own filtering.

### `non_shade_members: Node[]`
All non-shade agents in the room.
Typically:
- coordinator
- researchers
- judges

### `team_grid_nodes: Node[]`
Ordered list optimized for grid/session view.
Recommended order:
1. coordinator
2. researcher/judge pairs in topic order

The grid should use this first if present.

### `views.grid`
Suggested descriptor for the grid view:

```json
{
  "kind": "non_shade_team_grid",
  "description": "Coordinator + researcher/judge cells for quick session switching.",
  "nodes": [ ...team_grid_nodes ]
}
```

---

## 5. Graph-specific fields

### `views.graph`
Suggested descriptor for graph rendering:

```json
{
  "kind": "topic_cluster_graph",
  "description": "Coordinator / topic / shade topology with communication edges.",
  "nodes": [ ...members ],
  "edges": [ ...edges ]
}
```

The graph should consume `nodes`, `edges`, and `topics` together.

---

## 6. Counts

### `counts`
Convenience counts for UI badges.
Expected keys:
- `topics`
- `members`
- `non_shade_members`
- `shades`
- `edges`

Example:

```json
{
  "topics": 3,
  "members": 8,
  "non_shade_members": 5,
  "shades": 3,
  "edges": 9
}
```

---

## 7. Node shape

Each node represents an agent.

Example:

```json
{
  "agent_id": "AG-0042",
  "name": "researcher-vision",
  "role": "researcher",
  "specialization": "libris-researcher",
  "status": "running",
  "phase": "drafting",
  "goal": "Research recent RL papers",
  "project": "/home/dopppo/Projects/charon",
  "topic_slug": "recent-rl-papers",
  "source": "tmux",
  "hasTmux": true,
  "phase_summary": "Writing first synthesis draft.",
  "live_line": "→ sending draft for review",
  "parent_agent_id": "AG-0001",
  "contract_id": "",
  "contract_type": "",
  "contract_status": "",
  "contract_current_phase_id": "",
  "contract_expected_outputs": [],
  "contract_metadata": {}
}
```

## Important fields

### Identity / routing
- `agent_id`
- `name`
- `role`
- `parent_agent_id`

### Live display
- `status`
- `phase`
- `phase_summary`
- `live_line`

### Topic grouping
- `topic_slug`

### Session integration
- `hasTmux`
- `source`

### Shade contract details
Used mainly for shade detail panels:
- `contract_id`
- `contract_type`
- `contract_status`
- `contract_current_phase_id`
- `contract_expected_outputs`
- `contract_metadata`

---

## 8. Edge shape

Each edge represents a semantic communication relationship.

Example:

```json
{
  "from_agent_id": "AG-0042",
  "to_agent_id": "AG-0048",
  "from_role": "researcher",
  "to_role": "judge",
  "topic_slug": "recent-rl-papers",
  "message_kind": "draft_for_review",
  "summary": "Submitting first draft for critique.",
  "last_active_at": "2026-03-29T08:01:10+00:00",
  "activity_strength": 1.0,
  "active_now": true
}
```

## Important fields
- `from_agent_id`
- `to_agent_id`
- `topic_slug`
- `message_kind`
- `summary`
- `last_active_at`
- `activity_strength`
- `active_now`

---

## 9. Topic card shape

Each topic card groups researcher/judge/shades and status.

Example:

```json
{
  "topic_slug": "recent-rl-papers",
  "title": "Recent RL papers",
  "status": "active",
  "phase": "judging",
  "checkpoint_count": 2,
  "best_checkpoint_id": "ckpt_...",
  "draft_report_path": ".../draft.md",
  "researcher": { "...": "Node" },
  "judge": { "...": "Node" },
  "shades": [ { "...": "Node" } ],
  "contracts": [ { "...": "ContractSummary" } ]
}
```

---

## 10. Contract summary shape

Contract summaries are useful in the topic detail panel.

Typical fields:
- `contract_id`
- `contract_type`
- `status`
- `current_phase_id`
- `current_phase_name`
- `current_phase_objective`
- `phase_count`
- `completed_phases`
- `shade_agent_id`
- `parent_agent_id`
- `metadata`
- `expected_outputs`
- `last_event_type`
- `last_event_ts`
- `last_event_payload`

---

## 11. F4 consumption rules

## Team grid view
Use:
- `views.grid.nodes` if present
- otherwise `team_grid_nodes`
- otherwise `non_shade_members`

Render only coordinator/researchers/judges in this view.
Shades should not take their own persistent grid cells.

## Graph view
Use:
- `topics`
- `views.graph.nodes` or `nodes`
- `views.graph.edges` or `edges`

Render shades only in the graph view or in expanded topic detail mode.

---

## 12. Refresh model

Current recommendation:
- poll every 1–2 seconds

This is enough for:
- edge brightness updates
- node phase changes
- live-line changes
- topic/card progress

Later this can be replaced with streamed room events.

---

## 13. Backward compatibility guidance

UI clients should tolerate missing optional fields.

Recommended fallbacks:
- if `views` missing, derive grid from non-shade `nodes`
- if `phase_summary` missing, display `phase`
- if `live_line` missing, display `goal` or topic title
- if `counts` missing, derive counts locally

---

## 14. Final summary

A single Libris swarm-state payload should support two synchronized F4 representations:
- a **non-shade team grid** for quick room/session switching among coordinator, researchers, and judges
- a **full graph view** for seeing topic structure, shade fanout, and active communication flow

This allows the user to switch between a practical operator view and a rich swarm visualization without leaving the same Libris room.
