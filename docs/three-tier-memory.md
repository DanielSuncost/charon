# Charon Three-Tier Memory

Charon agents share a three-tier memory hierarchy that gives every agent
continuity across sessions, shared knowledge across a project, and personal
working context — without requiring any external services.

## The Hierarchy

| Tier | What it stores | Lifetime | Shared with | Changes |
|------|---------------|----------|-------------|---------|
| **User Model** | Who you are — preferences, coding style, corrections, workflow habits | Permanent | Every agent, every project | Rarely — when you correct an agent or it notices a pattern |
| **Project Knowledge** | What's true about the codebase — architecture decisions, conventions, known issues, build commands | Project lifetime | Every agent on that project | Weekly — as agents learn about the codebase |
| **Agent Working Memory** | What just happened — recent task summaries, current approach, temporary observations | Rolling window (last 20 tasks) | Only the owning agent | Every task |

## Why three tiers?

**The User Model is permanent because you only teach a preference once.**
When you correct Agent A's coding style, Agent B already knows on its
next task. Delete an agent, archive a project, reset a session — the
user model survives. New agents inherit everything you've ever told any
agent.

**Project Knowledge is shared because agents on the same codebase need
the same context.** When Agent A discovers that the auth module has a
race condition, Agent B working on the API layer should know that before
it touches shared state. Project knowledge is the collective intelligence
of every agent that has ever worked in that directory.

**Working Memory is private because it's the agent's scratchpad.** "I
just tried approach X and it failed because of Y, so I'm going to try
Z." This is fast-moving, noisy, and only relevant to the agent that's
mid-task. Other agents don't need it.

## How memory flows into the system prompt

At the start of every task, Charon assembles the agent's system prompt
from all three tiers:

```
┌─────────────────────────────────────────────────────┐
│  SYSTEM PROMPT                                      │
│                                                     │
│  1. Agent identity (name, role, project, goal)      │
│  2. User Model (frozen snapshot, ~2000 chars)       │
│  3. Project Knowledge (frozen snapshot, ~3000 chars) │
│  4. Working Memory (last 5 tasks + summary, ~1500)  │
│  5. Goal context (objectives, active/blocked goals) │
│  6. Coordination (other agents, pending boundaries) │
│  7. Tools + guidelines                              │
│  8. Context files (AGENTS.md, CHARON.md)            │
│  9. Date + working directory                        │
└─────────────────────────────────────────────────────┘
```

Total memory footprint: ~6500 characters ≈ 1600 tokens. Small enough to
always include, large enough to be useful.

**Frozen snapshot pattern:** Memory is captured once at task start and
never mutated during execution. Mid-task writes update the backing store
(SQLite + markdown files) but don't change the running prompt. This
preserves the LLM's prefix cache for the entire task. The next task gets
a fresh snapshot.

## How agents write to memory

**User Model** — any agent can write. The agent is told in its system
prompt to proactively save what it learns:

> "You have access to a shared user profile that persists across all
> sessions. When the user corrects you, expresses a preference, or you
> notice a consistent pattern, save it. Don't wait to be asked."

Writes are scanned for prompt injection, deduplicated, and bounded
(2000 char limit — the agent must curate, not hoard).

**Project Knowledge** — any agent on the project can write. Same
scanning, dedup, and bounds (3000 chars).

**Working Memory** — only the owning agent writes. Updated automatically
after each task with a summary. Older entries are compacted during
context window management.

## Human readable and editable

All three tiers are backed by SQLite for concurrent safety, but also
exported as plain text files:

- `.charon_state/USER.md` — your user profile
- `.charon_state/projects/<id>/KNOWLEDGE.md` — per-project knowledge
- `.charon_state/agents/<id>/working_memory.json` — per-agent memory

You can read, edit, or delete entries at any time. Changes are picked up
on the next task start. No embedding models, no vector databases, no
graph infrastructure. Just text that humans and LLMs both understand.

## What sets this apart

Most agent frameworks have no memory at all (pi), or memory tied to a
single agent instance (Hermes MEMORY.md). Charon's three-tier approach
means:

- **New agents are never blank.** They inherit your preferences and the
  project's accumulated knowledge from their first task.
- **Agent replacement is seamless.** Delete an underperforming agent and
  create a fresh one — it immediately has all the context the old one
  had, minus the stale working memory.
- **Cross-agent learning is automatic.** One agent's discovery becomes
  every agent's knowledge, scoped to the appropriate tier.
- **Nothing requires external services.** SQLite + text files. Runs
  fully local on your machine.

## Search

Conversation search is built using SQLite FTS5. The `Search` tool lets
agents query past conversations by keyword, filtered by agent or role.
The index rebuilds automatically on first use. No external search
service needed.

## Background Consolidation

A background process periodically analyzes recent interactions and
updates the user model automatically. It runs only when there's fresh
user signal (at least one new message since last scan) and uses a
configurable model tier. Scan traces are stored in SQLite and viewable
via `/consolidation`. Configure with `/consolidation model`,
`/consolidation interval`, `/consolidation on|off`.

## Structured User Model

The user model has 7 categories: style, coding, tooling, workflow,
corrections, intentions, and patterns. Agents write to it via the
`UserModel` tool. It renders as a compact block in the system prompt
with `═══` delimiters and usage stats. Human-editable via
`.charon_state/USER.md`.

See [User Model Schema](docs/plans/user-model-schema-design.md) for
the full design.
