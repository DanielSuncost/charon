# Libris Implementation Architecture

> Concrete architecture spec for implementing Libris's autonomous multi-agent research mode in Charon.
>
> Date: 2026-03-27
> Status: Proposed
> Related:
> - `docs/plans/libris-research-agent.md`
> - `docs/plans/libris-implementation-plan.md`
> - `docs/plans/libris-autonomous-research-operation.md`

---

## 1. Scope

This document specifies the implementation architecture for Libris's dominant operating mode:

**Autonomous Research Operation**

In this mode, a broad user prompt launches a coordinator-led research swarm with:
- a top-level research coordinator
- topic-specific researcher/judge pairs
- bounded research shades under each researcher
- checkpointed iterative refinement
- a dedicated TUI swarm view

This architecture is intended to be implementable using Charon's current primitives:
- projects
- tools
- shades
- judge loops
- memory
- TUI session grid

---

## 2. Architectural principles

1. **Projects remain the unit of ownership**
   Libris runs inside normal Charon projects with `kind: research` or `kind: hybrid`.

2. **Artifacts are first-class**
   Every meaningful intermediate and final output is saved to disk.

3. **The swarm is inspectable**
   Agents are visible as sessions in the TUI, not hidden behind one opaque command.

4. **Iteration is checkpointed**
   Every judge cycle produces a durable checkpoint.

5. **The judge is user-aligned, not purely formal**
   It critiques reports against user model + project goals, not just style.

6. **Broad tasks decompose into bounded subproblems**
   Coordinator → researchers → shades.

7. **The system must degrade gracefully**
   If advanced features fail, the run should still produce inspectable files and partial results.

8. **Budget awareness is mandatory**
   Long-running Libris operations must continuously monitor wall time, token usage, cost, and concurrency.

9. **Model selection is policy-driven**
   Coordinator/judge/researcher/shade roles should be assignable to different model tiers, including cheap or local models for routine shade work.

---

## 3. Runtime topology

## 3.1 Primary topology

```text
User prompt
  ↓
Research Coordinator
  ├── Topic Researcher A
  │     ├── Shade A1
  │     ├── Shade A2
  │     └── Judge A
  ├── Topic Researcher B
  │     ├── Shade B1
  │     ├── Shade B2
  │     └── Judge B
  └── Topic Researcher C
        ├── Shade C1
        ├── Shade C2
        └── Judge C
```

## 3.2 Minimal topology

For smaller prompts or constrained resources:

```text
User prompt
  ↓
Researcher
  ├── Shades
  └── Judge
```

## 3.3 Optional global judge

A global judge may exist at the operation level to critique topic selection and final delivery choices, but this is optional for v1. The essential judge is the per-topic judge.

---

## 4. Core runtime objects

## 4.1 Research operation

A top-level runtime record representing one broad Libris run.

Suggested fields:

```json
{
  "operation_id": "rop_20260327_001",
  "project_id": "charon",
  "prompt": "Research the current best topics in reinforcement learning research...",
  "mode": "autonomous_research_operation",
  "status": "running",
  "coordinator_agent_id": "AG-0101",
  "created_at": "...",
  "updated_at": "...",
  "stop_requested": false,
  "selected_topic_ids": ["top_1", "top_2"],
  "delivered_topic_ids": [],
  "budget": {
    "max_wall_hours": 336,
    "max_total_tokens": 5000000,
    "max_total_cost_usd": 50,
    "max_topics": 20,
    "max_checkpoints_per_topic": 12,
    "max_concurrent_researchers": 4,
    "max_concurrent_shades": 16
  },
  "model_policy": {
    "coordinator": "strong",
    "judge": "strong",
    "researcher": "fast",
    "shade": "cheap_local"
  },
  "usage": {
    "input_tokens": 0,
    "output_tokens": 0,
    "total_tokens": 0,
    "estimated_cost_usd": 0.0
  }
}
```

## 4.2 Topic dossier

Represents one promising lead selected for deep investigation.

```json
{
  "topic_id": "top_rl_world_models",
  "operation_id": "rop_20260327_001",
  "slug": "rl-world-models",
  "title": "World-model based RL techniques",
  "why_interesting": "Rapid progress, likely relevant to long-horizon planning goals",
  "status": "researching",
  "researcher_agent_id": "AG-0102",
  "judge_agent_id": "AG-0103",
  "checkpoint_count": 3,
  "best_checkpoint_id": "ckp_003",
  "budget": {
    "max_total_tokens": 800000,
    "max_checkpoints_per_topic": 8
  },
  "model_policy_override": {
    "researcher": "strong",
    "shade": "cheap_local"
  },
  "usage": {
    "total_tokens": 120000,
    "estimated_cost_usd": 1.84
  }
}
```

