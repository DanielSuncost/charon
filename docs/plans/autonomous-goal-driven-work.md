# Autonomous Goal-Driven Agent Work

> Design for making Charon agents work independently on goals without
> constant user input. Integrates with existing designs for timed
> sessions, overseer, shade orchestration, and the goal hierarchy.
>
> Created: 2026-03-21
> Status: Design
> Depends on: ideas-and-timed-sessions.md, overseer-agent-design.md,
>   agent-system-prompt-memory-design.md

---

## 1. The Gap

Currently, Charon agents only work when a task is in the queue. When
the queue is empty, the daemon loop sleeps. A goal-driven agent should:

1. Recognize its goals
2. Confirm understanding with the user
3. Plan how to achieve them
4. Define what "done" looks like
5. Execute independently, making checkpoints
6. Verify completion against acceptance criteria
7. Report results and ask for next direction

---

## 2. Goal Lifecycle (new states)

Current goal states: `active`, `backlog`, `blocked`, `completed`, `failed`

New states needed:

```
backlog → proposed → confirmed → planning → executing → verifying → completed
                                                                  → failed
                                                                  → blocked
```

| State | Meaning |
|-------|---------|
| `backlog` | Idea captured, not yet prioritized |
| `proposed` | Agent proposes to work on this, awaiting user confirmation |
| `confirmed` | User confirmed, agent can plan and execute |
| `planning` | Agent is decomposing the goal into sub-tasks |
| `executing` | Agent or shades are working on sub-tasks |
| `verifying` | Agent is checking acceptance criteria |
| `completed` | Acceptance criteria met, user notified |

### Confirmation flow

```
Agent: "I'd like to work on: Add rate limiting to the API.
        My plan: 1) Research existing middleware, 2) Implement rate limiter,
        3) Add tests, 4) Update docs.
        Done when: Rate limit middleware deployed, tests passing,
        /api/health returns 429 after 100 req/min.
        
        Proceed? [confirm/revise/defer]"

User: "confirm" or "looks good, but also add per-user limits"
```

The agent creates the goal with `status: proposed` and the plan +
acceptance criteria attached. The user's confirmation moves it to
`confirmed`. If the user revises, the agent updates and re-proposes.

### Acceptance criteria

The existing `acceptance_criteria` field on goals gets populated:

```json
{
  "goal_id": "goal-abc123",
  "title": "Add rate limiting to the API",
  "status": "confirmed",
  "acceptance_criteria": [
    "Rate limit middleware exists in src/api/middleware/",
    "Tests in tests/api/test_rate_limit.py pass",
    "GET /api/health returns 429 after 100 requests in 1 minute",
    "Per-user rate limits configurable via env var"
  ],
  "plan": [
    {"step": 1, "description": "Research existing middleware options", "status": "pending"},
    {"step": 2, "description": "Implement rate limiter with per-user tracking", "status": "pending"},
    {"step": 3, "description": "Write unit and integration tests", "status": "pending"},
    {"step": 4, "description": "Update API docs", "status": "pending"}
  ]
}
```

---

## 3. Self-Assignment from Goals

When the daemon loop finds no pending tasks, instead of sleeping it
checks for confirmed goals that need work:

```python
def self_assign_from_goals(state_dir, agent):
    """Find the next thing to work on from confirmed goals."""
    
    # 1. Check for goals in 'confirmed' state (need planning)
    # 2. Check for goals in 'executing' state (have pending plan steps)
    # 3. Check for goals in 'verifying' state (need acceptance check)
    # 4. If nothing, check backlog for ideas to propose
    # 5. If truly nothing, return None (sleep)
```

This is the core change that makes the agent autonomous. The idle loop
becomes a goal-scanning loop.

### Priority order for self-assignment

1. **Verifying goals** — acceptance criteria need checking (quick)
2. **Executing goals** — next plan step needs a task (continue work)
3. **Confirmed goals** — need initial planning (start new work)
4. **Backlog ideas** — propose to user if nothing else to do

---

## 4. Planning Step

When a confirmed goal has no plan yet, the agent creates one using the
LLM. This is a special task type:

```python
{
    "task_type": "goal_planning",
    "goal_id": "goal-abc123",
    "instruction": "Create an execution plan for: Add rate limiting..."
}
```

The agent outputs a structured plan (steps with descriptions). The plan
is stored on the goal and each step becomes a potential task.

For complex goals, planning naturally produces shade contracts — each
plan step maps to a shade phase.

---

## 5. Verification Step

When all plan steps are complete, the agent runs acceptance criteria
checks. This is another special task type:

```python
{
    "task_type": "goal_verification",
    "goal_id": "goal-abc123",
    "acceptance_criteria": ["tests pass", "429 after 100 req"],
    "instruction": "Verify these acceptance criteria are met..."
}
```

The agent runs commands, reads files, and potentially uses browser tools
to check each criterion. Results are recorded as evidence on the goal.

If any criterion fails, the goal moves back to `executing` with a new
plan step to address the failure.

---

## 6. Extended Tools

### 6.1 Browser tool (Playwright)

```python
BROWSER_TOOL_DEF = {
    'name': 'Browser',
    'description': 'Navigate web pages, interact with elements, take screenshots.',
    'input_schema': {
        'properties': {
            'action': {'enum': ['navigate', 'click', 'type', 'screenshot', 'get_text', 'wait']},
            'url': {'type': 'string'},
            'selector': {'type': 'string'},
            'text': {'type': 'string'},
        }
    }
}
```

Requires: `pip install playwright && playwright install chromium`

Use cases:
- Verify web app works after changes
- Check API responses via browser
- Screenshot UI for review
- Run E2E tests

