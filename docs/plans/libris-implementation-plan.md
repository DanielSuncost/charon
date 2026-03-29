# Libris Implementation Plan

> Concrete implementation plan for shipping Libris as Charon's native research system, while keeping research work organized within the existing project model.
>
> Date: 2026-03-27
> Status: Proposed
> Parent design: `docs/plans/libris-research-agent.md`

---

## 1. Executive Summary

Libris should be implemented as a **research capability layered onto Charon's existing project abstraction**, not as a wholly separate top-level `ResearchProject` primitive in v1.

### Core decision

Use:
- **regular Charon projects** as the canonical unit of ownership, memory, tasks, and UI
- **project metadata** to distinguish research work:
  - `kind: software | research | hybrid`
  - `research_mode: exploratory | literature | audit | compare | product`
- a standardized **`research/` artifact subtree** inside each project
- a **cross-project source/claim index** for recall and reuse

This gives us:
- zero duplication of the project model
- compatibility with current memory and dashboard systems
- support for both pure research dossiers and implementation-linked research
- a migration path to a dedicated `ResearchProject` primitive later if needed

---

## 2. Product Goals

Libris v1 should let a user:

1. Run `/research <topic>` and get a usable evidence table
2. Run `/deepresearch <topic>` and get:
   - a draft brief
   - a final brief
   - provenance / citation validation output
3. Reuse prior research across sessions and projects
4. Organize research artifacts so they remain inspectable, editable, and searchable
5. Run research either:
   - inside a software project (e.g. `charon`)
   - as a standalone research project (e.g. `mechanistic-interpretability-2024`)

### Non-goals for v1

- full knowledge graph infrastructure
- complex entity resolution across the entire filesystem
- highly polished citation formatting engines
- external runtime dependency on a separate research framework
- automatic perfect deduplication of all sources/claims

---

## 3. Key Architectural Decision: Project Model

## Recommendation

**Do not introduce a separate `ResearchProject` base abstraction in v1.**

Instead, extend the current project concept with typed metadata and research-specific directories.

### Why

Charon already uses projects as the unit for:
- project knowledge
- agent coordination
- dashboard/session grouping
- long-horizon continuity
- task ownership
- memory scoping

Creating a separate top-level research object would force parallel implementations for:
- project listing
- metadata storage
- dashboard rendering
- agent assignment
- memory routing
- artifact organization
- indexing

That would slow delivery and likely create confusion for hybrid efforts like "build Libris in Charon", where research and implementation belong together.

### Decision rule

Use **project metadata** rather than separate types.

Example:

```json
{
  "id": "charon",
  "name": "Charon",
  "kind": "hybrid",
  "research_mode": "product",
  "tags": ["agents", "research", "tooling"]
}
```

Standalone dossier:

```json
{
  "id": "mechanistic-interpretability-2024",
  "name": "Mechanistic Interpretability 2024",
  "kind": "research",
  "research_mode": "literature",
  "tags": ["ml", "papers", "interp"]
}
```

---

## 4. Data Model

## 4.1 Project metadata

Add `project.json` under each project state directory.

Suggested schema:

```json
{
  "id": "charon",
  "name": "Charon",
  "kind": "software",
  "research_mode": null,
  "status": "active",
  "root_path": "/home/dopppo/Projects/charon",
  "linked_paths": [],
  "parent_project_id": null,
  "tags": ["agents", "local-first"],
  "summary": "Persistent multi-agent coding system",
  "created_at": "...",
  "updated_at": "..."
}
```

New fields:
- `kind`: `software | research | hybrid`
- `research_mode`: optional specialization for Libris workflows
- `tags`: lightweight discovery and filtering
- `summary`: dashboard/search preview

## 4.2 Research artifact model

Inside each project, add a standard subtree:

```text
.charon_state/projects/<project-id>/
  project.json
  KNOWLEDGE.md
  research/
    dossier.md
    questions.md
    claims.jsonl
    entities.jsonl
    index.json
    briefs/
    evidence/
    provenance/
    sources/
      sources.jsonl
      snapshots/
```

