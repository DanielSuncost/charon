# Charon Memory, Goals & User Model Architecture

> Comprehensive design for Charon's memory hierarchy, goal structure,
> persistent user model, and how they wire together.
>
> Created: 2026-03-20
> Status: Active design discussion
> Related: agent-system-prompt-memory-design.md, agent-workstream.md

---

## 1. The Three Things That Need Memory

Charon has three distinct entities that accumulate state over time:

1. **The User** — one person, persists forever, shared across all agents and projects
2. **The Project** — a codebase or workspace, persists across sessions, shared across agents working on it
3. **The Agent** — a persistent worker, accumulates task experience and working knowledge

Each needs its own memory tier with different lifetimes, visibility, and curation strategies.

---

## 2. Memory Architecture

### Tier 1: User Model (global, permanent, shared by all agents)

**What it stores:**
- Communication preferences (concise vs detailed, technical level)
- Coding style (naming conventions, error handling preference, test philosophy)
- Tool preferences (preferred languages, frameworks, package managers)
- Workflow habits (reviews before merge? prefers small PRs? wants tests first?)
- Schedule patterns (when active, timezone)
- Correction history (things the user corrected → learned preferences)
- Cross-project patterns (design principles applied across projects)

**Lifetime:** Permanent. Survives agent deletion, project archival, everything.

**Storage:** `user_model` table in SQLite (already exists), key-value with structured JSON values. Also persisted as `USER.md` in `.charon_state/` for human readability.

