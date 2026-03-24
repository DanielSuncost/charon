# Unified Agent Architecture & Coin System

> The daemon loop runs alongside the interactive chat as one seamless
> agent. The user can hand over control for autonomous work and take
> it back at any time.
>
> Created: 2026-03-21
> Status: Design
> Related: autonomous-goal-driven-work.md, agent-system-prompt-memory-design.md

---

## 1. Agent Modes

Every Charon agent is always in one of these modes:

| Mode | What's happening | Who's driving |
|------|-----------------|---------------|
| **interactive** | User is chatting, agent responds to messages | User |
| **autonomous** | Agent is working through goals independently | Daemon |
| **delegating** | Agent spawned shades, waiting for results | Daemon + Shades |
| **idle** | Nothing to do, waiting for user or scheduled work | Neither |

The mode shows in the status bar: `♡ interactive ↑0 ↓0 ctx:0%`
And in the dashboard as a property of each agent.

### Mode transitions

```
interactive ──"work on this for 2 hours"──→ autonomous
interactive ──"spawn shades for this"────→ delegating
autonomous  ──user sends a message────────→ interactive
autonomous  ──goals complete───────────────→ idle
delegating  ──all shades complete──────────→ interactive (reports results)
idle        ──user sends a message────────→ interactive
idle        ──scheduled task fires────────→ autonomous
```

---

## 2. Handover Prompts

When the user asks for autonomous work, the agent presents a
confirmation box:

```
╭─ Autonomous Work ─────────────────────────────╮
│                                                │
│  Goal: Improve test coverage across all        │
│        modules                                 │
│                                                │
│  Plan:                                         │
│    1. Audit current coverage (pytest --cov)    │
│    2. Write tests for uncovered paths          │
│    3. Verify all tests pass                    │
│                                                │
│  Done when:                                    │
│    ✓ Coverage above 80%                        │
│    ✓ No test failures                          │
│                                                │
│  Duration: 2 hours                             │
│  Git checkpoints: every completed step         │
│                                                │
│  [Confirm]  [Revise]  [Cancel]                 │
╰────────────────────────────────────────────────╯
```

When spawning shades:

```
╭─ Shade Swarm ─────────────────────────────────╮
│                                                │
│  Spawning 5 shades for: Generate API test      │
│  fixtures for all endpoints                    │
│                                                │
│  Tasks:                                        │
│    1. GET /users fixtures                      │
│    2. POST /users fixtures                     │
│    3. GET /products fixtures                   │
│    4. PUT /orders fixtures                     │
│    5. DELETE /sessions fixtures                │
│                                                │
│  Model: gpt-4o-mini (fast tier)                │
│  Max concurrent: 3                             │
│                                                │
│  [Spawn]  [Edit tasks]  [Cancel]               │
╰────────────────────────────────────────────────╯
```

These are TUI components — the backend emits structured events
that the frontend renders as interactive boxes.

---

## 3. Coin System (future)

> Note: Design concept, not built yet. Record for future implementation.

Every task has a **coin cost** that reflects its weight:

```
Coin costs (approximate):
  Simple question/answer: 1 coin
  Read + respond: 2 coins
  Edit a file: 3 coins
  Multi-file refactor: 10 coins
  Shade phase: 5 coins per phase
  Batch task: 2 coins per item
  Autonomous hour: 30 coins
```

Coins create implicit prioritization:
- High-priority tasks get spent first
- Budget-conscious mode limits total coin spend per session
- The overseer tracks coin velocity (coins/hour) as a productivity metric
- Dashboard shows coin spend per agent, per project, per day

Coins are NOT a billing mechanism — they're a weight/priority signal
that helps the agent and the user reason about task cost. "This
refactor will cost ~40 coins, is it worth it right now?"

The coin metaphor ties to the Charon mythology: you need a coin
(obol) to cross the river. Tasks need coins to get executed.

---

## 4. Unified Daemon Architecture

### Current (broken)

```
TUI ──→ chat_backend.py ──→ ConversationEngine (direct)
                              ↑ no daemon, no orchestration

Daemon (separate process, usually not running)
  ──→ charon_loop.py ──→ shade delegation, goals, recurring tasks
```

### Target (unified)

```
TUI ──→ chat_backend.py ──→ ConversationEngine (interactive)
              │
              └──→ Daemon thread (always running)
                    ├── Heartbeat (timing)
                    ├── Consolidation (user model updates)
                    ├── Goal inference (when autonomous)
                    ├── Shade monitoring (check swarm progress)
                    ├── Recurring tasks (overseer cycles, reviews)
                    ├── Autonomous execution (when mode=autonomous)
                    └── Queue processing (shade phase tasks)
```

The daemon thread is the background worker we already built
(`_start_background_worker`). It just needs to also process
queued tasks (shade phases, autonomous goal steps) instead of
only doing consolidation and goal inference.

### Key principle

The daemon does nothing during interactive mode except:
- Heartbeat (timing)
- Consolidation (if fresh signal exists)
- Monitoring shade progress (if shades are running)

It only takes over execution when:
- The user explicitly says "work on this autonomously"
- A shade needs to execute a phase task
- A recurring task fires (overseer, timed session review)

The user can always interrupt by sending a message, which
switches back to interactive mode.

---

## 5. Status Bar & Dashboard

### Status bar (line 2)

```
Interactive:    ♡ interactive  ↑1234 ↓5678  ctx:42%
Autonomous:     ♡ autonomous (45m remaining)  ↑1234 ↓5678  ctx:42%
Delegating:     ♡ delegating (3/5 shades done)  ↑1234 ↓5678  ctx:42%
Idle:           ♡ idle  ↑0 ↓0  ctx:0%
```

### Dashboard agent card

```
┌─ charon-api-01 ──────────────────────┐
│ Status: autonomous                    │
│ Goal: Improve test coverage           │
│ Progress: Step 2/3                    │
│ Time: 45m / 2h remaining             │
│ Shades: 0 active                     │
│ Last: wrote tests/test_auth.py       │
└───────────────────────────────────────┘
```

---

## 6. Implementation Plan

1. **Agent mode tracking** — add `mode` field to agent state
   (interactive/autonomous/delegating/idle). Updated by the
   backend based on what's happening.

2. **Daemon thread processes queue** — extend the background
   worker to pick up and execute shade phase tasks and
   autonomous goal steps, not just consolidation.

3. **Handover protocol** — backend emits `handover_prompt`
   event with goal/plan/criteria/duration. TUI renders the
   confirmation box. User confirms/revises/cancels.

4. **Shade spawn protocol** — backend emits `shade_spawn_prompt`
   with task list/model/concurrency. TUI renders the box.

5. **Mode display** — status bar shows current mode. Dashboard
   shows mode per agent.

6. **Interrupt** — any user message during autonomous mode
   switches back to interactive. Agent acknowledges and
   reports what it was doing.

7. **Coin system** — future. Add coin field to tasks, track
   spend per agent/project, display in dashboard.
