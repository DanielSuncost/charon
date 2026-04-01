# Generic Operation Primitives

> Shared primitive layer for multi-agent operations in Charon.
>
> This is the reusable coordination substrate that should eventually support:
> - Libris autonomous research operations
> - Autonomous software development operations
> - future multi-agent operation types
>
> Date: 2026-03-28  
> Status: Proposed  
> Related: `docs/plans/libris-autonomous-research-operation.md`, `docs/plans/autonomous-software-development-operation.md`, `docs/plans/software-dev-operation-event-and-graph-schema.md`, `docs/plans/unified-agent-role-taxonomy.md`

---

## Purpose

Charon now has enough multi-agent behavior that operation-specific orchestration should not be reinvented separately for each domain.

Libris and Autonomous Software Development Operation should eventually share a generic operation substrate for:
- operation state
- work unit state
- assignments and handoffs
- checkpoints
- reviews
- evidence bundles
- decisions
- event streams
- graph projection
- room/thread projection

This document defines those generic primitives.

---

# Design principles

## 1. Generic core, domain-specific overlays

The generic layer should define reusable operation objects.

Domain layers should map onto them.

Examples:
- Libris: `work_unit = topic`
- Software-dev: `work_unit = workstream`

The generic layer should not hardcode research or coding assumptions.

---

## 2. Explicit state beats inference

Important multi-agent transitions should be recorded as first-class objects, not inferred from prompts or loose logs.

Examples:
- a checkpoint was submitted
- a review was requested
- a reviewer requested repair
- a coordinator selected a best-so-far result

These should not be reconstructed only from message text.

---

## 3. One primitive, multiple projections

Every generic primitive should be projectable into:
- storage state
- event stream
- room/thread narrative
- graph view
- F4 operation stream

If a primitive only works in one projection, it is underspecified.

---

## 4. Runtime roles and operation roles stay separate

These primitives must integrate with the unified role taxonomy:
- runtime role
- operation role
- specialization

The generic operation layer does not replace the role taxonomy; it uses it.

---

# 1. Core primitives

The generic operation substrate should define these primary objects.

## 1.1 Operation

Top-level multi-agent run for a broad user directive.

### Responsibilities
- owns all work units
- owns budgets and policy
- tracks global status
- records final decisions and deliveries

### Generic schema sketch

```json
{
  "operation_id": "op-001",
  "domain": "research | software_dev | other",
  "title": "Build a web app that does X",
  "prompt": "...",
  "status": "created | running | paused | stopped | failed | delivered | budget_exhausted",
  "coordinator_agent_id": "AG-001",
  "project_id": "charon-c564e8fd",
  "budget": {},
  "usage": {},
  "policy": {},
  "work_unit_ids": [],
  "selected_output_ids": [],
  "created_at": "...",
  "updated_at": "..."
}
```

### Domain mappings
- Libris: research operation
- Software-dev: development operation

---

## 1.2 Work Unit

A bounded lane of work inside an operation.

### Responsibilities
- gives the coordinator a manageable unit of delegation
- anchors worker assignment, checkpoints, and reviews
- provides the domain-specific focus point

### Generic schema sketch

```json
{
  "work_unit_id": "wu-frontend",
  "operation_id": "op-001",
  "work_unit_type": "topic | workstream | investigation | track",
  "title": "Frontend UI",
  "slug": "frontend-ui",
  "status": "proposed | selected | active | judging | revising | blocked | completed | dropped",
  "priority": 0.82,
  "owner_agent_id": "AG-frontend-1",
  "reviewer_agent_id": "AG-judge-1",
  "dependency_ids": [],
  "checkpoint_ids": [],
  "best_checkpoint_id": null,
  "metadata": {},
  "created_at": "...",
  "updated_at": "..."
}
```

### Domain mappings
- Libris: topic
- Software-dev: workstream

---

## 1.3 Assignment / Handoff

A directed transfer of responsibility or material between actors.

### Responsibilities
- records who asked whom to do what
- makes agent-to-agent transitions explicit
- supports room projection and graph projection

### Generic schema sketch

