# Software Development Runtime Module Plan

> Concrete module/file plan for implementing Autonomous Software Development Operation cleanly and maintainably.
>
> Date: 2026-03-28  
> Status: Proposed  
> Related: `docs/plans/autonomous-software-development-operation.md`, `docs/plans/software-dev-checkpoint-and-review-storage-spec.md`, `docs/plans/generic-operation-primitives.md`

---

## Purpose

This document maps the software-development swarm design onto concrete runtime modules so implementation can proceed without scattering logic across unrelated files.

The goal is to keep the software-dev operation:
- compatible with Libris patterns
- compatible with chat-room structures
- compatible with graph/F4 projection
- compatible with existing Charon agent/shade/judge infrastructure

---

# 1. Guiding structure

The software-dev runtime should be implemented as a thin domain layer over existing Charon primitives.

## Reuse existing infrastructure
- `conversation_engine.py`
- `agent_runtime.py`
- `shade_orchestrator.py`
- `judge_engine.py`
- `goal_runtime.py`
- `conversation_store.py`
- room/session registry and UI refresh plumbing

## Add software-dev-specific domain layer
New modules should handle:
- operation state
- workstream state
- checkpoint state
- review/evidence persistence
- coordinator loop
- implementer/judge/verifier role runners
- event emission
- projection summaries for UI

---

# 2. Recommended new modules

## 2.1 `apps/core-daemon/devop_runtime.py`

### Role
Canonical persistence/runtime layer for software development operations.

### Responsibilities
- create/load/update operation records
- create/load/update workstream records
- append events
- append handoffs
- append decisions
- manage checkpoint/review/evidence persistence
- expose operation summary helpers for UI

### Core API sketch

```python
def init_operation(...): ...
def get_operation_state(...): ...
def set_operation_status(...): ...
def save_candidate_workstreams(...): ...
def init_workstream(...): ...
def get_workstream_state(...): ...
def update_workstream_runtime(...): ...
def save_checkpoint(...): ...
def list_checkpoints(...): ...
def save_review(...): ...
def list_reviews(...): ...
def save_evidence_bundle(...): ...
def append_handoff(...): ...
def append_decision(...): ...
def append_operation_event(...): ...
def get_swarm_state(...): ...
def select_best_checkpoint(...): ...
def finalize_operation_selection(...): ...
```

### Why separate
This should be the software-dev analogue of `libris_runtime.py`, not mixed into goals or generic judge code.

---

## 2.2 `apps/core-daemon/devop_agents.py`

### Role
Role runners for software-dev agents.

### Responsibilities
- spawn development coordinator
- spawn implementers
- spawn judges
- spawn integration verifier
- build role prompts/instructions
- create operation-aware engines

### Core API sketch

```python
def spawn_devop_role(...): ...
def start_autonomous_software_operation(...): ...
def _run_operation_controller(...): ...
def _run_devop_role(...): ...
def create_devop_engine(...): ...
```

### Why separate
Mirrors `libris_agents.py` and keeps role-specific execution away from storage logic.

---

## 2.3 `apps/core-daemon/devop_orchestrator.py`

### Role
Domain-specific orchestration helpers for software work.

### Responsibilities
- scout implementation strategies/workstreams
- gather implementation leads
- choose workstream fanout
- detect dependency readiness
- wait for reviews/checkpoints
- trigger integration phase

### Core API sketch

```python
def scout_workstreams(...): ...
def score_workstreams(...): ...
def select_workstreams(...): ...
def wait_for_checkpoint(...): ...
def wait_for_review(...): ...
def wait_for_verification(...): ...
def build_integration_summary(...): ...
```

### Why separate
Keeps control-flow helpers out of role-runner code and out of persistence code.

---

## 2.4 `apps/core-daemon/devop_policy.py`

### Role
Policy and budget handling.

### Responsibilities
- normalize budget/policy
- evaluate budget exhaustion/warnings
- assign model tiers by role
- govern concurrency limits
- define repair-loop ceilings

### Core API sketch

```python
def default_budget(): ...
def default_model_policy(): ...
def normalize_budget(...): ...
def normalize_model_policy(...): ...
def evaluate_budget(...): ...
def choose_model_tier(role, policy): ...
```

### Why separate
Lets Libris and software-dev eventually share policy patterns without merging runtimes prematurely.

---

## 2.5 `apps/core-daemon/devop_projection.py`

### Role
Projection helpers for room/graph/F4 summaries.

### Responsibilities
- convert operation state to graph nodes/edges
- convert events to room-thread messages
- convert events/state to F4 stream items
- provide UI summary payloads

### Core API sketch

