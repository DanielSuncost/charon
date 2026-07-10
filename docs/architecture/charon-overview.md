# Charon — Architecture Overview

> A single-user agent operating system for software development.

## System Diagram

```
┌──────────────────────────────────────────────────────────────────┐
│                    Charon TUI  (Rust / crossterm)                │
│                                                                  │
│  ┌─────────────────────────────────────────────────────────┐     │
│  │  Session Grid  (F3)                                     │     │
│  │                                                         │     │
│  │  ┌──────────┐  ┌──────────┐  ┌──────────┐             │     │
│  │  │ agent-01 │  │ agent-02 │  │ tmux:dev │             │     │
│  │  │          │  │          │  │          │             │     │
│  │  │  (live   │  │  (live   │  │  (live   │             │     │
│  │  │   VTE    │  │   VTE    │  │   VTE    │             │     │
│  │  │ terminal)│  │ terminal)│  │ terminal)│             │     │
│  │  └──────────┘  └──────────┘  └──────────┘             │     │
│  │  Responsive grid · Enter: focus · type to interact     │     │
│  └─────────────────────────────────────────────────────────┘     │
│                                                                  │
│  Chat (F1) · Dashboard (F2) · Libris (F4) · Inter-agent (F5)    │
└────────────────────────────┬─────────────────────────────────────┘
                             │  WebSocket / IPC
┌────────────────────────────▼─────────────────────────────────────┐
│                      charon daemon (Python)                      │
│                                                                  │
│  charon_loop ──► agent_runtime ──► conversation_engine           │
│                                                                  │
│  ┌─────────────────────────────────────────────────────────┐     │
│  │  Memory                                                 │     │
│  │  memory_engine (FTS5 + embeddings) · Recall             │     │
│  │  UserModel (global) · ProjectKnowledge (per-project)    │     │
│  │  soft_specialization · task_ledger                      │     │
│  └─────────────────────────────────────────────────────────┘     │
│                                                                  │
│  ┌──────────────────────────┐  ┌──────────────────────────────┐  │
│  │  Libris (Research)       │  │  Judge Engine                │  │
│  │                          │  │                              │  │
│  │  Coordinator shade       │  │  implement → score →         │  │
│  │    │                     │  │  keep/rollback → converge    │  │
│  │    ├─► Researcher shades │  │                              │  │
│  │    │     └─► draft report│  │  Judge types:                │  │
│  │    └─► Judge shade       │  │  · quantitative (cmd+parse)  │  │
│  │          └─► critique    │  │  · correctness (test rate)   │  │
│  │          └─► checkpoint  │  │  · aesthetic (LLM rubric)    │  │
│  │    (loop until converged)│  │  · composite (weighted mix)  │  │
│  │                          │  │                              │  │
│  │  operation → topics →    │  │  Used by: Libris checkpoints │  │
│  │  sources → claims →      │  │  & SpawnJudgeLoop tool       │  │
│  │  report → checkpoint     │  │  (software dev automation)   │  │
│  └──────────────────────────┘  └──────────────────────────────┘  │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐    │
│  │  Shade Orchestrator                                      │    │
│  │  ephemeral workers · phase-driven · contract tracking   │    │
│  │  scope restrictions · budget limits                      │    │
│  │  (analysis → impl → verify → report)                    │    │
│  └──────────────────────────────────────────────────────────┘    │
│                                                                  │
└──────────────┬───────────────────────────────┬───────────────────┘
               │                               │
    ┌──────────▼──────────┐      ┌─────────────▼───────────────┐
    │  Local Agents        │      │     Charon's Boat            │
    │                      │      │                             │
    │  Persistent agents   │      │  Wraps any external agent   │
    │  run in-process or   │      │  (pi, Claude Code, Codex,   │
    │  as registered local │      │   opencode, etc.) and        │
    │  sessions. Sessions  │      │  surfaces it in the grid.   │
    │  appear in the grid  │      │                             │
    │  via ~/.charon/boats │      │  Pairing flow:              │
    │  registration files. │      │  code → identity keypair    │
    │                      │      │  → registered boat session  │
    │  Agents communicate  │      │  → appears in Session Grid  │
    │  via inbox events    │      │                             │
    │  (append_inbox_event)│      │  Remote: SSH tunnel         │
    └──────────────────────┘      │  Local: socket registration │
                                  └─────────────────────────────┘
```

## Key Concepts

### Rust TUI & Session Grid

The TUI is written in Rust (`crates/charon-tui`), using `crossterm` for raw terminal I/O. The Session Grid is a live multi-pane terminal multiplexer embedded directly in the TUI — not a wrapper around tmux.

Each pane is a **`SessionCell`**: a VTE terminal emulator (`vte` crate) fed by a **`ByteStream`** backend. Backend types:

| Backend | How it works | When used |
|---|---|---|
| **`LocalPty`** | `portable-pty` — spawns a child process with a real PTY | Charon-native sessions |
| **`TmuxPane`** | Polls `tmux capture-pane -e` (ANSI-preserving) at 100ms; writes input directly to the pane TTY device | Any existing tmux session |
| **`BoatPane`** | Unix socket or subprocess stream via the charons-boat protocol (base64-framed JSON) | External agents registered via Charon's Boat |
| **`CharonPane`** | Direct Unix socket to a native Charon session server (`NativeSessionServer`) | Charon-managed native sessions |