## 4.3 Checkpoint

Represents one judged report version.

```json
{
  "checkpoint_id": "ckp_003",
  "topic_id": "top_rl_world_models",
  "iteration": 3,
  "report_path": "research/operations/rop_20260327_001/topics/rl-world-models/checkpoints/003-report.md",
  "critique_path": "research/operations/rop_20260327_001/topics/rl-world-models/checkpoints/003-critique.md",
  "summary_path": "research/operations/rop_20260327_001/topics/rl-world-models/checkpoints/003-summary.md",
  "score": 0.84,
  "metrics": {
    "relevance": 0.91,
    "citation_quality": 0.80,
    "actionability": 0.76,
    "novelty": 0.85,
    "user_fit": 0.88
  },
  "selected_by_researcher": false,
  "selected_by_judge": true,
  "created_at": "..."
}
```

---

## 5. Storage architecture

## 5.1 Project root

Libris uses the existing project state root:

```text
.charon_state/projects/<project-id>/
```

## 5.2 Research subtree

Recommended structure:

```text
.charon_state/projects/<project-id>/
  project.json
  KNOWLEDGE.md
  research/
    index.json
    dossier.md
    topics/
    sources/
      sources.jsonl
      snapshots/
    claims.jsonl
    briefs/
    provenance/
    operations/
      <operation-id>/
        operation.json
        coordinator/
          plan.md
          candidate-topics.json
          final-selection.md
        topics/
          <topic-slug>/
            topic.json
            evidence/
            checkpoints/
              001-report.md
              001-critique.md
              001-summary.md
              002-report.md
              002-critique.md
              002-summary.md
            final/
              best-report.md
              best-critique.md
              delivery-note.md
```

## 5.3 Why operations/ exists

The `operations/` subtree captures the runtime shape of a broad research job:
- coordinator planning
- topic selection
- topic-specific checkpoint history
- final delivery choice

This is the key missing layer beyond simple evidence/brief storage.

---

## 6. Agent contracts

## 6.1 Research Coordinator contract

### Mission
Interpret a broad research prompt, scout the landscape, select the most promising leads, launch topic investigations, and choose the best final reports for delivery.

### Inputs
- user prompt
- project metadata
- user model
- project knowledge
- prior relevant briefs and sources

### Responsibilities
- define selection criteria
- perform broad scouting
- create ranked candidate topic list
- decide which topics receive full pipelines
- spawn topic researcher/judge pairs
- monitor progress and interruption state
- select final delivery bundle

### Output files
- `coordinator/plan.md`
- `coordinator/candidate-topics.json`
- `coordinator/final-selection.md`

### Allowed tools
- `Recall`
- `Search`
- `Web`
- `Paper`
- `Research`
- `SpawnShade`
- `SpawnBatch`
- `Read`
- `Write`

---

## 6.2 Researcher contract

### Mission
Produce the strongest report for one selected topic through evidence gathering, synthesis, and iterative revision.

### Responsibilities
- define focus questions for the topic
- spawn bounded shades
- aggregate evidence
- build report drafts
- respond to judge critiques
- maintain topic dossier state

### Output files
- `topics/<slug>/topic.json`
- `topics/<slug>/evidence/*`
- `topics/<slug>/checkpoints/*-report.md`

### Allowed tools
- `Web`
- `Paper`
- `Browser`
- `Research`
- `Read`
- `Write`
- `SpawnShade`
- `SpawnBatch`

---

## 6.3 Judge contract

### Mission
Critique a topic report from the perspective of the user's goals, preferences, and broader project needs.

### Responsibilities
- review each report version
- assign structured scores
- identify missing evidence, weak claims, poor fit, and unclear prioritization
- recommend bounded next-step revisions
- select best checkpoint on interruption or completion

### Output files
- `topics/<slug>/checkpoints/*-critique.md`
- `topics/<slug>/checkpoints/*-summary.md`

### Allowed tools
- `Read`
- `Recall`
- `Search`
- `Citation`
- `Research`
- `Write`

---

## 6.4 Research Shade contract

