# Libris ↔ Software Development Convergence Notes

> Notes on where Libris implementation patterns should later converge with the software-development swarm.
>
> Date: 2026-03-28  
> Status: Proposed  
> Related: `docs/plans/generic-operation-primitives.md`, `docs/plans/software-dev-runtime-module-plan.md`, `apps/core-daemon/libris_runtime.py`

---

## Purpose

Libris is currently the proving ground for Charon’s richer multi-agent operation model.

The software-development swarm should not fork away from it. Instead, we should identify exactly where the two systems can converge once Libris has settled enough real-world patterns.

This document records those likely convergence points.

---

# 1. What Libris already proves well

Libris already demonstrates useful domain runtime patterns:
- operation lifecycle state
- topic selection and activation
- candidate list persistence
- checkpoint persistence
- best-checkpoint nomination and final selection
- operation event log
- swarm-state summary generation
- role runners for coordinator/researcher/judge
- budget and usage tracking

These are strong indicators of what the generic multi-agent operation layer should look like.

---

# 2. Strongest convergence candidates

These are the pieces most likely to become shared generic infrastructure.

---

## 2.1 Operation state shape

### Libris today
`libris_runtime.py` already has:
- `init_operation`
- `get_operation_state`
- `set_operation_status`
- budget/usage tracking
- final selection support

### Software-dev analogue
Needs almost the same thing:
- create operation
- get operation state
- update status
- track budgets
- record final selection

### Convergence note
Operation record shape is an excellent candidate for generic extraction later.

Likely shared generic fields:
- operation id
- domain
- prompt/title
- status
- coordinator id
- budget
- usage
- selected work units
- delivered outputs

---

## 2.2 Work unit state shape

### Libris today
Topic state in `libris_runtime.py` already tracks:
- title/slug
- status
- researcher/judge ids
- checkpoint count
- best checkpoint id
- round counters
- budget/model override

### Software-dev analogue
Workstream state needs nearly the same shape.

### Convergence note
`topic` and `workstream` are likely just domain overlays on the same generic `work_unit` object.

---

## 2.3 Checkpoint persistence

### Libris today
Libris already has:
- checkpoint metadata
- checkpoint markdown artifacts
- checkpoint listing
- best-checkpoint nomination
- final selection support

### Software-dev analogue
Needs the same pattern, just with different artifact content.

### Convergence note
Checkpoint storage should converge strongly.
Main differences are artifact type and score labels, not structural shape.

---

## 2.4 Event log semantics

### Libris today
`append_operation_event(...)` is already a core operation-stream primitive.

### Software-dev analogue
Needs the exact same thing.

### Convergence note
Operation event envelope should become shared early, even before full runtime convergence.
This is the most important shared projection surface for:
- rooms
- graph view
- F4

---

## 2.5 Swarm-state summary generation

### Libris today
`get_libris_swarm_state(...)` already produces structured view-model-ish state.

### Software-dev analogue
Will need equivalent operation state summaries.

### Convergence note
This summary/projection layer is a good candidate for shared helper extraction later.

---

## 2.6 Role runner wrapper pattern

### Libris today
`libris_agents.py` already provides:
- role prompt construction
- role instruction construction
- agent spawning
- background operation control

### Software-dev analogue
Will need the same overall shape.

### Convergence note
The role-runner skeleton should converge, while role prompts and orchestration logic remain domain-specific.

---

# 3. Moderate convergence candidates

These are likely shareable, but should probably stabilize in Libris first.

---

## 3.1 Budget + model policy handling

### Libris today
Already supports:
- max wall time
- token/cost ceilings
- max topics
- max checkpoints
- role→model policy

### Software-dev analogue
Will need:
- max workstreams
- max revisions
- max concurrent workers/shades
- role→model policy

### Convergence note
Shared budget/policy normalization logic is likely worthwhile, but should emerge carefully to avoid overfitting to research-specific names.

---

## 3.2 Best-version selection logic

### Libris today
Already supports final selection from topic checkpoints.

### Software-dev analogue
Needs selection from workstream/integration checkpoints.

### Convergence note
Generic “select best checkpoint” helper is a strong candidate for future sharing.

---

## 3.3 Projection helper layer

### Libris today
Operation state is already rich enough to drive graph/F4/room projections.

### Software-dev analogue
Will want the same projections.

### Convergence note
Projection logic should almost certainly converge before persistence fully converges.

---

# 4. Areas that should remain domain-specific longer

These should not be prematurely generalized.

---

## 4.1 Domain prompts

Research and software work have very different worker/judge prompts.

Keep separate:
- Libris role prompts
- software-dev role prompts

---

## 4.2 Domain artifacts

### Libris artifacts
- briefs
- evidence tables
- claims
- provenance sidecars

### Software-dev artifacts
- diffs
- tests
- build logs
- screenshots
- integration reports

### Convergence note
Artifact reference shape can converge; artifact content schemas should remain domain-specific.

---

## 4.3 Domain orchestration heuristics

### Libris
- topic scouting
- source discovery
- evidence gathering
- report revision

### Software-dev
- workstream scouting
- dependency handling
- implementation iteration
- integration/release verification

### Convergence note
The skeleton converges; heuristic logic should remain separate.

---

# 5. Recommended convergence order

## Stage 1 — shape alignment only
Do now / soon:
- align naming
- align event envelope
- align checkpoint/review structures
- align graph vocabulary

## Stage 2 — shared projection helpers
Do once both systems emit compatible events:
- event → room helpers
- graph projection helpers
- F4 stream helpers

## Stage 3 — shared operation helpers
Do once both runtimes have proven patterns:
- operation state helper functions
- best checkpoint selection helpers
- budget/policy normalization helpers

## Stage 4 — shared generic runtime store
Do only when clearly justified:
- extract generic operation/work_unit/checkpoint/review store
- keep domain overlays thin

---

# 6. Practical implementation guidance right now

## For Libris work
- keep shipping real behavior
- keep its event/checkpoint/state model explicit
- avoid embedding too much meaning only in prompt text

## For software-dev work
- mirror Libris’s successful structural shapes
- avoid creating incompatible event/checkpoint schemas
- avoid forcing premature shared runtime code

---

# 7. Current best guess at eventual shared modules

These are the most likely future shared modules if convergence succeeds:

- `operation_store.py`
- `operation_events.py`
- `operation_projection.py`
- `operation_policy.py`
- `operation_selection.py`

With domain overlays like:
- `libris_runtime.py`
- `devop_runtime.py`

---

# Compact summary

Libris already proves much of the structure the software-development swarm will need.

The strongest likely convergence points are:
- operation state shape
- work-unit state shape
- checkpoint persistence
- event log semantics
- swarm-state summary generation
- role-runner skeleton

The most important shared near-term target is **event/checkpoint/review shape alignment**, because that unlocks shared room, graph, and F4 projections without forcing premature runtime unification.
