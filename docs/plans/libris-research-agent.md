# Libris: Research Agent for Charon

## Overview

Libris is a research-first agent system natively integrated into Charon, combining Feynman's evidence-gathering rigor with Charon's unique primitives (judge loops, semantic memory, shade swarms). It transforms Charon from a general-purpose agent into a capable research assistant that can conduct thorough investigations, synthesize literature, and produce cited outputs.

## Design Goals

1. **Native Integration** — No external runtime dependencies; uses Charon's existing tools and orchestration
2. **Judge-Driven Quality** — Research quality is measurable and optimizable via judge loops
3. **Memory-Aware Recall** — Leverages semantic memory to surface prior research on related topics
4. **Parallel Evidence Gathering** — Uses shade swarms for concurrent source collection
5. **Provenance Tracking** — Every claim links to verifiable sources with confidence levels

## Core Components

### 1. Research Tools

New tools added to `.charon/tools/`:

#### `paper_search`
Searches academic papers via alphaXiv API or arXiv. Returns structured results with abstract, authors, venue, and direct URLs.

```python
def paper_search(query: str, limit: int = 10, recency_filter: str = None) -> List[Paper]:
    """Search academic papers by query."""
```

#### `web_research`
Enhanced web search with content extraction capability. Can fetch full page content for deep reading.

```python
def web_research(query: str, include_content: bool = False, limit: int = 10) -> List[Source]:
    """Search web sources with optional full-content retrieval."""
```

#### `synthesize_findings`
Aggregates evidence from multiple sources into a structured brief with consensus points and disagreements.

```python
def synthesize_findings(sources: List[Source], focus_questions: List[str]) -> ResearchBrief:
    """Synthesize research findings from collected sources."""
```

#### `verify_citations`
Checks citation validity, detects dead links, and marks confidence levels.

```python
def verify_citations(brief_path: str) -> VerificationReport:
    """Verify citations in a research brief."""
```

### 2. Shade Contracts

Pre-defined shade patterns for research workflows:

#### `researcher`
Evidence-gathering specialist. Searches papers and web, extracts key claims with URLs, writes to evidence table.

**Tools:** `paper_search`, `web_research`, `read`, `bash`  
**Output:** `outputs/<slug>-evidence.md` with numbered sources and findings

#### `synthesizer`
Reads evidence files, identifies patterns/consensus/disagreements, produces draft brief.

**Tools:** `read`, `write`, `synthesize_findings`  
**Output:** `outputs/<slug>-draft.md`

#### `verifier`
Validates citations, checks for hallucinated sources, marks confidence levels.

**Tools:** `read`, `verify_citations`, `bash`  
**Output:** `outputs/<slug>.provenance.md`

### 3. Judge Loop Integration

Research quality is optimized via judge loops with multiple judge types:

#### Evidence Diversity Judge
Scores research on source variety (papers vs web, primary vs secondary).

```python
judge = AestheticJudge(
    rubric="Source diversity: mix of academic papers, documentation, and primary sources"
)
```

#### Citation Accuracy Judge
Measures percentage of verified citations vs total claims.

```python
judge = QuantitativeJudge(
    command="python verify_citations.py outputs/<slug>.md",
    parse="accuracy_percentage"
)
```

#### Synthesis Quality Judge
LLM scores the brief on clarity, structure, and actionability.

```python
judge = AestheticJudge(
    rubric="Clarity of findings, logical flow, actionable conclusions"
)
```

#### Composite Research Judge
Weighted combination for end-to-end quality:

```python
judge = CompositeJudge([
    (EvidenceDiversityJudge(), 0.3),
    (CitationAccuracyJudge(), 0.4),
    (SynthesisQualityJudge(), 0.3)
])
```

### 4. Memory Patterns

Libris leverages Charon's semantic memory in three ways:

#### Prior Research Recall
Before starting new research, recall prior investigations on related topics:

```python
memories = recall(
    query=f"research on {topic}",
    filter_type="research_brief",
    limit=5
)
```

This surfaces previous findings, avoiding redundant investigation.

#### Source Knowledge Graph
Indexed sources build a knowledge graph over time. Repeatedly-cited papers gain prominence; dead links are tracked.

#### Procedure Capture
Successful research approaches become procedures:

- "For ML topics: start with arXiv, then check GitHub repos"
- "For product research: prioritize official docs over blogs"

These are stored in semantic memory and injected into future research contexts.

## Workflows

### `/research <topic>`
Quick single-pass investigation. Spawns researcher shade, produces brief evidence table.

**Use case:** Fast lookup, initial exploration  
**Output:** `outputs/<slug>-evidence.md`

---

### `/deepresearch <topic>`
Multi-agent workflow with judge loop optimization:

1. **Plan phase** — Define focus questions and source strategy
2. **Evidence gathering** — Parallel researcher shades collect papers + web sources
3. **Synthesis** — Synthesizer shade produces draft brief
4. **Verification** — Verifier shade checks citations, marks confidence
5. **Judge loop** — Iteratively refine until quality target met

**Use case:** Comprehensive analysis requiring high confidence  
**Output:** `outputs/<slug>.md` + `outputs/<slug>.provenance.md`

