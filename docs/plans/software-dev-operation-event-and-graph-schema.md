# Software Development Operation Event + Graph Schema

> Shared schema direction for making Autonomous Software Development Operation compatible with:
> - Charon chat-room / conversation structure
> - Libris graph visualization
> - F4 structured stream / judge view
>
> Date: 2026-03-28  
> Status: Proposed  
> Related: `docs/plans/autonomous-software-development-operation.md`, `docs/plans/unified-agent-role-taxonomy.md`, `docs/plans/libris-autonomous-research-operation.md`

---

## Purpose

The software-development swarm must not become a separate orchestration universe.

It should reuse the same broad primitives that Libris is proving, so that a software operation can be represented consistently in:

1. **chat-room / conversation structures**
2. **graph visualization**
3. **F4 structured operation stream**
4. **checkpoint / review storage**

This document defines the shared event and graph model for the software-development version.

---

# Design principles

## 1. Domain-specific labels, shared generic structure

Libris and software development should share the same generic topology.

- Libris work unit = `topic`
- Software work unit = `workstream`

The shared layer should prefer generic names where possible, while allowing domain-specific display labels.

---

## 2. Every important step must be representable in 3 ways

Every important operation step should be representable as:

- a **room/thread event**
- a **graph node/edge relationship**
- an **F4 stream item**

If a primitive cannot be rendered in all 3 modes, it is probably too bespoke.

---

## 3. Checkpoints and reviews are first-class

A software swarm is not just a sequence of tool calls.

The durable units the user and system care about are:
- assignments
- checkpoints
- reviews
- evidence bundles
- final selections

These must be explicit objects, not inferred from loose logs.

---

# 1. Shared generic operation concepts

These concepts should exist across both Libris and software-development operations.

## Node-like concepts
- `operation`
- `work_unit`
- `agent`
- `checkpoint`
- `review`
- `artifact`
- `evidence_bundle`
- `decision`

## Edge-like concepts
- `owns`
- `assigns`
- `spawns`
- `submits`
- `reviews`
- `criticizes`
- `selects`
- `depends_on`
- `produces`
- `verifies`

---

# 2. Software-development specialization of the generic model

## `operation`
A top-level software development run.

Example:
- “Build a web app that does X”
- “Refactor the auth system and stabilize tests”

## `work_unit`
A software workstream within the operation.

Examples:
- frontend
- backend
- auth
- infra
- integration-testing

## `agent`
An actor in the operation.

Examples:
- development coordinator
- implementer
- judge
- verifier
- shade
- boat-wrapped external worker

## `checkpoint`
A development snapshot submitted for review.

Examples:
- draft implementation checkpoint
- revised checkpoint after critique
- integrated candidate release checkpoint

## `review`
A judge/verifier evaluation of a checkpoint.

Examples:
- judge critique
- verifier release-readiness verdict

## `artifact`
A durable referenced output.

Examples:
- markdown summary
- draft report
- test log
- screenshot
- diff bundle
- final checkpoint bundle

## `evidence_bundle`
The verification/provenance bundle supporting a checkpoint.

Examples:
- files changed
- tests run
- build output
- benchmark logs
- browser checks
- screenshots

## `decision`
A coordinator/judge/verifier choice.

Examples:
- select best checkpoint
- request repair
- defer workstream
- finalize delivery

---

# 3. Chat-room compatibility model

The software-dev operation should be representable in a room/thread structure.

## 3.1 Operation room

One high-level room for the entire operation.

### Carries
- coordinator planning and assignment events
- workstream activation/deactivation
- judge verdict summaries
- final selection decisions
- budget / stop / resume updates

### Purpose
This is the main narrative thread the user watches.

---

## 3.2 Workstream subthreads

Each workstream should have a linked thread or room identity.

Examples:
- `frontend`
- `backend`
- `infra`
- `integration`

### Carries
- implementer activity summaries
- shade fanout summaries
- checkpoint submissions
- critique returns
- verifier results

