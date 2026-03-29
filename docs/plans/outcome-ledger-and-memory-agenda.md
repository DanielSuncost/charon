# Charon Outcome Ledger + Memory Agenda

## Goal

Ship the work in this order:

1. Replace the sidebar task list with a **session-local outcome ledger**.
2. Once task/outcome semantics are stable, rebalance **session vs agent vs project memory**.
3. Strengthen **project-level coordination** so multiple specialized agents can collaborate automatically on the same project without sharing raw task transcript history.

---

## Milestone A — Session outcome ledger

### Product intent
The right-side Tasks tab should show only meaningful work outcomes from the current session:
- completed work the model actually accomplished
- failed / redirected attempts
- optionally one active current task

It should not show:
- one item per message
- raw prompt fragments
- weak summaries of the user's message text

### UX model
Render items like:
- `[+] added terminal title`
- `[+] fixed local token tracking`
- `[-] gpu_monitor launch attempt`
- `[~] investigating fresh-session memory bleed`

### Status semantics
- `[~]` active
- `[+]` completed
- `[-]` failed / redirected / abandoned

### Inference rules
- Start a task when the user makes a concrete request.
- Keep it active while the model works on it.
- Mark it completed when the user implicitly or explicitly accepts it and moves on.
- Mark it failed when the user redirects, rejects, or the task clearly times out / goes wrong.
- Store this ledger **per session only**.

### Data model
Each entry should carry:
- `task_id`
- `status`
- `kind`
- `object`
- `title`
- `instruction`
- `summary`
- `detail`
- `tokens_in`
- `tokens_out`
- `tool_calls`
- `turns`
- `files_touched`
- `ts`
- `resolved_at`

### Implementation notes
- Primary backend owner: `apps/tui/opentui/chat_backend.py`
- Primary frontend owner: `apps/tui/opentui/src/index.ts`
- Persistence: `.charon_state/conversations/<session-id>.outcomes.json`

### Done when
- A fresh session starts with an empty ledger.
- `/resume` restores the ledger for that session only.
- The pane no longer shows one item per user message.
- Titles are action-oriented plain language, not prompt fragments.

---

## Milestone B — Memory boundary cleanup

### Product intent
Make fresh sessions feel fresh while preserving:
- project context
- persistent agent specialization
- coordination across agents on the same project

### Desired split
#### Session memory
Use for:
- active thread state
- outcome ledger
- local blockers
- immediate next step
- detailed task history for that conversation only

#### Agent memory
Use for:
- specialization / role
- durable focus
- reusable role-specific lessons

Do **not** use for:
- raw task summaries
- transcript-like assistant prose
- unfinished conversational replies

#### Project memory / coordination
Use for:
- project knowledge
- active goals
- who is working on what
- touched / claimed files
- interventions / overlap warnings

### Implementation notes
- Audit prompt assembly in `apps/core-daemon/system_prompt_builder.py`
- Reduce the amount of session-like task carryover at the agent level
- Promote only durable facts to agent memory
- Promote only shared coordination facts to project memory

### Done when
- Saying `hello` in a fresh session does not continue old work from memory.
- A persistent agent still knows its role and broad focus.
- Detailed task flow is visible only in the resumed session, not in every fresh session.

---

## Milestone C — Project coordination layer

### Product intent
Allow multiple specialized agents on the same project to coordinate automatically.

### Shared coordination state should capture
- agent id / role
- current focus
- touched files
- claimed files / scopes
- status (`active`, `blocked`, `waiting`, `idle`)
- recent interventions / boundaries

### Desired behavior
- Overlap should be detected automatically.
- Interventions should be sent automatically when files / behavior overlap.
- The user should not have to mediate routine coordination.

### Done when
- Agents can see a compact shared coordination snapshot.
- Overlapping work triggers automatic intervention / boundary behavior.
- Shared project context is useful without exposing raw task transcript history.

---

## Delivery order

### Phase 1
Ship the new session-local outcome ledger first.

### Phase 2
Use the ledger semantics to clean up session vs agent vs project memory boundaries.

### Phase 3
Strengthen automatic multi-agent project coordination.

---

## Why this order

The sidebar work forces us to define the true unit of work in Charon:
- not a message
- not a transcript fragment
- but a user-requested outcome

Once that is stable, we can assign memory ownership correctly:
- outcome detail → session
- specialization / durable lessons → agent
- coordination / overlap → project
