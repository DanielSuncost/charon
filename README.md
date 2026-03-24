# Charon

**A single-user agent operating system.** Run persistent coding agents,
coordinate them with ephemeral worker swarms, and manage everything from
one terminal.

<p align="center">
  <img src="assets/mascot_sm.png" alt="Charon" width="480" />
</p>

---

## Why Charon

Other agent frameworks give you one agent, one session, no memory. You
re-explain context every time. You can't run multiple agents on the same
project without them colliding. You can't hand off work to a background
swarm and check on it later.

Charon is different:

- **Agents remember everything.** Every conversation is indexed into
  a local semantic memory. Agents recall past discussions by meaning,
  not just keywords — and they know when facts have changed.
- **Agents know you from day one.** Preferences, corrections, and
  project knowledge persist across sessions and transfer to new agents
  automatically. You teach something once.
- **Agents coordinate.** Boundary detection, scope negotiation, and an
  intervention graph prevent collisions automatically.
- **Agents delegate.** Complex tasks decompose into parallel shade
  swarms — ephemeral workers with scope restrictions and budget limits.
- **Agents build their own tools.** Drop a Python file in a directory
  and it's available. Or tell the agent to build what it needs.
- **Everything is local.** SQLite + local embeddings. No vector DB, no
  cloud memory service, no API keys for recall. Your data stays on
  your machine.

---

## Core Features

### Memory That Actually Works

