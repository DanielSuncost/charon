<p align="center">
  <img src="assets/mascot_sm.png" alt="Charon" width="480" />
</p>

<p align="center">
  <strong>An agent operating system for your local machine.</strong>
</p>

---

Charon is a terminal-native runtime for managing AI agents. It gives
agents persistent memory across sessions, coordinates parallel workers
with scope restrictions, and provides a single TUI where you can see
and interact with every agent session — local or remote — in one place.

Everything runs locally. Memory is SQLite + local embeddings. No cloud
services for recall or context. You own the data.

This is an active personal project. It works, it has tests, and I use
it daily. It is not aiming at production stability for other people's
workflows yet.

---

## Install

```bash
git clone https://github.com/DanielSuncost/charon.git
cd charon
./scripts/install.sh
charon
```

Handles macOS and Ubuntu. Installs Python deps into a project-local
venv, builds the Rust TUI, symlinks `charon` into `~/.local/bin`.

First run:
```
/setup provider lmstudio      # or claude-code, codex, api
/setup model <your-model>
```

More detail: [docs/install.md](docs/install.md)

---

## What it does

### Memory

Every conversation is indexed into a local vector database. Agents
recall past discussions by meaning, know when facts have changed, and
learn your preferences once across all projects.

Hybrid retrieval (vector similarity + FTS5 keywords, merged with
reciprocal rank fusion). Local embeddings (bge-base-en-v1.5, ~10ms per
recall, query-embedding dominated). Version chains detect when knowledge
has been superseded.