### Artifact types

#### Source record
Canonical record of a retrieved source.

```json
{
  "source_id": "src_...",
  "project_id": "charon",
  "topic_slug": "libris-indexing",
  "url": "https://...",
  "title": "...",
  "source_type": "paper",
  "authors": ["..."],
  "published_at": null,
  "retrieved_at": "...",
  "snapshot_path": "research/sources/snapshots/...md",
  "content_hash": "...",
  "credibility": "high",
  "tags": ["libris", "research"]
}
```

#### Claim record
Normalized extracted claim with provenance.

```json
{
  "claim_id": "clm_...",
  "project_id": "charon",
  "topic_slug": "libris-indexing",
  "source_id": "src_...",
  "text": "...",
  "confidence": "medium",
  "stance": "supports",
  "entity_refs": ["ent_..."],
  "created_at": "..."
}
```

#### Brief record
Represents a synthesis artifact.

```json
{
  "brief_id": "brf_...",
  "project_id": "charon",
  "topic_slug": "libris-indexing",
  "focus_questions": ["..."],
  "source_ids": ["src_1", "src_2"],
  "claim_ids": ["clm_1", "clm_2"],
  "draft_path": "research/briefs/libris-indexing-draft.md",
  "final_path": "research/briefs/libris-indexing.md",
  "provenance_path": "research/provenance/libris-indexing.provenance.md",
  "judge_scores": {
    "citation_accuracy": 0.92,
    "diversity": 0.78,
    "synthesis": 0.84
  },
  "created_at": "...",
  "updated_at": "..."
}
```

#### Entity record
Optional v1.1, but useful to define now.

```json
{
  "entity_id": "ent_...",
  "name": "Libris",
  "entity_type": "project",
  "aliases": ["research agent for Charon"],
  "project_ids": ["charon"],
  "source_ids": ["src_1"]
}
```

---

## 5. Indexing Strategy

Libris needs three levels of indexing.

## 5.1 Project index

Purpose:
- list all projects
- filter by `kind`
- show active research efforts in dashboard/TUI

Backed by:
- `project.json` files
- optionally mirrored to SQLite later

Fields:
- `id`
- `name`
- `kind`
- `research_mode`
- `status`
- `summary`
- `tags`
- `updated_at`

## 5.2 Per-project research index

Purpose:
- quick lookup of briefs, evidence files, and sources within one project
- avoid expensive filesystem scans on every command

Backed by:
- `research/index.json`

Fields:
- dossier topics
- latest evidence files
- latest briefs
- source counts by type
- claim counts
- last run status

## 5.3 Cross-project research index

Purpose:
- prior research recall
- source deduplication
- cross-project discovery
- "have we researched this already?"

Backed by:
- new SQLite tables preferred
- fallback JSONL mirrors for inspectability if desired

Suggested tables:
- `research_sources`
- `research_claims`
- `research_briefs`
- `research_entities` (optional initially)
- `research_topic_links`

### v1 principle

The global index should support:
- lookup by URL / title / hash
- lookup by topic slug / project id
- text search over source titles, claim text, brief summaries

### Search modes

1. **exact / structural**
   - URL match
   - project id match
   - source id match
2. **FTS5 keyword search**
   - titles
   - claims
   - summaries
3. **semantic recall**
   - layered on later through the existing recall engine

---

## 6. Tooling Plan

Libris should mostly be built as native Charon tools under `apps/core-daemon/tools/`.

## 6.1 New tools

### `paper_tool.py`
Tool name: `Paper`

Actions:
- `search`
- `get_abstract`
- `get_pdf_text` (optional if URL/PDF extraction already covers this)

Preferred provider order:
- arXiv API
- alphaXiv if useful and stable

v1 output:
- structured paper results
- title, authors, abstract, URL, published date

### `research_tool.py`
Tool name: `Research`

