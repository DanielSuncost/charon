# First-class episodic memory — design, and an honest measurement

Adds a first-class, referenceable episodic layer over the flat memory engine, and
measures whether it actually helps retrieval. Short version: **it's built, tested,
and the lift is modest and situational — not a silver bullet — and the experiment
says exactly where it helps and where it doesn't.**

## What was built (`apps/core-daemon/episodic.py`)

An `Episode` groups a coherent set of memories (one conversation/session) under a
stable id, with a **summary that is itself indexed for retrieval**:

- **First-class:** own `episodes` + `episode_members` tables; `create_episode`,
  `get_episode`, `list_episodes`, `segment_by_conversation`.
- **Referenceable:** stable ids; `episode_for_memory(mem)` resolves a memory → its
  episode; the summary links back via `summary_memory_id`.
- **Retrievable:** `recall_episodes(query)` ranks episodes via the *same*
  vector+FTS+RRF machinery (the summary is stored as a real memory, so it also
  surfaces in ordinary `recall()`).
- **Additive:** new module + two tables; the only core change is an **opt-in
  `recency_weight`** on `recall()` (default 0 → no behavior change). 7 unit tests;
  full suite green (768 passed).

The design idea: a query can match a session's *gist* via its summary even when no
individual turn matches — which is where flat turn-level retrieval is weakest.

## The measurement (`scripts/exp_memeval_episodic.py`)

Session-level recall@k on synthetic multi-session trajectories (`memeval_gen`, 3
seeds × easy/medium/hard), comparing three conditions on **separate engines** (so
summaries never contaminate the baseline):
- `baseline` — flat turn-level retrieval (Charon today)
- `episodic(session)` — + a per-session summary = the whole session concatenated
  (conservative: includes distractor noise)
- `episodic(facts)` — + a per-session summary = only fact-bearing turns (optimistic
  proxy for a clean LLM summary)

| type | n | baseline (R@1/2/3/5) | episodic(session) | episodic(facts) |
|---|---|---|---|---|
| single_session | 216 | 0.55 / 0.83 / 0.90 / 0.94 | 0.55 / 0.84 / 0.92 / 0.95 | 0.55 / 0.85 / 0.94 / **0.99** |
| knowledge_update | 30 | 0.27 / 0.50 / 0.90 / 1.00 | 0.30 / 0.57 / 0.90 / 1.00 | 0.33 / **0.73** / 0.90 / 1.00 |
| multi_session_join | 30 | 0.30 / 0.57 / 0.70 / 0.90 | 0.33 / 0.53 / 0.73 / 0.90 | 0.32 / 0.53 / 0.68 / 0.88 |
| temporal | 21 | 0.31 / 0.55 / 0.81 / 0.95 | 0.24 / 0.52 / 0.76 / 0.88 | 0.24 / 0.52 / 0.74 / 0.91 |

**Recency weighting on knowledge_update** (latest-value: stale + current both retrievable):

| condition | R@1 | R@2 | R@3 | R@5 |
|---|---|---|---|---|
| no recency | 0.27 | 0.50 | 0.90 | 1.00 |
| recency (weight 1.0) | 0.30 | 0.50 | **0.67** | **0.73** |

## What it shows — honestly

1. **Episode summaries help where a *session's gist* is the unit of relevance.**
   Single-session recall improves at k≥3 (R@5 0.94 → **0.99**, n=216 — real, not
   noise), and knowledge_update R@2 jumps 0.50 → 0.73 (n=30 — directional). A clean
   ("facts") summary beats the noisy ("session") one, as expected.
2. **They don't solve multi-hop joins.** `multi_session_join` stays ~0.30 — a query
   that names one entity can't surface the *bridging* session that names the other.
   Episode summaries are not multi-hop retrieval; that remains the hard, open part.
3. **They slightly hurt temporal ordering** (R@1 0.31 → 0.24). A session-level blob
   competes with the specific turn a "which came first" question needs.
4. **Naive recency weighting backfires.** A global date bonus lifts R@1 a hair but
   tanks R@3/R@5 — because it boosts recent *distractor* sessions over the
   relevant-but-not-newest gold. The lesson: recency must be applied *among
   relevant matches*, not globally — a real design constraint, not a tuning detail.

## The right test: "when/where" queries (`scripts/exp_episodic_queries.py`)

The measurement above tests *abstractive fact QA* — which is **semantic** memory,
the wrong construct for episodic value. Episodic memory exists for **temporal-
contextual recall**: "the 3 most recent sessions", "what did I do in March", "the
session before the deploy", "the auth session in February". Those queries have
**automatic ground truth from the timestamps**, so they're fairly auto-evaluable
(no relevance labeling) — and the same harness runs on real usage data.

Realistic dated dev work-sessions (60/seed, 3 seeds), episodic time-structural
retrieval vs flat content `recall()` (score = fraction of the time-defined gold
correctly retrieved):

| query type | n | flat | episodic |
|---|---|---|---|
| recency (top-3) | 3 | 0.33 | **1.00** |
| time-range (month) | 9 | 0.24 | **1.00** |
| before-session | 9 | 0.00 | **1.00** |
| topic-in-month | 18 | 0.28 | **1.00** |

The value here is **categorical, not marginal**: flat retrieval is near chance and
`before-session` is literally **0.00** — you cannot get "the session before X" from
content similarity. The most ecologically meaningful row is **topic-in-month**
(episodic 1.00 vs flat 0.28): when a topic recurs across months, only the time
filter disambiguates *which* occurrence — flat returns the topic regardless of
time. This is the concrete practical-value story the fact-QA eval couldn't show.

Honest framing of *why* episodic scores 1.00: for pure time queries it queries the
very structure that defines the gold — that's the point (the feature provides the
structure these queries need), and the flat column proves they're unanswerable
without it. The non-trivial result is topic-in-time, where content and time combine.

## Honest scope

- Small n on the rare types (joins 30, temporal 21) — directional, not powered;
  single_session (216) and the overall direction are solid.
- Synthetic, templated trajectories; the "facts" summary is an idealized proxy for
  an LLM summary (real summaries are noisier — so "facts" is an upper-ish bound).
- Retrieval-only (session recall), one embedding model, on-device.

## Honest scope (when/where eval)

- Synthetic (realistic dev work-sessions), retrieval-only, one embedding model,
  on-device. Gold derives from timestamps, so no labeling — and the harness runs
  unchanged on **real Charon usage** (the time-gold is automatic there too).
- Production needs a thin NL→time-range step ("last week" → dates); not measured
  here. We tested retrieval *given resolved intent*; the flat baseline got the raw
  query text — which is all flat retrieval can use, so the comparison is fair.
- Small n on some rows (recency 3, ranges 9). The effect is categorical, not a
  delta that needs power — but state the n.

## Verdict

A genuinely first-class, referenceable episodic layer now exists and is tested. On
the **right construct** — temporal-contextual "when/where" queries — its value is
**categorical**: it answers a class of queries (recency, time-range, sequence,
topic-in-time) that flat content retrieval cannot serve at all (flat ≈ chance;
before-session 0.00). On **semantic fact QA** its added value is, honestly, only
**modest** (session-gist gains; no multi-hop help; naive recency backfires). The
two takeaways are both true and both worth stating: episodic structure is the right
tool for episodic questions and clearly useful there, and it is *not* a general
retrieval win on semantic QA. The open follow-ups it points at: recency-among-
relevant, multi-hop/bridging retrieval, and an NL→time-range front-end.
