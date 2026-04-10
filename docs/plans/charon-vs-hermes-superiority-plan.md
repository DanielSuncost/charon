# Charon Superiority Plan

> Objective: make Charon superior to Hermes on both individual agent capability and the multi-agent operating environment.
>
> This is not a feature-copying exercise. The goal is to close important capability gaps while deepening the parts of Charon that define its category.

Updated: 2026-04-05

---

## 1. Objective

Charon should win on **both** axes:

1. **Single-agent capability**
   - memory and recall
   - browser/web operation
   - search and retrieval
   - checkpoints / undo
   - MCP and external tool interoperability
   - reliability, safety, and long-run coherence

2. **Multi-agent operating environment**
   - persistent named agents
   - shared user/project memory
   - direct inter-agent coordination
   - bounded delegation via shades
   - terminal-native multi-session oversight
   - agent-agnostic orchestration and bridging

The target is not merely “different from Hermes.” The target is:
- comparable or better **individual agent quality**
- clearly better **agent operations environment**
- a stronger long-term platform for software development work

---

## 2. Planning principles

### 2.1 Preserve Charon’s core identity

Do not weaken or deprioritize the foundations that already differentiate Charon:

- persistent named agents
- three-tier memory (`UserModel`, `ProjectKnowledge`, per-agent working memory)
- project-scoped shared knowledge
- session grid / live multi-session TUI
- agent-to-agent coordination
- shade orchestration
- Libris research system
- judge loops
- Charon’s Boat / external agent bridging

### 2.2 Close real gaps before adding more autonomy

Before increasing autonomy, close foundational gaps in:

- browser/web capability
- session recall quality
- rollback/checkpointing
- MCP support
- safety / approval flow
- model fallback and runtime reliability

### 2.3 Prefer strategic superiority over feature mimicry

When Hermes has a capability, Charon should either:

- reach parity if the feature is table-stakes, or
- surpass it in a way that reinforces Charon’s operating-system model

We should avoid building low-leverage clones of features that do not improve Charon’s core value proposition.

### 2.4 Success means observable workflow superiority

Each workstream needs an observable success condition, not just an implementation artifact.

Examples:
- not “add browser tool,” but “reliably complete real login/form/research tasks”
- not “add search summaries,” but “agents can recover useful prior work with minimal prompt waste”
- not “add checkpoints,” but “users can undo agent changes safely and instantly”

---

## 3. Non-goals

These are explicitly **not** immediate goals of this plan:

- chasing every messaging platform integration for its own sake
- copying Hermes feature-for-feature without a Charon-specific reason
- sacrificing TUI / multi-agent work to maximize generic chatbot polish
- adding autonomy faster than we improve rollback, approval, and observability
- coupling Charon’s identity to Hermes comparisons in user-facing messaging

---

## 4. Workstreams

## Workstream A — Capability parity foundations

These are the highest-priority gaps where Hermes is strong and Charon must reach parity or better.

### A1. Memory and recall superiority

#### Goal
Make Charon’s memory system stronger than Hermes in retrieval quality, trust, and cross-agent usefulness.

#### Current basis
Charon already has:
- three-tier memory
- shared user model
- shared project knowledge
- per-agent working memory
- search / recall primitives

#### Needed deliverables
- hybrid recall combining:
  - keyword / FTS retrieval
  - semantic retrieval
  - LLM summarization of retrieved episodes
- recall results with:
  - provenance
  - confidence hints
  - recency / freshness indicators
- memory quality controls:
  - deduplication
  - contradiction detection
  - stale-entry review
  - merge / canonicalization of repeated facts
- review UX for:
  - inspect / accept / reject / prune memory entries
- cross-agent consolidation:
  - convert repeated discoveries into canonical project facts
- “what should I know before I start?” memory packets for new agents

#### Success criteria
- agents retrieve prior work as concise summaries, not raw snippets
- users can inspect where facts came from
- new agents are immediately useful because project memory is actionable
- cross-agent memory is more useful and trustworthy than Hermes’ agent-local memory stack

---

### A2. Browser and web operations

