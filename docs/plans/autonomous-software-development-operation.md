# Autonomous Software Development Operation

> Shareable product/vision spec for Charon’s dominant autonomous software development mode.
>
> Date: 2026-03-28  
> Status: Proposed  
> Related: `docs/plans/autonomous-goal-driven-work.md`, `docs/plans/overseer-agent-design.md`, `docs/plans/libris-autonomous-research-operation.md`

---

## One-line vision

The user gives Charon a broad software development directive, and **Charon launches a live multi-agent development operation** that scouts implementation strategies, selects promising workstreams, produces and integrates outputs, critiques them against the user’s goals and standards, iterates through judged checkpoints, and returns the strongest working result.

---

## Canonical user prompt

Example:

> “Build a web app that does X, with a clean backend, a usable frontend, and enough tests that we can trust it.”

This is a canonical Charon software-development use case.

---

## Core behavior

When given a broad software directive, Charon enters **Autonomous Software Development Operation** mode.

At minimum it spawns:
- a **Development Coordinator**
- a **Judge**

Depending on task shape, it also spawns:
- one or more **Implementer agents**
- one or more **paired workstream judges**
- multiple bounded **development shades**
- optionally one or more **boat-wrapped external workers** (Pi, Hermes, etc.)
- optionally an **Integration Verifier**

The system operates as a **live, inspectable software organization** with iterative refinement.

---

## Why this is a dominant Charon mode

This should be one of Charon’s primary operating modes because it addresses the highest-value software tasks:

- broad product-building rather than single-step coding
- architectural and implementation strategy selection
- parallel work across multiple workstreams
- iterative refinement rather than one-shot code generation
- personalization to the user’s actual goals, coding standards, and project priorities
- visibility into the development process while it runs

---

# Agent roles

---

## 1. Development Coordinator

Top-level orchestrator for the whole development operation.

### Responsibilities
- interpret the user’s broad software directive
- derive workstreams, constraints, and success criteria
- perform or supervise broad solution scouting
- identify promising implementation strategies
- decide which workstreams deserve full execution pipelines
- launch implementer/judge workflows for selected workstreams
- track progress across all active workstreams
- choose the best result(s) to present when the run stops or completes

### Inputs
- user prompt
- user model
- project context
- existing codebase state
- prior project memory
- broad implementation scouting results

### Outputs
- architecture/workstream shortlist
- selected strategy and execution topology
- final result selection decision

### Example
For a “build a web app” prompt, the coordinator might identify:
- frontend UI workstream
- backend API workstream
- auth/data model workstream
- deployment/infra workstream
- testing/integration workstream

It then decides which of these should run in parallel immediately and which should wait on dependencies.

---

## 2. Implementer

Lead builder for a single selected workstream.

### Responsibilities
- turn a chosen workstream into concrete engineering tasks
- write code, tests, docs, and integration glue
- spawn shades for bounded implementation or investigation tasks
- produce working checkpoints
- respond to judge critiques
- refine outputs over multiple cycles

### Inputs
- assigned workstream from coordinator
- prior checkpoints
- judge feedback
- repository/codebase state
- user model signals relevant to quality and style

### Outputs
- implementation checkpoints
- revised code/test/doc bundles
- evidence bundles
- progress summaries

---

## 3. Development Shades

Small bounded subagents working under an implementer.

### Responsibilities
Each shade handles a clearly scoped subproblem, for example:
- “Implement input validation in the login form”
- “Write integration tests for the auth API”
- “Investigate why the build is failing in CI”
- “Compare two libraries for this workstream”
- “Refactor this module to match the project pattern”

### Properties
- narrow scope
- short-lived
- evidence-first
- reports back in structured form

### Outputs
- patch summaries
- file changes
- test results
- open questions
- implementation evidence artifacts

---

## 4. Judge

A standing critique agent aligned to the user.

### Responsibilities
- review outputs from implementers from the perspective of:
  - the user’s stated goals
  - the user model
  - the project context
  - the architecture and constraints
- identify weaknesses, missing coverage, regressions, poor prioritization, or irrelevance
- evaluate correctness, usefulness, quality, fit, and readiness
- send actionable critique back to the implementer
- score checkpoints and track progress over time

### Core principle
The judge is not just checking tests or style. It acts as a **proxy for the user’s software standards and priorities**.

### Example critique dimensions
- Does this actually satisfy the requested feature?
- Is the architecture appropriate for the project?
- Are tests sufficient for the risk level?
- Is the implementation overcomplicated or too shallow?
- Are major edge cases missing?
- Is this the right thing to build next?
- Does this fit the user’s likely preferences for maintainability, speed, and clarity?

