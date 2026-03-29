# Charon — Architecture Overview

> A single-user agent operating system for software development.

## System Diagram

```
┌──────────────────────────────────────────────────────────────────┐
│                      Charon TUI (OpenTUI/JS)                     │
│         Session Grid · Projects · Chat · Libris · Dashboard      │
└────────────────────────────┬─────────────────────────────────────┘
                             │  WebSocket / IPC
┌────────────────────────────▼─────────────────────────────────────┐
│                      core-daemon (Python)                        │
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
    │  run in-process or   │      │  (Pi, Claude Code, Codex,   │
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

### Memory Tiers
- **UserModel** — global, shared across all agents and all projects. Preferences, corrections, user facts.
- **ProjectKnowledge** — per-project. Architecture decisions, conventions, build commands.
- **memory_engine** — per-agent episodic memory (FTS5 full-text + embeddings for semantic recall).

---

## How External Agents Appear in the Grid

```
Pi / Claude Code / any agent
         │
         │  runs charons-boat (pairing code flow)
         │  OR is auto-detected via ~/.charon/boats/ registration
         ▼
  boat session record
  {id: "boat-pi", source: "boat", boatSessionId: "pi", ...}
         │
         ▼
  Charon TUI Session Grid
  (live session cell, interact via boat channel)
```

Remote agents follow the same model — just over an SSH tunnel. No tmux is required for agents that self-register via Charon's Boat.
