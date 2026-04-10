# Charon Superiority — Phase 1 Plan

> Companion to:
> - `docs/plans/charon-vs-hermes-superiority-plan.md`
> - `docs/plans/charon-vs-hermes-delta-matrix.md`
>
> Scope: close the most obvious capability gaps where Hermes appears stronger today.

Updated: 2026-04-05

---

## 1. Phase 1 objective

By the end of Phase 1, Charon should no longer lose obvious comparisons on core single-agent capability.

Phase 1 is successful when a fair evaluator can say:
- Charon is competitive on the fundamentals of agent capability
- Charon’s weaker areas are no longer browser, search, rollback, or interoperability basics
- the remaining differentiation question becomes product model and operating environment, not missing essentials

---

## 2. In-scope workstreams

Phase 1 includes six workstreams:

1. browser / web stack
2. summarized + semantic session recall
3. transparent checkpoints and undo
4. first-class MCP support
5. model fallback and runtime reliability
6. approval / safety flow

These are ordered by user-visible impact and strategic necessity.

---

## 3. Milestone structure

## Milestone 1 — Browser/web parity

### Goal
Deliver browser and web-research capability that can handle real modern tasks.

### Deliverables
- stable browser session model
- DOM/accessibility-tree browsing
- screenshots + vision analysis
- click / type / scroll / upload / download support
- login and multi-step form flows
- local backend reliability
- optional cloud backend path
- extraction + summarization path for web content
- browser actions included in task summaries / memory capture

### Acceptance tests
- log into a common SaaS app using browser automation
- complete a multi-step form flow
- inspect a web page and summarize relevant content
- debug a simple web UI issue using browser state + screenshot

### Dependencies
- browser process lifecycle stability
- screenshot/image handling
- safe website policy controls

### Risks
- high complexity / high edge-case surface area
- browser reliability can consume disproportionate implementation time

---

## Milestone 2 — Session recall that actually works

### Goal
Make “what happened last time?” a reliable workflow.

### Deliverables
- hybrid FTS + semantic retrieval
- LLM summaries for retrieved episodes
- result formatting with:
  - dates
  - files
  - outcomes
  - agent attribution
- filters by project / agent / time
- “recent relevant episodes” UX
- provenance surfaced in results

### Acceptance tests
- recover a prior fix for a known bug from history
- answer “what did we decide about X?” from prior sessions
- retrieve relevant episodes without overwhelming context with transcript noise

### Dependencies
- strong indexing of task episodes
- clear summarization prompt strategy
- normalized search result schema

### Risks
- retrieval quality may look good in demos but fail in realistic multi-project history

---

## Milestone 3 — Transparent checkpoints and rollback

### Goal
Make agent edits safe by default.

### Deliverables
- automatic checkpoint creation before file mutations
- checkpoint metadata:
  - agent
  - task
  - goal
  - timestamp
- restore flow
- checkpoint diff inspection
- “undo last agent action” UX
- integration with:
  - shades
  - judge loops
  - failed runs

### Acceptance tests
- perform agent edit → restore previous state immediately
- inspect checkpoint diff before rollback
- recover from a failed automation run safely

### Dependencies
- git-based shadow checkpoint manager or equivalent robust storage model
- metadata integration with agent runtime

### Risks
- hidden complexity around edge cases, large repos, nested git, ignored files

---

## Milestone 4 — MCP as a first-class capability layer

### Goal
Let Charon connect cleanly to the wider MCP ecosystem.

### Deliverables
- MCP client runtime integration
- dynamic tool discovery
- namespaced tool registration
- collision handling
- per-agent/per-project enablement
- setup and auth UX
- approval/policy controls for MCP tools

### Acceptance tests
- connect to at least one representative local MCP server
- connect to at least one remote/server-backed MCP provider
- expose discovered tools safely and clearly to the model
- disable or scope MCP by agent/project

### Dependencies
- runtime tool registration infrastructure
- policy and approval framework

### Risks
- tool namespace confusion
- security / trust issues with remote MCP tools

---

## Milestone 5 — Runtime reliability and model fallback

### Goal
Reduce brittle failures and improve day-to-day dependability.