#### Goal
Match or exceed Hermes on browser-based research and web-app operation.

#### Needed deliverables
- persistent browser sessions across steps
- DOM / accessibility-tree browsing
- screenshots + vision analysis
- robust interaction support:
  - click
  - type
  - scroll
  - upload
  - download
  - login flows
  - multi-step form workflows
- local and remote/cloud browser backends
- website policy and safety controls
- browser state captured in task summaries / compaction
- extraction + summarization UX that is pleasant for agents to use

#### Success criteria
- Charon agents can reliably use modern web apps
- web research is practical and repeatable
- browser-assisted debugging is dependable
- browser workflows are at least as usable and robust as Hermes

---

### A3. Session search and recall UX

#### Goal
Make session history genuinely useful as a software-development case history.

#### Needed deliverables
- FTS + semantic hybrid session retrieval
- per-result LLM summaries
- clustering by task / theme / subsystem
- filters by:
  - project
  - agent
  - time
  - files touched
  - outcome
- episode cards instead of raw excerpts
- “what happened last time we dealt with X?” workflows
- cross-session comparison / diff tools

#### Success criteria
- agents and users can recover prior solutions quickly
- search returns distilled, actionable memory rather than transcript fragments
- Charon session recall clearly exceeds raw snippet search systems

---

### A4. Transparent checkpoints and undo

#### Goal
Make agent file modification safer and more reversible than Hermes.

#### Needed deliverables
- automatic checkpoints before mutations
- checkpoint metadata linked to:
  - agent
  - task
  - goal
  - timestamp
- easy restore / rollback UX
- checkpoint diff inspection
- “undo the last agent action” support
- checkpoint integration with:
  - shades
  - judge loops
  - failed automation runs

#### Success criteria
- users trust agents to edit code because rollback is easy
- reverting an agent’s change is a first-class workflow
- checkpoints are transparent, reliable, and low-friction

---

### A5. MCP support

#### Goal
Treat MCP as a first-class interoperability layer.

#### Needed deliverables
- MCP client support in the runtime
- dynamic tool discovery
- namespace isolation and collision handling
- per-agent and per-project MCP enablement
- auth and configuration UX
- policy / approval controls for remote MCP tools
- prompt integration that helps agents use MCP effectively

#### Success criteria
- Charon can use important MCP servers out of the box
- users can safely and clearly manage MCP-enabled capabilities
- MCP support reaches or exceeds Hermes in practicality

---

### A6. Reliability, approval, and safety

#### Goal
Make Charon more dependable and safer than Hermes for daily autonomous work.

#### Needed deliverables
- provider fallback
- retry / backoff strategy
- agent-specific provider/model routing
- optional approval flow for destructive actions
- risk-aware command classification
- secret access controls
- network policy controls
- audit trails for important actions
- persistent-agent safety options, not only shade scope safety

#### Success criteria
- fewer dead-end failures from model/provider issues
- destructive operations are easier to control
- users trust Charon with longer-running and more autonomous tasks

---

## Workstream B — Deepen Charon-native strengths

These are areas where Charon already has a strong identity and should decisively surpass Hermes.

### B1. Stronger shades than Hermes subagents

#### Goal
Make shades clearly more reliable and more structured than generic subagent delegation.

#### Needed deliverables
- stronger contract templates
- explicit acceptance criteria
- artifact requirements by contract type
- phase-aware execution and checkpoints
- partial failure handling
- phase resume / retry
- better parent-child memory handoff
- stronger observability:
  - phase status
  - cost
  - token usage
  - model routing
  - outputs
- learned contract templates from repeated successful patterns

#### Success criteria
- shades feel like bounded workers with guarantees, not just extra agent threads
- delegated work becomes more inspectable, repeatable, and reliable than Hermes subagents

---

### B2. Real inter-agent coordination

#### Goal
Turn Charon’s multi-agent system into a true collaborative organization.

#### Needed deliverables
- shared task board across agents
- ownership / lease system for files or subsystems
- conflict detection and warnings
- request / handoff / negotiate protocols
- dependency tracking between agents
- manager / specialist / reviewer coordination patterns
- inspectable inbox / coordination UI
- shared situational awareness packet in prompts