Scored 78.8% on
[LongMemEval_S](https://github.com/xiaowu0162/LongMemEval) with a GPT-4o
reader, against the benchmark's oracle-retrieval ceiling of ~82.4% for the
same reader (measured 2026-03). The notable part is the retrieval stack —
bge-base embeddings, hybrid vector + FTS5, RRF — running **fully on-device
with no network calls**, where most published results at or above this use
substantially heavier machinery. (The GPT-4o reader is a cloud call;
"on-device" refers to retrieval and embeddings.) Reader scores are sensitive
to the model and judge prompt, so this is our own harness under stated
conditions, not a leaderboard rank. Retrieval quality (recall@k) is
reproducible with no API — `scripts/bench_longmemeval.py --retrieval-only`,
sample in `results/longmemeval/`
([details](docs/plans/semantic-memory-engine.md#longmemeval_s-benchmark)).

[Architecture](docs/plans/semantic-memory-engine.md) /
[Three-tier design](docs/three-tier-memory.md)

### Shades

Ephemeral worker agents with their own conversation, model, and scope
restrictions. An agent can spawn shades to do work in parallel —
each one is prevented from touching files outside its contract.

```
You: "Generate test fixtures for all 6 tool modules"
Agent: [spawns 6 shades, max 6 concurrent]
```

Sequential contracts for multi-step work. Parallel batches for
independent tasks. Budget limits on tokens, time, and iterations.

### Judge Loops

Define a quality signal and Charon iterates until it converges.
The signal can be a benchmark, a test suite, an LLM rubric, or a
composite of several.

```
You: "Optimize my RL trainer. Metric is mean episode reward.
      Only touch train.py and model.py. 100 iterations max."

Charon: [baseline]  142.7
        [iter  1]   148.3  cosine LR schedule             KEPT
        [iter  3]   162.8  GAE lambda 0.95 -> 0.98        KEPT
        [iter 19]   194.2  wider net + above               KEPT
        Converged — best: 194.2 (+36.1%)
```

Each iteration: snapshot, implement, judge, keep or rollback, feed
critique to the next round. Checkpoints use shadow git so your repo
stays clean.

| Judge type | Signal | Example |
|------------|--------|---------|
| Performance | Benchmark numbers | "Get p99 under 50ms" |
| Correctness | Test pass rate | "Fix these 12 failing tests" |
| Aesthetic | LLM scores against a rubric | "Rewrite for clarity" |
| Composite | Weighted mix | "Fast, correct, and readable" |

### Session Grid

A terminal multiplexer built into the TUI. Each cell is a real VTE
terminal emulator. You see rendered output and type into any session
without leaving Charon.

Sessions can be native Charon agents, existing tmux sessions, or
external agents (Claude Code, Hermes, pi, Codex) wrapped via
Charon's Boat and connected over Unix sockets.

```bash
charons-boat wrap --name review -- pi   # appears in grid
```

### Conversation Rooms

Multi-agent conversation rooms where two or more agents discuss a
topic with structured turn-taking. Charon manages orchestration and
keeps turn state visible.

```
/conversation hermes strategist critic <topic>
```

Archetypes: peer, teacher/student, debate, strategist/critic,
architect/reviewer, pair-programmers.

### Remote Coordination (Harbor)

Dispatch structured tasks to agents on remote machines. The local
Charon (the "Harbor") builds a context packet from memory and project
knowledge, sends it over SSH, and the remote worker executes it.
Workers can query Harbor's memory mid-task. Results and new memories
flow back and get indexed locally.

```
/voyage dispatch gpu-box agent-01 "run the full benchmark suite"
```

### Autonomous Work

```
/autonomous on
```

The agent proposes goals inferred from conversation, plans steps, works
through them with git checkpoints, and verifies completion. Set time
and token budgets. Interrupt anytime.

### Multi-Provider

```bash
charon claude-code      # Anthropic
charon codex            # OpenAI
charon lmstudio         # local models
```

Switch mid-session with `/provider`. Separate provider config for
shades. All provider communication uses raw httpx — no SDK
dependencies.

### Tools

Built-in: Read, Write, Edit, Bash, Git, Http, Search, Recall,
UserModel, ProjectKnowledge, SpawnShade, SpawnBatch, SpawnJudgeLoop,
Web, Browser, and more.

Dynamic loader: drop a `.py` file in `.charon/tools/` and it's
available after `/tools reload`.

---

## Architecture

```
charon/
├── apps/core-daemon/              # Python agent runtime
│   ├── conversation_engine.py     # Multi-turn LLM with tool use and steering
│   ├── memory_engine.py           # Hybrid vector + FTS5 search
│   ├── judge_engine.py            # Iterative optimization with scoring
│   ├── shade_orchestrator.py      # Sequential shade contracts
│   ├── batch_orchestrator.py      # Parallel shade swarms
│   ├── harbor.py                  # Remote task dispatch (Harbor protocol)
│   ├── autonomous.py              # Goal-driven self-assignment
│   ├── providers/                 # Anthropic, OpenAI, local (httpx)
│   └── tools/                     # Built-in + dynamic plugin loader
├── crates/charon-tui/             # Rust TUI (crossterm + vte + portable-pty)
│   ├── src/main.rs                # Event loop, views, rendering
│   ├── src/backend.rs             # LocalPty, TmuxPane, BoatPane, CharonPane
│   ├── src/terminal.rs            # Screen buffer + scrollback
│   └── src/clipboard.rs           # Cross-platform clipboard (pbcopy, OSC52)
├── tools/charons-boat/            # External agent bridge + Harbor worker
├── libs/store.py                  # SQLite persistence (WAL)
└── docs/                          # Design documents
```

---

## Status

Active development. 748 tests, full suite run in CI on every push. Used
daily as a primary working environment.

What works:
- Memory, recall, version chains, user model consolidation
- Shade swarms with scope enforcement
- Judge loops with checkpoint/rollback
- Multi-provider (Claude, Codex, local models)
- Session grid with live VTE terminals
- Conversation rooms
- Harbor protocol for remote dispatch
- Browser automation (Playwright)
- Dynamic tool loader

What's planned:
- MCP support
- Procedural memory (learned multi-step approaches)
- Per-agent provider config
- Transparent checkpoints before file mutations
- Voice integration

See [capability roadmap](docs/plans/capability-roadmap.md) for the
full list.

---

## Design Principles

1. **Users talk to agents, never to shades.** Shades are internal
   workers managed by the agent.
2. **Source of truth is text.** User profile, project knowledge, and
   working memory are human-readable markdown, always inspectable.
3. **Degrade gracefully.** No vector deps — keyword search still works.
   No provider — orchestration still works. Shade failure — agent
   continues.
4. **Budget everything.** Scope, token, and time limits enforced at the
   tool-call level.
5. **Append-only truth.** Events, interventions, and phase transitions
   are never deleted.

---

## Documentation

| Document | Description |
|----------|-------------|
| [Install](docs/install.md) | Setup on macOS and Ubuntu |
| [Semantic Memory Engine](docs/plans/semantic-memory-engine.md) | Hybrid retrieval, LongMemEval benchmarks |
| [Three-Tier Memory](docs/three-tier-memory.md) | User / project / agent context hierarchy |
| [Procedures & Judge Loops](docs/plans/procedure-learning-and-optimization-loops.md) | Iterative optimization with pluggable scoring |
| [Autonomous Work](docs/plans/autonomous-goal-driven-work.md) | Goal-driven self-assignment |
| [Capability Roadmap](docs/plans/capability-roadmap.md) | Prioritized feature plan (P0–P3) |
| [Master Plan](docs/plans/MASTER_PLAN.md) | Architecture and build phases |

---

## License

MIT