```python
def project_graph(...): ...
def project_room_messages(...): ...
def project_f4_stream(...): ...
def summarize_operation(...): ...
def summarize_workstream(...): ...
```

### Why separate
Prevents projection logic from leaking into persistence and orchestration modules.

---

# 3. Recommended modifications to existing modules

## 3.1 `apps/core-daemon/tools/__init__.py`

### Change
Extend `ToolContext` with operation-aware metadata.

### Add fields
```python
operation_id: str = ''
operation_domain: str = ''
work_unit_id: str = ''
operation_role: str = ''
runtime_role: str = ''
parent_agent_id: str = ''
```

### Why
This is required so software-dev agents and Libris agents can use the same tool system cleanly within an operation.

---

## 3.2 `apps/core-daemon/conversation_engine.py`

### Change
Support richer operation-aware tool context injection.

### Minimal requirement
Allow callers to construct engines with consistent operation-scoped metadata passed through tool context or surrounding wrapper.

### Preferred implementation
Do not deeply couple the engine to software-dev specifics.
Instead, make `create_devop_engine(...)` and `create_libris_engine(...)` wrap the engine consistently.

---

## 3.3 `apps/core-daemon/judge_engine.py`

### Change
Reuse scoring logic where useful, but do not force software-dev checkpoint reviews into judge-loop-only concepts.

### Add support around it
A software-dev review persistence layer should sit outside the pure loop engine.

---

## 3.4 `apps/core-daemon/shade_orchestrator.py`

### Change
Continue using it for bounded subwork, but ensure metadata can include:
- operation id
- workstream id
- parent implementer id
- contract type

This mirrors current Libris usage.

---

## 3.5 `apps/tui/opentui/chat_backend.py`

### Change
Eventually expose software-dev operation summaries in refresh payloads, similar to how Libris swarm state should be surfaced.

### Needed payloads later
- active software-dev operations
- workstreams
- latest checkpoints
- latest reviews
- current coordinator/judge phases

---

# 4. Suggested implementation phases

## Phase 1 — persistence backbone
Implement:
- `devop_runtime.py`
- checkpoint/review/evidence storage
- append-only events

### Exit criteria
Can create a software-dev operation, create a workstream, save a checkpoint, save a review, and query operation state.

---

## Phase 2 — role runners
Implement:
- `devop_agents.py`
- coordinator/implementer/judge/verifier role prompts
- consistent engine wrapper

### Exit criteria
Can spawn coordinator and implementer/judge workers with operation-scoped metadata.

---

## Phase 3 — orchestration helpers
Implement:
- `devop_orchestrator.py`
- workstream scouting/selection/wait helpers
- revision loop helpers

### Exit criteria
Can run one workstream through checkpoint → review → revision → acceptance.

---

## Phase 4 — projection helpers
Implement:
- `devop_projection.py`
- graph projection
- room projection
- F4 projection

### Exit criteria
Software-dev operations can render in room/thread, graph, and F4 structures.

---

## Phase 5 — convergence with Libris patterns
Implement:
- shape alignment review with `libris_runtime.py`
- shared helper extraction where proven

### Exit criteria
Libris and software-dev use compatible event/checkpoint/review vocabulary and can share projection tooling.

---

# 5. Boundaries and anti-patterns

## Do not put software-dev orchestration into:
- `goal_runtime.py`
- `judge_engine.py`
- `conversation_engine.py`
- `shade_orchestrator.py`

Those should remain generic/shared infrastructure.

## Do not rely on prompt-only coordination
Important transitions must be represented in persisted operation objects.

## Do not make the room model the execution engine
Rooms are a projection, not the hidden canonical runtime.

---

# 6. Concrete file map summary

## New files
- `apps/core-daemon/devop_runtime.py`
- `apps/core-daemon/devop_agents.py`
- `apps/core-daemon/devop_orchestrator.py`
- `apps/core-daemon/devop_policy.py`
- `apps/core-daemon/devop_projection.py`

## Existing files to extend later
- `apps/core-daemon/tools/__init__.py`
- `apps/core-daemon/conversation_engine.py`
- `apps/core-daemon/shade_orchestrator.py`
- `apps/core-daemon/judge_engine.py`
- `apps/tui/opentui/chat_backend.py`

---

# Compact summary

The software-dev swarm should be implemented as a clean domain layer over existing Charon primitives, with 5 focused modules:

- `devop_runtime.py` — canonical state/persistence
- `devop_agents.py` — role runners
- `devop_orchestrator.py` — control-flow helpers
- `devop_policy.py` — budgets/model policy
- `devop_projection.py` — room/graph/F4 projection

This keeps the software-dev system maintainable, mirrors Libris structurally, and prepares both systems for future convergence around shared generic operation primitives.