#### Success criteria
- multiple agents can work in parallel without stepping on each other
- handoffs are explicit and recoverable
- coordination is a first-class strength rather than an implied property

---

### B3. Operational project knowledge

#### Goal
Make project memory actively shape behavior rather than merely store notes.

#### Needed deliverables
- project conventions surfaced before edits
- build/test/run recipes available as structured packets
- architecture and subsystem maps
- known gotchas surfaced before risky operations
- onboarding packet for new agents
- file / subsystem ownership hints
- project knowledge health checks and pruning

#### Success criteria
- new agents become useful immediately
- repeated project-specific mistakes decrease
- project knowledge materially improves execution quality

---

### B4. Best-in-class agent operations TUI

#### Goal
Make Charon’s TUI the strongest operational interface for agents, including external ones.

#### Needed deliverables
- better session grid ergonomics
- overlays for:
  - specialization
  - mode
  - goal
  - task state
- memory / checkpoint / shade event visibility
- search and recall panels
- inbox / coordination panels
- live diff and artifact panels
- checkpoint browser
- intervention graph / timeline UI
- strong keyboard workflows
- excellent failure-state visibility

#### Success criteria
- the TUI is a compelling reason to use Charon even with external agents
- users can oversee complex agent activity without losing the thread

---

### B5. Better compaction and context hygiene

#### Goal
Keep long-running agents coherent and reduce context drift.

#### Needed deliverables
- file-aware compaction
- tool-aware compaction
- goal-aware summaries
- artifact-aware summaries
- memory flush before compaction
- pair sanitization for tool-call/result history
- compaction quality checks
- improved continuity after long autonomous runs

#### Success criteria
- long-running Charon agents stay coherent longer than Hermes agents
- compaction preserves the information that actually matters for future work

---

## Workstream C — Workflow superiority

These workstreams move Charon from parity into clearly better day-to-day developer leverage.

### C1. Procedures / skills / learned workflows

#### Goal
Build a first-class reusable procedure system that outperforms Hermes skills.

#### Needed deliverables
- reusable procedures with arguments
- discovery and ranking
- versioning and review
- project-local and global procedures
- procedure safety / trust levels
- linkage to shades and judge loops
- learning from repeated successful workflows

#### Success criteria
- repeated tasks become faster and more reliable
- Charon learns workflows in a way that compounds across projects and agents

---

### C2. Automation and scheduling

#### Goal
Make Charon the best place to run long-lived development automations.

#### Needed deliverables
- stronger recurring automations
- goal-aware schedules
- automations that create/update backlog state
- scheduled code health audits
- scheduled research refreshes
- approval gates for automations
- retries, observability, and result routing

#### Success criteria
- users can trust Charon with useful recurring engineering work
- automation output is inspectable and actionable

---

### C3. Developer workflow integrations

#### Goal
Connect Charon directly to real software-development workflows.

#### Needed deliverables
- strong GitHub / forge integration
- issue / PR / review workflows
- notification and reporting surfaces
- excellent X integration where it supports shipping / research workflows
- selective chat/email integrations where they add leverage

#### Success criteria
- Charon participates in the actual software-development loop, not only in terminal conversations

---

## Workstream D — Strategic moat

These are the moves that make Charon difficult to substitute even if other agent systems remain individually strong.

### D1. Deep external-agent bridge plugins

#### Goal
Make Charon the best host and operating layer even for non-Charon agents.

#### Needed deliverables
- deep bridge plugins for:
  - Hermes
  - pi
  - Claude Code
  - Codex
  - OpenCode
  - OpenClaw
- structured task dispatch where possible
- result capture and normalization
- capability maps by agent type
- metadata normalization across agent systems
- memory bridging and import paths

#### Success criteria
- Charon can operate heterogeneous agent fleets better than any individual agent framework can operate itself

---

### D2. Unified mixed-agent memory and search

#### Goal
Search, recall, and operate across work done by many different agent systems.

