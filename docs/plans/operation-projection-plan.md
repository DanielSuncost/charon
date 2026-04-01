# Operation Projection Plan

> Projection model for turning generic operation primitives into:
> - room/thread narratives
> - graph visualization
> - F4 structured operation streams
>
> This plan is intended to keep Libris and Autonomous Software Development Operation aligned.
>
> Date: 2026-03-28  
> Status: Proposed  
> Related: `docs/plans/generic-operation-primitives.md`, `docs/plans/software-dev-operation-event-and-graph-schema.md`, `docs/plans/libris-autonomous-research-operation.md`, `docs/plans/autonomous-software-development-operation.md`

---

## Purpose

Charon should not maintain separate hidden orchestration representations for:
- storage state
- room narratives
- graph visualization
- F4 streams

Instead, operation state should be canonical, and the UI-facing views should be **projections** of that state.

This document defines how generic operation primitives should project into the 3 main UI/narrative surfaces:

1. **Rooms / threads**
2. **Graph view**
3. **F4 structured stream**

---

# Core principle

## Canonical truth

The canonical layer should be:
- operation records
- work unit records
- handoff records
- checkpoint records
- review records
- evidence bundle records
- decision records
- append-only operation events

## Derived views

The following should be projections, not separate hidden truth sources:
- room timelines
- room summaries
- graph nodes/edges
- F4 stream cards
- operation overview widgets

This keeps the system inspectable and avoids drift between views.

---

# 1. Input primitives for projection

All projections consume the same generic operation substrate.

## Required inputs
- `Operation`
- `WorkUnit`
- `Assignment/Handoff`
- `Checkpoint`
- `Review`
- `EvidenceBundle`
- `Decision`
- `OperationEvent`

## Optional enrichments
- runtime role / operation role / specialization
- latest agent phase summary
- budget status
- linked artifacts
- controller relationships

---

# 2. Room / thread projection

Rooms should render the operation as a human-readable collaborative narrative.

---

## 2.1 Projection goals

Rooms should answer:
- what is happening at the operation level?
- what is happening inside each work unit?
- what was handed off to whom?
- what checkpoint is under review?
- what did the judge/verifier decide?
- what did the coordinator choose next?

Rooms should **not** become the hidden execution engine.

---

## 2.2 Room topology

### A. Operation room

One top-level room per operation.

#### Contains
- operation lifecycle events
- coordinator decisions
- work-unit selection and prioritization
- summary review outcomes
- final output selection
- stop/pause/resume state

#### Purpose
This is the main narrative the user watches.

---

### B. Work-unit subthreads

Each work unit gets a linked subthread or room identity.

Examples:
- Libris: topic thread
- Software-dev: workstream thread

#### Contains
- assignment receipt
- worker progress summaries
- shade summaries
- checkpoint submissions
- local reviews and revision requests
- verifier/integration notes if scoped locally

#### Purpose
This is the detailed lane-level timeline.

---

### C. Review-linked messages

Each review should be projectable as a linked message object that references:
- `operation_id`
- `work_unit_id`
- `checkpoint_id`
- `review_id`

This allows room UIs to support:
- jumping from room timeline → checkpoint
- jumping from checkpoint → critique
- jumping from critique → selected best version

---

## 2.3 Room message classes

Projection should create narrative message classes like:

### `operation_status`
Examples:
- operation started
- budget warning
- operation stopped

### `assignment`
Examples:
- coordinator assigned workstream to implementer
- coordinator opened topic for researcher

### `worker_progress`
Examples:
- implementer submitted summary
- researcher completed evidence pass
- shade completed bounded task

### `checkpoint_notice`
Examples:
- checkpoint submitted
- checkpoint marked best-so-far

### `review_notice`
Examples:
- judge requested repair
- verifier accepted checkpoint

### `decision_notice`
Examples:
- coordinator selected final output
- coordinator deferred a work unit

---

## 2.4 Projection rules for rooms

### Rule 1
Every significant `OperationEvent` should be convertible into a room message.

### Rule 2
Not every low-level event must be shown in the top-level room.
Use filtering/aggregation.

### Rule 3
Room messages should preserve links to canonical IDs.
Do not flatten them into pure prose.

### Rule 4
Room summary text should be regenerated from canonical records when needed, not treated as the true source.

---

# 3. Graph projection

The graph should show topology, dependencies, handoffs, and review structure.

---

## 3.1 Projection goals

The graph should answer:
- what actors exist?
- what work units exist?
- who owns what?
- who handed what to whom?
- what checkpoints were produced?
- what reviews attached to those checkpoints?
- what decisions selected final outputs?
- what dependencies or bottlenecks exist?

---

## 3.2 Graph node projection

### From `Operation`
Project an `operation` node.

### From `WorkUnit`
Project a `work_unit` node.

### From agent identity/role state
Project an `agent` node.

### From `Checkpoint`
Project a `checkpoint` node.

### From `Review`
Project a `review` node.

### From `EvidenceBundle`
Project an `evidence_bundle` node.

### From `Decision`
Project a `decision` node.

---

## 3.3 Graph edge projection

### `Operation` → `WorkUnit`
Project `owns`

### `Coordinator` → `Worker`
Project `assigns`

### `Worker` → `Checkpoint`
Project `submits`

### `Reviewer` → `Checkpoint`
Project `reviews`

### `Review` → `Checkpoint`
Project `criticizes`

### `Checkpoint` → `EvidenceBundle`
Project `produces`

### `Decision` → `Checkpoint`
Project `selects`

### `WorkUnit` → `WorkUnit`
Project `depends_on`

