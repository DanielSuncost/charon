# Libris Autonomous Research Operation

> Shareable product/vision spec for Libris's dominant autonomous research mode.
>
> Date: 2026-03-27
> Status: Proposed
> Related: `docs/plans/libris-research-agent.md`, `docs/plans/libris-implementation-plan.md`

---

## One-line vision

The user gives Charon a broad research directive, and **Libris launches a live multi-agent research operation** that scouts topics, selects promising leads, produces full reports, critiques them against the user's preferences and goals, iterates through checkpoints, and returns the strongest final dossiers.

---

## Canonical user prompt

Example:

> "Research the current best topics in reinforcement learning research and generate full reports for any new techniques that seem interesting or relevant to our broader goals."

This is a canonical Libris use case.

---

## Core behavior

When given a broad research directive, Libris enters **Autonomous Research Operation** mode.

At minimum it spawns:
- a **Research Coordinator**
- a **Judge**

Depending on task shape, it also spawns:
- one or more **Researcher agents**
- multiple **research shades** for bounded investigation tasks

The system operates as a **live, inspectable swarm** with iterative refinement.

It should also support **long-running autonomous execution**:
- continue until user intervention
- continue until time/token/cost budgets are exhausted
- continue for hours, days, or weeks when explicitly configured
- dynamically shift work toward cheaper/local models for low-value or routine subtasks

---

## Why this is a dominant Libris mode

This should be one of Libris's primary operating modes because it addresses the highest-value research tasks:
- broad discovery rather than single lookup
- open-ended exploration across many candidate directions
- iterative refinement rather than one-shot output
- personalization to the user's actual goals, standards, and interests
- visibility into the research process while it runs

---

## Agent roles

## 1. Research Coordinator

Top-level orchestrator for the whole research operation.

### Responsibilities
- interpret the user's broad research directive
- derive the search space and selection criteria
- perform or supervise broad topical scouting
- identify promising candidate topics/leads
- decide which leads deserve full report pipelines
- launch researcher/judge workflows for selected leads
- track progress across all active investigations
- monitor operation budgets (time, tokens, cost, concurrency)
- adapt strategy as budgets tighten
- choose the best report(s) to present to the user when the run stops or completes

### Inputs
- user prompt
- user model
- project context
- prior research memory
- broad topical search results

### Outputs
- candidate lead list
- selected topics for deep investigation
- final report selection decision

### Example
For an RL prompt, the coordinator might identify:
- test-time adaptation methods
- RL with world models
- preference optimization alternatives
- offline RL advances
- multi-agent RL coordination techniques

It then decides which of these are worthy of full deep-report workflows.

---

## 2. Researcher

Lead investigator for a single selected topic.

### Responsibilities
- turn a chosen lead into concrete research questions
- gather sources
- spawn subagents/shades for bounded evidence-gathering tasks
- synthesize findings into structured reports
- respond to judge critiques
- refine and rebuild reports over multiple cycles

### Inputs
- assigned topic from coordinator
- prior report checkpoints
- judge feedback
- source corpus
- user model signals relevant to prioritization

### Outputs
- report drafts
- revised report checkpoints
- evidence tables
- source and claim records

---

## 3. Research Shades

Small bounded subagents working under a researcher.

### Responsibilities
Each shade investigates a clearly scoped subquestion, for example:
- "Find the strongest recent papers on offline RL in 2024–2026"
- "Extract claims and benchmarks for method X"
- "Check whether this technique has code, adoption, and replication"
- "Find official sources, repo evidence, and critiques"

### Properties
- narrow scope
- short-lived
- evidence-first
- reports back in structured form

### Outputs
- source summaries
- extracted claims
- confidence levels
- open questions
- evidence artifacts

---

## 4. Judge

A standing critique agent aligned to the user.

### Responsibilities
- review report drafts from the perspective of:
  - the user's stated goals
  - the user model
  - the broader project context
- identify weaknesses, gaps, overclaims, poor prioritization, or irrelevance
- evaluate clarity, usefulness, novelty, and fit
- send actionable critique back to the researcher
- score checkpoints and track progress over time

