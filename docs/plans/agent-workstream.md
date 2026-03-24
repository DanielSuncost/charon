# Charon Agent Workstream — Handoff Document

> For the agent working on Charon's core agent behavior, memory, orchestration,
> and onboarding. The UI/UX workstream is handled separately.

## Current Status (2026-03-21)

**374 tests passing.** All original 7 priorities from this doc are either
complete or superseded:

| Original Priority | Status |
|-------------------|--------|
| 1. Wire SQLite | ✅ Done — dual-write to all modules, store_adapter.py |
| 2. Onboarding auto-setup | ✅ Done — creates agent + detects processes on /setup complete |
| 3. System prompt & personality | ✅ Done — 10-layer builder with identity, memory, goals, coordination |
| 4. Memory retention | ✅ Done — structured user model (7 categories), project knowledge, consolidation |
| 5. Shade execution with real LLM | ✅ Done — parallel batch swarms, scope enforcement, model routing |
| 6. RLM | ⬜ Deferred — goal hierarchy with autonomous self-assignment covers the core use case |
| 7. Conflict/merge hardening | ⚠️ Partial — boundary detection + proposals work, auto-resolution not built |

**Additional features built beyond the original plan:**
- Steering + follow-up queues (interrupt/queue messages during streaming)
- Autonomous goal-driven work (goal states, confirmation, self-assignment)
- Dynamic tool loader (agents build their own tools)
- HTTP, Git, Search tools
- Recurring task support with scheduling
- Intelligent task summarization (fact-based, not truncated chatter)
- Shade usage stats (tokens per shade/agent/model)
- Background worker (consolidation, goal inference, queue processing during chat)
- Multi-provider support with mid-session switching
- Conversation persistence and FTS5 search

**See README.md for the full feature list and current architecture.**

---

> ⚠️ The rest of this document reflects the ORIGINAL state when the workstream
> started. It is preserved for historical context but is no longer accurate
> for current priorities. See `autonomous-goal-driven-work.md`,
> `unified-daemon-and-coins.md`, and `user-model-schema-design.md` for
> current designs.

## Project Context

Charon is a single-user, multi-project agent operating system. It lets you run
persistent coding agents, coordinate them with ephemeral "shade" workers, and
manage sessions across local and remote machines.

The codebase lives at `/home/dopppo/Projects/charon`. It's a Python backend
with a Bun/TypeScript frontend (OpenTUI). Your work is entirely in Python.

## What Already Exists and Works

### Core Engine (all in `apps/core-daemon/`)

| File | Lines | What it does | Status |
|------|-------|-------------|--------|
| `conversation_engine.py` | 430 | Multi-turn LLM agent loop: stream → tool calls → execute → loop. Auto-compaction when context grows. | ✅ Works but needs enrichment |
| `tools/__init__.py` | 350 | Read, Write, Edit, Bash tools matching pi-agent semantics. Truncation, error handling. | ✅ Works |
| `providers/__init__.py` | 110 | Provider abstraction (Anthropic, OpenAI-compat, local) | ✅ Works |
| `providers/httpx_openai.py` | 300 | Zero-dependency streaming provider for LM Studio/Ollama. Handles `<think>` blocks. | ✅ Works |
| `providers/anthropic.py` | 180 | Anthropic Claude with thinking support | ✅ Works |
| `provider_bridge.py` | 230 | Reads onboarding.json + auth tokens → creates Provider + ModelInfo | ✅ Works |
| `agent_runtime.py` | 500 | Task execution. `run_task_tick()` dispatches to ConversationEngine when LLM mode active, falls back to heuristic. Per-agent engine caching. | ✅ Works |
| `charon_loop.py` | 993 | F00 persistent loop. Task queue, retry, idle, stop-file, shade delegation, boundary detection, goal tracking. | ✅ Works |
| `shade_orchestrator.py` | 400 | Contract-based task decomposition. Phases, branch-from-failure, event logging. | ✅ Works |
| `agent_lifecycle.py` | 210 | Agent CRUD, tmux session management, auto-naming. | ✅ Works |
| `agent_policy.py` | 67 | Shade delegation heuristics | ✅ Works |
| `goal_runtime.py` | 288 | Project/session/goal hierarchy, intent ingestion, context packets | ✅ Works |
| `intervention_graph.py` | 129 | Append-only message/intervention graph with conversation threading | ✅ Works |
| `conversation_runtime.py` | 305 | Thread management, task enqueueing | ✅ Works |
| `boundary_runtime.py` | 104 | Scope overlap detection, proposal/resolution | ✅ Works |
| `llm_adapter.py` | 156 | Legacy LM Studio adapter (sync, urllib). Being replaced by providers/ | ⚠️ Legacy |
| `user_model.py` | 83 | User preferences stub | ⚠️ Stub |
| `charon_auth.py` | 252 | OAuth flow for Claude/Codex. PKCE + local callback. | ✅ Works |