Most agent frameworks have no memory at all. The ones that do ship it
to a cloud service. Charon keeps everything local — and scores
[78.8% on LongMemEval](https://github.com/xiaowu0162/LongMemEval),
the standard benchmark for long-term AI memory, approaching the
cloud-hosted state of the art.

Two systems work together. Your agent always has context, and can
always dig deeper:

**Always in context** — a structured profile of who you are and what
the project needs, injected into every conversation automatically:

| | What it knows | Shared with |
|---|---|---|
| **Your preferences** | Coding style, tool choices, corrections you've made | Every agent, every project |
| **Project knowledge** | Architecture decisions, conventions, known issues | Every agent on that project |
| **Working memory** | What just happened, current approach | Only the owning agent |
| **Recalled facts** | Relevant memories from past conversations | Auto-injected per task |

**Deep recall on demand** — every conversation is indexed into a local
vector database. When the agent needs to remember something specific,
it searches by meaning:

```
You: "What did we decide about the auth module last week?"
Agent: [searches semantic memory]
       3 relevant memories (4ms):
       1. Decided to migrate auth to OAuth2 with PKCE flow
       2. Auth module has a race condition in token refresh
       3. User prefers passport.js over custom auth
```

The agent doesn't need to be told when to search. Relevant context is
auto-injected from memory at the start of every task. When that's not
enough, the agent searches deeper on its own — it knows what it knows
and what it needs to look up.

Under the hood: hybrid vector similarity + keyword search, merged with
reciprocal rank fusion. Version chains track knowledge updates so the
agent always knows which facts are current. ~5ms recall on thousands
of memories, running entirely on CPU.
[Architecture →](docs/plans/semantic-memory-engine.md) ·
[Three-tier design →](docs/three-tier-memory.md)

### Shade Swarms

Tell an agent to do 20 things in parallel and it spawns a swarm:

```
You: "Generate test fixtures for all 6 tool modules in parallel"
Agent: [calls SpawnBatch with 6 tasks, max 6 concurrent]
       All 6 shades running. Check progress with /batch
```

Each shade gets its own conversation engine, its own model (configurable
per complexity tier), and scope restrictions that prevent it from
touching files outside its contract. Results are tracked per-task with
model and token usage stats.

**Sequential contracts** for complex multi-step work (analyze →
implement → verify → report). **Parallel batches** for independent
identical tasks. The agent picks the right pattern.

### Autonomous Goal-Driven Work

Toggle autonomous mode and the agent works through confirmed goals
independently:

```
/autonomous on
/confirm                    — approve a proposed goal
```

The agent proposes goals with acceptance criteria, plans execution
steps, works through them with git checkpoints, and verifies completion.
You can set time budgets, token budgets, and interrupt at any time by
sending a message.

Goals are inferred from conversation in the background — the agent
proactively suggests what it thinks you want done and waits for your
confirmation before starting.

### Multi-Provider Support

```bash
charon                        # default provider
charon claude-code            # start with Claude
charon codex                  # start with Codex
charon lmstudio               # start with local models
charon --provider opencode    # any provider
```

Switch mid-session with `/provider codex`. Configure separate
providers for shades with `/setup shade-model auto` — fast models
for simple tasks, strong models for complex ones.

### Dynamic Tool System

14 built-in tools: Read, Write, Edit, Bash, Git, Http, Search, Recall,
UserModel, ProjectKnowledge, SpawnShade, SpawnBatch, Web, Browser.

Plus a dynamic loader: drop a `.py` file in `.charon_state/tools/` or
`<project>/.charon/tools/` and it's available to the agent after
`/tools reload`. Or tell the agent to build the tool itself.

### Agent Coordination

- **Boundary detection** — automatically detects when agents' scopes
  overlap
- **Intervention graph** — append-only DAG recording every coordination
  decision with causal chains
- **Session branching** — branch any conversation from any point

### Charon's Boat

Bridge any agent framework into Charon's network:

```bash
charons-boat wrap -- pi       # wraps pi-agent in tmux
charons-boat wrap -- hermes   # wraps hermes
```

Appears in the session grid. View output, send input, monitor status.

---

## Quick Start

```bash
# Install Bun (https://bun.sh), then:
git clone <repo> && cd charon
uv pip install -r requirements.txt   # Python deps (httpx, browser-use)
cd apps/tui/opentui && bun install && cd ../../..
./charon
```

First run:
```
/setup provider lmstudio      # or claude-code, codex, api
/setup model qwen3-30b-a3b
/setup complete
```

You're chatting with a Charon agent that remembers everything.

**Key commands:**
```
/idea <text>             — capture an idea to the backlog
/goals                   — view goals (active, backlog, blocked)
/confirm                 — approve a proposed goal
/autonomous on|off       — toggle autonomous work mode
/provider <name>         — switch provider mid-session
/batch                   — check shade swarm progress
/shades                  — view shade usage stats
/history                 — task history for this agent
/tools                   — list all tools (built-in + dynamic)
/tools reload            — reload dynamic tools
/consolidation           — view user model scan traces
/search <query>          — search past conversations
```

**TUI controls:** F1 Chat · F2 Dashboard · F3 Sessions · Ctrl+T
Timestamps · Escape Abort · Enter (while streaming) Steer

---

## Architecture

```
charon/
├── apps/core-daemon/              # Python agent runtime
│   ├── conversation_engine.py     # Multi-turn LLM with tool use, steering, compaction
│   ├── system_prompt_builder.py   # 10-layer context-aware prompt assembly
│   ├── agent_runtime.py           # Task execution with intelligent summarization
│   ├── memory_engine.py           # Semantic memory: sqlite-vec + FTS5 hybrid search
│   ├── memory_indexer.py          # Background conversation → memory indexing
│   ├── memory_extractor.py        # LLM-based fact extraction from sessions
│   ├── shade_orchestrator.py      # Sequential contract lifecycle
│   ├── batch_orchestrator.py      # Parallel shade swarms
│   ├── autonomous.py              # Goal-driven self-assignment
│   ├── consolidation.py           # Background user model analysis
│   ├── user_model_structured.py   # 7-category structured user profile
│   ├── model_registry.py          # Multi-tier model routing for shades
│   ├── task_summarizer.py         # Fact-based task summaries
│   ├── task_ledger.py             # Unified task history
│   ├── goal_runtime.py            # Goals with idea capture + promotion
│   ├── charon_loop.py             # Daemon loop with heartbeat + recurring tasks
│   ├── shade_stats.py             # Token usage tracking per shade/agent/model
│   ├── providers/                 # Anthropic, OpenAI, local (httpx, zero SDK deps)
│   └── tools/                     # 11 built-in + dynamic plugin loader
├── apps/tui/opentui/              # Bun + OpenTUI terminal frontend
├── libs/store.py                  # SQLite persistence (WAL, 14 tables)
├── tools/charons-boat/            # Universal agent bridge
└── docs/                          # Design documents
```

---

## Status

**545 tests passing.**

### Done

- Multi-turn conversation with streaming, tool use, steering, follow-up queues
- 10-layer system prompt (identity, user model, project knowledge, working memory, goals, coordination, shade contracts, tools, context files)
- Three-tier memory with structured user model (7 categories) and background consolidation
- Semantic recall: local vector search (bge-base-en-v1.5, 768d) + FTS5 hybrid with RRF fusion, version chains, temporal indexing. **78.8% on [LongMemEval_S](https://github.com/xiaowu0162/LongMemEval)** (vs. Supermemory 81.6%)
- Parallel shade swarms with per-task complexity, model routing, scope enforcement, token tracking
- Sequential shade contracts with phase lifecycle and branch-from-failure
- Autonomous goal-driven work with goal states, confirmation flow, self-assignment
- Intelligent fact-based task summarization
- Conversation search (FTS5)
- Git tool with agent metadata in commits
- HTTP tool
- Dynamic tool loader (agent builds own tools)
- Recurring task support with scheduling
- Task ledger and history
- Quick idea capture and goal management
- Boundary detection and intervention graph
- Agent mode tracking (interactive/autonomous/delegating/idle)
- Background worker (consolidation, goal inference, queue processing, batch monitoring)
- Multi-provider support with mid-session switching
- SQLite persistence with JSON fallback and auto-migration
- Conversation persistence and resume

### Planned

- Soft specialization (task classification, role-aware compaction)
- Per-agent provider config (pair programming with mixed providers)
- Contract templates (learned shade patterns)
- Overseer agent role (autonomous project management)
- Timed work sessions with git checkpoints and metrics
- Objective/milestone goal hierarchy
- Web search and browser tools
- Voice integration
- Remote agent linking
- Coin system (task weight/priority)

---

## Documentation

| Document | Description |
|----------|-------------|
| [Three-Tier Memory](docs/three-tier-memory.md) | Three-tier context + semantic recall |
| [Semantic Memory Engine](docs/plans/semantic-memory-engine.md) | Vector search, hybrid retrieval, LongMemEval benchmark |
| [User Model Schema](docs/plans/user-model-schema-design.md) | 7-category structured profile with consolidation |
| [System Prompt Design](docs/plans/agent-system-prompt-memory-design.md) | 10-layer prompt, compaction, specialization |
| [Autonomous Work](docs/plans/autonomous-goal-driven-work.md) | Goal states, self-assignment, verification |
| [Unified Daemon & Coins](docs/plans/unified-daemon-and-coins.md) | Agent modes, handover prompts, coin system |
| [Ideas & Timed Sessions](docs/plans/ideas-and-timed-sessions.md) | Idea capture, timed work, git checkpoints |
| [Overseer Agent](docs/plans/overseer-agent-design.md) | Autonomous project management role |
| [Memory & Goals](docs/plans/agent-memory-goals-usermodel-design.md) | Full memory/goal architecture |
| [V1 Design Charter](docs/plans/2026-03-16-charon-agents-shades-remote-v1.md) | Scope, phasing, design decisions |
| [Master Plan](docs/plans/MASTER_PLAN.md) | Build phases and architecture |

---

## Design Principles

1. **Users talk to agents, never to shades.** Shades are internal
   workers. The user interface is always a persistent agent.
2. **Memory is text.** Human-readable, human-editable, LLM-native.
   No opaque embeddings as source of truth.
3. **Degrade gracefully.** No dashboard → CLI works. No provider →
   orchestration works. No remote → local works. Shade failure →
   agent continues.
4. **Budget everything.** Shade contracts have scope, token, and time
   limits. Enforced at the tool call level.
5. **Append-only truth.** Events, interventions, and phase transitions
   are never deleted.
6. **Agents build what they need.** Dynamic tool loader lets agents
   create their own tools at runtime.