This becomes the high-level orchestration and indexing tool.

Actions:
- `init_topic`
- `add_source`
- `add_claim`
- `list_topics`
- `build_evidence_table`
- `save_brief`
- `index_project`
- `search_sources`
- `search_claims`
- `get_topic`

Responsibility:
- normalize topic slug
- ensure `research/` tree exists
- create/update source and claim records
- maintain `research/index.json`
- optionally mirror into SQLite

### `citation_tool.py`
Tool name: `Citation`

Actions:
- `verify_brief`
- `check_url`
- `verify_source_ids`
- `report`

Responsibility:
- dead link detection
- unresolved citation references
- confidence downgrade rules
- provenance sidecar generation

## 6.2 Reuse existing tools

Existing tools that Libris should compose rather than replace:
- `Web` — search + extract
- `Browser` — JS-heavy sites and interactive pages
- `Search` — prior conversation keyword search
- `Recall` — semantic memory retrieval
- `SpawnShade` / `SpawnBatch` — concurrent evidence gathering
- `SpawnJudgeLoop` — iterative quality optimization
- `Read` / `Write` / `Edit` / `Bash` — normal artifact handling

---

## 7. Workflow Plan

## 7.1 `/research <topic>`

Goal: fast single-pass research.

Flow:
1. resolve current project context
2. initialize topic dossier under `research/`
3. optionally recall prior related briefs
4. run web search + paper search
5. extract top sources
6. write evidence table
7. index sources and claims
8. update `research/index.json`

Outputs:
- `research/evidence/<slug>-evidence.md`
- source records in `research/sources/sources.jsonl`
- claim records in `research/claims.jsonl`
- updated `research/dossier.md`

## 7.2 `/deepresearch <topic>`

Goal: high-confidence multi-step workflow.

Flow:
1. initialize topic and focus questions
2. recall prior research
3. spawn parallel researcher shades
4. aggregate evidence
5. synthesize draft
6. verify citations
7. run judge loop
8. publish final brief
9. index final artifacts globally

Outputs:
- `research/evidence/<slug>-evidence.md`
- `research/briefs/<slug>-draft.md`
- `research/briefs/<slug>.md`
- `research/provenance/<slug>.provenance.md`

## 7.3 `/lit <topic>`

Goal: paper-heavy literature review.

Specialization:
- prioritize `Paper.search`
- extract consensus / disagreements / open questions
- produce citation matrix

## 7.4 `/audit <paper-or-claim>`

Goal: verify research against a codebase or implementation.

Specialization:
- extract key claims from source material
- compare against code/docs/tests/metrics
- mark supported, contradicted, unresolved

---

## 8. Shade Contracts

Define fixed role prompts/contracts for:

## 8.1 `researcher`

Responsibilities:
- gather sources
- extract claims verbatim where possible
- preserve URLs and provenance
- avoid unsupported synthesis

Allowed tools:
- `Paper`
- `Web`
- `Browser`
- `Read`
- `Bash`
- `Research`

Outputs:
- source records
- evidence markdown
- claim records

## 8.2 `synthesizer`

Responsibilities:
- read evidence artifacts
- identify agreement/disagreement/open questions
- produce draft brief with source references

Allowed tools:
- `Read`
- `Write`
- `Research`

## 8.3 `verifier`

Responsibilities:
- check that cited source ids exist
- check URLs resolve
- ensure summary claims map to evidence
- produce provenance report

Allowed tools:
- `Read`
- `Bash`
- `Citation`
- `Research`

---

## 9. Memory and Recall Plan

Libris should integrate with Charon's existing memory stack rather than inventing a separate one.

## 9.1 Project knowledge

Research conclusions that are durable and project-relevant should be summarized into:
- `.charon_state/projects/<id>/KNOWLEDGE.md`

Rule:
- raw evidence does **not** go into project knowledge
- stable conclusions, decisions, and recommended procedures do