### 6.2 Git tool (structured)

```python
GIT_TOOL_DEF = {
    'name': 'Git',
    'description': 'Structured git operations with automatic checkpointing.',
    'input_schema': {
        'properties': {
            'action': {'enum': ['status', 'diff', 'commit', 'branch', 'checkout', 'log']},
            'message': {'type': 'string'},
            'branch': {'type': 'string'},
            'files': {'type': 'array', 'items': {'type': 'string'}},
        }
    }
}
```

Why not just Bash: The Git tool automatically creates checkpoint commits
at plan step boundaries, tags them with goal/step metadata, and prevents
common mistakes (committing to main, force pushing).

### 6.3 HTTP tool

```python
HTTP_TOOL_DEF = {
    'name': 'Http',
    'description': 'Make HTTP requests. Test APIs, check endpoints, fetch data.',
    'input_schema': {
        'properties': {
            'method': {'enum': ['GET', 'POST', 'PUT', 'DELETE', 'PATCH']},
            'url': {'type': 'string'},
            'headers': {'type': 'object'},
            'body': {'type': 'string'},
        }
    }
}
```

---

## 7. Schedule System

Merges with the existing timed sessions and overseer designs:

### Recurring tasks

```python
{
    "task_type": "recurring",
    "interval_minutes": 30,
    "instruction": "Check goal progress and plan next steps",
    "next_run_at": "2026-03-21T15:00:00Z",
}
```

The daemon loop recognizes recurring tasks and re-enqueues them after
completion. Used by:
- **Overseer** monitoring cycles (every 30 min)
- **Consolidation** checks (every 100 min, already implemented)
- **Timed session** review intervals (configurable)
- **Autonomous agents** self-check cycles (every N minutes)

### Self-check cycle

An autonomous agent periodically reviews its progress:

1. Am I still on track for my current goal?
2. Have any dependencies changed?
3. Should I adjust my approach?
4. Are there new user messages I should respond to?

This is a lightweight LLM call (fast model) that either continues
executing or adjusts the plan.

---

## 8. What This Looks Like End-to-End

```
User: "Add rate limiting to the API"

Agent: [creates goal with status='proposed']
       "I'll add rate limiting with these steps:
        1. Research middleware options
        2. Implement per-user rate limiter
        3. Write tests (unit + integration)
        4. Update API docs
        Done when: tests pass, /api/health returns 429 after 100 req/min.
        Proceed?"

User: "yes, also add Redis backend for distributed rate limiting"

Agent: [updates plan, moves to status='confirmed']
       [creates shade contract with 4 phases]
       [Shade 1: researches middleware, reports findings]
       [Shade 2: implements rate limiter with Redis]
       [Shade 3: writes tests]
       [Shade 4: updates docs]
       [All shades complete]
       [Agent runs verification: checks tests, hits /api/health 100 times]
       "Done. Rate limiting implemented with Redis backend.
        - src/api/middleware/rate_limit.py (new)
        - tests/api/test_rate_limit.py (new, 8 tests passing)
        - /api/health returns 429 after 100 req/min ✓
        - Redis connection configurable via REDIS_URL env var"

Agent: [idle, checks backlog]
       "Your backlog has 3 ideas. Based on the API work we just did,
        I'd suggest 'Add request logging middleware' next since the
        middleware infrastructure is fresh. Want me to plan it?"
```

---

## 9. Implementation Priority

Ordered by what unblocks the most value:

| # | Task | Depends on | Effort |
|---|------|-----------|--------|
| 1 | **Goal states + confirmation flow** | goal_runtime.py | Small — add states + propose/confirm functions |
| 2 | **Acceptance criteria on goals** | goal states | Small — populate the existing field |
| 3 | **Self-assignment in idle loop** | goal states | Medium — scan goals, create tasks |
| 4 | **Planning task type** | self-assignment | Medium — LLM decomposes goal into steps |
| 5 | **Verification task type** | acceptance criteria | Medium — run checks, evaluate results |
| 6 | **Recurring task support** | daemon loop | Small — re-enqueue after completion |
| 7 | **Git tool** | tools system | Medium — structured operations + checkpointing |
| 8 | **HTTP tool** | tools system | Small — wrapper around httpx |
| 9 | **Browser tool** | playwright | Medium — requires playwright dep |
| 10 | **Timed sessions** | recurring tasks, git tool | Already designed, needs implementation |
| 11 | **Overseer recurring cycle** | recurring tasks | Already designed, needs wiring |

Items 1-3 make the agent autonomous. Items 4-5 make it goal-directed.
Items 6-9 give it more capabilities. Items 10-11 are existing designs
that plug in once the foundation is there.

---

## 10. What Already Exists (no duplication)

| Existing | Where | Reused here |
|----------|-------|-------------|
| Goal hierarchy | `goal_runtime.py` | Extended with new states + plan/criteria fields |
| Shade orchestration | `shade_orchestrator.py` | Planning step outputs shade contracts |
| Heartbeat | `charon_loop.py` | Drives recurring task scheduling |
| Consolidation | `consolidation.py` | Template for recurring background work |
| Timed sessions design | `ideas-and-timed-sessions.md` | Implemented via recurring tasks + git tool |
| Overseer design | `overseer-agent-design.md` | Implemented via recurring tasks + goal scanning |
| Idea capture | `goal_runtime.ingest_idea()` | Backlog feeds self-assignment |
| Task summarizer | `task_summarizer.py` | Summaries used in self-check reviews |
| System prompt with goals | `system_prompt_builder.py` | Agent sees its goals + plan in prompt |
