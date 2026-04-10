# Charon Superiority Benchmarks

> Companion to:
> - `docs/plans/charon-vs-hermes-superiority-plan.md`
> - `docs/plans/charon-vs-hermes-delta-matrix.md`
> - `docs/plans/charon-superiority-phase-1.md`
>
> Purpose: define concrete benchmark tasks and evaluation criteria so “better than Hermes” is measured in practice, not argued abstractly.

Updated: 2026-04-05

---

## 1. Benchmark philosophy

A competitive claim is only credible if it survives task-based evaluation.

Benchmarks should measure three kinds of strength:

1. **Single-agent capability**
   - can the system solve real tasks reliably?
2. **Multi-agent operational strength**
   - can the system coordinate and supervise many agents effectively?
3. **Platform trust and usability**
   - can users understand, control, and recover from agent behavior?

Benchmarks should prefer:
- realistic developer workflows
- observable outputs
- repeatability
- postmortem inspectability

Benchmarks should avoid:
- cherry-picked toy tasks
- purely subjective comparison without artifact review
- one-off demos that cannot be repeated

---

## 2. Evaluation dimensions

For each benchmark, score the system on:

### 2.1 Task success
- Did it complete the task correctly?
- Was the result usable?

### 2.2 Reliability
- Did it succeed consistently across runs?
- Did it recover from minor failures?

### 2.3 Efficiency
- How many turns, tools, or retries were needed?
- Was context used efficiently?

### 2.4 Observability
- Could the user understand what happened while it was happening?
- Were outputs, logs, and state transitions inspectable?

### 2.5 Safety / recoverability
- Could bad actions be prevented or rolled back?
- Were risky actions surfaced appropriately?

### 2.6 Multi-agent leverage
- Where applicable, did multiple agents outperform a single-agent baseline?

A simple scoring template:
- 0 = failed / absent
- 1 = weak
- 2 = partial
- 3 = good
- 4 = excellent

---

## 3. Single-agent capability benchmarks

## Benchmark S1 — Prior-fix recall

### Prompt
“Find how we fixed the previous failure in subsystem X and summarize the approach, files changed, and unresolved issues.”

### What it tests
- session search
- semantic recall
- summarization quality
- provenance

### Success criteria
- returns the correct prior episode
- includes files, decisions, and outcome
- avoids flooding the context with raw transcript noise

### Artifacts to inspect
- retrieved episodes
- summary quality
- provenance trail

---

## Benchmark S2 — Decision recall

### Prompt
“What did we decide about architecture choice Y, and why?”

### What it tests
- project knowledge quality
- recall from historical sessions and planning docs
- precision of summary

### Success criteria
- states the decision accurately
- includes rationale and source
- distinguishes current truth from stale historical discussion

---

## Benchmark S3 — Browser research task

### Task
Visit a target website, gather information about a topic/product/feature, and produce a concise report with links and evidence.

### What it tests
- navigation
- extraction
- page understanding
- reliability on real websites

### Success criteria
- navigates successfully
- gathers relevant information
- cites sources accurately
- produces a coherent report

---

## Benchmark S4 — Browser workflow task

### Task
Log in to a test web app, complete a multi-step flow, and verify the final state.

### What it tests
- browser interaction robustness
- form handling
- session persistence
- multi-step state tracking

### Success criteria
- completes the flow without getting lost
- verifies the final state correctly
- handles minor page changes/retries gracefully

---

## Benchmark S5 — Safe code modification + rollback

### Task
Make a code change that intentionally introduces a failure, then recover using built-in checkpointing/rollback.

### What it tests
- checkpointing
- diff visibility
- rollback UX
- recoverability

### Success criteria
- automatic checkpoint exists
- bad change can be identified and reverted quickly
- recovery preserves trust and clarity

---

## Benchmark S6 — MCP interoperability

### Task
Connect to a representative MCP server, discover tools, use them correctly, and constrain them by project/agent policy.

### What it tests
- MCP integration
- tool discovery
- namespacing
- policy controls

### Success criteria
- tools are discovered and usable
- collisions/policies are handled clearly
- the model uses MCP tools appropriately

---

## Benchmark S7 — Long-run coherence

### Task
Run a long multi-step software task that requires file reads, edits, tests, and compaction across many turns.

### What it tests
- compaction quality
- memory hygiene
- long-run task continuity

### Success criteria
- the agent preserves the right facts across compression boundaries
- it does not drift or repeat bad prior assumptions excessively
- final output remains coherent with prior work

---

## Benchmark S8 — Provider outage recovery

### Task
Simulate model/provider failure in the middle of work and evaluate fallback/retry behavior.

### What it tests
- runtime resilience
- graceful degradation
- error clarity

### Success criteria
- the system recovers or fails clearly
- fallback path preserves task continuity where possible
- user experience remains understandable

---

## 4. Multi-agent benchmarks

## Benchmark M1 — Parallel subsystem work

