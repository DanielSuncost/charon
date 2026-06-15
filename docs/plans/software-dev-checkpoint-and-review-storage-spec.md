# Software Development Checkpoint + Review Storage Spec

> Concrete storage-facing spec for Autonomous Software Development Operation.
>
> Date: 2026-03-28  
> Status: Proposed  
> Related: `docs/plans/autonomous-software-development-operation.md`, `docs/plans/software-dev-operation-event-and-graph-schema.md`, `docs/plans/generic-operation-primitives.md`

---

## Purpose

This document defines the concrete durable objects the software-development swarm should persist so that:
- coordinator, implementers, judges, and verifiers can interact cleanly
- checkpoints can be reviewed, resumed, and compared
- room/thread, graph, and F4 projections have stable source data
- future web review space can reuse the same artifacts

This is intentionally storage-facing and implementation-friendly.

---

# 1. Core storage objects

The software-dev operation should persist these first-class records:

1. `dev_operation`
2. `dev_workstream`
3. `dev_handoff`
4. `dev_checkpoint`
5. `dev_review`
6. `dev_evidence_bundle`
7. `dev_decision`
8. `dev_event`

These may later unify with the generic operation substrate, but the software-dev layer should use these shapes from day one.

---

# 2. Suggested storage layout

## Preferred long-term shape

```text
.charon_state/operations/
  <operation_id>/
    operation.json
    events.jsonl
    handoffs.jsonl
    decisions.jsonl
    workstreams/
      <workstream_slug>/
        workstream.json
        checkpoints/
        reviews/
        evidence/
        artifacts/
```

## Acceptable near-term software-specific shape

```text
.charon_state/software_ops/
  operations/
    <operation_id>/
      operation.json
      events.jsonl
      handoffs.jsonl
      decisions.jsonl
      workstreams/
        <workstream_slug>/
          workstream.json
          checkpoints/
          reviews/
          evidence/
          artifacts/
```

### Recommendation
Use the near-term software-specific path if implementation starts before generic convergence with Libris, but keep the internal object names generic enough to migrate later.

---

# 3. Operation record

## File
```text
operation.json
```

## Schema sketch

```json
{
  "operation_id": "op-dev-001",
  "domain": "software_dev",
  "title": "Build web app that does X",
  "prompt": "Build a web app that does X",
  "status": "running",
  "project_root": "/home/user/Projects/charon",
  "project_id": "charon-c564e8fd",
  "coordinator_agent_id": "AG-coord-1",
  "global_judge_agent_id": "AG-judge-global-1",
  "integration_verifier_agent_id": "AG-verifier-1",
  "budget": {},
  "usage": {},
  "policy": {},
  "selected_workstream_ids": [],
  "delivered_checkpoint_ids": [],
  "created_at": "...",
  "updated_at": "..."
}
```

## Required semantics
- one record per software-dev operation
- top-level status source of truth
- owns workstream set, budget, and final delivery references

---

# 4. Workstream record

## File
```text
workstreams/<slug>/workstream.json
```

## Schema sketch

```json
{
  "workstream_id": "wu-frontend",
  "operation_id": "op-dev-001",
  "work_unit_type": "workstream",
  "slug": "frontend-ui",
  "title": "Frontend UI",
  "status": "active",
  "priority": 0.82,
  "summary": "User-facing web interface",
  "constraints": [],
  "acceptance_criteria": [],
  "dependency_ids": ["wu-backend-api"],
  "owner_agent_id": "AG-frontend-1",
  "paired_judge_agent_id": "AG-judge-frontend-1",
  "checkpoint_ids": [],
  "best_checkpoint_id": null,
  "revision_round": 0,
  "created_at": "...",
  "updated_at": "..."
}
```

## Required semantics
- one record per workstream
- must point to owner worker and optional paired judge
- must track dependencies and best checkpoint candidate

---

# 5. Handoff records

## File
```text
handoffs.jsonl
```

## JSONL row shape

```json
{
  "handoff_id": "ho-001",
  "operation_id": "op-dev-001",
  "workstream_id": "wu-frontend",
  "kind": "assignment",
  "from_agent_id": "AG-coord-1",
  "to_agent_id": "AG-frontend-1",
  "from_role": "coordinator",
  "to_role": "implementer",
  "subject_id": null,
  "status": "sent",
  "summary": "Assigned frontend UI workstream",
  "payload": {},
  "created_at": "..."
}
```

## Canonical kinds
- `assignment`
- `checkpoint_submission`
- `review_request`
- `critique_return`
- `verification_request`
- `verification_return`
- `decision_notice`

## Required semantics
- append-only
- explicit directional coordination record
- usable as room narrative input and graph edge projection source

---

# 6. Checkpoint record

## Files
```text
workstreams/<slug>/checkpoints/<checkpoint_id>-meta.json
workstreams/<slug>/checkpoints/<checkpoint_id>.md
```

## Metadata schema sketch

```json
{
  "checkpoint_id": "cp-frontend-03",
  "operation_id": "op-dev-001",
  "workstream_id": "wu-frontend",
  "producer_agent_id": "AG-frontend-1",
  "status": "submitted",
  "summary": "Added form validation and tests",
  "report_path": "workstreams/frontend-ui/checkpoints/cp-frontend-03.md",
  "artifact_refs": [
    {"type": "diff", "path": ".../artifacts/cp-frontend-03.diff"},
    {"type": "summary", "path": ".../checkpoints/cp-frontend-03.md"}
  ],
  "evidence_bundle_id": "ev-frontend-03",
  "review_ids": ["rv-frontend-03"],
  "scorecard": {
    "requirements_fit": 0.84,
    "test_adequacy": 0.71,
    "code_quality": 0.79,
    "integration_readiness": 0.62,
    "user_fit": 0.88,
    "overall": 0.80
  },
  "best_so_far": true,
  "created_at": "...",
  "updated_at": "..."
}
```