### `Worker` → `Shade`
Project `spawns`

### `Verifier` → `Checkpoint`
Project `verifies`

---

## 3.4 Graph projection rules

### Rule 1
Graph is derived from canonical records, not manually edited.

### Rule 2
Graph may add layout/group metadata, but not new semantic truth.

### Rule 3
Libris and software-dev should use the same base node/edge vocabulary.
They differ mainly by labels and artifact types.

### Rule 4
Graph should support filtering by:
- operation
- work unit
- role
- agent
- checkpoint status
- review decision

---

# 4. F4 structured stream projection

F4 should show the live unfolding of an operation as a structured event stream.

---

## 4.1 Projection goals

F4 should answer:
- what is the operation doing right now?
- what is the currently active work unit?
- what is the judge/verifier doing?
- what checkpoint was just submitted?
- what critique/decision just happened?
- what is the coordinator about to do next?

F4 is not just a room view and not just a session view.
It is the **structured operational stream**.

---

## 4.2 F4 stream sections

### A. Operation stream
Top-level sequence of major operation events.

Examples:
- operation created
- work unit selected
- budget warning
- delivery selected

### B. Active reviews
Current reviews in progress.

Examples:
- judge reviewing checkpoint cp-003
- verifier validating integrated output

### C. Work-unit detail panel
Shows the selected work unit’s:
- latest phase
- latest checkpoint
- latest review summary
- latest decision state

### D. Artifact/detail panel
Shows linked:
- report path
- checkpoint summary
- critique summary
- evidence bundle summary

---

## 4.3 Stream item classes

### `operation_event_item`
For major lifecycle/coordination events.

### `phase_item`
For agent/work-unit phase changes.

### `checkpoint_item`
For checkpoint creation/submission/selection.

### `review_item`
For review start/completion/verdict.

### `decision_item`
For coordinator/verifier/judge decisions.

---

## 4.4 F4 projection rules

### Rule 1
F4 items should come from canonical events plus current state snapshots.

### Rule 2
F4 must preserve canonical IDs so the user can jump to:
- work unit
- checkpoint
- review
- artifact

### Rule 3
F4 should support role/domain filtering:
- coordinator only
- judge only
- verifier only
- one work unit only

### Rule 4
F4 should show the latest structured state even if some raw worker sessions are hidden.

---

# 5. Shared projection mapping table

| Primitive | Room projection | Graph projection | F4 projection |
|---|---|---|---|
| Operation | operation room | operation node | operation stream item |
| Work Unit | subthread | work_unit node | work-unit card/detail |
| Handoff | assignment/review request message | assigns/submits/reviews edge | stream handoff item |
| Checkpoint | checkpoint notice | checkpoint node | checkpoint item |
| Review | critique/review notice | review node + reviews edge | review item |
| Evidence Bundle | linked artifact summary | evidence_bundle node | detail panel item |
| Decision | decision notice | decision node + selects edge | decision item |
| Operation Event | timeline message | optional graph update trigger | core stream event |

---

# 6. Projection timing model

Different projections can update at different cadences while sharing the same source.

## Room projection
- append or refresh on significant events
- summarize noisy repeated worker events

## Graph projection
- update when topology-changing events happen
- may refresh incrementally or on demand

## F4 projection
- update continuously from operation events and state snapshots
- optimized for live unfolding

---

# 7. Projection-specific aggregation rules

## 7.1 Rooms prefer narrative aggregation

Rooms should compress noise.

Example:
- five shade completions may collapse into one worker progress summary

## 7.2 Graph prefers structural fidelity

Graph should retain important topology distinctions.

Example:
- multiple checkpoints and reviews remain distinct nodes

## 7.3 F4 prefers temporal clarity

F4 should show the unfolding sequence clearly.

Example:
- checkpoint submitted
- review started
- repair requested
- revision spawned
- checkpoint accepted

These should remain separate stream items.

---

# 8. Compatibility with Libris

This projection plan is intentionally generic.

## Libris mapping
- operation room = research operation room
- work-unit thread = topic thread
- graph work_unit node = topic node
- checkpoint = report checkpoint
- review = judge critique
- evidence = sources/claims/provenance
- F4 = research swarm progression

## Software-dev mapping
- operation room = development operation room
- work-unit thread = workstream thread
- graph work_unit node = workstream node
- checkpoint = implementation checkpoint
- review = judge/verifier verdict
- evidence = tests/build/diff/screenshots
- F4 = development swarm progression

---

# 9. Recommended implementation sequence

## Phase 1
Ensure all domain runtimes emit clean canonical operation events.

## Phase 2
Build shared projection helpers:
- event → room message
- state → graph nodes/edges
- events + state → F4 stream items

## Phase 3
Add operation-aware navigation in UI:
- jump from stream item → room thread
- jump from stream item → graph focus
- jump from checkpoint → review → artifact

---

# 10. Non-goals

This projection plan is not intended to:
- define the full visual design of every UI
- replace raw session viewing in F3
- require a monolithic renderer

It only defines how canonical operation state should feed multiple views consistently.

---

# Compact summary

Canonical operation primitives should project consistently into 3 main user-facing views:

1. **Rooms/threads** for human-readable collaborative narrative
2. **Graph view** for structural topology and dependencies
3. **F4 structured stream** for live operation unfolding

These should all derive from the same canonical state:
- operations
- work units
- handoffs
- checkpoints
- reviews
- evidence bundles
- decisions
- operation events

Libris and software-dev operations should therefore share a projection model even when their domain labels and artifacts differ.