### Outputs
- critique summaries
- quality scores
- checkpoint metadata
- candidate best-so-far versions

---

## 5. Paired Workstream Judge

Optional but preferred for larger operations.

### Responsibilities
- stay attached to one workstream
- review outputs continuously
- provide local critique quickly
- surface best checkpoint candidates to the global judge or coordinator

### Why this exists
In large software operations, one global judge can become a bottleneck. Paired judges preserve locality and faster iteration.

---

## 6. Integration Verifier

Optional specialized role for system-wide readiness.

### Responsibilities
- verify that independently produced workstreams integrate correctly
- run end-to-end checks
- evaluate release readiness
- identify cross-workstream regressions and compatibility issues

### Inputs
- accepted workstream outputs
- test results
- build results
- app/runtime evidence

### Outputs
- integration status
- release-readiness verdict
- final blocking issue list

---

# End-to-end workflow

---

## Phase 1: Intake and framing

The coordinator:
1. parses the request
2. reads user model + project context
3. reads codebase and prior project memory
4. determines:
   - product goal
   - likely architecture constraints
   - expected output type
   - likely quality bar
   - exploration breadth

### Output
A **development operation plan** containing:
- scope
- target workstreams
- selection criteria
- expected deliverable shape

---

## Phase 2: Broad solution scouting

The coordinator performs an initial sweep across:
- codebase structure
- existing architecture
- current tests and docs
- relevant tools/dependencies/framework options
- project memory
- open goals/backlog

### Goal
Produce a shortlist of promising implementation strategies and workstreams.

### Output
A ranked candidate list including:
- workstream
- why it matters
- dependency/risk level
- expected user value
- estimated relevance to current goal
- recommended action: ignore / defer / execute now

---

## Phase 3: Workstream selection

The coordinator selects promising workstreams for deeper execution.

For each selected workstream, Charon launches:
- an **Implementer**
- optionally a paired **Judge**

The coordinator may run multiple workstreams in parallel.

---

## Phase 4: Workstream execution

Each implementer:
1. formalizes the workstream into engineering tasks
2. spawns shades for bounded implementation or investigation work
3. collects results and integrates them
4. builds an initial implementation checkpoint

Shades may handle:
- coding
- testing
- debugging
- architecture comparison
- documentation
- refactoring
- verification tasks

---

## Phase 5: Judge critique cycle

The implementer submits a checkpoint to the judge.

The judge:
1. critiques the implementation
2. identifies flaws, missing evidence, or weak decisions
3. produces a concise checkpoint assessment
4. returns targeted improvement requests

The implementer then:
1. absorbs the feedback
2. reruns or spawns additional bounded tasks
3. revises the implementation
4. submits a new checkpoint

This repeats until:
- quality target is met
- time/budget is exhausted
- the user interrupts
- diminishing returns are detected

---

## Phase 6: Integration and system verification

When multiple workstreams are involved, accepted outputs are gathered into an integration phase.

The integration verifier or coordinator:
1. combines accepted workstream outputs
2. runs integration/build/test/app checks
3. identifies cross-workstream issues
4. requests fixes or approves system-level readiness

---

# Checkpoint model

Every judge cycle creates a **development checkpoint**.

---

## Each checkpoint contains

### 1. Full implementation snapshot
A complete version of the implementation state at that stage.

This may reference:
- branch/checkpoint id
- commit/checkpoint metadata
- file bundle
- artifact bundle

### 2. Critique summary
A short topline summary including:
- strengths
- flaws
- implementation quality
- readiness
- confidence
- recommended next actions

### 3. Quality metadata
Examples:
- requirement fit
- test adequacy
- code quality
- integration readiness
- architectural fit
- user-fit
- overall judge score

### 4. Revision diff metadata
What changed since the last checkpoint:
- files changed
- tests added/fixed
- regressions resolved
- docs updated
- remaining weaknesses

---

## Why checkpoints matter

They enable:
- pause/resume
- user inspection
- best-version selection
- auditability
- safe interruption without losing progress

---

# Evidence bundle model

This is the software-development equivalent of Libris’s evidence/provenance layer.

Every implementation checkpoint should be backed by an **evidence bundle**.

## Evidence bundle contents
- changed files
- patch/diff summary
- commands executed
- tests run
- test results
- build results
- benchmark results when relevant
- screenshots when relevant
- endpoint/browser verification evidence when relevant
- judge comments
- worker summaries
- branch/checkpoint references

## Why evidence matters
The system should not only say “this is done.” It should be able to show:
- what changed
- what was verified
- what is still uncertain
- why the judge accepted or rejected it

---

# Interruption behavior