```json
{
  "handoff_id": "ho-001",
  "operation_id": "op-001",
  "work_unit_id": "wu-frontend",
  "kind": "assignment | draft_submission | review_request | critique_return | verification_request | decision_notice",
  "from_agent_id": "AG-coordinator-1",
  "to_agent_id": "AG-frontend-1",
  "from_role": "coordinator",
  "to_role": "implementer",
  "subject_id": "cp-003",
  "payload": {},
  "status": "sent | received | acknowledged | superseded",
  "created_at": "..."
}
```

### Why this matters
This is the clean generic object that bridges:
- operation control flow
- room narrative
- graph edges
- inspectable coordination

---

## 1.4 Checkpoint

A durable submitted intermediate result.

### Responsibilities
- gives judges and coordinators something stable to review
- supports interruption/resume
- enables best-version selection
- acts as a meaningful progress milestone

### Generic schema sketch

```json
{
  "checkpoint_id": "cp-001",
  "operation_id": "op-001",
  "work_unit_id": "wu-frontend",
  "producer_agent_id": "AG-frontend-1",
  "status": "draft | submitted | reviewed | accepted | rejected | best_so_far | delivered",
  "summary": "Added validation and tests",
  "artifact_refs": [],
  "evidence_bundle_id": "ev-001",
  "review_ids": [],
  "scorecard": {},
  "metadata": {},
  "created_at": "...",
  "updated_at": "..."
}
```

### Domain mappings
- Libris: report checkpoint
- Software-dev: implementation checkpoint

---

## 1.5 Review

A structured evaluation of a checkpoint.

### Responsibilities
- captures critique, scoring, and decision
- separates evaluation from execution
- enables judge/verifier roles

### Generic schema sketch

```json
{
  "review_id": "rv-001",
  "operation_id": "op-001",
  "work_unit_id": "wu-frontend",
  "checkpoint_id": "cp-001",
  "reviewer_agent_id": "AG-judge-1",
  "review_type": "judge | verifier | selector_support",
  "status": "queued | reviewing | completed",
  "decision": "accept | repair_requested | reject | escalate | verify_more",
  "summary": "Good progress, but edge cases are missing.",
  "critique_ref": null,
  "scores": {},
  "requested_changes": [],
  "created_at": "...",
  "updated_at": "..."
}
```

### Domain mappings
- Libris: report critique
- Software-dev: code/result critique or system verification verdict

---

## 1.6 Evidence Bundle

The provenance and verification support attached to a checkpoint.

### Responsibilities
- records what supports a checkpoint
- makes judgments auditable
- helps users inspect “why this was accepted/rejected”

### Generic schema sketch

```json
{
  "evidence_bundle_id": "ev-001",
  "operation_id": "op-001",
  "work_unit_id": "wu-frontend",
  "checkpoint_id": "cp-001",
  "artifacts": [],
  "verification": {},
  "summary": "12 tests passed; 1 screenshot captured; sources verified.",
  "created_at": "..."
}
```

### Domain mappings
- Libris: sources, claims, provenance, citation checks
- Software-dev: diffs, tests, builds, screenshots, benchmarks, endpoint checks

---

## 1.7 Decision

A coordinator or selector choice that changes operation direction.

### Responsibilities
- records important steering/finalization decisions
- enables best-version selection and resumability

### Generic schema sketch

```json
{
  "decision_id": "dec-001",
  "operation_id": "op-001",
  "work_unit_id": "wu-frontend",
  "decision_type": "select_best_checkpoint | drop_work_unit | escalate_to_user | finalize_delivery | reprioritize",
  "actor_agent_id": "AG-coordinator-1",
  "subject_id": "cp-004",
  "summary": "Checkpoint cp-004 chosen as best-so-far",
  "metadata": {},
  "created_at": "..."
}
```

---

# 2. Generic supporting layers

These are not top-level artifacts the user necessarily manipulates directly, but they are required for a coherent operation system.

## 2.1 Operation Event

Append-only structured event used for streaming, room projection, and graph updates.

### Generic schema sketch

```json
{
  "event_id": "evt-001",
  "ts": "...",
  "operation_id": "op-001",
  "work_unit_id": "wu-frontend",
  "kind": "checkpoint_submitted",
  "from_agent_id": "AG-frontend-1",
  "to_agent_id": "AG-judge-1",
  "summary": "Frontend checkpoint submitted",
  "payload": {}
}
```