### Mission
Answer one bounded research subquestion with evidence and source references.

### Responsibilities
- gather a small set of relevant sources
- extract claims carefully
- report uncertainty clearly
- avoid broad synthesis outside assigned scope

### Output files
- evidence notes or source/claim inserts recorded through `Research`

### Allowed tools
- `Web`
- `Paper`
- `Browser`
- `Research`
- `Read`
- `Bash`

---

## 7. Budget and model policy architecture

## 7.1 Operation budgets

Each operation should support explicit long-running constraints:
- `max_wall_hours`
- `max_total_tokens`
- `max_total_cost_usd`
- `max_topics`
- `max_checkpoints_per_topic`
- `max_concurrent_researchers`
- `max_concurrent_shades`

Budget checks should happen at safe boundaries:
- before opening a new topic
- before spawning more shades
- before another judge cycle
- before escalating to stronger models

## 7.2 Usage accounting

Libris should record at least:
- input tokens
- output tokens
- total tokens
- estimated cost
- usage by role
- usage by model
- usage by topic

## 7.3 Model policy

The operation should carry a role-based model policy.

Example:

```json
{
  "coordinator": "strong",
  "judge": "strong",
  "researcher": "fast",
  "shade": "cheap_local"
}
```

The coordinator should be able to adapt this policy under budget pressure, especially by keeping routine shade work on cheap/local models and reserving strong models for coordination, judging, and high-value synthesis.

## 7.4 Budget-aware coordination behavior

When nearing budget exhaustion, the coordinator should prefer:
- continuing only top-value topics
- shrinking shade fanout
- selecting best-so-far checkpoints
- stopping low-value critique cycles
- avoiding expensive escalations unless strategically justified

## 8. Source acquisition architecture

Libris must be source-diverse and domain-aware. It should not rely on generic web search alone.

## 8.1 Source classes

### Technical / ML / CS research
Primary backends should include:
- arXiv
- Semantic Scholar
- OpenAlex
- Crossref
- Papers with Code
- GitHub
- official lab / research blog sources
- curated digests / trending feeds where available

### Nontechnical / broader research
Primary backends should include:
- OpenAlex
- Google Scholar–style scholarly discovery where feasible
- Library of Congress / bibliographic catalogs
- institutional repositories
- government / standards / think-tank sources
- domain-specific indexes such as PubMed, SSRN, RePEc, NBER, JSTOR-like sources where practical

## 8.2 Acquisition layers

### Discovery layer
Find candidate topics, papers, institutions, repos, and source clusters.

### Canonical ingestion layer
Normalize discovered sources into canonical source records with metadata, provenance, and optional snapshots.

### Expansion layer
Use shades to investigate promising sources or source clusters in parallel.

### Synthesis layer
Researchers aggregate source clusters into evidence tables, draft reports, and checkpointed report revisions.

## 8.3 Lead scoring

Promising sources should be scored on:
- recency
- credibility
- novelty
- implementation availability
- citation / influence signals
- user-fit
- primary vs secondary source quality

These scores do not need to be perfect in v1, but Libris should maintain a promising-source shortlist rather than treating all discovered sources equally.

### Promising-source index

Libris should maintain a lightweight promising-source index before canonical ingestion.

Suggested record shape:

```json
{
  "lead_id": "lead_...",
  "operation_id": "rop_...",
  "topic_slug": "vision-language-model-improvements",
  "title": "...",
  "url": "...",
  "source_type": "paper",
  "backend": "arxiv",
  "lead_score": 0.82,
  "subscores": {
    "recency": 0.9,
    "credibility": 0.8,
    "novelty": 0.75,
    "implementation_signal": 0.6,
    "user_fit": 0.85
  },
  "recommended_action": "deep_read"
}
```

This index is what the coordinator should use for topic fanout and what shades should use for source procurement / summary work.

## 8.4 Tool implications

Libris should grow toward at least three source-acquisition tools:
- `Paper` — scholarly paper search and metadata retrieval
- `SourceDiscovery` — broad discovery across digests, repos, official sources, and trend surfaces
- `Scholar` — broader academic / bibliographic discovery for nontechnical research

v1 may begin with `Paper` plus existing `Web`, but the architecture should explicitly target a broader ingestion stack.

## 8.5 v1 source-discovery implementation path