### Purpose
This is where detailed progress unfolds per workstream.

---

## 3.3 Review-linked messages

Review messages should reference:
- `operation_id`
- `workstream_id`
- `checkpoint_id`
- `review_id`

This allows them to render in:
- room timelines
- graph view
- F4 stream
- future review UI

---

# 4. Graph visualization model

The graph should use the same high-level vocabulary as Libris.

---

## 4.1 Software-dev graph nodes

### Operation node
```json
{
  "node_type": "operation",
  "id": "op-webapp-001",
  "label": "Build web app that does X",
  "domain": "software_dev"
}
```

### Workstream node
```json
{
  "node_type": "work_unit",
  "work_unit_type": "workstream",
  "id": "ws-frontend",
  "label": "Frontend UI",
  "domain": "software_dev"
}
```

### Agent node
```json
{
  "node_type": "agent",
  "id": "AG-frontend-1",
  "runtime_role": "persistent_agent",
  "operation_role": "implementer",
  "specialization": "frontend"
}
```

### Checkpoint node
```json
{
  "node_type": "checkpoint",
  "id": "cp-frontend-03",
  "label": "Frontend checkpoint 03",
  "status": "submitted"
}
```

### Review node
```json
{
  "node_type": "review",
  "id": "rv-frontend-03",
  "label": "Judge review",
  "decision": "repair_requested"
}
```

### Evidence bundle node
```json
{
  "node_type": "evidence_bundle",
  "id": "ev-frontend-03",
  "label": "Evidence bundle"
}
```

### Decision node
```json
{
  "node_type": "decision",
  "id": "dec-final-01",
  "label": "Coordinator selected best checkpoint"
}
```

---

## 4.2 Software-dev graph edges

### Coordinator owns operation
```json
{
  "edge_type": "owns",
  "from": "AG-coordinator-1",
  "to": "op-webapp-001"
}
```

### Operation contains workstream
```json
{
  "edge_type": "owns",
  "from": "op-webapp-001",
  "to": "ws-frontend"
}
```

### Coordinator assigns implementer
```json
{
  "edge_type": "assigns",
  "from": "AG-coordinator-1",
  "to": "AG-frontend-1",
  "work_unit_id": "ws-frontend"
}
```

### Implementer spawns shade
```json
{
  "edge_type": "spawns",
  "from": "AG-frontend-1",
  "to": "AG-shade-17"
}
```

### Implementer submits checkpoint
```json
{
  "edge_type": "submits",
  "from": "AG-frontend-1",
  "to": "cp-frontend-03"
}
```

### Judge reviews checkpoint
```json
{
  "edge_type": "reviews",
  "from": "AG-judge-frontend",
  "to": "cp-frontend-03",
  "review_id": "rv-frontend-03"
}
```

### Review criticizes checkpoint
```json
{
  "edge_type": "criticizes",
  "from": "rv-frontend-03",
  "to": "cp-frontend-03",
  "decision": "repair_requested"
}
```

### Checkpoint produces evidence bundle
```json
{
  "edge_type": "produces",
  "from": "cp-frontend-03",
  "to": "ev-frontend-03"
}
```

### Verifier validates checkpoint
```json
{
  "edge_type": "verifies",
  "from": "AG-verifier-1",
  "to": "cp-integration-01"
}
```

### Coordinator selects final checkpoint
```json
{
  "edge_type": "selects",
  "from": "AG-coordinator-1",
  "to": "cp-final-02",
  "decision_id": "dec-final-01"
}
```

### Workstream dependency
```json
{
  "edge_type": "depends_on",
  "from": "ws-frontend",
  "to": "ws-backend"
}
```

---

# 5. Event schema for F4 and room timelines

The event stream should be structured, append-only, and operation-scoped.

## 5.1 Generic event envelope