Example:
- "For product research, prioritize official docs over blogs"
- "Prior work on browser automation found Playwright fallback acceptable but slower than CDP"

## 9.2 Recall integration

Index these into semantic memory when available:
- final briefs
- stable conclusions
- procedure learnings
- important source summaries

Do **not** aggressively index:
- every raw extraction chunk
- every intermediate draft
- noisy duplicate claims

## 9.3 Search integration

Add FTS5 coverage for:
- brief titles
- topic slugs
- source titles
- claim text

This should work even if semantic memory is unavailable.

---

## 10. Filesystem Layout

## 10.1 Standalone research project

```text
.charon_state/projects/mechanistic-interpretability-2024/
  project.json
  KNOWLEDGE.md
  research/
    dossier.md
    questions.md
    claims.jsonl
    entities.jsonl
    index.json
    evidence/
      transformer-circuits-evidence.md
    briefs/
      transformer-circuits-draft.md
      transformer-circuits.md
    provenance/
      transformer-circuits.provenance.md
    sources/
      sources.jsonl
      snapshots/
        src_001.md
        src_002.md
```

## 10.2 Hybrid software + research project

```text
.charon_state/projects/charon/
  project.json
  KNOWLEDGE.md
  research/
    dossier.md
    evidence/
    briefs/
    provenance/
    sources/
```

This avoids splitting product work from supporting research work.

---

## 11. Implementation Phases

## Phase 0 — Foundation Decisions (1–2 days)

Deliverables:
- approve the project model decision (`kind: research` / `kind: hybrid`)
- finalize directory layout
- finalize source/claim/brief schemas
- finalize command semantics for `/research`, `/deepresearch`, `/lit`, `/audit`

Acceptance criteria:
- one agreed doc covering metadata, layout, and index strategy
- no open design ambiguity about project vs research project in v1

## Phase 1 — Project Metadata + Research Layout (2–3 days)

Work:
- add `project.json` support
- add project metadata loader helpers
- add `kind` and `research_mode`
- add research directory initialization helpers
- add `research/index.json` generation

Files likely touched:
- project/state management modules
- dashboard project listing logic
- any project discovery code

Acceptance criteria:
- a project can be marked `research` or `hybrid`
- Libris can initialize a research subtree for a project
- TUI/dashboard can display project kind

## Phase 2 — Core Research Persistence (3–5 days)

Work:
- implement `Research` tool
- implement source/claim/brief persistence
- implement snapshot writing
- implement evidence table generation
- implement per-project reindexing
- implement lead scoring and a promising-source index for coordinator/researcher scouting

Acceptance criteria:
- can create a topic dossier and persist source records
- can regenerate `research/index.json`
- evidence tables are stable and inspectable
- promising sources can be ranked and stored before deep ingestion

## Phase 3 — Academic Search (2–4 days)

Work:
- implement `Paper` tool
- support arXiv search
- support Semantic Scholar metadata lookup/search where feasible
- support OpenAlex metadata lookup/search where feasible
- normalize paper metadata into source records
- optionally support PDF extraction via existing Web extraction path

Acceptance criteria:
- `/lit` can retrieve and store paper results
- paper sources appear in the same canonical source index as web sources
- Libris can retrieve scholarly results from more than one source backend

## Phase 4 — Citation Verification (2–4 days)

Work:
- implement `Citation` tool
- verify URL reachability
- verify source id references in briefs
- generate provenance sidecar
- define downgrade rules for low-confidence citations

Acceptance criteria:
- a brief can be checked automatically
- provenance sidecar reports verified / unverified / dead links

## Phase 5 — Orchestration Commands (3–5 days)

Work:
- add `/research`
- add `/deepresearch`
- add `/lit`
- add `/audit`
- wire command routing to Libris flows
- add Libris intake for explicit research goals and stopping conditions
- map stopping conditions into structured operation budgets

Acceptance criteria:
- commands create expected artifacts under the active project
- failures are recoverable and leave inspectable intermediate files
- Libris can ask for a clearer research standard when the user prompt is underspecified