### Rule
All significant state changes should emit an operation event.

---

## 2.2 Graph Projection

A derived view over operation primitives.

### Input
- operations
- work units
- handoffs
- checkpoints
- reviews
- decisions

### Output
- nodes
- edges
- optional layout metadata

### Rule
The graph should be **derived**, not the source of truth.

---

## 2.3 Room / Thread Projection

A derived narrative view over operation primitives.

### Input
- operation events
- handoffs
- checkpoint submissions
- review summaries
- decisions

### Output
- operation room timeline
- work-unit subthreads
- review-linked messages

### Rule
Room/thread state should be a projection over operation state and events, not a separate hidden orchestration system.

---

## 2.4 F4 Stream Projection

A derived operational stream view over the same events.

### Input
- operation events
- active reviews
- latest checkpoints
- latest decisions

### Output
- structured live unfolding of operation

### Rule
F4 should show structured operation progression, not rely only on raw terminal logs.

---

# 3. Generic lifecycle shape

The generic layer should support this broad lifecycle across domains.

## Generic flow
1. create operation
2. define/select work units
3. assign workers
4. workers may spawn subworkers/shades
5. worker submits checkpoint
6. reviewer evaluates checkpoint
7. decision made:
   - accept
   - revise
   - reject
   - escalate
8. best checkpoint may be nominated/selected
9. final outputs chosen
10. operation delivered / paused / stopped

This is the shared skeleton across Libris and software-dev.

---

# 4. Domain overlay examples

## 4.1 Libris overlay

### Generic → Libris
- operation → research operation
- work unit → topic
- worker → researcher
- review → judge critique
- evidence bundle → source/claim/provenance set
- decision → final report selection

---

## 4.2 Software-dev overlay

### Generic → software-dev
- operation → development operation
- work unit → workstream
- worker → implementer
- review → judge/verifier review
- evidence bundle → diff/test/build/screenshot bundle
- decision → best checkpoint selection / delivery choice

---

# 5. Recommended generic identifiers

For future consistency, generic IDs should avoid domain-specific prefixes.

Examples:
- `op-...` for operation
- `wu-...` for work unit
- `ho-...` for handoff
- `cp-...` for checkpoint
- `rv-...` for review
- `ev-...` for evidence bundle
- `dec-...` for decision
- `evt-...` for event

Domain-specific display names can still exist, but internal IDs should remain generic.

---

# 6. Compatibility with current codebase

This generic layer should align with existing Charon primitives rather than replace them abruptly.

## Existing primitives that already help
- `ConversationEngine`
- `shade_orchestrator.py`
- `judge_engine.py`
- `goal_runtime.py`
- `libris_runtime.py`
- room/conversation structures
- graph visualization in the Libris path

## Important rule
The generic operation layer should **not** force immediate rewrites.

Instead:
- current Libris code can keep moving
- software-dev design can mirror Libris now
- shared primitives can later be extracted from proven patterns

---

# 7. Recommended implementation strategy

## Phase 1: Domain-first, shape-aligned
- keep Libris using `libris_runtime.py`
- design software-dev runtime using equivalent object shapes
- align event names and graph shapes now

## Phase 2: Shared projection helpers
- create shared graph projection helpers
- create shared event-to-room projection helpers
- create shared F4 stream projection helpers

## Phase 3: Shared generic operation store
- extract operation/work_unit/checkpoint/review/evidence/decision into a common runtime layer
- keep domain overlays thin

---

# 8. Non-goals

This generic operation layer is **not** meant to:
- eliminate domain-specific richness
- force one monolithic runtime immediately
- replace all Charon task/goal systems
- expose every internal object directly to end users

It is a coordination substrate, not a user-facing complexity explosion.

---

# Compact summary

Charon should eventually support a shared generic operation substrate built from these primitives:
- **Operation**
- **Work Unit**
- **Assignment/Handoff**
- **Checkpoint**
- **Review**
- **Evidence Bundle**
- **Decision**
- **Operation Event**

These primitives should project cleanly into:
- persistent storage
- room/thread narratives
- graph visualization
- F4 structured operation streams

Libris and Autonomous Software Development Operation should differ mainly in domain overlays and artifact content, not in the fundamental shape of coordination.