#### Needed deliverables
- shared indexing of external-agent work
- normalized task episodes across agent types
- mixed-agent search and summarization
- cross-agent provenance and attribution
- “what happened across all agents on this project?” workflows

#### Success criteria
- Charon becomes the place where all agent work becomes searchable and operationally legible

---

### D3. Charon as agent operating system

#### Goal
Make Charon the default control plane for persistent software agents.

#### Needed deliverables
- stable agent registry
- stronger lifecycle management
- richer agent metadata and specialization
- better remote linking
- fleet-level policy controls
- stronger interoperability and coordination primitives

#### Success criteria
- Charon is not just an agent runtime; it is the environment in which agent systems are run, coordinated, and supervised

---

## 5. Phased roadmap

## Phase 1 — Close obvious capability gaps

Priority order:
1. browser/web stack
2. summarized + semantic session recall
3. transparent checkpoints
4. first-class MCP
5. model fallback / reliability
6. approval / safety flow

### Exit criteria
- Charon no longer loses obvious comparisons on core agent capability
- day-to-day single-agent workflows are competitive with Hermes

---

## Phase 2 — Sharpen Charon-native strengths

Priority order:
7. stronger shade contracts
8. stronger inter-agent coordination
9. operational project knowledge
10. TUI operations polish
11. better compaction / context hygiene

### Exit criteria
- Charon is clearly stronger than Hermes on multi-agent operation and oversight
- persistent-agent workflows become a compelling reason to switch

---

## Phase 3 — Overtake on workflow power

Priority order:
12. procedures / skills system
13. automation / scheduling
14. developer workflow integrations
15. artifact-centric auditability

### Exit criteria
- Charon becomes a stronger day-to-day engineering system, not just a better shell around agents

---

## Phase 4 — Strategic moat

Priority order:
16. deep bridge plugins
17. unified mixed-agent memory / search
18. Charon as the operating layer for all agents

### Exit criteria
- even strong external agent systems increase in value when operated through Charon
- Charon becomes difficult to substitute because it owns the operational layer

---

## 6. Dependencies and sequencing notes

- transparent checkpoints should land before more aggressive autonomous code-editing workflows
- approval / safety should land before expanding persistent-agent autonomy significantly
- model fallback and runtime reliability should land before pushing heavier automation workloads
- stronger recall should land before expecting long-running persistent agents to outperform session-based systems consistently
- bridge plugins should follow stronger internal metadata normalization, not precede it

---

## 7. How we measure success

We should define benchmark tasks in both categories.

### 7.1 Single-agent benchmark tasks
- recover a prior solution from session history
- complete a multi-step browser workflow
- modify a codebase safely and roll back changes
- use MCP-provided capabilities successfully
- stay coherent across long-running tasks and compaction boundaries

### 7.2 Multi-agent benchmark tasks
- split work across multiple persistent agents without collision
- coordinate specialist and reviewer agents
- complete bounded delegated work via shades with artifacts and verification
- operate mixed external and native agents in one workspace
- inspect and intervene across many live sessions without confusion

### 7.3 Platform benchmark tasks
- onboard a new agent into an existing project successfully
- recover from provider outages gracefully
- search all relevant project work regardless of which agent performed it
- run scheduled automations and inspect their outputs

---

## 8. Immediate next-step planning conversion

This document is the backbone. To make it executable, convert it into:

1. a ranked engineering roadmap
2. a delta matrix of current status vs desired state
3. named project milestones per phase
4. success metrics and demo tasks per workstream
5. issue clusters / epics for implementation

Recommended next docs:
- `docs/plans/charon-vs-hermes-delta-matrix.md`
- `docs/plans/charon-superiority-phase-1.md`
- `docs/plans/charon-superiority-benchmarks.md`

---

## 9. Summary

To become better than Hermes in all important respects, Charon must do two things simultaneously:

1. reach parity or better on the fundamentals of agent capability
2. extend its lead as the best operating environment for persistent multi-agent software work

If we do only the second, Hermes remains the stronger answer for many users.
If we do only the first, Charon loses what makes it special.
The plan succeeds only if both happen together.