## Phase 6 — Shade Workflows (3–5 days)

Work:
- define `researcher`, `synthesizer`, `verifier` contracts
- use batch spawning for parallel source gathering
- standardize handoff files and paths

Acceptance criteria:
- `/deepresearch` can run evidence gathering in parallel
- each shade writes to agreed artifact paths without collisions

## Phase 7 — Judge Loop Integration (2–4 days)

Work:
- implement research judges:
  - evidence diversity
  - citation accuracy
  - synthesis quality
- define composite score
- wire judge loop into `/deepresearch`

Acceptance criteria:
- deep research can iterate toward a target quality score
- score breakdown is visible in outputs or logs

## Phase 8 — Memory + Cross-Project Recall (3–5 days)

Work:
- global research SQLite tables or equivalent index
- index final briefs and canonical source metadata
- integrate with `Recall`
- add keyword search over claims and briefs

Acceptance criteria:
- prior research can be surfaced for related topics
- duplicate URLs can be detected across projects
- users can find research outputs beyond one project

## Phase 9 — TUI / UX Surfacing (2–4 days)

Work:
- show project kind in project lists
- show research outputs in session/dashboard views
- optionally add a research summary panel or command help

Acceptance criteria:
- users can tell which projects are research/hybrid
- output paths are discoverable from the UI

---

## 12. Risks and Mitigations

## Risk 1: Too much schema too early

Mitigation:
- keep records minimal in v1
- prefer append-only JSONL + simple SQLite tables
- avoid mandatory entity graphing initially

## Risk 2: Duplicate/low-quality source ingestion

Mitigation:
- canonicalize URLs where possible
- use content hashes on snapshots
- mark duplicates instead of trying to perfectly prevent them

## Risk 3: Research artifacts become noisy and unmaintained

Mitigation:
- separate raw evidence from final briefs
- only index stable outputs globally
- keep dossier and `research/index.json` as curated summaries

## Risk 4: Hybrid projects become confusing

Mitigation:
- make topic-level artifacts explicit under `research/`
- keep implementation outputs outside `research/`
- add clear `kind` metadata to project descriptors

## Risk 5: Over-dependence on semantic memory

Mitigation:
- require FTS5 / structured indexing to work first
- make semantic recall optional enhancement, not hard dependency

---

## 13. Acceptance Criteria for Libris v1

Libris v1 is done when all of the following are true:

1. A user can mark a project as `research` or `hybrid`
2. `/research <topic>` creates a dossier with evidence and indexed sources
3. `/deepresearch <topic>` creates draft, final brief, and provenance outputs
4. web and paper sources are stored in one canonical source model
5. claims are stored with provenance references
6. final research outputs are searchable across projects
7. prior briefs can be recalled for related topics
8. project knowledge can capture stable lessons from research
9. all artifacts remain human-readable and editable on disk

---

## 14. Suggested Build Order for Charon Specifically

If we want fastest path to usable value, implement in this order:

1. **project metadata + `research/` layout**
2. **Research tool persistence layer**
3. **lead scoring + promising-source index**
4. **Paper + SourceDiscovery ingestion**
5. **`/research` command using existing Web tool**
6. **citation/provenance verification**
7. **parallel shades for `/deepresearch`**
8. **judge loop optimization**
9. **cross-project recall/indexing improvements**
10. **TUI polish**

This sequence ensures Libris becomes useful early, before the full "native research swarm" vision is complete.

---

## 15. Future Upgrade Path

If v1 reveals that research truly needs a separate lifecycle, we can later promote research dossiers into a dedicated abstraction.

Trigger conditions for introducing `ResearchProject` later:
- different permissions / access rules
- different UI mode from normal projects
- different memory semantics
- need for many nested dossiers with independent lifecycle state
- strong evidence that metadata-on-project is no longer sufficient

Until then, the simpler model should hold.
