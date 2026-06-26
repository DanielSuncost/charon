# Plan: from a temporal-query test to a credible agentic-memory benchmark

We have a small, working temporal/episodic-query test (`exp_episodic_queries.py`)
with a useful property — **ground truth derived from timestamps, so it auto-grades
and runs on real usage data**. This is the plan to grow it into something a
benchmarking team would take seriously. The guiding principle is the gap it
exploits: existing agentic-memory benchmarks (LongMemEval and kin) lean on
*abstractive fact QA* (semantic memory) and under-test the **temporal/episodic and
procedural** axes. That gap is the contribution.

## What exists today (the seed)

- 4 query types over synthetic dated work-sessions — recency, time-range,
  sequence (before/after), topic-in-time — graded by timestamp-derived gold.
- Result: episodic retrieval exact (1.00), flat content retrieval ≈ chance.
- The same harness runs unchanged on real episode data (now that task completions
  create first-class episodes).

## Phase 1 — Make the data real and harder (cheap, high-credibility)

1. **Run on real usage.** Point the harness at an actual Charon `MemoryEngine`
   (your own history, privately). Time-structural gold is automatic — no labeling.
   This converts "synthetic but realistic" into "demonstrated on real sessions."
2. **Public realistic corpora.** Add adapters for public multi-session agent traces
   / conversation logs with timestamps (so others can reproduce without private
   data). Keep the harness public, the private data private.
3. **Harder distributions.** Scale to hundreds of sessions, recurring topics,
   overlapping windows, bursty vs sparse activity — and report degradation curves,
   not single numbers.

## Phase 2 — Close the realism gaps in the queries

4. **Natural-language temporal resolution as a measured component.** Today we feed
   resolved date ranges. Add an NL→time-range step ("last week", "two sessions
   ago", "around the auth refactor") and **score it separately** — parsing error vs
   retrieval error must be distinguishable.
5. **More episodic query types:** co-occurrence ("what else was happening when X"),
   durative ("the period I worked on Y"), multi-constraint (topic ∧ time ∧ agent),
   and counterfactual-free relative time.
6. **Procedural queries.** Add a procedural track: given a goal, does the system
   retrieve the right learned procedure, and does success-weighting demote bad
   ones? Then the harder, real question (Phase 4): does *reusing* a procedure
   improve task success / reduce steps?

## Phase 3 — Grade answers, not just retrieval

7. **End-to-end QA.** Add a reader step and grade **answer correctness** for the
   temporal/contextual questions (with an LLM-judge + the timestamp gold as a
   check), not only episode-recall@k. This measures whether an agent can actually
   *answer* "when did we decide X", which is the user-facing payoff.
8. **Metrics suite:** episode-recall@k, temporal precision/coverage, NL-parse
   accuracy, end-to-end answer accuracy, and latency — reported **per query type**,
   because the failure modes differ by type.

## Phase 4 — Baselines and the comparative claim

9. **Baselines on the temporal subset:** flat RAG, a LongMemEval-style hybrid, a
   recency-only heuristic, and the episodic layer. The expected (and useful) story:
   semantic systems collapse on temporal/sequence queries; episodic structure is
   required. Quantify *how much*.
10. **Procedural-reuse experiment:** run an agent on a task family with vs without
    procedural memory; measure success rate and step count. This is the one that
    turns "we store procedures" into "procedures help," and it ties to Charon's
    verifiable-reward/judge loop (success is gradable).

## Phase 5 — Package as a benchmark others can run

11. **Harness + datasets + leaderboard format:** a CLI, versioned task specs,
    reference results, and a scoring script — the open-source-eval-harness
    deliverable. Document the construct distinction (semantic vs episodic vs
    procedural) explicitly, since conflating them is the field's current blind spot.

## Honesty guardrails (so every claim survives scrutiny)

- **State the construct.** Don't call temporal-query performance "memory recall";
  it's a *different axis* than abstractive QA. The contribution is testing that axis.
- **Separate parse from retrieve from read.** Report them independently.
- **Auto-gold is a strength and a limit:** timestamp-derived gold is objective for
  structural queries but doesn't cover content-relevance judgments (topic-in-time
  needs some human/LLM adjudication at scale) — say so.
- **Synthetic vs real:** label every table. Lead with real-usage numbers once
  Phase 1 lands.
- **Don't claim procedural value until Phase 4** — retrieval/reinforcement mechanics
  working is not the same as procedures improving outcomes.

## Why this is the right bet for an agentic-memory audience

It's exactly the JD's center of gravity — *designing novel benchmark tasks and
evaluation methodologies for semantic/episodic/procedural memory across multi-
session trajectories* — built on a real, auto-gradable seed, with a credible path
to public datasets and an open harness, and an explicit thesis (existing benchmarks
under-test the temporal and procedural axes) that a reviewer can immediately judge.