### Storage

| File | Lines | What it does | Status |
|------|-------|-------------|--------|
| `libs/store.py` | 880 | SQLite persistence. 14 tables, WAL mode, full CRUD, migration from JSON. | ✅ Works but NOT wired in |

The daemon (`charon_loop.py`) and all runtime modules still use JSON files in
`.charon_state/`. The SQLite store exists and is tested (49 tests) but nothing
reads/writes from it yet. **This is a key integration task.**

### Tests

193 tests passing in 3.5 seconds. Test files in `tests/`.

### CLI Scripts (in `scripts/`)

| Script | What it does |
|--------|-------------|
| `charon_chat.py` | Direct chat with ConversationEngine (no daemon) |
| `charon_agents.py` | Full agent lifecycle CLI: create, session, setup, task, shade ops |
| `charon_delegate.py` | Legacy delegation runner |

## What Needs To Be Built

### Priority 1: Wire SQLite Store Into Daemon

Currently all state is in JSON files with no locking. Under concurrent shade
workers this will corrupt. The SQLite store (`libs/store.py`) has every table
needed. The task:

1. Create an adapter layer that the existing modules can call
2. Replace `_read_json` / `_write_json` / `_append_jsonl` patterns with store calls
3. Keep JSON files as a fallback/export format
4. Run `migrate_from_json()` on first startup to import existing state

Key modules to update:
- `charon_loop.py` — queue operations
- `agent_lifecycle.py` — agent CRUD
- `agent_runtime.py` — working memory, inbox, attempts
- `shade_orchestrator.py` — contract storage
- `boundary_runtime.py` — boundary proposals
- `goal_runtime.py` — project/session/goal docs
- `conversation_runtime.py` — task enqueueing

### Priority 2: Onboarding Auto-Setup

When onboarding completes (`/setup complete`), Charon should automatically:

1. Create a default "charon-main" agent (persistent, assigned to the configured project)
2. Start the daemon loop in the background
3. Detect other agent processes on the machine (use `apps/tui/process_inspector.py`)
4. Show detected agents in the dashboard
5. If the user chose "no-provider", skip agent creation but still detect others

The onboarding flow is documented in `docs/onboarding-summary.md`. The
compatibility contract is in `docs/contracts/onboarding-compatibility.md`.

Currently the `/setup` commands work and save to `onboarding.json`, but nothing
happens after `/setup complete` — no agent is created, no daemon starts.

### Priority 3: Agent System Prompt & Personality

The conversation engine uses a generic system prompt built by
`build_system_prompt()` in `conversation_engine.py`. This needs to become
Charon-specific:

1. Charon should know it's Charon (not a generic assistant)
2. It should know about its project, its goals, its recent memory
3. It should know about other agents and how to coordinate
4. The system prompt should include the agent's goal and project context
5. Memory notes from working memory should be injected as context

Look at how pi-agent builds its system prompt in
`/home/dopppo/Projects/pi-mono/packages/coding-agent/src/core/system-prompt.ts`
for reference.

### Priority 4: Memory Retention & Consolidation

Currently `agent_runtime.py` saves the last 20 task summaries to working memory.
This needs to become a real memory system:

1. **Event memory** (append-only): every task lifecycle event, delegation decision,
   merge/conflict outcome. Already partially done via `intervention_graph.py`.
2. **Knowledge memory** (derived): per-agent durable memory, per-project context,
   global user preferences. Compacted periodically.
3. **Context packets**: `goal_runtime.py` already builds these. They need to be
   enriched with memory and injected into the system prompt.

See `docs/plans/2026-03-16-charon-agents-shades-remote-v1.md` section 7 for the
full memory strategy.

### Priority 5: Shade Execution With Real LLM

The shade orchestrator creates contracts with phases, but the actual execution
in `_run_task_with_engine` treats shade tasks the same as regular tasks. Shades
need:

1. Budget enforcement (token limit, time limit per phase)
2. Scoped tool access (restrict to contract's scope)
3. Contract constraints injected into the system prompt
4. Phase-specific objectives as the instruction
5. Result validation against expected_outputs
6. Branch-from-failure with the LLM re-evaluating the approach

The shade contract spec is in `docs/plans/2026-03-16-charon-agents-shades-remote-v1.md`
section 13.

### Priority 6: RLM (Recursive Learning Machine)

Feature F52. Persistent agents should be able to recursively decompose tasks:

1. Every recursion unit has: id, parent_id, objective, budget, outcome
2. Enforce depth, token/time budget, and timeout limits
3. Promote only finalized outputs into durable knowledge layers
4. Trace graph for debugging and replay

Schema is in `docs/contracts/rlm-node.schema.json`.

### Priority 7: Agent Specialization

Agents should develop specializations over time through use. This is NOT a
rigid role taxonomy — it's an emergent property.

**Data model:** Add `specialization` field to agent schema (string, default empty).
Examples: "infrastructure", "frontend", "data pipeline", "documentation".

**How it gets set:**
1. User explicitly tells the agent: "you're my infrastructure agent"
2. Agent proposes it based on task patterns: "I've been doing mostly database
   work. Should I specialize as your database agent?"
3. Via `/agent specialize <id> <label>` command

**Boundary awareness (gentle, not annoying):**
When an agent receives a task outside its specialization, it should ONCE say:
"I can help with this, but it's outside my usual focus (database/backend).
Want me to handle it, or spin up a separate agent for frontend work?"

If the user says "just do it", the agent does it and doesn't ask again for
that type of task. This is a soft nudge, not a gate.

**What the UI shows (already implemented):**
- Dashboard agent list: `● scout (infrastructure)` — gold label in parentheses
- Dashboard detail panel: Role field shows specialization in gold when set
- Session Grid summary line: `infrastructure — deploying nginx config`
- All rendered via `agent.specialization` field which the UI reads from agent data

**Storage:** Add to agent schema in `docs/contracts/agent.schema.json`.
Store in agents.json and SQLite agents table. The `specialization` field
should be in the agent's working memory so it persists across sessions.

### Priority 8: Conflict/Merge Hardening

When multiple agents work on overlapping scopes:

1. Boundary detection already works (`boundary_runtime.py`)
2. Need: persistent-agent conflict resolver workflow
3. Need: escalation path to user (the "Overseer")
4. Need: "no work lost" guarantee via replayable logs

## Key Design Documents

Read these before starting:

1. `docs/plans/MASTER_PLAN.md` — Overall project plan and architecture decisions
2. `docs/plans/2026-03-16-charon-agents-shades-remote-v1.md` — V1 design charter (the most important doc)
3. `docs/plans/2026-03-16-phase0-spec-freeze-checklist.md` — Spec freeze checklist
4. `docs/plans/F00-v1-implementation-spec.md` — Persistent loop spec
5. `docs/contracts/command-contracts.md` — Command surface
6. `docs/contracts/onboarding-compatibility.md` — Onboarding contract
7. `docs/onboarding-summary.md` — Onboarding flow

## Key Contracts (JSON Schemas)

In `docs/contracts/`:
- `agent.schema.json` — Agent entity
- `task.schema.json` — Task entity
- `event.schema.json` — Event entity
- `node-link.schema.json` — Remote node link
- `rlm-node.schema.json` — RLM recursion node

Test fixtures in `tests/contracts/fixtures/valid/` and `invalid/`.

## How To Run Things

```bash
# Run all tests
cd /home/dopppo/Projects/charon
python -m pytest tests/ -q

# Direct chat (no daemon, no TUI)
python scripts/charon_chat.py --provider local

# Agent lifecycle CLI
python scripts/charon_agents.py create --goal "test" --project /tmp/test --no-tmux
python scripts/charon_agents.py list
python scripts/charon_agents.py session AG-0001 --no-daemon

# Setup
python scripts/charon_agents.py setup status
python scripts/charon_agents.py setup provider lmstudio
python scripts/charon_agents.py setup model qwen3-30b-a3b
python scripts/charon_agents.py setup complete

# Run the daemon loop directly
python apps/core-daemon/charon_loop.py --state-dir .charon_state --max-cycles 5

# Check current state
cat .charon_state/onboarding.json | python -m json.tool
cat .charon_state/agents.json | python -m json.tool | head -30
```

## Environment

- Python 3.12, pytest 9.0
- LM Studio running on localhost:1234 with qwen3-30b-a3b
- 5090 GPU, 32GB VRAM
- httpx 0.28.1 (the only non-stdlib dependency for providers)
- No openai or anthropic SDK installed (httpx provider used instead)
- SQLite via stdlib sqlite3

## What NOT To Touch

- `apps/tui/opentui/` — the UI workstream handles this
- `tools/charons-boat/` — the UI workstream handles this
- `apps/core-daemon/tmux_capture.py` — the UI workstream handles this
- Frontend/backend protocol — coordinate with UI workstream if changes needed

## Definition of Done (V1)

From `docs/plans/2026-03-16-charon-agents-shades-remote-v1.md`:

1. Three concurrent projects manageable with restart-safe context
2. Persistent remote agent controllable reliably from local dashboard
3. Long-session continuity with low context-loss and traceable recursive decisions
4. Parallel work merges without silent drops or unrecoverable conflict states
5. Users interact only with persistent agents; no direct shade control required
