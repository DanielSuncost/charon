<p align="center">
  <img src="assets/mascot_sm.png" alt="Charon" width="480" />
</p>

<p align="center">
  <strong>An agent operating system for your local machine — staffed by long-lived specialist agents.</strong>
</p>

---

Charon runs a *team* of specialists, not a chat window. You staff your
machine with persistent agents — a release engineer, a feature engineer,
a security engineer, an optimization engineer — each with a standing role
charter, its own memory, and a track record that survives new sessions,
restarts, and even provider switches: swap the model, keep the engineer.

Everything runs locally. Memory is SQLite + local embeddings. No cloud
services for recall or context. You own the data — and so do your
specialists: their identity and history live in files on your disk, not
in a provider's account.

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

### Specialists

Long-lived agents with roles you assign. A specialist carries a standing
charter injected into every task's system prompt, accumulates working
memory and episodic history under its own agent id, and records decisions
with rationale that you — or any other agent — can query later: *who
decided this, when, and why*.

```
/specialist create release-engineer
/specialist create security-engineer
/specialist assign AG-0007 "database reliability engineer"
```

Built-in templates: `release-engineer`, `feature-engineer`,
`security-engineer`, `optimization-engineer` — or assign any custom
specialization. Assigned roles are locked: the auto-labeler that tags
generalist agents by their recent work never overwrites a specialist
you named.

### Memory

Every conversation is indexed into a local vector database. Agents
recall past discussions by meaning and learn your preferences once
across all projects. Retrieval runs fully on-device: bge-base-en-v1.5
embeddings + sqlite-vec with an FTS5 keyword index, ~10ms per recall.
No cloud calls for recall.

What it actually does, measured on a LongMemEval_S subset: plain vector
search carries it. The FTS5 + reciprocal-rank-fusion "hybrid" adds
nothing on abstractive questions, and version-chain update-detection
gives no measurable retrieval gain. Single-session recall is
near-saturated; multi-session is the hard case (recall@1 ≈0.27).
Reproduce with the eval scripts under `scripts/` (`exp_memory_ablation.py`,
`exp_memeval.py`).

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

Define a quality signal and Charon iterates: snapshot → implement →
judge → keep-if-better / rollback → repeat → converge. Checkpoints use
a shadow git repo so your working tree stays clean, and rollback is
byte-exact (it also removes files a discarded iteration added).

A real, reproducible run (`scripts/judge_loop_example.py`) optimizing a
program's printed metric, where the keep/rollback machinery is the point:

```
tick  action     score  kept   best
1     baseline   10.0   -      10.0
2     iterated   68.0   True   68.0     # improvement, kept
3     iterated   38.0   False  68.0     # regression, rolled back via shadow git
4     iterated   308.0  True   308.0    # hit target -> converged (1 rollback)
```

This run uses the deterministic Quantitative judge (the score is the
program's output, no LLM), so it reproduces exactly. The LLM-implementer
path — where a model proposes each change — also runs end-to-end: it
reads a frozen checker, edits within its scope, and converges, with the
frozen-file and rollback gates holding. Demonstrated on small tasks, not
a benchmarked agent capability.

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

### Autonomous Research (Libris)

```
/libris research the role of reinforcement learning in the brain during skill vs language learning
```

Libris is a multi-agent research swarm: a coordinator scouts topics,
researchers investigate them against the live scholarly literature
(arXiv, Semantic Scholar, OpenAlex), and a judge critiques the drafts.
Every claim is graded for **confidence** and **evidence strength**, and
contradicting evidence is surfaced rather than smoothed over. Results
render to a self-contained, citation-linked HTML report.

**See real output:** three fully-cited demo reports on frontier science
questions (RL in the brain, gut microbiome & neurodegeneration, epigenetic
aging clocks) are in [demos/libris/](demos/libris/) — 27/27 citations
verified against CrossRef.

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

Active development. Full test suite run in CI on every push. Used daily
as a primary working environment.

What works:
- Memory recall and user-model / preference consolidation
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

## Documentation

| Document | Description |
|----------|-------------|
| [Install](docs/install.md) | Setup on macOS and Ubuntu |
| [Three-Tier Memory](docs/three-tier-memory.md) | User / project / agent context hierarchy |
| [Procedures & Judge Loops](docs/plans/procedure-learning-and-optimization-loops.md) | Iterative optimization with pluggable scoring |
| [Autonomous Work](docs/plans/autonomous-goal-driven-work.md) | Goal-driven self-assignment |
| [Remote Agent Teams](docs/remote-agent-teams.md) | Fleet configuration, team roles, Harbor dispatch |
| [Capability Roadmap](docs/plans/capability-roadmap.md) | Prioritized feature plan (P0–P3) |
| [Master Plan](docs/plans/MASTER_PLAN.md) | Architecture and build phases |

---

## License

MIT
