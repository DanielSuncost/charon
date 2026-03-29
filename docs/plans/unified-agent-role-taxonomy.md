# Unified Agent Role Taxonomy

> Shared role model for Charon core, Libris, and Autonomous Software Development Operation.
>
> Date: 2026-03-28  
> Status: Proposed  
> Related: `docs/plans/libris-autonomous-research-operation.md`, `docs/plans/autonomous-software-development-operation.md`, `docs/plans/overseer-agent-design.md`, `docs/plans/2026-03-16-charon-agents-shades-remote-v1.md`

---

## Purpose

Charon currently has a relatively flat runtime model:
- persistent agents
- shades
- optional overseer behavior
- external sessions via charon's boat
- a judge subsystem that is becoming more agent-like

At the same time, higher-level operation designs like **Libris** and **Autonomous Software Development Operation** use richer role language:
- coordinator
- researcher
- implementer
- judge
- verifier
- shades

This document defines a **unified taxonomy** so that:
- Charon core stays simple
- operation-specific roles remain expressive
- backend payloads stay consistent
- the TUI can label agents clearly
- future web UIs and review spaces can share one model

---

# Design principle

A single field called `role` is not enough.

Charon should distinguish between three different concepts:

1. **Runtime role** — what kind of actor this is in the system
2. **Operation role** — what function this actor serves in the current swarm/operation
3. **Specialization** — what topic/workstream/domain this actor is focused on

These must not be conflated.

---

# 1. Runtime role

Runtime role describes the actor's execution nature inside Charon.

## Canonical runtime roles

### `persistent_agent`
Long-lived, named, user-visible Charon agent.

Properties:
- durable memory
- user-steerable
- project-aware
- can own goals and tasks
- may spawn shades
- may coordinate other agents

Examples:
- a normal Charon coding agent
- an overseer
- a Libris coordinator
- a Libris researcher
- a software implementer

---

### `shade`
Ephemeral internal worker spawned by a persistent agent.

Properties:
- contract/phase bounded
- short-lived
- narrow scope
- not normally first-class user-facing
- reports back to parent

Examples:
- research source-gathering shade
- test-writing shade
- verification shade
- refactor shade

---

### `external_worker`
A worker not natively running as a Charon agent, but attached into Charon via boat or another bridge.

Properties:
- may be interactive or autonomously driven
- may be manual, hybrid, or autonomous
- represented in session grid
- may receive work packets from a Charon coordinator

Examples:
- boat-wrapped Pi session
- boat-wrapped Hermes session
- future remote worker session

---

### `judge_actor`
An actor whose primary job is evaluation rather than implementation.

Properties:
- may be implemented as a persistent agent, subsystem, or future dedicated runtime
- reviews outputs, checkpoints, and artifacts
- emits verdicts and quality metadata
- may run continuously during an operation

Examples:
- Libris topic judge
- software workstream judge
- global judge supervising multiple workstreams

---

### `system_service`
Non-user-facing background component that participates operationally but is not an agent in the conversational sense.

Properties:
- registry, orchestration, scheduling, event routing, memory jobs
- may surface state in UI
- not treated as a worker/coordinator in normal swarm views

Examples:
- session registry
- autonomy orchestrator
- checkpoint manager
- memory compactor

---

## Notes

- In the current codebase, most user-facing actors are still effectively just `persistent_agent`.
- `judge_actor` may initially be implemented as metadata around the existing judge engine rather than a fully separate runtime.
- `external_worker` is essential now that boat sessions are entering the unified control model.

---

# 2. Operation role

Operation role describes what an actor is doing within a particular multi-agent operation.

A `persistent_agent` may have different operation roles in different contexts.

Example:
- same runtime type: `persistent_agent`
- operation role in Libris: `researcher`
- operation role in software swarm: `implementer`

## Canonical operation roles

### `coordinator`
Top-level orchestrator for an operation.

Responsibilities:
- interpret broad directive
- define scope and selection criteria
- choose workstreams/topics
- assign workers
- track progress
- choose final outputs

Examples:
- Libris Research Coordinator
- Development Coordinator
- high-autonomy overseer for a project

---

### `worker`
Generic execution role for an actor that performs substantive task work.

Responsibilities:
- execute assigned tasks
- produce outputs
- respond to critique
- emit status/evidence

This is the broad umbrella role.

---

### `researcher`
Specialized worker for research operations.

Responsibilities:
- investigate a topic
- gather sources
- synthesize findings
- revise reports under critique