The first practical source-discovery layer should include:
- `Paper` for scholarly search (arXiv, Semantic Scholar, OpenAlex)
- `SourceDiscovery` for broad lead generation across:
  - repos (GitHub)
  - official sources
  - digest / trending surfaces
  - heuristic discovery queries via web search

This gives coordinators and shades a way to find promising source clusters before deep reading and canonical indexing.

## 9. Tool architecture

## 7.1 Required new tools

### A. `Paper`
Academic paper search and retrieval.

Actions:
- `search`
- `get_abstract`
- `get_metadata`
- `extract_pdf_text` (optional if delegated to `Web.extract`)

### B. `Research`
Primary Libris persistence and indexing tool.

Actions:
- `init_operation`
- `init_topic`
- `save_candidate_topics`
- `add_source`
- `add_claim`
- `save_evidence`
- `save_checkpoint`
- `list_checkpoints`
- `mark_best_checkpoint`
- `finalize_delivery`
- `search_sources`
- `search_claims`
- `get_topic_state`
- `request_stop`
- `get_operation_state`

### C. `Citation`
Verification and provenance checking.

Actions:
- `verify_report`
- `check_url`
- `validate_source_refs`
- `build_provenance_sidecar`

## 7.2 Reused existing tools

- `Web`
- `Browser`
- `Search`
- `Recall`
- `SpawnShade`
- `SpawnBatch`
- `SpawnJudgeLoop` (used selectively)
- `Read`
- `Write`
- `Edit`
- `Bash`

---

## 8. Message and file contracts

The architecture should be file-backed even when agents exchange information conversationally.

## 8.1 Candidate topic schema

```json
{
  "topic_id": "top_...",
  "title": "...",
  "slug": "...",
  "summary": "...",
  "why_interesting": "...",
  "relevance_to_user": "...",
  "evidence_strength": "low|medium|high",
  "novelty": "low|medium|high",
  "recommended_action": "ignore|monitor|deep_research"
}
```

## 8.2 Shade result schema

```json
{
  "shade_id": "AG-...",
  "topic_id": "top_...",
  "question": "...",
  "summary": "...",
  "source_ids": ["src_1", "src_2"],
  "claim_ids": ["clm_1", "clm_2"],
  "confidence": "medium",
  "open_questions": ["..."]
}
```

## 8.3 Judge critique schema

```json
{
  "topic_id": "top_...",
  "iteration": 2,
  "overall_score": 0.81,
  "scores": {
    "relevance": 0.90,
    "citation_quality": 0.78,
    "actionability": 0.74,
    "novelty": 0.86,
    "user_fit": 0.88
  },
  "strengths": ["..."],
  "weaknesses": ["..."],
  "required_fixes": ["..."],
  "optional_improvements": ["..."],
  "stop_condition_met": false
}
```

## 8.4 Checkpoint topline summary schema

```json
{
  "checkpoint_id": "ckp_002",
  "topic_id": "top_...",
  "iteration": 2,
  "topline": "Strong topical coverage, but still weak on implementation readiness and benchmarks.",
  "best_qualities": ["..."],
  "major_flaws": ["..."],
  "structure_assessment": "clear but incomplete",
  "recommended_next_action": "gather benchmark and repo evidence before next revision"
}
```

---

## 9. Orchestration flow

## 9.1 Start flow

When the user issues a broad Libris prompt:

1. resolve project context
2. create `operation_id`
3. `Research.init_operation`
4. spawn coordinator session
5. switch or offer switch to Libris TUI view
6. coordinator writes plan + candidate topics
7. coordinator selects leads
8. for each selected lead:
   - `Research.init_topic`
   - spawn researcher
   - spawn paired judge

## 9.2 Topic loop

For each topic:

1. researcher defines focus questions
2. researcher spawns shades
3. shades gather evidence and persist it
4. researcher assembles report draft
5. judge critiques report
6. `Research.save_checkpoint`
7. if stop condition not met:
   - researcher revises
   - loop continues

## 9.3 Interruption flow

When the user stops the run:

1. set `operation.stop_requested = true`
2. coordinator, researchers, and judges check stop flag at safe boundaries
3. each researcher nominates best checkpoint
4. each judge nominates best checkpoint
5. coordinator selects delivery bundle
6. operation marked `stopped` or `completed_partial`

---

## 10. Stop and convergence logic

## 10.1 Topic stop conditions

A topic investigation may stop when:
- judge marks quality target as met
- max iterations reached
- no meaningful improvement over N iterations
- user interruption requested
- source exhaustion or evidence dead-end

