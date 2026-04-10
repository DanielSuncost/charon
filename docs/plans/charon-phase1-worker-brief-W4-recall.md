# Worker Brief W4 — Session Recall and Summarized Search

Parent plan: `docs/plans/charon-autonomous-implementation-master-plan.md`
Status: Ready for autonomous implementation

## Objective
Make prior work retrievable as concise, actionable episodes rather than transcript fragments.

## Scope
Implement:
- hybrid retrieval:
  - FTS/keyword
  - semantic retrieval
- LLM summarization of retrieved episodes
- result schema containing:
  - date/time
  - project
  - agent
  - key files
  - outcome
  - provenance
- filters by project / agent / time
- practical ranking for prior-work recovery

## Must not break
- current Search behavior as a fallback path
- current memory tiers and prompt assembly
- project knowledge / user model tools

## Constraints
- return episodes, not transcript dumps
- no-match path must be clean and non-hallucinatory
- summarization must be bounded and cheap enough for routine use
- provenance must be surfaced

## Required tests
- known prior-fix retrieval returns correct episode
- summary includes files/actions/outcome
- no-match behavior is clean
- retrieval works across realistic multi-session history

## Acceptance benchmark
- “Find how we solved this last time” works reliably

## Deliverables
1. implementation
2. tests
3. short design note
4. explicit list of unchanged public interfaces
5. known limitations