### Core principle
The judge is not just checking grammar or citations. It acts as a **proxy for the user's standards and interests**.

### Example critique dimensions
- Is this actually relevant to the user's broader goals?
- Is the report too academic and not actionable enough?
- Are the most important techniques being prioritized?
- Are practical constraints like implementation complexity being considered?
- Are citations strong enough?
- Are claims too speculative?

### Outputs
- critique summaries
- quality scores
- checkpoint metadata
- candidate best-so-far versions

---

## End-to-end workflow

## Phase 1: Intake and framing

The coordinator:
1. parses the request
2. reads user model + project context
3. recalls prior related research
4. determines:
   - domain
   - desired output type
   - likely relevance criteria
   - exploration breadth

### Output
A **research operation plan** containing:
- scope
- target themes
- selection criteria
- expected output format

---

## Phase 2: Broad scouting

The coordinator performs an initial sweep across:
- arXiv / paper search
- web search
- official docs / repos
- prior Libris memory
- relevant project context

### Goal
Produce a shortlist of promising leads.

### Output
A ranked candidate list including:
- topic
- why it matters
- novelty
- evidence strength
- estimated relevance to user/project
- recommended action: ignore / monitor / deep research

---

## Phase 3: Lead selection

The coordinator selects promising topics for deep investigation.

For each selected lead, Libris launches:
- a **Researcher**
- a paired **Judge**

The coordinator may run multiple topics in parallel.

---

## Phase 4: Topic investigation

Each researcher:
1. formalizes the topic into research questions
2. spawns shades for bounded evidence gathering
3. collects source results
4. builds an initial report draft

Shades may investigate:
- papers
- repos
- benchmarks
- implementation maturity
- criticism / counterevidence
- relevance to user goals

---

## Phase 5: Judge critique cycle

The researcher submits a draft to the judge.

The judge:
1. critiques the report
2. identifies flaws and missing evidence
3. produces a concise checkpoint assessment
4. returns targeted improvement requests

The researcher then:
1. absorbs the feedback
2. reruns or spawns additional bounded shade tasks
3. revises the report
4. submits a new version

This repeats until:
- quality target is met
- time/budget is exhausted
- the user interrupts
- diminishing returns are detected

---

## Checkpoint model

Every judge cycle creates a **report checkpoint**.

## Each checkpoint contains

### 1. Full report snapshot
A complete version of the report at that stage.

### 2. Critique summary
A short topline summary including:
- strengths
- flaws
- structural quality
- relevance
- confidence
- recommended next actions

### 3. Quality metadata
Examples:
- citation score
- evidence diversity
- actionability
- novelty
- user-fit
- overall judge score

### 4. Revision diff metadata
What changed since the last checkpoint:
- added sources
- new claims
- resolved gaps
- remaining weaknesses

## Why checkpoints matter
They enable:
- pause/resume
- user inspection
- best-version selection
- auditability
- safe interruption without losing progress

---

## Budgeted autonomous execution

This mode must support running with little or no user intervention for extended periods.

### Supported control styles
- "keep going until I stop you"
- "run overnight"
- "run for 3 days"
- "run for 2 weeks or until you hit 5M tokens"
- "stop after 50 dollars of model usage"

### Required budget controls
At the operation level, Libris should support:
- max wall-clock duration
- max total tokens
- max total cost
- max topics selected
- max checkpoints per topic
- max concurrent researchers
- max concurrent shades

### Coordinator behavior under budget pressure
The coordinator should not merely track budget passively. It should react strategically, for example:
- stop opening marginal new topics
- reduce shade fanout
- switch routine work to cheaper or local models
- shorten critique cycles on low-priority topics
- finalize best-so-far checkpoints when nearing budget exhaustion

### Model tiering
Libris should support role-based model policy such as:
- coordinator → strong model
- judge → strong model
- researcher → medium/fast model
- shade → cheap or local model

This is especially important for long-running research operations, where many shade tasks are extraction/summarization problems that do not justify the strongest model.

## Interruption behavior

If the user intervenes, the research process pauses or stops gracefully.