All backends implement the same `ByteStream` trait: `read_available` → `write_bytes` → `resize`. The VTE parser (`AnsiParser`) feeds bytes into `TerminalState`, which is rendered by the grid at up to 30 fps.

This means **you can type into any session directly from the Session Grid** — including tmux sessions and any agent wrapped by Charon's Boat — without leaving the TUI.

### Persistent Agents
Each agent has durable memory, a project assignment, an inbox, and a **soft specialization** label (auto-derived from recent working memory — e.g. "auth", "database", "shade & agent"). Agents communicate directly with each other via inbox events — no user in the middle.

### Shades
Ephemeral worker agents spawned for complex tasks. They run structured phases (analysis → implementation → verification → report), respect scope restrictions and budget limits, and report back. Users never interact with shades directly.

### Libris (Research System)
Multi-agent research orchestration:
1. **Coordinator** scouts the topic landscape, selects high-value topics
2. **Researcher shades** investigate each topic — gathering sources, saving claims, writing draft reports
3. **Judge shades** critique each draft — scoring relevance, evidence quality, and actionability — then save a **checkpoint**
4. The researcher/judge loop repeats until the report converges
5. Source procurement shades run in parallel for lead gathering

Supports `research`, `software`, and `hybrid` project kinds.

### Judge Engine
The judge is a first-class primitive embedded in both Libris and the general software automation system. Every judge loop follows the same contract:

```
implement → evaluate (score + feedback) → keep if improved, rollback if not → repeat → converge
```

Judge types:
- **Quantitative** — run a command, parse a number
- **Correctness** — test suite pass rate
- **Aesthetic** — LLM scores output against a rubric
- **Composite** — weighted mix of any of the above

Exposed via the `SpawnJudgeLoop` tool for software development automation (perf tuning, test coverage, code quality, prose editing).

### Charon's Boat
Universal session bridge — wraps any external agent and surfaces it in the Session Grid. Works locally via socket registration files (`~/.charon/boats/*.json`) and remotely via SSH tunnel + pairing code → identity keypair exchange.

The TUI's `BoatPane` backend connects directly to the boat's Unix socket and exchanges base64-framed JSON (`subscribe` / `input` / `resize` / `output`), giving full bidirectional byte-stream access to the wrapped agent's PTY.

### Memory Tiers
- **UserModel** — global, shared across all agents and all projects. Preferences, corrections, user facts.
- **ProjectKnowledge** — per-project. Architecture decisions, conventions, build commands.
- **memory_engine** — per-agent episodic memory (FTS5 full-text + embeddings for semantic recall).

---

## How External Agents Appear in the Grid

```
pi / Claude Code / any agent
         │
         │  runs charons-boat (pairing code flow)
         │  OR is auto-detected via ~/.charon/boats/ registration
         ▼
  boat session record  (~/.charon/boats/<id>.json)
  {id: "boat-pi", source: "boat", socket: "/path/to.sock", ...}
         │
         ▼
  Charon TUI Session Grid
  ┌──────────────┐
  │  pi          │  ← live VTE terminal, full bidirectional I/O
  │              │    via BoatPane ByteStream backend
  │ > ...        │
  └──────────────┘
  Enter to focus, type to interact — without leaving the TUI
```

Remote agents follow the same model — just over an SSH tunnel. No tmux required for agents that self-register via Charon's Boat.

---

## Codebase Layout

```
charon/
├── src/charon/                    # Python agent runtime (installable package)
│   ├── charon_loop.py             # Daemon entry point
│   ├── conversation/              # Multi-turn LLM engine with tool use and steering
│   ├── memory/                    # Semantic memory: vector + FTS5 hybrid search
│   ├── agents/                    # Task execution, lifecycle, autonomy
│   ├── shade/                     # Sequential shade contracts
│   ├── automation/                # Parallel shade swarms, schedulers, checkpoints
│   ├── judge/                     # Judge Loop: iterative optimization
│   ├── libris/                    # Research operations
│   ├── fleet/                     # Remote dispatch (Harbor), fleet sync
│   ├── providers/                 # Anthropic, OpenAI, local (httpx)
│   ├── infra/                     # SQLite store (WAL), registries, diagnostics
│   └── tools/                     # Built-in tools + dynamic plugin loader
├── crates/charon-tui/             # Rust TUI (crossterm + vte + portable-pty)
│   ├── src/main.rs                # Entry point, event loop, view routing
│   ├── src/grid.rs                # Responsive grid layout (N cells, aspect-aware)
│   ├── src/session.rs             # SessionCell: VTE state + ByteStream backend
│   ├── src/backend.rs             # ByteStream: LocalPty, TmuxPane, BoatPane, CharonPane
│   ├── src/terminal.rs            # TerminalState: screen buffer, scrollback
│   ├── src/parser.rs              # AnsiParser: VTE event → TerminalState
│   ├── src/native_session.rs      # NativeSessionServer: Unix socket session host
│   ├── src/app.rs                 # App state: views, sessions, chat, inter-agent
│   ├── src/render.rs              # Rendering primitives
│   ├── src/chat.rs                # Chat view state
│   └── src/backend.rs             # Backend discovery (boats dir + tmux)
├── tools/charons-boat/            # External agent bridge
└── docs/                          # Design documents
```