## 10.2 Operation stop conditions

The full operation may stop when:
- all selected topics complete
- user interrupts
- budget/time limit reached
- coordinator decides broad scouting quality is insufficient for more fanout

## 10.3 Best-version selection

Each topic should support at least three notions of best:
- latest checkpoint
- highest-scoring checkpoint
- best-for-user-fit checkpoint

The coordinator chooses which to deliver.

---

## 11. TUI architecture

## 11.1 Libris swarm view

Add a specialized research view in the TUI that renders the active operation as a grid.

### Each tile should show
- agent name / id
- role (`coordinator`, `researcher`, `judge`, `shade`)
- assigned topic
- current phase
- status
- latest activity line
- current iteration/checkpoint
- current score if applicable

## 11.2 Grouping

The view should support:
- grouping by topic
- grouping by hierarchy
- collapsing shades
- highlighting blocked or critique-waiting agents

## 11.3 Actions

The user should be able to:
- open a session
- inspect checkpoint files
- inspect critique summaries
- stop/pause the run
- optionally reprioritize topics
- optionally mark a checkpoint as preferred

## 11.4 Data feed

The TUI should consume structured status events emitted from:
- operation state updates
- topic state updates
- checkpoint saves
- judge critiques
- spawn/complete events

---

## 12. State/event model

Add an append-only event log for operations.

Suggested path:

```text
research/operations/<operation-id>/events.jsonl
```

Example events:
- `operation_started`
- `candidate_topics_written`
- `topic_selected`
- `researcher_spawned`
- `judge_spawned`
- `shade_spawned`
- `shade_completed`
- `checkpoint_saved`
- `best_checkpoint_nominated`
- `delivery_selected`
- `operation_stopped`

Why:
- powers TUI live updates
- enables replay/debugging
- supports auditability

---

## 13. Memory integration

## 13.1 Inputs from memory

Coordinator and judges should consume:
- user model
- project knowledge
- prior research briefs
- prior topic summaries

## 13.2 Outputs to memory

Only stable outputs should be promoted to memory:
- final selected reports
- durable strategic conclusions
- procedure learnings
- high-value source summaries

Do not automatically promote:
- every checkpoint
- every intermediate critique
- every raw extraction

---

## 14. Recommended implementation sequence

## Stage 1: Storage + runtime records
- define operation/topic/checkpoint schemas
- implement `Research.init_operation`, `init_topic`, `save_checkpoint`
- implement `operations/` filesystem layout

## Stage 2: Coordinator flow
- broad topic scouting
- candidate topic persistence
- lead selection
- spawn researcher/judge pairs

## Stage 3: Topic loop
- shade spawning
- evidence persistence
- report generation
- judge critique persistence
- checkpoint creation

## Stage 4: Interruption + best-version selection
- stop flag handling
- researcher/judge nomination
- coordinator final selection

## Stage 5: TUI swarm view
- live grid
- state/event feed
- checkpoint inspection affordances

## Stage 6: Quality optimization
- judge scoring improvements
- optional use of `SpawnJudgeLoop`
- convergence heuristics

---

## 15. Minimum viable implementation

Libris MVP for this mode is complete when:

1. a broad research prompt creates an operation record
2. a coordinator produces a candidate topic list
3. selected topics spawn researcher/judge pairs
4. researchers can spawn bounded shades
5. each judge cycle saves a checkpointed report + critique
6. interruption causes best-version nomination and final selection
7. the TUI can show the coordinator, researchers, judges, and shades in a live grid

---

## 16. Stretch features

Later improvements:
- global operation-level judge
- automatic topic clustering
- stronger entity graphing
- report diff visualization in TUI
- user steering controls during runtime
- best-of-N synthesis for report delivery
- cross-topic deduplication and merge suggestions

---

## 17. Compact architecture statement

Libris implements broad research prompts as a coordinator-led multi-agent operation. A research coordinator scouts the topic space, selects promising leads, and launches a researcher–judge pair for each lead. Each researcher decomposes the topic into bounded shade tasks, aggregates evidence, and iteratively revises a report. Each judge critiques the report using the user model and project goals, producing structured scores and checkpoint summaries. Every critique cycle saves a durable checkpoint. The entire swarm is visible in a dedicated TUI grid, and when the run stops, the coordinator selects the strongest checkpointed reports for delivery.
