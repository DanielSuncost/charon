<p align="center">
  <img src="assets/mascot_sm.png" alt="Charon" width="480" />
</p>

<p align="center">
  <strong>A single-user agent operating system.</strong><br/>
  Persistent agents with real memory, parallel worker swarms,<br/>
  and everything running locally.
</p>

---

## Why Charon

Other agent frameworks give you one agent, one session, no memory.
You re-explain context every time. Charon is different:

- **Real memory.** Every conversation is indexed locally. Agents recall
  past discussions by meaning, know when facts have changed, and learn
  your preferences once across all agents and projects. Scores
  [78.8% on LongMemEval_S](https://github.com/xiaowu0162/LongMemEval)
  — approaching cloud-hosted state of the art, running entirely local.
- **Parallel work.** Complex tasks decompose into shade swarms —
  ephemeral workers with scope restrictions and budget limits, running
  in parallel while you do other things.
- **Your machine, your data.** SQLite + local embeddings. No cloud
  memory service, no API keys for recall, no data leaving your machine.

---

## Screenshots

<!-- TODO: Replace with actual screenshots of the three views -->

| Chat | Dashboard | Sessions |
|------|-----------|----------|
| *F1 — Chat view* | *F2 — Dashboard* | *F3 — Session grid* |

---

## Memory

Most agent frameworks forget everything between conversations. The ones
that remember use cloud services you don't control. Charon keeps
everything local and searchable.

**Your agent always has context.** Every conversation starts with what
the agent already knows about you — preferences, project conventions,
recent work, and relevant facts from past sessions. This happens
automatically. You never re-explain.

**Your agent can recall anything.** Every conversation is indexed into a
local vector database. When the agent needs something specific, it
searches by meaning — not just keywords:

```
You: "What did we decide about the auth module last week?"
Agent: 3 relevant memories (4ms):
       1. Decided to migrate auth to OAuth2 with PKCE flow
       2. Auth module has a race condition in token refresh
       3. User prefers passport.js over custom auth
```

**Facts stay current.** When a new memory is similar to an existing one
but the content has changed — your 5K time improved, you moved cities,
the project switched frameworks — the engine detects the overlap via
embedding similarity, marks the old version superseded, and links them
in a chain. The agent sees both but knows which is current.
([How version detection works →](docs/plans/semantic-memory-engine.md#3-version-chains-for-knowledge-updates))

Under the hood:
- **Hybrid retrieval** — vector similarity + keyword search, merged
  with [reciprocal rank fusion](https://plg.uwaterloo.ca/~gvcormac/cormacksigir09-rrf.pdf)
- **Local embeddings** — bge-base-en-v1.5 (768d), runs on CPU, ~5ms
  per recall
- **Version chains** — similarity ≥ 0.80 with different content
  triggers an update link. Old facts stay accessible but ranked lower
- **Human-readable exports** — structured profile and project knowledge
  are always inspectable as markdown alongside the database

[Architecture →](docs/plans/semantic-memory-engine.md) ·
[Three-tier design →](docs/three-tier-memory.md)

---

## Shade Swarms

Tell an agent to do 20 things in parallel and it spawns a swarm:

```
You: "Generate test fixtures for all 6 tool modules"
Agent: [spawns 6 shades, max 6 concurrent]
       All 6 running. Check progress with /batch
```

Each shade gets its own conversation, model (configurable per complexity
tier), and scope restrictions preventing it from touching files outside
its contract. Sequential contracts for multi-step work. Parallel batches
for independent tasks.

---

## Autonomous Work

```
/autonomous on
/confirm                    — approve a proposed goal
```

The agent proposes goals, plans steps, works through them with git
checkpoints, and verifies completion. Set time and token budgets.
Interrupt anytime by sending a message. Goals are inferred from
conversation — the agent suggests what it thinks you want done and
waits for confirmation.

---

## Multi-Provider

```bash
charon                        # default provider
charon claude-code            # Claude
charon codex                  # Codex
charon lmstudio               # local models
```

Switch mid-session with `/provider`. Configure separate providers for
shades — fast models for simple tasks, strong models for complex ones.
All provider communication uses raw httpx — zero SDK dependencies.

---

## Tools

14 built-in: Read, Write, Edit, Bash, Git, Http, Search, Recall,
UserModel, ProjectKnowledge, SpawnShade, SpawnBatch, Web, Browser.

Plus a dynamic loader: drop a `.py` file in `.charon/tools/` and
it's available after `/tools reload`. Or tell the agent to build
the tool itself.

---

## Quick Start

```bash
git clone <repo> && cd charon
uv pip install -r requirements.txt
cd apps/tui/opentui && bun install && cd ../../..
./charon
```

First run:
```
/setup provider lmstudio      # or claude-code, codex, api
/setup model <your-model>
/setup complete
```

**Key commands:**
```
/idea <text>             — capture an idea
/goals                   — view goals
/confirm                 — approve a proposed goal
/autonomous on|off       — toggle autonomous mode
/provider <name>         — switch provider
/batch                   — shade swarm progress
/history                 — task history
/tools                   — list all tools
```

---

## Architecture

```
charon/
├── apps/core-daemon/              # Python agent runtime
│   ├── conversation_engine.py     # Multi-turn LLM with tool use and steering
│   ├── memory_engine.py           # Semantic memory: vector + FTS5 hybrid search
│   ├── memory_indexer.py          # Background conversation indexing
│   ├── system_prompt_builder.py   # Layered context-aware prompt assembly
│   ├── agent_runtime.py           # Task execution with summarization
│   ├── shade_orchestrator.py      # Sequential shade contracts
│   ├── batch_orchestrator.py      # Parallel shade swarms
│   ├── autonomous.py              # Goal-driven self-assignment
│   ├── consolidation.py           # Background user model learning
│   ├── user_model_structured.py   # 7-category structured user profile
│   ├── providers/                 # Anthropic, OpenAI, local (httpx, zero SDK)
│   └── tools/                     # 14 built-in + dynamic plugin loader
├── apps/tui/                      # Terminal frontend
├── libs/store.py                  # SQLite persistence (WAL)
├── tools/charons-boat/            # External agent bridge
└── docs/                          # Design documents
```

---

## Status

**545 tests passing.**

Semantic recall scores **78.8% on
[LongMemEval_S](https://github.com/xiaowu0162/LongMemEval)** (state of
the art cloud-hosted: 81.6%). Retrieval accuracy: 98.5% at R@10.

### Done

- Semantic memory with hybrid vector + keyword search, version chains,
  temporal indexing, auto-injected context, and on-demand recall
- Structured user model (7 categories) with background consolidation
- Multi-turn conversation with streaming, tool use, and steering
- Parallel shade swarms with scope enforcement and token tracking
- Sequential shade contracts with phase lifecycle
- Autonomous goal-driven work with confirmation flow
- Multi-provider support with mid-session switching
- Dynamic tool loader (agents build their own tools)
- Agent coordination with boundary detection and intervention graph
- Git integration with agent metadata in commits
- SQLite persistence with conversation search and resume

### Planned

- Rust TUI
- Per-agent provider config
- Contract templates (learned shade patterns)
- Overseer agent role
- Voice integration
- Remote agent linking

---

## Design Principles

1. **Users talk to agents, never to shades.** The user interface is
   always a persistent agent. Shades are internal workers.
2. **Source of truth is text.** The user profile, project knowledge,
   and working memory are human-readable markdown, always inspectable
   and editable. Embeddings accelerate search but don't replace the
   text record.
3. **Degrade gracefully.** No vector deps → keyword search still works.
   No dashboard → CLI works. No provider → orchestration works.
   Shade failure → agent continues.
4. **Budget everything.** Shade contracts have scope, token, and time
   limits enforced at the tool call level.
5. **Append-only truth.** Events, interventions, and phase transitions
   are never deleted.
6. **Agents build what they need.** Dynamic tool loader lets agents
   create tools at runtime.

---

## Documentation

| Document | Description |
|----------|-------------|
| [Semantic Memory Engine](docs/plans/semantic-memory-engine.md) | Hybrid retrieval, LongMemEval benchmark |
| [Three-Tier Memory](docs/three-tier-memory.md) | Context hierarchy design |
| [User Model Schema](docs/plans/user-model-schema-design.md) | 7-category structured profile |
| [Autonomous Work](docs/plans/autonomous-goal-driven-work.md) | Goal states, self-assignment |
| [System Prompt Design](docs/plans/agent-system-prompt-memory-design.md) | Layered prompt assembly |
| [Master Plan](docs/plans/MASTER_PLAN.md) | Build phases and architecture |

---

## License

MIT
