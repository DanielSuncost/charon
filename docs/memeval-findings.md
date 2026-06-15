# memeval: synthetic multi-session retrieval eval — generator + harness (exploratory)

A small, on-device tool for authoring agentic-memory eval data with *ground truth*
and *controllable difficulty*, to localize which axis breaks retrieval. Exploratory:
the surface text is synthetic/templated and the samples are small, so the numbers
below are observations to motivate experiments, not results to cite.

## Components

- **`scripts/memeval_gen.py`** — generates timestamped multi-session conversation
  trajectories with ground truth. Question types: `single_session`,
  `knowledge_update` (an attribute's value changes across sessions → tests
  latest-value), `multi_session_join` (answer needs facts from ≥2 sessions), and
  `temporal` (which was mentioned first). Controllable knobs: session count,
  turns/session, **distractor ratio**, update count, join count. Deterministic per
  `--seed`; **self-validating** (every question must be answerable from its gold
  turns). Presets: `easy` / `medium` / `hard`.
- **`scripts/exp_memeval.py`** — ingests each trajectory into a fresh `MemoryEngine`
  and measures **session-level recall@k per type × per difficulty**, plus a
  vector/FTS/hybrid mode comparison on the hardest type. On-device (bge-base +
  sqlite-vec), no API.

Run: `PYTHONPATH=apps/core-daemon CHARON_EMBED_BACKEND=local python scripts/exp_memeval.py --seeds 3`

## Observations (3 seeds/difficulty; hybrid recall@k — note the small per-cell n)

| difficulty | type | n | R@1 | R@2 | R@3 | R@5 |
|---|---|---|---|---|---|---|
| easy | single_session | 30 | 0.73 | 0.97 | 1.00 | 1.00 |
| easy | knowledge_update | 3 | 0.33 | 1.00 | 1.00 | 1.00 |
| easy | multi_session_join | 3 | 0.33 | 0.67 | 0.83 | 1.00 |
| easy | temporal | 3 | 0.33 | 0.83 | 1.00 | 1.00 |
| medium | single_session | 66 | 0.55 | 0.86 | 0.89 | 0.97 |
| medium | knowledge_update | 9 | 0.33 | 0.33 | 0.89 | 1.00 |
| medium | multi_session_join | 9 | 0.28 | 0.67 | 0.89 | 0.94 |
| medium | temporal | 6 | 0.50 | 0.58 | 0.83 | 1.00 |
| hard | single_session | 120 | 0.51 | 0.78 | 0.88 | 0.91 |
| hard | knowledge_update | 18 | 0.22 | 0.50 | 0.89 | 1.00 |
| hard | multi_session_join | 18 | 0.31 | 0.50 | 0.58 | 0.86 |
| hard | temporal | 12 | 0.21 | 0.46 | 0.75 | 0.92 |

**1. Latest-value retrieval fails at the top rank.** `knowledge_update` R@1 is
0.22–0.33 across difficulties but recovers to ~0.89 by R@3. The engine surfaces
*both* the stale and the current statement and does not rank the latest first —
retrieval alone doesn't resolve supersession. (Mirrors the LongMemEval
knowledge-update weakness, here reproduced under control.)

**2. Multi-session joins degrade with distraction.** The hardest type (the answer
needs *both* gold sessions): R@5 erodes 1.00 → 0.94 → 0.86 from easy to hard as the
distractor ratio rises 0.2 → 0.4 → 0.6. Getting *both* required sessions into top-k
is where memory breaks.

**3. Retrieval-mode comparison on joins — suggestive only, not powered.** On
`multi_session_join` at recall@3:

| difficulty | vector | fts | hybrid |
|---|---|---|---|
| easy | 0.67 | 0.67 | 0.83 |
| medium | 0.83 | 0.67 | 0.89 |
| hard | 0.64 | 0.44 | 0.58 |

The numbers hint that hybrid's value might be difficulty-dependent (helpful when
distraction is low, a drag when it's high). **But the per-cell n is 3–18 over 3
seeds and the gaps are ≈0.05–0.15 — well within noise. This is not a claim.** It is
at most a hypothesis to test at real n (more seeds, ideally on real dialogue rather
than templated text) before stating anything.

## Honest limits

- **Small n** (joins: 3–18 per cell over 3 seeds). Directional, not precise; widen
  seeds before staking magnitudes.
- **Synthetic, templated** surface text is easier than real dialogue — absolute
  recall is optimistic; treat as *relative* difficulty signal, not a benchmark score.
- **Retrieval-only** (gold-session recall). No reader / answer-correctness yet.
- **One embedding model** (bge-base), on-device.

## What this is

Exploratory infrastructure: a way to author synthetic multi-session eval data with
ground truth and dial difficulty to localize failure modes. It reproduces the known
weak spots (latest-value at top rank, multi-session joins under distraction) on
controlled data. It does **not** yet produce a powered, transferable finding — the
surface text is templated and synthetic, and the n is small. Treat it as a tool and
a set of hypotheses, not a result.
