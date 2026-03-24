# Charon Agents + Remote Coordination V1 Design Charter

Date: 2026-03-16
Status: Draft (scope-locked for implementation planning)

## 1) North Star

Charon is a single-user, multi-project agent operating system.

You should be able to:
- run persistent Charon agents across multiple projects,
- coordinate local and remote agents from one dashboard,
- preserve long-running context with durable memory,
- develop in parallel without workflow collisions.

## 2) V1 Scope (What We Build Now)

Build only the essential capabilities:
- Persistent Charon agents (user-facing, long-lived)
- Automatic internal Shades (ephemeral worker processes managed only by Charon)
- Unified command surface centered on persistent agents
- Local + remote registry, visibility, and control of persistent agents
- Durable memory and event logging backbone
- Conflict/merge negotiation handled by persistent agents

Important UX rule:
- Users do not directly create, command, or inspect Shades as first-class actors.
- Shades are an internal execution mechanism only.
- User interaction remains with persistent Charon agents.

## 3) V1 Non-Goals

Do not build in V1:
- Full Gas Town role taxonomy (Mayor/Deacon/Dogs/etc.)
- Beads clone
- Complex autonomous patrol ecosystems
- Full distributed mesh consensus
- Large plugin framework

## 4) Role Model

User-facing model:
- Agent: persistent, named, controllable, project-aware

Internal runtime model:
- Worker: ephemeral Shade invoked by a persistent agent for sub-tasks

No extra user-facing typology is required right now. Specialization is implicit metadata:
- specialization = generalist | project:<id>

## 5) Control Model

Primary pattern:
- Overseer -> persistent Charon agent
- Persistent Charon agent -> internal Shades (automatic)
- Internal Shades -> persistent agent reportbacks
- Persistent agent resolves conflicts/merges and reports to Overseer

Command contract focus:
- /agent create
- /agent list
- /agent assign <project>
- /agent task <instruction>
- /agent link <remote>
- /agent inbox
- /agent thread

No /shade commands in user-facing interface.

## 6) Remote Linking (Critical)

V1 design:
- Each Charon node exposes secure control API
- Trusted registry of known nodes
- Heartbeats + capability metadata
- Remote command dispatch to persistent agents
- Audited event trail for all remote actions

Security baseline:
- Per-node identity key
- Short-lived auth token/session
- Permission scopes (view/control/admin)
- Replay protection where feasible

Reliability baseline:
- Retry/reconnect behavior
- Poll fallback if streaming unavailable
- Partial failure isolation

## 7) Memory and Context Durability

Two-layer memory strategy:

A) Event memory (source of truth, append-only)
- task lifecycle events
- delegation decisions
- merge/conflict outcomes
- remote control actions

B) Knowledge memory (derived, stable state)
- per-agent durable memory files
- per-project distilled context
- global user preference/context memory
- periodic compaction with provenance references to event memory

Representation approach:
- JSONL + structured snapshots as canonical V1 storage
- optional graph/cognee-like derived index later
- never discard canonical source events

## 8) RLM Integration (Define Precisely)

V1 RLM contract:
- Persistent agents may recursively decompose tasks internally
- Every recursion unit has: id, parent_id, objective, budget, outcome
- Enforce depth, token/time budget, and timeout limits
- Promote only finalized outputs into durable knowledge layers

## 9) Graceful Degradation Requirements

System must still function under reduced conditions:
- No dashboard -> CLI commands still operate
- No provider/model -> orchestration and management still operate
- No remote stream -> polling status works
- Remote outage -> local agents continue independently
- Internal Shade failures -> persistent agents continue and recover

## 10) Phased Execution Plan

Phase 0: Spec freeze
- Finalize schemas for agent, task, event, memory snapshot, remote link
- Finalize command contracts and RLM limits

Exit criteria:
- No unresolved architecture blockers for V1

Phase 1: Local persistent-agent orchestration
- Implement /agent create/list/assign/task
- Add internal automatic Shade lifecycle (not user-facing)
- Durable event logging + per-agent memory files
- Dashboard lists/controls persistent agents only

Exit criteria:
- Three concurrent projects manageable with restart-safe context

Phase 2: Remote linking
- Secure link handshake and enrollment
- Remote registry visibility in dashboard
- Remote command dispatch + status streams/polling fallback
- Reconnect/retry and audit logging

Exit criteria:
- Persistent remote agent controllable reliably from local dashboard

Phase 3: Memory hardening + RLM
- Add memory compaction and global/project layering
- Add recursion trace graph with budget enforcement
- Add context-restore validation checks

Exit criteria:
- Long-session continuity with low context-loss and traceable recursive decisions

Phase 4: Conflict/merge hardening
- Persistent-agent conflict resolver workflow
- Escalation path to Overseer
- “No work lost” guarantee via replayable logs

Exit criteria:
- Parallel work merges without silent drops or unrecoverable conflict states

## 11) Open Questions To Resolve Before Build Start

1. Remote topology in V1:
- static trusted node list or central relay

2. Auth rollout:
- shared token first or mTLS-first

3. Transport:
- polling-only V1 or SSE/WebSocket + polling fallback

4. No-provider behavior:
- what local non-LLM operations remain active

5. Internal Shade backend:
- subprocess pool vs external delegated runner first

6. Storage:
- file-only V1 or SQLite index from day one

7. RLM defaults:
- recursion depth, token budget, timeout budget

8. Merge policy:
- auto-merge thresholds vs mandatory overseer checkpoints

9. UI priority:
- Textual primary, curses fallback policy

10. Node enrollment UX:
- pairing code vs key-file exchange

## 12) Design Recommendations

- Keep one user-facing persistent agent model; specialization is metadata.
- Keep Shades internal and automatic; never expose as primary user control objects in V1.
- Use secure registry + API for remote control instead of mesh complexity.
- Keep append-only event memory as canonical truth.
- Treat cognee-style graph memory as derived acceleration layer.
- Budget and trace recursive reasoning from day one.

## 13) Mandatory Shade Contract + Phase Indexing (Prominent V1 Rule)

This is now a hard requirement for Charon:
- Every delegated Shade run must be represented by a contract object with explicit fields:
  - goal
  - constraints[]
  - expected_outputs[]
  - ordered phases[]
- Every phase must have a stable phase_id (P01, P02, ...) and lookup_key (contract_id:phase_id).
- Every phase transition must emit append-only events (queued/completed/failed/branched).
- Parent Charon must be able to inspect progress by contract_id and phase_id instantly.

Branch-and-resume requirement:
- If a phase fails, Charon should branch from the failure boundary (or just before it) rather than replaying the entire sequence.
- Branches must be explicit, tracked by branch_id, and preserve prior execution history.
- A branch operation must reset only the selected phase and downstream phases, keeping upstream completed phases intact.

Storage requirement (V1 baseline):
- Canonical contract index: .charon_state/shade_contracts.json
- Canonical phase event log: .charon_state/shade_phase_events.jsonl

Operational commands expected:
- list contracts
- inspect one contract
- inspect phase events
- branch/resume from phase

This indexing and replayability model is the foundation for rapid failure investigation and minimal recompute in multi-agent workflows.