### Deliverables
- provider fallback path
- retry/backoff policy
- health-aware selection logic
- graceful degradation when a provider is down
- better model routing defaults
- clearer runtime errors and recovery messages

### Acceptance tests
- simulate provider outage and recover automatically
- degrade to fallback model without losing task continuity
- preserve user trust during partial failures

### Dependencies
- provider abstraction quality
- telemetry / error classification

### Risks
- fallback logic can create confusing behavior if not surfaced cleanly

---

## Milestone 6 — Safety and approvals

### Goal
Make autonomous and semi-autonomous operation safer and more controllable.

### Deliverables
- command risk classification
- approval prompts for destructive actions
- optional approval modes by agent / project
- network / secret access policy controls
- persistent-agent safety controls, not only shade scope controls
- action audit log

### Acceptance tests
- destructive shell/file action triggers approval in the configured mode
- harmless actions do not create unnecessary friction
- policies can be tuned per project or agent role

### Dependencies
- action classification
- checkpointing (to lower risk even when approvals are bypassed)

### Risks
- too much friction degrades usability
- too little friction degrades trust

---

## 4. Execution order inside Phase 1

Recommended order:

### Track A — Safety baseline first
1. transparent checkpoints
2. runtime reliability / fallback
3. approval flow foundation

### Track B — Capability parity
4. session recall
5. browser / web
6. MCP support

Reasoning:
- checkpoints + fallback + approvals reduce risk while we increase capability
- recall improves agent usefulness broadly
- browser and MCP add power once runtime safety is in place

Alternative order if user-facing wow factor is prioritized:
1. browser / web
2. session recall
3. checkpoints
4. MCP
5. fallback
6. approvals

But this is riskier operationally.

---

## 5. Deliverable checklist

## Phase 1 checklist

### Browser / web
- [ ] persistent browser session abstraction
- [ ] page state inspection model
- [ ] screenshot support
- [ ] vision-assisted page analysis
- [ ] interaction primitives
- [ ] login/session persistence story
- [ ] browser task summarization
- [ ] representative end-to-end tests

### Session recall
- [ ] normalized episode index
- [ ] hybrid retrieval
- [ ] summary generation for retrieved episodes
- [ ] provenance in results
- [ ] filters and ranking
- [ ] retrieval quality benchmarks

### Checkpoints
- [ ] automatic checkpoint trigger points
- [ ] checkpoint metadata model
- [ ] restore flow
- [ ] diff UI / inspection path
- [ ] undo-last-action UX
- [ ] failed-run recovery flow

### MCP
- [ ] client integration
- [ ] dynamic discovery
- [ ] tool namespacing
- [ ] config/auth UX
- [ ] policy controls
- [ ] example MCP integrations

### Reliability
- [ ] provider failure taxonomy
- [ ] retry policy
- [ ] fallback policy
- [ ] degraded-mode UI/messages
- [ ] health metrics/logging

### Safety
- [ ] risk classification
- [ ] approval prompt model
- [ ] secret/network policy controls
- [ ] persistent-agent safety settings
- [ ] audit logging

---

## 6. Exit criteria

Phase 1 is complete when all of the following are true:

1. Charon can complete realistic browser/web tasks reliably
2. Charon can recover prior project work through summarized recall
3. Charon can safely roll back agent changes with minimal friction
4. Charon can use MCP tools in a first-class way
5. Charon handles provider failures gracefully
6. Charon has a usable safety and approval story for dangerous actions

If any of these remain weak, Phase 2 should not be treated as a full competitive step forward.

---

## 7. Benchmarks to run before closing Phase 1

- browser login + navigation benchmark
- browser form-fill benchmark
- prior-fix retrieval benchmark
- rollback-from-bad-edit benchmark
- MCP tool-discovery benchmark
- provider-failure recovery benchmark
- destructive-command approval benchmark

These should be recorded in the benchmark doc rather than treated as ad hoc demos.

---

## 8. Suggested next execution artifact

After this doc, create implementation epics or milestone docs for:
- browser/web parity
- recall/search upgrade
- checkpoint/rollback system
- MCP integration
- reliability + approval framework