```json
{
  "event_id": "evt-001",
  "ts": "2026-03-28T12:00:00Z",
  "operation_id": "op-webapp-001",
  "work_unit_id": "ws-frontend",
  "topic_slug": "",
  "kind": "checkpoint_submitted",
  "from_agent_id": "AG-frontend-1",
  "to_agent_id": "AG-judge-frontend",
  "from_role": "implementer",
  "to_role": "judge",
  "summary": "Frontend checkpoint 03 submitted for review",
  "payload": {}
}
```

### Notes
- `work_unit_id` is generic and preferred
- `topic_slug` can remain for Libris backward compatibility
- software-dev should use `work_unit_id` / `workstream_id`

---

## 5.2 Canonical software-dev event kinds

### Operation lifecycle
- `operation_created`
- `operation_started`
- `operation_paused`
- `operation_resumed`
- `operation_stop_requested`
- `operation_stopped`
- `operation_failed`
- `operation_completed`
- `operation_budget_warning`
- `operation_budget_exhausted`

### Workstream lifecycle
- `workstream_created`
- `workstream_selected`
- `workstream_deferred`
- `workstream_blocked`
- `workstream_unblocked`
- `workstream_completed`
- `workstream_failed`

### Assignment / coordination
- `work_assigned`
- `assignment_acknowledged`
- `dependency_declared`
- `dependency_resolved`
- `priority_changed`
- `handoff_sent`
- `handoff_received`

### Agent execution
- `agent_spawned`
- `agent_phase_changed`
- `agent_summary_reported`
- `shade_spawned`
- `shade_completed`
- `external_worker_attached`
- `external_worker_result_received`

### Checkpoints
- `checkpoint_started`
- `checkpoint_submitted`
- `checkpoint_saved`
- `checkpoint_revised`
- `best_checkpoint_nominated`
- `best_checkpoint_selected`

### Reviews / judge
- `review_requested`
- `review_started`
- `review_completed`
- `review_repair_requested`
- `review_accepted`
- `review_rejected`
- `review_escalated`

### Verification / integration
- `verification_started`
- `verification_passed`
- `verification_failed`
- `integration_started`
- `integration_blocked`
- `integration_ready`

### Delivery / finalization
- `final_candidate_selected`
- `delivery_bundle_prepared`
- `delivery_completed`

---

# 6. Software-dev checkpoint schema

A checkpoint must be usable in rooms, graph view, F4, and future review UI.

## 6.1 Checkpoint record

```json
{
  "checkpoint_id": "cp-frontend-03",
  "operation_id": "op-webapp-001",
  "workstream_id": "ws-frontend",
  "agent_id": "AG-frontend-1",
  "status": "submitted",
  "summary": "Added form validation and tests",
  "report_path": "operations/op-webapp-001/workstreams/frontend/checkpoints/cp-frontend-03.md",
  "evidence_bundle_id": "ev-frontend-03",
  "review_ids": ["rv-frontend-03"],
  "scorecard": {
    "requirements_fit": 0.84,
    "test_adequacy": 0.71,
    "code_quality": 0.79,
    "user_fit": 0.88,
    "overall": 0.80
  },
  "best_so_far": true,
  "created_at": "...",
  "updated_at": "..."
}
```

---

## 6.2 Evidence bundle record

```json
{
  "evidence_bundle_id": "ev-frontend-03",
  "operation_id": "op-webapp-001",
  "workstream_id": "ws-frontend",
  "checkpoint_id": "cp-frontend-03",
  "changed_files": [
    "src/ui/login.tsx",
    "tests/login.test.tsx"
  ],
  "artifacts": [
    {"type": "test_log", "path": ".../tests.log"},
    {"type": "screenshot", "path": ".../login-form.png"},
    {"type": "diff", "path": ".../patch.diff"}
  ],
  "verification": {
    "tests_passed": 12,
    "tests_failed": 1,
    "build_status": "passed"
  },
  "created_at": "..."
}
```

---

## 6.3 Review record

