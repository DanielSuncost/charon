# Phase 0 Spec Freeze Checklist - Agents, Remote Links, Memory, and Onboarding Compatibility

Date: 2026-03-16
Status: Ready to execute
Related docs:
- docs/plans/2026-03-16-charon-agents-shades-remote-v1.md
- docs/plans/2026-03-15-voice-onboarding-and-agent-priority-plan.md

Goal
- Freeze contracts before implementation so local/remote persistent-agent workflows, internal Shades, durable memory, and onboarding/setup all align.

Architecture baseline
- User interacts only with persistent Charon agents.
- Shades are internal-only workers spawned/managed by persistent agents.
- Remote control is first-class and secure.
- Event log is canonical memory truth; promoted knowledge memory is policy-controlled.

---

## A. Contract files to create (spec-only, no runtime refactor yet)

Create these docs first:
1) docs/contracts/agent.schema.json
2) docs/contracts/task.schema.json
3) docs/contracts/event.schema.json
4) docs/contracts/node-link.schema.json
5) docs/contracts/rlm-node.schema.json
6) docs/contracts/command-contracts.md
7) docs/contracts/onboarding-compatibility.md

Definition of done for this section:
- All fields are explicit, typed, and versioned.
- Every schema includes required fields and validation rules.
- Cross-file references are documented.

---

## B. Required schema contracts (minimum fields)

### B1) Agent schema (persistent only in user-facing APIs)
Required fields:
- id (string)
- name (string)
- specialization (generalist|project:<id>)
- project (string|null)
- status (idle|running|blocked|error|stopped)
- capabilities (string[])
- link_scope (local|remote)
- node_id (string)
- created_at / updated_at / last_heartbeat (ISO8601)

Internal-only fields allowed but hidden from primary UX:
- internal_worker_count
- last_internal_worker_event

### B2) Task schema
Required fields:
- id, title, instruction
- owner_agent_id
- project
- priority (low|normal|high|urgent)
- status (pending|in_progress|completed|failed|blocked)
- created_at, updated_at
- result_summary (nullable)

Optional but recommended:
- parent_task_id
- escalation_required (bool)

### B3) Event schema (canonical append-only log)
Required fields:
- id
- ts
- event_type
- actor_type (user|agent|node|system|internal_worker)
- actor_id
- correlation_id
- payload (object)
- signature (nullable in local mode)

Event types to freeze now:
- loop_start, loop_idle, loop_halt, loop_exit
- agent_created, agent_assigned, agent_status_changed
- task_created, task_started, task_succeeded, task_failed
- remote_link_enrolled, remote_link_revoked, remote_command_dispatched, remote_command_ack
- internal_worker_spawned, internal_worker_completed, internal_worker_failed
- rlm_node_started, rlm_node_completed, rlm_budget_exceeded

### B4) Node link schema (remote trust + routing)
Required fields:
- node_id
- display_name
- base_url
- trust_state (pending|trusted|revoked)
- enrollment_method (pairing_code|key_file)
- key_fingerprint
- scopes (view|control|admin)[]
- created_at, updated_at, last_seen

### B5) RLM node schema
Required fields:
- id
- parent_id (nullable)
- root_task_id
- objective
- depth
- budget {max_depth, max_tokens, max_seconds}
- usage {tokens, seconds}
- status (running|completed|failed|halted_budget)
- output_ref (event/log pointer)

---

## C. Command contracts (user-facing)

Freeze behavior for:
- /agent create <name> [--project <project>|--generalist]
- /agent list
- /agent assign <agent> <project>
- /agent task <agent> <instruction>
- /agent link add <node>
- /agent link list
- /agent link revoke <node>
- /agent inbox <agent>
- /agent thread <agent>

Rules:
- No /shade command in user-facing surface.
- Any Shade activity appears only as translated agent-level status unless diagnostics mode is enabled.
- Commands must be available in Textual UI and CLI parity mode.

---

## D. Onboarding/setup compatibility requirements (must-pass)

Current onboarding state in apps/tui/onboarding_state.py uses:
- provider_mode
- provider
- model
- provider_auth
- project
- complete
- step

Current setup command shape already includes:
- /setup provider <name>
- /setup no-provider
- /setup model <name>
- /setup project <name>
- /setup complete|status|reset

Phase 0 compatibility requirements:
1) Keep all existing setup commands valid.
2) Add, do not break, new onboarding fields needed for remote agent usage:
   - default_agent_name
   - enable_remote_links (bool)
   - default_link_scope (local|remote|hybrid)
   - diagnostics_mode (off|on)
3) Maintain backward compatibility loading old onboarding.json (missing new fields should auto-default).
4) Provider/no-provider decision must gate only model execution, not agent management UI.
5) Onboarding completion should not require remote linking; remote can be added later.

Validation checks:
- Old onboarding.json loads without crash.
- New onboarding flow can finish in both provider and no-provider modes.
- /setup status shows new fields with safe defaults.

---

## E. Security and reliability contract checks

Security checks:
- Every remote command includes correlation_id and signer identity.
- Scope check enforced before dispatch.
- Revoked node cannot dispatch or receive control commands.

Reliability checks:
- Poll fallback works if stream channel unavailable.
- Reconnect does not duplicate task execution (idempotency via correlation_id).
- Local agents continue when remote links fail.

---

## F. Test plan for Phase 0 contract freeze

Contract tests to add (schema-level):
- tests/contracts/test_agent_schema.py
- tests/contracts/test_task_schema.py
- tests/contracts/test_event_schema.py
- tests/contracts/test_node_link_schema.py
- tests/contracts/test_rlm_node_schema.py

Behavioral contract tests:
- tests/contracts/test_command_contracts.py
- tests/contracts/test_onboarding_backward_compat.py

Minimum pass gates:
- All schema fixtures validate.
- Old onboarding fixture validates and loads.
- New onboarding fixture validates and loads.
- Command parsing rejects /shade user commands.

---

## G. Immediate implementation order after freeze

1) F51 (link registry + enrollment)
2) F41 (remote dispatch)
3) F50 (internal Shade orchestration)
4) F23/F24/F25 (memory promotion + conflict handling)
5) F52 (RLM recursion graph + budgets)

Reason:
- secure connectivity and command rails before deeper autonomous behavior.

---

## H. Open decisions to resolve in kickoff meeting

1) Enrollment default: pairing code vs key file first
2) Transport default: polling-only vs SSE + polling fallback
3) Storage default for links/events index: file-only vs SQLite index
4) Default RLM budgets for V1
5) Diagnostics mode visibility policy in dashboard (hidden by default confirmed)

Exit criteria for Phase 0
- Contracts approved and committed.
- Onboarding compatibility checks approved.
- Implementation can start without architectural ambiguity.