Then:
1. the **Researcher** reviews checkpoint summaries and selects its best version
2. the **Judge** independently selects its best version
3. the **Coordinator** compares these recommendations
4. the coordinator decides what to deliver to the user

### Delivery rule
The coordinator may choose:
- the latest version
- the highest-scoring version
- the most user-aligned version
- multiple versions with a recommendation

This is important because the best report is not always the newest one.

---

## Preferred topology: coordinator-led fanout

This should be explicitly supported as the preferred architecture for broad prompts.

### Shape
Broad prompt
→ **Coordinator**
→ identifies worthy topics
→ for each worthy topic:
- spawn **Researcher**
- spawn paired **Judge**
- researcher spawns **Shades**

### Example tree
- Coordinator
  - Researcher A
    - Shade A1
    - Shade A2
    - Shade A3
    - Judge A
  - Researcher B
    - Shade B1
    - Shade B2
    - Judge B
  - Researcher C
    - Shade C1
    - Shade C2
    - Shade C3
    - Judge C

This should be considered a canonical Libris swarm structure.

---

## Output model

## Per-topic outputs
For each investigated lead, Libris produces:
- evidence table
- source index
- claim index
- one or more report checkpoints
- judge critiques
- final or best-so-far report

## Coordinator outputs
At the top level, Libris produces:
- shortlist of promising topics
- investigation status overview
- selected final recommendations
- ranked delivery bundle for user review

## Final user-facing result
The user should receive:
1. an executive summary
2. recommended full reports
3. short rationale for why these were chosen
4. optionally a next-best list for future investigation

---

## Alignment to the user model

This is a core part of the spec.

The judge and coordinator both use:
- the user model
- project knowledge
- prior research memory
- current goals

To determine:
- what counts as interesting
- what counts as relevant
- what level of novelty matters
- whether the user prefers practical techniques, theoretical advances, implementable ideas, or long-term bets

Libris is therefore not doing generic research. It is doing **personalized research triage and refinement**.

---

## Specialized TUI mode

This must be a first-class part of the Libris experience.

When a research operation starts, the user can switch to a dedicated **research swarm view**.

### The view shows
A real-time grid of active sessions, including:
- coordinator
- judges
- researchers
- active shades

### Each grid cell shows
- agent role
- assigned topic
- current phase
- recent activity
- status
- output/checkpoint path
- current score or critique state when available

### Example phases
- scouting
- selecting leads
- gathering sources
- synthesizing
- judging
- revising
- checkpoint saved
- complete

### User capabilities in this view
- watch agents work in real time
- open any session
- inspect report checkpoints
- inspect critiques
- stop/pause the operation
- optionally promote/demote topic priority
- optionally select a preferred checkpoint manually

This view should make Libris feel like a **visible research organization in motion**, not a black box.

---

## Dominant Libris modes of operation

The autonomous scouting mode should be treated as a top-tier Libris capability alongside:
- broad autonomous topic scouting
- focused deep research
- literature review
- audit mode

But the mode specified here should be one of the most visible and important modes of Libris.

---

## Success criteria

Libris succeeds in this mode when it can reliably:
1. accept a broad research directive
2. derive a shortlist of promising topics
3. launch parallel topic investigations
4. use judge-guided iterative refinement
5. save report checkpoints per critique cycle
6. stop gracefully on user interruption
7. choose the best versions to present
8. show the whole process in a dedicated live TUI grid
9. personalize outputs to the user's goals and preferences

---

## Compact product statement

**Libris Autonomous Research Operation** is Charon's native multi-agent research mode for broad research prompts. It launches a visible swarm composed of a research coordinator, researcher agents, judge agents, and bounded research shades. The coordinator scouts the topic space, selects promising leads, and assigns each lead to a researcher–judge pair. Researchers gather evidence and synthesize reports; judges critique those reports from the perspective of the user model and project goals. This loop repeats through checkpointed report versions until quality is sufficient or the user interrupts. At any time, the user can watch the swarm in a specialized TUI grid showing all active sessions in real time. When the run stops, the coordinator selects the strongest report versions for delivery to the user.