```json
{
  "review_id": "rv-frontend-03",
  "operation_id": "op-webapp-001",
  "workstream_id": "ws-frontend",
  "checkpoint_id": "cp-frontend-03",
  "reviewer_agent_id": "AG-judge-frontend",
  "status": "completed",
  "decision": "repair_requested",
  "summary": "Validation works, but tests miss edge cases.",
  "critique_path": ".../rv-frontend-03-critique.md",
  "scores": {
    "requirements_fit": 0.84,
    "test_adequacy": 0.58,
    "code_quality": 0.79,
    "user_fit": 0.88,
    "overall": 0.73
  },
  "requested_changes": [
    "Add tests for empty and malformed email input",
    "Verify disabled submit state"
  ],
  "created_at": "..."
}
```

---

# 7. F4 stream rendering model

F4 should present structured operation unfolding, not raw terminal output.

## 7.1 Stream sections

### A. Operation stream
Shows major operation events:
- operation started
- workstream selected
- final checkpoint selected
- delivery completed

### B. Active reviews
Shows:
- checkpoint submitted
- review started
- repair requested
- accepted

### C. Selected workstream detail
Shows:
- latest checkpoint
- latest review summary
- latest agent phase
- evidence links

---

## 7.2 Example F4 entries

```text
[12:02] Coordinator selected workstream: frontend UI
[12:03] Implementer(frontend) spawned 2 shades
[12:05] Shade completed: login validation tests drafted
[12:07] Frontend checkpoint cp-frontend-03 submitted
[12:08] Judge started review of cp-frontend-03
[12:09] Judge requested repair: missing edge-case tests
[12:10] Coordinator routed revision back to implementer(frontend)
[12:16] Frontend checkpoint cp-frontend-04 accepted
[12:21] Verifier passed integration checkpoint cp-integration-01
[12:23] Coordinator selected cp-final-02 for delivery
```

---

# 8. Compatibility with Libris

The software-dev schema should intentionally mirror Libris.

## Libris mapping
- `operation` → research operation
- `work_unit` → topic
- `worker` → researcher
- `review` → judge critique
- `checkpoint` → report checkpoint
- `evidence_bundle` → source/claim/provenance bundle

## Software-dev mapping
- `operation` → development operation
- `work_unit` → workstream
- `worker` → implementer
- `review` → judge critique
- `checkpoint` → development checkpoint
- `evidence_bundle` → test/build/diff/screenshot bundle

This shared shape should allow:
- similar graph rendering code
- similar F4 structured stream rendering code
- similar room/thread visualization
- similar future review-space UI

---

# 9. Recommended storage direction

The storage layer should remain explicit and inspectable.

## Suggested files

```text
.charon_state/software_ops/
  operations.json
  operation_events.jsonl
  checkpoints.json
  reviews.json
  evidence_bundles.json
```

Or, if unified with generic operation storage later:

```text
.charon_state/operations/
  <operation_id>/
    operation.json
    events.jsonl
    work_units/
    checkpoints/
    reviews/
    artifacts/
```

The latter is likely the cleaner long-term direction if Libris and software-dev eventually share generic operation primitives.

---

# 10. Migration guidance

## Near-term
Do not force Libris and software-dev into one implementation immediately.

Instead:
- keep Libris implementation moving
- design software-dev schemas to mirror Libris where possible
- ensure event names and graph shapes are compatible

## Medium-term
Extract shared generic operation primitives once both systems prove the pattern.

Likely generic primitives:
- operation state
- work unit state
- checkpoint state
- review state
- event log
- graph projection

---

# Compact summary

The software-development swarm should use the same structural pattern as Libris and be representable uniformly in:
- chat-room / conversation structures
- graph visualization
- F4 structured stream
- checkpoint/review storage

To do that, it should model explicit:
- operations
- workstreams
- agents
- checkpoints
- reviews
- evidence bundles
- decisions

and emit append-only structured events for assignments, checkpoints, reviews, verification, and final selection. Libris and software-dev should differ mainly in labels and artifact content, not in orchestration shape.
