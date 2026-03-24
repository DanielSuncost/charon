# Overseer Agent — Autonomous Project Management

> Design for Charon's overseer agent role: a specialized agent that
> monitors, coordinates, and manages other agents at the project level.
>
> Created: 2026-03-20
> Status: Design

---

## Concept

An overseer is a persistent Charon agent that specializes in project
management. Instead of writing code, it watches other agents work,
maintains a bird's-eye view of the project, and makes strategic
decisions about priorities, staffing, and direction.

You create one like any other agent:

```
/agent create --role overseer --project /my/project
```

Then you talk to it like any other agent — give it high-level
instructions, ask it questions about progress, tell it to change
priorities. It translates those into concrete actions across the
agent network.

## What the Overseer Does

### 1. Progress Monitoring (scheduled)

The overseer runs on a configurable schedule (default: every 30 minutes
when agents are active). On each cycle it:

- Reads every agent's recent inbox events and task completions
- Summarizes what each agent accomplished since last check
- Appends the summary to a structured project log

The log is a cleanly indexed file:

```
## 2026-03-20 14:30 — Progress Check

### charon-api-01 (implementer)
- Completed: OAuth2 token refresh endpoint
- In progress: Rate limiting middleware
- 3 tasks completed, 0 failed, 1 pending

### charon-frontend-01 (implementer)
- Completed: Login form component, unit tests
- Blocked: Waiting for API schema for user profile page
- 2 tasks completed, 0 failed, 1 blocked

### Summary
- API work on track. Frontend blocked on API schema — need to
  prioritize schema finalization.
- Rate limiting and login flow can proceed in parallel.
```

### 2. Bird's-Eye Project View (maintained continuously)

The overseer maintains a `PROJECT_STATUS.md` file that it keeps current:

```markdown
# Project Status: my-project

## Staffing
- 2 active agents (charon-api-01, charon-frontend-01)
- 0 idle agents
- Recommendation: Frontend is blocked — consider assigning a shade
  to finalize the API schema

## Work Division
- API layer: charon-api-01 (80% of recent tasks)
- Frontend: charon-frontend-01 (100% of recent tasks)
- Shared/infra: unassigned — no agent covering src/shared/
- Gap: Nobody is working on deployment pipeline

## Goals
- [x] Basic auth flow (completed Mar 19)
- [ ] OAuth2 integration (70% — token refresh done, scopes pending)
- [ ] User profile page (blocked — needs API schema)
- [ ] Rate limiting (30% — middleware started)

## Velocity
- 8 tasks/day average this week (up from 5 last week)
- Estimated completion for current milestone: Mar 25

## Risks
- Frontend-API coupling: frontend agent is frequently blocked
  waiting for API changes. Consider having them share a boundary
  protocol or having API agent prioritize schema-first development.
```

### 3. Goal Setting and Deadline Management

The overseer creates objectives and milestones in the goal hierarchy:

```
Objective: "Ship OAuth2 integration by Mar 25"
  ├── Milestone: "API endpoints complete" (deadline: Mar 22)
  │   └── assigned to: charon-api-01
  ├── Milestone: "Frontend auth flow complete" (deadline: Mar 24)
  │   └── assigned to: charon-frontend-01
  └── Milestone: "Integration testing" (deadline: Mar 25)
      └── assigned to: both agents
```

It tracks progress against deadlines and adjusts when reality diverges
from plan.

### 4. Feature Prioritization

The overseer can recommend:
- **Adding features** — "Based on the auth module's structure, adding
  2FA would be straightforward. Suggest adding it to the milestone."
- **Deprioritizing features** — "Rate limiting is lower priority than
  the user profile page. Recommend deferring to next sprint."
- **Reducing scope** — "The full OAuth2 flow with all providers is too
  ambitious for this timeline. Recommend shipping with GitHub only,
  adding others in a follow-up."

These are proposals — the user confirms or overrides.

### 5. Direct Agent Communication

The overseer uses Charon's inter-agent intervention system to
communicate with other agents:

```python
# Overseer sends a priority change to an agent
intervention_graph.append_intervention(
    conversation_id=agent.conversation_id,
    actor_agent_id=overseer.id,
    content="Priority change: pause rate limiting work. The frontend "
            "is blocked on the user profile API schema. Please "
            "finalize and publish the schema endpoint first.",
    intervention_of_message_id=agent.last_task_message_id,
)
```

The receiving agent sees this as a coordination message in its inbox
and adjusts its work accordingly.

## How It Uses Charon's Existing Systems

| Charon capability | How the overseer uses it |
|-------------------|------------------------|
| Three-tier memory | Reads all agents' working memory (via project knowledge tier). Writes project-level strategic notes. |
| Goal hierarchy | Creates objectives and milestones. Tracks progress. Adjusts deadlines. |
| Intervention graph | Sends priority changes and instructions to other agents. |
| Boundary detection | Monitors for scope conflicts between agents. Proactively resolves them. |
| Shade orchestration | Can spawn shades for analysis tasks (e.g., "audit the codebase for tech debt"). |
| Soft specialization | The overseer IS a specialization — it naturally evolves from a generalist that does mostly coordination work. |
| Conversation engine | You talk to it directly. It uses tools to read agent state, project files, and write status documents. |

## Schedule Mechanism

The overseer's monitoring cycle is a recurring task in the queue:

```python
{
    "id": "overseer-check-<timestamp>",
    "task_type": "overseer_cycle",
    "owner_agent_id": "AG-OVERSEER",
    "instruction": "Run scheduled progress check",
    "interval_minutes": 30,
    "next_run_at": "2026-03-20T15:00:00Z",
}
```

The daemon loop recognizes `overseer_cycle` tasks and re-enqueues them
after completion. The overseer can also be triggered on-demand by user
messages.

## What Makes This Different From a Planning Document

A planning document is static. The overseer is alive:

- It **updates itself** based on actual progress, not estimates
- It **notices problems** (blocked agents, scope gaps, velocity drops)
  before you ask
- It **acts on problems** by communicating with agents directly
- It **learns** from what works and what doesn't (via memory tiers)
- You can **talk to it** and give it strategic direction in natural
  language

It's a project manager that reads every commit message, every task
result, and every agent interaction — and distills it into actionable
intelligence.

## Implementation Notes

The overseer doesn't require new infrastructure. It's a regular Charon
agent with:
1. `role: overseer` (a soft specialization)
2. A recurring task in the queue (the schedule)
3. Read access to other agents' inbox and task history (via SQLite)
4. Write access to the intervention graph (already exists)
5. Write access to project knowledge (part of three-tier memory)
6. Custom system prompt section emphasizing monitoring, analysis, and
   strategic thinking over code production

The only new code needed:
- Scheduled task re-enqueueing in the daemon loop
- Overseer-specific system prompt section
- Agent inbox/history read functions (for cross-agent visibility)
- `PROJECT_STATUS.md` write convention