Libris-specific but still part of the unified taxonomy.

---

### `implementer`
Specialized worker for software development operations.

Responsibilities:
- write code/tests/docs
- integrate changes
- revise outputs under critique
- produce implementation checkpoints

---

### `judge`
Evaluator role within an operation.

Responsibilities:
- critique outputs
- score checkpoints
- request repair or approve
- track quality over time

Examples:
- Libris topic judge
- software workstream judge
- global operation judge

---

### `verifier`
Validation-focused role.

Responsibilities:
- verify correctness or readiness
- run checks
- confirm evidence
- test integration/system quality

Examples:
- citation verifier in research mode
- integration verifier in software mode
- release-readiness verifier

---

### `observer`
Monitoring or oversight role without primary implementation responsibility.

Responsibilities:
- monitor operation health
- surface blockers
- summarize progress
- recommend adjustments

Examples:
- overseer when used as project monitor
- future audit observer

---

### `selector`
Actor responsible for choosing best-so-far or final outputs.

Responsibilities:
- compare candidate checkpoints
- weigh scores and user alignment
- choose what to deliver

Often this is the same actor as `coordinator`, but the role is conceptually distinct.

---

### `integrator`
Actor responsible for combining independently generated outputs.

Responsibilities:
- merge workstreams
- resolve compatibility issues
- coordinate final assembly

Examples:
- software integration lead
- final report bundler in research mode

---

## Notes

- `worker` is the generic umbrella operation role.
- `researcher` and `implementer` are domain-specialized worker roles.
- `judge`, `verifier`, `integrator`, and `selector` are not runtime types; they are operation functions.

---

# 3. Specialization

Specialization describes the subject area or workstream focus.

This is not the same as runtime role or operation role.

## Examples of specialization

### General specialization
- `generalist`
- `project:<id>`

### Software specialization
- `frontend`
- `backend`
- `auth`
- `database`
- `infra`
- `testing`
- `tui`
- `api`
- `docs`
- `performance`

### Research specialization
- `offline-rl`
- `world-models`
- `paper-triage`
- `literature-review`
- `benchmark-analysis`

### Temporary scoped specialization
- `auth-ui-workstream`
- `rate-limit-verification`
- `source-verification`
- `integration-tests`

---

# 4. Putting the three layers together

## Example A: regular coding agent

```json
{
  "runtime_role": "persistent_agent",
  "operation_role": "implementer",
  "specialization": "frontend"
}
```

## Example B: Libris lead researcher

```json
{
  "runtime_role": "persistent_agent",
  "operation_role": "researcher",
  "specialization": "offline-rl"
}
```

## Example C: internal test shade

```json
{
  "runtime_role": "shade",
  "operation_role": "verifier",
  "specialization": "integration-tests"
}
```

## Example D: boat-wrapped Hermes worker

```json
{
  "runtime_role": "external_worker",
  "operation_role": "implementer",
  "specialization": "backend"
}
```

## Example E: operation-wide judge

```json
{
  "runtime_role": "judge_actor",
  "operation_role": "judge",
  "specialization": "software-quality"
}
```

---

# 5. Mapping current Charon concepts to unified taxonomy

## Current: persistent agent
Maps to:
- `runtime_role = persistent_agent`
- `operation_role = variable`
- `specialization = current soft specialization or project label`

Possible operation roles:
- coordinator
- implementer
- researcher
- observer
- integrator
- selector

---

## Current: shade
Maps to:
- `runtime_role = shade`
- `operation_role = variable`
- `specialization = task scope`

Possible operation roles:
- worker
- implementer
- researcher
- verifier

---

## Current: overseer
Maps to:
- `runtime_role = persistent_agent`
- `operation_role = observer` or `coordinator`
- `specialization = project-management` or project-specific domain

Important note:
Overseer is best understood as a **persistent agent specialization plus operation role**, not as a separate runtime class.

---

## Current: judge engine
Maps to:
- `runtime_role = judge_actor`
- `operation_role = judge`
- `specialization = operation/domain-specific`

Important note:
This is not fully normalized in code yet, but should become the target model for UI and orchestration.

---

## Current: charon's boat sessions
Maps to:
- `runtime_role = external_worker`
- `operation_role = variable`
- `specialization = inferred from session/project/work`

Examples:
- manual Pi session helping with docs
- autonomous Hermes session implementing API code

---

# 6. Mapping Libris roles to unified taxonomy