## Markdown content expectations
The markdown body should be human-readable and summarize:
- what changed
- why it matters
- known weaknesses
- evidence highlights
- next likely actions

## Required semantics
- checkpoint metadata is canonical
- markdown body is the primary user-inspectable artifact
- checkpoint must be linkable to reviews and evidence bundle

---

# 7. Review record

## Files
```text
workstreams/<slug>/reviews/<review_id>-meta.json
workstreams/<slug>/reviews/<review_id>-critique.md
```

## Metadata schema sketch

```json
{
  "review_id": "rv-frontend-03",
  "operation_id": "op-dev-001",
  "workstream_id": "wu-frontend",
  "checkpoint_id": "cp-frontend-03",
  "reviewer_agent_id": "AG-judge-frontend-1",
  "review_type": "judge",
  "status": "completed",
  "decision": "repair_requested",
  "summary": "Validation works, but tests miss edge cases.",
  "critique_path": "workstreams/frontend-ui/reviews/rv-frontend-03-critique.md",
  "scores": {
    "requirements_fit": 0.84,
    "test_adequacy": 0.58,
    "code_quality": 0.79,
    "integration_readiness": 0.62,
    "user_fit": 0.88,
    "overall": 0.73
  },
  "requested_changes": [
    "Add tests for malformed email input",
    "Verify disabled submit state"
  ],
  "created_at": "...",
  "updated_at": "..."
}
```

## Decision enum
- `accept`
- `repair_requested`
- `reject`
- `escalate`
- `verify_more`

## Required semantics
- review is distinct from checkpoint
- review captures evaluation, not implementation
- multiple reviews may attach to one checkpoint over time

---

# 8. Evidence bundle record

## File
```text
workstreams/<slug>/evidence/<evidence_bundle_id>.json
```

## Schema sketch

```json
{
  "evidence_bundle_id": "ev-frontend-03",
  "operation_id": "op-dev-001",
  "workstream_id": "wu-frontend",
  "checkpoint_id": "cp-frontend-03",
  "changed_files": [
    "src/ui/login.tsx",
    "tests/login.test.tsx"
  ],
  "commands": [
    "npm test -- login",
    "npm run build"
  ],
  "artifacts": [
    {"type": "test_log", "path": ".../artifacts/login-tests.log"},
    {"type": "build_log", "path": ".../artifacts/build.log"},
    {"type": "screenshot", "path": ".../artifacts/login-form.png"},
    {"type": "diff", "path": ".../artifacts/cp-frontend-03.diff"}
  ],
  "verification": {
    "tests_passed": 12,
    "tests_failed": 1,
    "build_status": "passed",
    "browser_checks": [
      "Login form renders",
      "Validation message appears on invalid email"
    ]
  },
  "summary": "12 tests passed, build passed, 1 browser screenshot captured.",
  "created_at": "..."
}
```

## Required semantics
- bundle should support both machine filtering and human inspection
- evidence must answer: what supports this checkpoint?

---

# 9. Decision record

## File
```text
decisions.jsonl
```

## JSONL row shape

```json
{
  "decision_id": "dec-001",
  "operation_id": "op-dev-001",
  "workstream_id": "wu-frontend",
  "decision_type": "select_best_checkpoint",
  "actor_agent_id": "AG-coord-1",
  "subject_id": "cp-frontend-04",
  "summary": "Checkpoint cp-frontend-04 selected as best-so-far for frontend.",
  "metadata": {},
  "created_at": "..."
}
```

## Canonical types
- `select_best_checkpoint`
- `drop_workstream`
- `reprioritize`
- `escalate_to_user`
- `finalize_delivery`
- `request_integration_fix`

---

# 10. Event log

## File
```text
events.jsonl
```

## JSONL row shape

```json
{
  "event_id": "evt-001",
  "ts": "...",
  "operation_id": "op-dev-001",
  "workstream_id": "wu-frontend",
  "kind": "checkpoint_submitted",
  "from_agent_id": "AG-frontend-1",
  "to_agent_id": "AG-judge-frontend-1",
  "summary": "Frontend checkpoint submitted for review",
  "payload": {
    "checkpoint_id": "cp-frontend-03"
  }
}
```

## Required semantics
- append-only
- source for F4 and room/thread projection
- graph updates should derive from these events + canonical state

---

# 11. Selection and interruption behavior

## When user interrupts
The system should store enough data to support:
- implementer best checkpoint nomination
- judge best checkpoint nomination
- coordinator final selection

## Required stored references
At minimum:
- `workstream.best_checkpoint_id`
- `checkpoint.best_so_far`
- decision records for final choice

---

# 12. Implementation guidance

## Phase 1
Implement only:
- operation
- workstream
- checkpoint
- review
- evidence bundle
- event log

## Phase 2
Add:
- handoffs
- decision log
- richer artifact refs

## Phase 3
Add:
- generic operation-store convergence with Libris
- graph/room/F4 projection helpers

---

# Compact summary

The software-dev swarm should persist explicit durable records for:
- operations
- workstreams
- handoffs
- checkpoints
- reviews
- evidence bundles
- decisions
- events

The minimum viable useful storage set is:
- `operation.json`
- `workstream.json`
- checkpoint metadata + markdown body
- review metadata + critique markdown
- evidence bundle JSON
- append-only events log

These records should be the canonical source for rooms, graph view, F4, interruption behavior, and later web review space.
