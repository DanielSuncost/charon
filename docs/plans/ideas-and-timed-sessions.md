# Quick Idea Capture & Timed Work Sessions

> Two features that extend Charon's goal system and daemon loop with
> rapid idea capture and time-bounded autonomous work.
>
> Created: 2026-03-20
> Status: Idea capture implemented. Timed sessions designed.

---

## 1. Quick Idea Capture (`/idea`)

### Problem

You're mid-conversation, a feature idea strikes, and you don't want to
derail your current task to describe it fully. You need a one-liner
capture that goes straight into the backlog.

### Solution

```
/idea Add rate limiting to the API
/idea Support multiple git remotes in charons-boat
/idea Voice wake word should work even when TUI is backgrounded
```

Each `/idea` creates a goal node with `status: 'backlog'` and
`intent_type: 'idea'`. It's stored immediately, never spawns a task,
and never becomes the active goal. It sits in the project's goal list
until promoted.

### Querying goals

```
/goals                    — show all goals (active + backlog + blocked)
/goals backlog            — show only backlog ideas
/goals active             — show current work
/goals prioritize         — ask the agent to rank the backlog
/goals promote <id>       — move an idea from backlog to active
```

### Implementation (done)

- `goal_runtime.ingest_idea()` — creates a backlog goal node
- `goal_runtime.list_goals()` — list goals with optional status filter
- `goal_runtime.promote_idea()` — move backlog → active
- 7 tests covering creation, accumulation, filtering, promotion

### How it connects to existing systems

- The **overseer agent** picks up backlog ideas during monitoring cycles
  and suggests which to promote based on current progress
- The **system prompt** goal context section shows backlog count so the
  agent knows ideas are waiting
- The **context packet** already includes active/blocked goals — backlog
  is a natural addition

---

## 2. Timed Work Sessions

### Problem

You want to tell Charon: "Spend the next 2 hours improving test coverage.
Use `pytest --cov` as the metric. Review your approach every 30 minutes.
Make git commits at every checkpoint."

Current Charon can execute a single task but has no concept of:
- Wall-clock time awareness
- Periodic self-review
- Metric-driven iteration
- Git checkpoint discipline
- Exploration branching ("build me 3 alternatives")

### Heartbeat (implemented)

The daemon loop now emits **heartbeat events** every N cycles (default:
30 cycles ≈ 60 seconds at 2s sleep):

```json
{
  "event": "heartbeat",
  "cycle": 150,
  "uptime_seconds": 302.4
}
```

The heartbeat interval is configurable via `CHARON_HEARTBEAT_INTERVAL`
env var. This gives the system wall-clock awareness without changing the
task execution model.

### Timed session design (planned)

A timed session is a new task type:

```python
{
    "task_type": "timed_session",
    "instruction": "Improve test coverage",
    "duration_minutes": 120,
    "review_interval_minutes": 30,
    "metrics": ["pytest --cov --cov-report=term-missing"],
    "checkpoint_strategy": "git_commit",
    "exploration_mode": null,   # or "alternatives" for branching
    "started_at": "...",
    "next_review_at": "...",
    "deadline_at": "...",
}
```

### Lifecycle

1. **Start**: run metric baseline, create git branch
   `charon/timed-<id>`, commit current state
2. **Work**: execute instruction normally via conversation engine
3. **Review** (heartbeat detects `now >= next_review_at`):
   - Run metrics again, compare to baseline
   - Generate self-review: "Coverage 67% → 74%. Focusing on API module.
     Auth module still low. Adjusting approach."
   - Git commit with review as message
   - Update `next_review_at`
4. **Deadline** (heartbeat detects `now >= deadline_at`):
   - Final metric run
   - Summary report with before/after comparison
   - Final git commit
   - Task completes

### Exploration mode

When the user says "build me 3 alternatives":

```
/timed 2h "Refactor the auth module" --alternatives 3 --metric "pytest tests/auth"
```

1. Agent creates git branches: `charon/alt-1`, `charon/alt-2`, `charon/alt-3`
2. Spawns a shade per branch (or works sequentially if single-model)
3. Each shade takes a different approach
4. At deadline: summary comparing all branches with metric results
5. User inspects: `git diff charon/alt-1..charon/alt-2`

This maps to existing shade orchestration — each alternative is a shade
contract with a single phase scoped to its git branch.

### What's needed to build this

| Component | Status | Notes |
|-----------|--------|-------|
| Heartbeat in daemon loop | ✅ Done | Emits every ~60s with cycle + uptime |
| Wall-clock tracking | ✅ Done | `loop_start_time` in run_loop() |
| `timed_session` task type | ⬜ Planned | New handler in process_task() |
| Review interval check | ⬜ Planned | Heartbeat triggers review subtask |
| Git tool (branch/commit/diff) | ⬜ Planned | New tool or bash wrapper |
| Metric baseline/compare | ⬜ Planned | Run command, parse, store |
| Exploration branching | ⬜ Planned | Wire shade contracts to git branches |
| `/timed` command in TUI | ⬜ Planned | Parse duration, metrics, options |

### Git checkpoint discipline

The timed session enforces:
- Branch creation at session start (never pollutes main)
- Commit at every review interval (ample checkpoints)
- Commit at task completion (nothing lost)
- Commit messages include metric snapshots and self-review

This means the user can always:
- `git log charon/timed-<id>` to see every step
- `git diff HEAD~3..HEAD` to see recent changes
- `git stash` and `git checkout main` to get back to clean state
- Review the agent's work at any granularity

### Relationship to existing features

| Feature | How timed sessions use it |
|---------|-------------------------|
| Shade orchestration | Exploration mode spawns shades per alternative |
| Goal hierarchy | Timed session creates a goal with deadline metadata |
| Working memory | Review summaries stored as memory notes |
| Heartbeat | Drives review interval and deadline detection |
| System prompt | Agent sees remaining time and last review summary |
| Overseer | Can create timed sessions as part of sprint planning |

---

## 3. Command Surface

### Idea capture

```
/idea <text>                          — capture an idea to backlog
/idea <text> --priority high          — capture with priority
/goals                                — list all goals
/goals backlog                        — list backlog ideas
/goals active                         — list active work
/goals promote <goal-id>              — promote idea to active
/goals prioritize                     — ask agent to rank backlog
```

### Timed sessions (planned)

```
/timed <duration> "<instruction>"     — start a timed session
/timed 2h "Improve coverage" --metric "pytest --cov"
/timed 1h "Refactor auth" --alternatives 3
/timed 30m "Fix flaky tests" --review 10m
/timed status                         — show active timed sessions
/timed stop                           — end current timed session early
```
