# Libris Remaining Work Roadmap

> Shared implementation snapshot and forward plan for finishing Libris.
>
> Date: 2026-03-29
> Status: Active
> Related:
> - `docs/plans/libris-implementation-plan.md`
> - `docs/plans/libris-implementation-architecture.md`
> - `docs/plans/libris-autonomous-research-operation.md`
> - `docs/plans/libris-f4-graph-viz-spec.md`

---

## 1. Goal

Finish Libris as a native Charon research system with:
- broad source acquisition
- coordinator/researcher/judge/shade orchestration
- checkpointed iterative refinement
- long-running autonomous operation with budgets
- a live F4 room/graph view for observing and intervening in research swarms

---

## 2. Current status summary

## Done

### Core runtime and storage
- research-enabled project layout
- Libris operations
- topic dossiers
- checkpoints
- final delivery artifacts
- operation budgets / usage accounting / model policy
- promising-source index
- swarm-state projection

### Tooling
- `Research`
- `Paper`
- `SourceDiscovery`
- existing `Web` / `Browser`

### Roles and orchestration
- coordinator agent
- researcher agent
- judge agent
- source-procurement shades
- basic coordinator fanout
- bounded researcher → judge → researcher → judge loop
- final coordinator checkpoint selection

### Visualization backend
- role-labeled nodes
- communication edges
- dynamic phases
- contract-aware shade nodes
- activity-strength decay for edges
- F4 graph viz spec

### Intake
- `/libris <prompt>`
- natural language trigger for starting Libris
- goal clarification options
- custom goal support
- stop-condition capture
- partial structured budget parsing

---

## In progress

### Procurement ingestion
- procurement shade outputs can now be summarized and partially ingested
- still needs more reliable source/claim extraction and stronger evidence integration

### Graph-ready backend
- backend shape is strong enough for F4
- frontend implementation still pending

---

## Not yet done

### UI / room experience
- actual F4 room implementation
- room switching between Libris/dev-team/conversation rooms
- graph rendering
- animated bright/dim communication lines
- intervention UX in the room

### Stronger research quality loop
- critique decomposition into bounded follow-up questions
- targeted gap-fill shades
- multiple bounded judge cycles beyond the current simple loop
- better stopping logic for convergence/plateau

### Better source acquisition and ranking
- stronger result fusion across backends
- richer nontechnical scholarly sources
- stronger official-source discovery
- better ranking signals and deduping

### Delivery polish
- operation-level executive summary
- ranked multi-topic delivery bundle
- user-facing concise overview plus full reports

### Reliability
- resume/recovery for long-running research operations
- stronger watchdog/retry logic
- more complete budget enforcement and adaptive planner behavior

---

## 3. Priority order

## Priority 1 — highest value now

### P1. F4 room + graph UI
Build the first working F4 Libris room using current swarm state.

Acceptance:
- can switch to F4
- can select a Libris room/operation
- graph renders coordinator/researchers/judges/shades
- edges brighten when active
- topic clusters are visible
- selection/detail panel works

### P2. Improve canonical ingestion from shade outputs
Make procurement and future research shades feed structured outputs into:
- sources
- claims
- evidence
- report drafts

Acceptance:
- shade work reliably improves downstream researcher quality
- shade outputs are not just freeform summaries

### P3. Stronger critique-driven refinement
Make judge outputs drive better follow-up work.

Acceptance:
- critiques create bounded next-step tasks
- researcher revisions clearly respond to prior critiques
- optional targeted shade reruns for gaps

### P4. Better final delivery bundle
Coordinator should produce a polished final result.

Acceptance:
- executive summary across topics
- why each chosen topic matters
- best checkpoint selection is visible and inspectable

---

## Priority 2 — next after that

### P5. Shade contract taxonomy
Add explicit Libris contract types:
- `libris_paper_triage`
- `libris_claim_extraction`
- `libris_gap_fill`
- `libris_contradiction_check`
- `libris_implementation_signal_check`

### P6. Better ranking/fusion
Improve promising-source ranking using:
- backend-aware weighting
- recency quality
- stronger deduping
- influence/citation signals where possible

### P7. Nontechnical research support
Extend source acquisition for nontechnical domains.

Targets:
- OpenAlex deeper use
- scholarly/general bibliographic sources
- institutional/government repositories
- library/catalog style sources

---

## Priority 3 — polish and robustness

### P8. Long-running reliability
- restart safety
- partial recovery
- watchdogs
- stale-agent detection
- stronger budget adaptation

### P9. Richer room/intervention semantics
- room-level interventions
- node-targeted interventions
- room message provenance
- mixed-agent room polish (Charon/Hermes/Pi)

### P10. Advanced graph UX
- topic focus mode
- collapsible shade trays
- richer pulse animation
- event timeline playback

---

## 4. Immediate next build sequence

### Step 1
Implement the first working F4 room UI against the existing swarm-state backend.

### Step 2
Strengthen procurement ingestion so completed shade contracts produce better canonical source/claim records.

### Step 3
Improve the researcher/judge loop so critiques trigger more targeted follow-up work.

### Step 4
Add polished final delivery bundle generation.

### Step 5
Expand shade contract taxonomy.

---

## 5. Concrete task checklist

## Backend
- [ ] Improve `libris_procurement_ingest.py` claim/source extraction quality
- [ ] Add critique-to-gap task generation
- [x] Add `libris_gap_fill` contract type
- [x] Add `libris_claim_extraction` contract type
- [x] Add `libris_contradiction_check` contract type
- [ ] Add operation-level executive summary generation
- [ ] Add multi-topic delivery bundle artifact
- [ ] Improve result fusion between `Paper` and `SourceDiscovery`
- [ ] Add stronger budget adaptation logic in coordinator

## TUI / room system
- [ ] Add F4 room registration
- [ ] Add Libris room type
- [ ] Poll `get_swarm_state(operation_id)`
- [ ] Render topic-clustered graph
- [ ] Render communication edges with brightness from `activity_strength`
- [ ] Add detail panel for selected node/topic
- [ ] Add room event log panel
- [ ] Add intervention controls

## Specs / docs
- [ ] Add Libris intake contract doc
- [ ] Add Libris swarm-state data contract doc
- [ ] Add Libris shade contract taxonomy doc
- [ ] Add Libris checkpoint/judge rubric spec

---

## 6. Definition of done for Libris v1.0

Libris v1.0 is done when all of the following are true:

1. A user can say something like:
   - "start a Libris research project on emerging techniques in computer vision from the last few months"
2. Libris performs intake, including goal clarification and optional stopping conditions
3. Libris launches a coordinator-led swarm
4. The coordinator uses broad source acquisition and lead scoring
5. Researchers, judges, and shades produce structured artifacts
6. Libris iterates through at least one meaningful critique/refinement cycle
7. The coordinator selects final reports for delivery
8. The user can switch to F4 and watch the room/graph live
9. The user can inspect nodes, edges, topics, and checkpoints
10. The user can intervene in the room without breaking the operation

---

## 7. Recommendation right now

The best parallel split is:

### TUI side
Build the first working F4 room against the current swarm-state backend.

### Backend side
Continue strengthening:
- procurement ingestion
- critique-driven reruns
- final delivery bundle

This gets Libris to a compelling demo state fastest.