---

### `/lit <topic>`
Literature review focused on academic papers:

1. Search papers via alphaXiv/arXiv
2. Identify consensus, disagreements, open questions
3. Produce structured literature review with citation matrix

**Use case:** Academic-style literature survey  
**Output:** `papers/<slug>-literature-review.md`

---

### `/audit <paper-or-claim>`
Compare paper claims against codebase or implementation:

1. Extract key claims from paper
2. Verify against actual code/metrics/docs
3. Report mismatches and verified claims

**Use case:** Validate research against reality  
**Output:** `outputs/<slug>-audit.md`

---

### `/compare <topic-a> <topic-b>`
Side-by-side comparison matrix:

1. Research both topics in parallel
2. Extract comparable dimensions
3. Produce structured comparison table

**Use case:** Feature comparison, technology selection  
**Output:** `outputs/<slug>-comparison.md`

## Output Conventions

### File Naming
All outputs use slug-based naming derived from topic (lowercase, hyphens, ≤5 words):

```
outputs/
  <slug>-evidence.md      # Raw evidence table
  <slug>-draft.md         # Synthesis draft
  <slug>.md              # Final brief
  <slug>.provenance.md    # Citation verification sidecar
papers/
  <slug>-literature-review.md  # Academic-style reviews
```

### Evidence Table Format
Standardized format for source tracking:

```markdown
| # | Source | URL | Key Claim | Type | Confidence |
|---|--------|-----|-----------|------|------------|
| 1 | Smith et al. (2024) | https://arxiv.org/... | Scaling laws hold up to 100B params | primary | high |
| 2 | OpenAI Blog | https://openai.com/... | GPT-4 uses mixture of experts | secondary | medium |
```

### Provenance Sidecar
Tracks verification status for each claim:

```markdown
# Provenance: <slug>

## Source Accounting
- Total sources collected: 15
- Sources verified: 12
- Dead links detected: 2
- Confidence distribution: high=8, medium=4, low=0

## Verification Notes
[Source #3] URL redirects to different domain — marked as inferred
[Source #7] No author listed — confidence downgraded to medium
```

## Integration Points with Charon

### Session Grid
Libris workflows appear in the session grid (`charon-tui` F3):
- Researcher shades show progress during evidence gathering
- Judge loop iterations visible as sequential phases
- Final output path displayed on completion

### Autonomous Mode
Research tasks can be proposed autonomously:

```
Agent proposes: "I notice we're discussing scaling laws. 
                 Should I run a deep research to gather latest papers?"
User confirms → /deepresearch "scaling laws 2024"
```

### Memory Bridge
Libris outputs auto-index into semantic memory:
- Research briefs tagged with `type=research_brief`
- Sources indexed for future recall
- Topics linked via embedding similarity

## Implementation Phases

### Phase 1: Core Tools (Week 1)
- [ ] Implement `paper_search` tool with alphaXiv integration
- [ ] Implement `web_research` with content extraction
- [ ] Add evidence table output format to write tool

### Phase 2: Shade Contracts (Week 2)
- [ ] Define researcher shade contract
- [ ] Define synthesizer shade contract
- [ ] Test parallel evidence gathering via batch spawning

### Phase 3: Judge Loop Integration (Week 3)
- [ ] Implement Evidence Diversity Judge
- [ ] Implement Citation Accuracy Judge
- [ ] Wire judge loop into `/deepresearch` workflow

### Phase 4: Memory Integration (Week 4)
- [ ] Auto-index research outputs to semantic memory
- [ ] Add prior research recall to system prompt
- [ ] Implement procedure capture for successful approaches

## Example Usage

```bash
# Quick research
charon /research "transformer alternatives"

# Deep investigation with quality optimization
charon /deepresearch "mechanistic interpretability progress 2024"

# Literature review
charon /lit "RLHF alternatives"

# Audit paper claims against codebase
charon /audit "arxiv:2310.12345"
```

## Success Metrics

1. **Citation Accuracy** — ≥90% of claims have verifiable URLs
2. **Source Diversity** — Mix of ≥3 source types (papers, docs, repos) per brief
3. **Recall Utility** — ≥70% of new research tasks benefit from prior memory recall
4. **Judge Loop Convergence** — Average 3-5 iterations to quality target

---

## Appendix: Feynman Adaptation Notes

### What We Keep
- Evidence table format with numbered sources
- Provenance sidecar concept
- Slug-based file naming
- Four-agent workflow pattern (researcher, synthesizer, verifier, writer)

### What We Improve
- **Judge loops** — Feynman lacks iterative quality optimization; Libris uses judges to refine research until target met
- **Semantic memory** — Feynman's session search is basic; Charon's hybrid retrieval surfaces richer context
- **Parallel gathering** — Feynman is sequential; Libris spawns researcher shades in parallel via batch spawning
- **Native integration** — No external runtime needed; runs as native Charon tools and contracts

### What We Drop
- alphaXiv CLI dependency (we use it as a tool, not a runtime)
- Separate agent binaries (all work within single Charon session)
- Complex package system (skills are just shade contracts in Charon)