### Task
Assign multiple agents to different subsystems of one repo and complete changes in parallel without conflict.

### What it tests
- coordination
- ownership boundaries
- conflict avoidance
- shared memory usefulness

### Success criteria
- agents do not step on each other’s files unnecessarily
- shared project knowledge improves execution
- outputs merge cleanly

---

## Benchmark M2 — Specialist + reviewer workflow

### Task
One agent implements a change, another reviews/criticizes it, and the originating agent updates based on review.

### What it tests
- inter-agent coordination
- specialization
- review loops
- handoff quality

### Success criteria
- reviewer catches meaningful issues
- implementation improves after review
- coordination overhead remains manageable

---

## Benchmark M3 — Shade contract benchmark

### Task
Spawn one or more shades to complete bounded subtasks with explicit acceptance criteria and artifacts.

### What it tests
- contract discipline
- artifact production
- bounded delegation quality
- parent/child reporting

### Success criteria
- shades stay within scope
- artifacts are useful and inspectable
- parent agent can integrate results confidently

---

## Benchmark M4 — Multi-agent recovery after partial failure

### Task
Inject a failure in one worker while other agents continue, then measure recovery and coordination behavior.

### What it tests
- robustness of orchestration
- partial failure handling
- task continuity

### Success criteria
- one agent’s failure does not collapse the whole effort
- recovery path is visible and manageable
- successful work from unaffected agents is preserved

---

## Benchmark M5 — Persistent population continuity

### Task
Run a series of tasks across days with multiple named agents assigned to a project, then evaluate continuity and accumulated usefulness.

### What it tests
- persistent named agents
- specialization
- shared project memory
- long-lived productivity

### Success criteria
- agents improve with history
- new tasks benefit from prior project-specific knowledge
- continuity outperforms isolated-session workflows

---

## 5. Platform / operating-system benchmarks

## Benchmark P1 — New-agent onboarding

### Task
Create a fresh agent on an existing project and evaluate how quickly it becomes useful.

### What it tests
- user model injection
- project knowledge injection
- onboarding quality
- memory packet usefulness

### Success criteria
- the new agent is not “blank”
- it knows conventions, architecture, and relevant context quickly

---

## Benchmark P2 — Session grid supervision

### Task
Supervise several live sessions simultaneously, intervene in one, inspect another, and track system state without confusion.

### What it tests
- TUI ergonomics
- operational visibility
- session management

### Success criteria
- user can effectively oversee many sessions
- interventions are fast and understandable
- operational UI reduces cognitive load rather than increasing it

---

## Benchmark P3 — Mixed-agent workspace benchmark

### Task
Run native Charon agents and external bridged agents in one workspace, then search and reason over the combined work.

### What it tests
- Charon’s Boat
- metadata normalization
- mixed-agent search / memory
- operating-system thesis

### Success criteria
- heterogeneous agents can coexist usefully
- work remains searchable and attributable
- Charon adds operational value even when some agents are external

---

## Benchmark P4 — Scheduled automation benchmark

### Task
Configure recurring automations, inspect their outputs, and verify that failures are visible and recoverable.

### What it tests
- automation runtime
- scheduling
- observability
- result routing

### Success criteria
- automations run reliably
- outputs are understandable and actionable
- failures are easy to investigate

---

## 6. Benchmark scorecards

Each benchmark run should capture:
- date
- branch / commit
- model/provider configuration
- benchmark ID
- success/failure
- score by dimension
- notes
- artifacts / logs / screenshots

Suggested scorecard template:

```md
## Benchmark: S3 — Browser research task
- Date:
- Commit:
- Provider/model:
- Result: pass / partial / fail

### Scores
- Task success:
- Reliability:
- Efficiency:
- Observability:
- Safety/recoverability:
- Multi-agent leverage:

### Notes
- 

### Artifacts
- 
```

---

## 7. Benchmark packs by phase

## Phase 1 benchmark pack
- S1 prior-fix recall
- S3 browser research
- S4 browser workflow
- S5 safe code modification + rollback
- S6 MCP interoperability
- S8 provider outage recovery

## Phase 2 benchmark pack
- M1 parallel subsystem work
- M2 specialist + reviewer workflow
- M3 shade contract benchmark
- M5 persistent population continuity
- P2 session grid supervision

## Phase 3 benchmark pack
- C-style workflow/procedure benchmarks (to be added)
- P4 scheduled automation benchmark
- deeper developer workflow integration benchmarks

## Phase 4 benchmark pack
- P3 mixed-agent workspace benchmark
- mixed-agent search / recall benchmark
- fleet-level supervision benchmarks

---

## 8. How this should be used

This document should become the basis for:
- milestone exit reviews
- regression checks after major architecture changes
- competitive evaluation against Hermes and other systems
- demos that are honest because they map to repeatable tasks

If Charon cannot pass these benchmarks, claims of superiority should be treated as aspirational rather than current reality.