**Size budget:** 2000 chars rendered for system prompt injection (matches Hermes' scale). Larger backing store on disk, but only the curated summary goes into prompts.

**Who writes to it:** Any agent can propose writes. Writes are:
- Scanned for injection (Hermes-style threat pattern matching)
- Deduplicated (no duplicate entries)
- Bounded (char limit enforced, agent must curate)

**How it gets into the system prompt:**
```
══════════════════════════════════════════════
USER PROFILE [67% — 1,340/2,000 chars]
══════════════════════════════════════════════
§ Prefers concise responses with code examples over explanation
§ Uses Python 3.12+, uv for deps, ruff for linting, pytest for tests
§ Names things with snake_case, dislikes abbreviations in variable names
§ Wants tests written alongside implementation, not as a separate step
§ Reviews code carefully — prefers small focused changes over large refactors
§ Timezone: UTC-5, typically active 9am-11pm
§ Corrected: don't use bare except, always catch specific exceptions
```

**Frozen snapshot pattern:** Like Hermes, the user model is captured at agent/task start and injected as a frozen block. Mid-session writes update the backing store but don't change the running prompt. This preserves prefix cache.

**How agents learn about the user:** Two mechanisms:
1. **Explicit corrections** — user says "no, do it this way" → agent writes to user model
2. **Observed patterns** — after N tasks, an agent notices patterns (e.g., user always asks for tests) and proposes a memory entry

The key insight from Hermes: the agent should be told to **proactively save** what it learns. This goes in the system prompt as behavioral guidance:

> "You have access to a shared user profile that persists across all sessions.
> When the user corrects you, expresses a preference, or you notice a consistent
> pattern, save it using the user_model tool. Don't wait to be asked."

### Tier 2: Project Knowledge (per-project, durable, shared by agents on that project)

**What it stores:**
- Architecture decisions ("we use a monorepo with apps/ and libs/")
- Known issues ("the auth module has a race condition under concurrent requests")
- Build/deploy conventions ("deploy via `make deploy-staging`, needs VPN")
- File organization patterns ("tests mirror src/ structure, named test_*.py")
- Dependency notes ("pinned to React 18 because of X incompatibility")
- Recent high-level progress ("API v2 migration 60% complete, auth and users done")

**Lifetime:** Persists as long as the project exists. Survives agent restarts and replacement.

**Storage:** `goal_projects` table in SQLite (already exists, currently stores goals). Extend the project doc to include a `knowledge` section. Also persisted as `PROJECT_KNOWLEDGE.md` in `.charon_state/projects/<id>/` for human readability.

**Size budget:** 3000 chars for system prompt injection. Larger backing store.

**Who writes to it:** Any agent assigned to the project. Same scanning/dedup/bounds as user model.

**How it differs from context files:** `AGENTS.md`/`CHARON.md` are **user-maintained** static files. Project knowledge is **agent-maintained** learned state. Both get injected into the system prompt, in separate sections:
```
# Project Context (from AGENTS.md — user maintained)
...

# Project Knowledge (agent learned)
══════════════════════════════════════════════
PROJECT: charon [45% — 1,350/3,000 chars]
══════════════════════════════════════════════
§ Monorepo: apps/core-daemon (Python), apps/tui/opentui (Bun/TS)
§ Tests: pytest in tests/, 207 passing, run with `python -m pytest tests/ -q`
§ SQLite store in libs/store.py, WAL mode, 14 tables
§ Provider bridge reads onboarding.json → creates Provider + ModelInfo
§ Shade contracts stored in shade_contracts.json, phase events in JSONL
§ The daemon loop in charon_loop.py processes queue.json every 2s
```

### Tier 3: Agent Working Memory (per-agent, session-scoped, private)

**What it stores:**
- Recent task summaries (what I just did, what happened)
- Current approach notes (strategy I'm following, why)
- Temporary observations (this file is weird, that test is flaky)
- Active reasoning chain (for multi-step tasks)

**Lifetime:** Rolling window. Last 20 task summaries kept. Older entries compacted into a running summary during compaction events.

**Storage:** `agent_working_memory` table in SQLite (already exists). Also in per-agent JSON files.

**Size budget:** Uncapped in storage. For system prompt injection: last 5 task summaries + compacted summary of older work. Roughly 1500 chars.

**Who writes to it:** Only the owning agent. This is private memory — other agents don't see it.

**This is the fast-changing layer.** User model changes rarely. Project knowledge changes weekly. Working memory changes every task.

---

## 3. Goal Structure

### Current state

The goal hierarchy is:
```
Project (home-dopppo-projects-charon)
  └── Session (session-ag-0011)
       └── Goal (goal-76cb35ab1c: "run: echo branded-shell-ok")
            └── linked_tasks: [task-66e4710ab3]
            └── evidence: [{summary: "..."}]
```

This works for tracking individual tasks but doesn't capture the higher-level structure of what the user actually wants to accomplish.

### Proposed enrichment

Add two levels above individual goals:

```
Objective (persistent, user-defined or inferred)
  "Build Charon into a working multi-agent OS"
  ├── Milestone: "Agent system prompt and memory working"
  │   ├── Goal: "Implement system prompt builder"
  │   ├── Goal: "Wire memory into prompt"
  │   └── Goal: "Add context file discovery"
  ├── Milestone: "Shade execution with real constraints"
  │   ├── Goal: "Inject contract into shade prompt"
  │   └── Goal: "Add budget enforcement"
  └── Milestone: "Soft specialization"
      └── Goal: "Task classification + affinity tracking"
```

**Objectives** are long-lived (days to weeks). The user states them ("I want to ship Charon V1") or the agent infers them from patterns of goals.

**Milestones** are medium-lived (hours to days). They group related goals into coherent deliverables. Created by the agent when decomposing objectives.

**Goals** are short-lived (minutes to hours). The existing goal nodes. Created per user intent or shade phase.

### Why this matters for the system prompt

The context packet injected into the system prompt currently shows individual goals. With the enriched hierarchy, it shows:

```
# Objectives
- Build Charon into a working multi-agent OS
  → Active milestone: Agent system prompt and memory working (2/3 goals done)
  → Next milestone: Shade execution with real constraints

# Active Goals
- Implement system prompt builder (in progress, 1 task linked)
- Wire memory into prompt (pending)
```

This gives the agent strategic awareness — it knows not just what to do next, but WHY and what comes after.

### How objectives are created

1. **User states explicitly:** "My goal is to ship Charon V1 by end of month"
2. **Agent infers from patterns:** After 5+ related goals, proposes: "It looks like you're building a multi-agent coordination system. Want me to track this as an objective?"
3. **From planning documents:** Agent reads `MASTER_PLAN.md` or `docs/plans/` and extracts objectives

---

## 4. User Model: The Persistent Cross-Session Identity

### Why this is important for Charon specifically

Pi doesn't need a user model — every session is independent. Hermes has USER.md but it's one file for one agent. Charon's user model is different because:

1. **Multiple agents share one user.** When the user corrects Agent A's coding style, Agent B should learn that too.
2. **Agents come and go.** When you delete an agent and create a new one, the new agent should already know your preferences.
3. **Cross-project patterns.** Your preference for "small PRs" applies everywhere, not just one project.

### How it works end-to-end

**Writing (any agent → user model):**
```python
# Agent notices user correction
user_model_tool(action='add', content='Prefers explicit error handling, no bare except')

# Agent observes pattern after 5 tasks
user_model_tool(action='add', content='Always wants tests alongside implementation')

# Agent records correction
user_model_tool(action='add', target='corrections',
                content='Told not to use `typing.Optional`, use `X | None` instead')
```

**Storage flow:**
```
Agent calls user_model tool
  → Content scanned for injection
  → Dedup check against existing entries
  → Char budget check (2000 chars)
  → Written to SQLite user_model table
  → Also written to .charon_state/USER.md (human-readable)
  → NOT injected into current prompt (frozen snapshot)
  → Will appear in ALL agents' prompts on next task start
```

**Reading (system prompt injection at task start):**
```python
def build_user_model_block(db) -> str:
    """Build the user model section for system prompt injection."""
    model = user_model_get(db)  # from SQLite
    
    sections = []
    
    # Preferences
    prefs = model.get('preferences', {})
    if prefs:
        for key, entry in prefs.items():
            sections.append(entry.get('value', ''))
    
    # Corrections (high-signal entries)
    corrections = model.get('corrections', [])
    for c in corrections[-10:]:
        sections.append(f"Corrected: {c}")
    
    # Observed patterns
    patterns = model.get('patterns', [])
    for p in patterns[-10:]:
        sections.append(p)
    
    content = '\n§\n'.join(sections)
    if not content:
        return ''
    
    char_limit = 2000
    pct = int(len(content) / char_limit * 100)
    header = f"USER PROFILE [{pct}% — {len(content):,}/{char_limit:,} chars]"
    sep = '═' * 46
    return f"{sep}\n{header}\n{sep}\n{content}"
```

### What makes this different from Hermes

| Aspect | Hermes USER.md | Charon User Model |
|--------|---------------|-------------------|
| Scope | One agent, one instance | All agents, all projects |
| Storage | Flat file, § delimited | SQLite table + markdown export |
| Access | Only the owning agent reads/writes | All agents read, any agent writes |
| Content | Free-form notes | Structured: preferences, corrections, patterns |
| Curation | Agent self-curates | Agent proposes, dedup + bounds enforced |
| Injection scanning | Yes | Yes (same threat patterns) |

### The user can edit it directly

`USER.md` in `.charon_state/` is always kept in sync with SQLite. The user can:
- `cat .charon_state/USER.md` to see what agents know about them
- Edit it directly — changes are picked up on next agent task start
- Delete entries they disagree with

This is the "human readable/editable" advantage of text memory.

---

## 5. How Everything Wires Together

### At task start (system prompt assembly)

```
1. Load user model from SQLite (frozen snapshot)
2. Load project knowledge for this agent's project (frozen snapshot)
3. Load agent's working memory (last 5 summaries + compacted older)
4. Load goal context packet (active/blocked goals, current objective)
5. Load context files (AGENTS.md, CHARON.md from project dir)
6. Load coordination context (other agents, pending boundaries)
7. If shade: load contract constraints
8. Assemble system prompt with all layers
```

### At task completion

```
1. Update agent working memory (add task summary)
2. If user corrected something → propose user model write
3. If learned something about the project → propose project knowledge write
4. Update goal status (completed/failed/blocked)
5. If shade: report results to parent contract
```

### At compaction time

```
1. Select compaction template based on agent specialization
2. Generate summary (iterative update if previous summary exists)
3. Replace older messages with summary
4. Invalidate cached system prompt → forces rebuild on next task
5. Reload memory from disk (captures any mid-session writes)
```

### At agent creation

```
1. User model already available (global, not per-agent)
2. Project knowledge already available (per-project, not per-agent)
3. Working memory starts empty
4. Goals inherited from project context
5. Specialization starts as 'generalist'
```

---

## 6. SQLite Schema Additions

The existing schema mostly covers this. Additions needed:

```sql
-- Project knowledge (extends existing goal_projects)
CREATE TABLE IF NOT EXISTS project_knowledge (
    project_id  TEXT NOT NULL,
    entry_id    TEXT PRIMARY KEY,
    content     TEXT NOT NULL,
    source_agent_id TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pk_project ON project_knowledge(project_id);

-- User model corrections (supplement existing user_model key-value)
-- The existing user_model table works for preferences.
-- Add a structured entries table for corrections and patterns.
CREATE TABLE IF NOT EXISTS user_model_entries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    category    TEXT NOT NULL,  -- 'preference', 'correction', 'pattern', 'note'
    content     TEXT NOT NULL,
    source_agent_id TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ume_category ON user_model_entries(category);

-- Objectives and milestones (extend existing goal system)
CREATE TABLE IF NOT EXISTS objectives (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'active',
    project_id  TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    source      TEXT NOT NULL DEFAULT 'inferred'  -- 'user_stated', 'inferred', 'from_document'
);

CREATE TABLE IF NOT EXISTS milestones (
    id              TEXT PRIMARY KEY,
    objective_id    TEXT NOT NULL,
    title           TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'active',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
```

---

## 7. Implementation Order

1. **User model tool** — the `user_model` tool that agents call to read/write the user profile. Injection scanning. Char budget. Dedup. This is the foundation everything else builds on.

2. **System prompt builder** — the layered prompt assembly from the previous design doc, now with user model + project knowledge injection.

3. **Project knowledge store** — per-project learned knowledge with agent write access.

4. **Goal hierarchy enrichment** — objectives and milestones above the existing goal nodes.

5. **Memory refresh on task start** — ensure all three memory tiers are fresh in the system prompt, not stale from engine cache.

6. **Compaction integration** — role-aware templates that preserve the right information per specialization.

7. **FTS5 search** — add full-text search over task history, project knowledge, and user model for cross-session recall.