If the user intervenes, the development process pauses or stops gracefully.

Then:
1. the **Implementer** reviews its checkpoints and selects its best version
2. the **Judge** independently selects its best version
3. the **Coordinator** compares these recommendations
4. the coordinator decides what to deliver to the user

### Delivery rule
The coordinator may choose:
- the latest version
- the highest-scoring version
- the safest passing version
- the most user-aligned version
- multiple candidate versions with a recommendation

This matters because the best implementation is not always the newest one.

---

# Preferred topology: coordinator-led fanout

This should be explicitly supported as the preferred architecture for broad software prompts.

### Shape
Broad prompt  
→ **Development Coordinator**  
→ identifies promising workstreams  
→ for each worthy workstream:
- spawn **Implementer**
- optionally spawn paired **Judge**
- implementer spawns **Shades**
- optionally use **boat-wrapped external workers**

### Example tree
- Coordinator
  - Implementer A (frontend)
    - Shade A1
    - Shade A2
    - Judge A
  - Implementer B (backend)
    - Shade B1
    - Shade B2
    - Judge B
  - Implementer C (infra)
    - Shade C1
    - Judge C
  - Integration Verifier

This should be considered a canonical Charon swarm structure.

---

# Output model

---

## Per-workstream outputs
For each workstream, Charon produces:
- implementation checkpoints
- evidence bundles
- judge critiques
- accepted or best-so-far outputs
- status summaries

## Coordinator outputs
At the top level, Charon produces:
- shortlist of candidate workstreams/strategies
- execution status overview
- selected final recommendations
- ranked delivery bundle for user review

## Final user-facing result
The user should receive:
1. an executive summary
2. the recommended implementation result or checkpoint
3. a short rationale for why it was chosen
4. evidence of quality and verification
5. optionally a next-best list or unfinished work list

---

# Alignment to the user model

This is a core part of the spec.

The judge and coordinator both use:
- the user model
- project knowledge
- current goals
- prior development memory
- observed user preferences

To determine:
- what counts as good enough
- what level of polish matters
- whether the user prefers speed, robustness, maintainability, minimalism, experimentation, or completeness
- what should be prioritized first
- how much evidence is required before saying something is ready

Charon is therefore not doing generic autonomous coding. It is doing **personalized software development triage and refinement**.

---

# Specialized TUI mode

This must be a first-class part of the Charon experience.

When a development operation starts, the user can switch to a dedicated **development swarm view**.

### The view shows
A real-time grid of active sessions, including:
- coordinator
- judges
- implementers
- active shades
- external workers when attached

### Each grid cell shows
- agent role
- assigned workstream
- current phase
- recent activity
- status
- checkpoint path
- autonomy level
- current judge state or score when available

### Example phases
- scouting
- selecting workstreams
- implementing
- testing
- integrating
- judging
- revising
- checkpoint saved
- complete
- blocked

### User capabilities in this view
- watch agents work in real time
- open any session
- inspect checkpoints
- inspect critiques
- inspect evidence bundles
- stop/pause the operation
- adjust autonomy level per worker
- optionally promote/demote workstream priority
- optionally select a preferred checkpoint manually

This view should make Charon feel like a **visible software organization in motion**, not a black box.

---

# Dominant Charon modes of operation

The autonomous development operation should be treated as a top-tier Charon capability alongside:
- direct chat-driven coding
- supervised multi-agent execution
- research via Libris
- optimization/judge loops
- autonomous project management via overseer

But the mode specified here should be one of the most visible and important modes of Charon.

---

# Success criteria

Charon succeeds in this mode when it can reliably:
1. accept a broad software directive
2. derive a shortlist of promising workstreams
3. launch parallel workstream execution
4. use judge-guided iterative refinement
5. save development checkpoints per critique cycle
6. stop gracefully on user interruption
7. choose the best versions to present
8. show the whole process in a dedicated live TUI grid
9. personalize outputs to the user’s goals and preferences

---

# Compact product statement

**Autonomous Software Development Operation** is Charon’s native multi-agent software-building mode for broad development prompts. It launches a visible swarm composed of a development coordinator, implementer agents, judge agents, development shades, and optional external workers. The coordinator scouts solution strategies, selects promising workstreams, and assigns each workstream to an implementer–judge pipeline. Implementers write code, tests, docs, and integration artifacts; judges critique those outputs from the perspective of the user model, project goals, and software-quality standards. This loop repeats through checkpointed implementation versions until quality is sufficient or the user interrupts. At any time, the user can watch the swarm in a specialized TUI grid showing all active sessions in real time. When the run stops, the coordinator selects the strongest implementation versions for delivery to the user.
