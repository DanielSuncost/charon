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

## Honest scope

- Small n on the rare types (joins 30, temporal 21) — directional, not powered;
  single_session (216) and the overall direction are solid.
- Synthetic, templated trajectories; the "facts" summary is an idealized proxy for
  an LLM summary (real summaries are noisier — so "facts" is an upper-ish bound).
- Retrieval-only (session recall), one embedding model, on-device.

## Verdict

A genuinely first-class, referenceable episodic layer now exists and is tested.
Its measured value is **modest and targeted** (session-gist retrieval), with two
honest negatives (no multi-hop help; naive recency backfires) that point at the
*next* real work: recency-among-relevant, and multi-hop/bridging retrieval for the
joins. The point of building it behind the eval was to know which of those claims
we can make — and the answer is "a small, real gain on session-gist queries,"
nothing more, stated with the numbers to back it.