## Research Coordinator
```json
{
  "runtime_role": "persistent_agent",
  "operation_role": "coordinator",
  "specialization": "research-orchestration"
}
```

## Researcher
```json
{
  "runtime_role": "persistent_agent",
  "operation_role": "researcher",
  "specialization": "<topic>"
}
```

## Research Shade
```json
{
  "runtime_role": "shade",
  "operation_role": "researcher",
  "specialization": "bounded-evidence-task"
}
```

## Judge
```json
{
  "runtime_role": "judge_actor",
  "operation_role": "judge",
  "specialization": "research-quality"
}
```

## Verifier (if separate)
```json
{
  "runtime_role": "shade" | "persistent_agent",
  "operation_role": "verifier",
  "specialization": "citation-verification"
}
```

---

# 7. Mapping Autonomous Software Development roles to unified taxonomy

## Development Coordinator
```json
{
  "runtime_role": "persistent_agent",
  "operation_role": "coordinator",
  "specialization": "development-orchestration"
}
```

## Implementer
```json
{
  "runtime_role": "persistent_agent" | "external_worker",
  "operation_role": "implementer",
  "specialization": "frontend|backend|infra|auth|..."
}
```

## Development Shade
```json
{
  "runtime_role": "shade",
  "operation_role": "implementer" | "verifier",
  "specialization": "bounded-dev-task"
}
```

## Judge
```json
{
  "runtime_role": "judge_actor",
  "operation_role": "judge",
  "specialization": "software-quality"
}
```

## Integration Verifier
```json
{
  "runtime_role": "persistent_agent" | "shade",
  "operation_role": "verifier" | "integrator",
  "specialization": "integration-readiness"
}
```

---

# 8. Recommended payload model

The backend should eventually expose these separately.

## Recommended agent/session payload fields

```json
{
  "runtime_role": "persistent_agent",
  "operation_role": "implementer",
  "specialization": "frontend",
  "autonomy_level": "delegating_autonomous",
  "control_mode": "autonomous"
}
```

## Why this matters

Without this split:
- `role` becomes overloaded
- TUI labels become confusing
- Libris and software ops invent incompatible naming schemes
- future review UIs and orchestration payloads become messy

With this split:
- runtime behavior stays stable
- operation views become clear
- specialization remains flexible

---

# 9. TUI labeling guidance

The TUI should not dump all three layers raw in every place.

Instead, choose the right layer for the context.

## Sessions grid
Show:
- operation role
- specialization
- autonomy badge
- judge status if relevant

Example:
```text
Implementer · frontend [L3] [J:review]
Researcher · offline-rl [L2]
Judge · software-quality [active]
```

## Dashboard details
Show all three layers:
- runtime role
- operation role
- specialization
- autonomy
- controller relationship if any

## Judge view
Show:
- judge actor
- subject operation role
- specialization/workstream/topic

---

# 10. Migration guidance for current codebase

## Near-term
Do not break current schema immediately.

Continue supporting current fields like:
- `role`
- `specialization`
- inferred session metadata

But begin introducing normalized internal fields in refresh payloads and orchestration state.

## Recommended transitional mapping

### Existing `role`
Use as a best-effort legacy field, but derive:
- `runtime_role`
- `operation_role`

### Existing `specialization`
Keep using it, but interpret it as the specialization layer, not the whole role model.

### Judge system
Initially treat it as `runtime_role = judge_actor` in payloads, even if implemented internally as a subsystem.

---

# 11. Non-goals

This taxonomy does **not** require:
- a giant rigid role hierarchy
- exposing every internal role as a user command
- abandoning the simplicity of the current persistent-agent + shade model

It is a clarification layer, not a bureaucracy layer.

---

# 12. Compact summary

Charon should distinguish three different things:

1. **Runtime role** — what kind of actor it is (`persistent_agent`, `shade`, `external_worker`, `judge_actor`)
2. **Operation role** — what function it serves right now (`coordinator`, `researcher`, `implementer`, `judge`, `verifier`, `observer`, `integrator`, `selector`)
3. **Specialization** — what domain or workstream it focuses on (`frontend`, `backend`, `offline-rl`, `auth`, `testing`, etc.)

Libris and Autonomous Software Development Operation should both be understood as higher-level swarm patterns built on top of the same runtime model. This keeps Charon core simple while allowing operation-specific role structures to be expressive, inspectable, and consistent across the TUI, backend payloads, and future web review interfaces.
