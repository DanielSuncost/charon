# Ablating a memory system's own retrieval features — per-category recall and four honest nulls

A small, reproducible, **on-device** evaluation of Charon's memory retrieval on a
LongMemEval_S subset. The point here is **measurement discipline**: I ablated my
own retrieval features and report where they don't help.

> **Summary:** A reproducible harness that ablates a memory system's
> own features against LongMemEval with per-category recall@k, and several of them —
> RRF "hybrid" fusion, version-chain update detection — deliver **no measurable
> retrieval gain over plain vector search**. Knowing where a feature *doesn't* help is
> where you decide what to actually build.

Reproduce (no API, bge-base + sqlite-vec, ~minutes on a laptop):

```
PYTHONPATH=apps/core-daemon CHARON_EMBED_BACKEND=local \
  python scripts/exp_memory_ablation.py --per-type 12
# -> results/exp_memory_ablation.json   (and _ftsfix.json for the FTS-fixed run)
```

## Per-category recall@1 (vector retrieval) — the real difficulty frontier

n ≈ 12 questions/type, on-device, retrieval-only (session-level recall).

| Category | recall@1 | recall@5 |
|---|---|---|
| single-session-assistant | 1.00 | 1.00 |
| single-session-user | 0.92 | 0.92 |
| single-session-preference | 0.75 | 1.00 |
| temporal-reasoning | 0.51 | 0.91 |
| knowledge-update | 0.50 | 1.00 |
| **multi-session** | **0.27** | 0.96 |

Single-session recall is near-saturated; **multi-session and temporal/knowledge-update
are where retrieval actually struggles** (multi-session R@1 = 0.27). An aggregate
number hides this; the per-category split is the useful artifact.

## Four negative results (each deterministic and reproducible)

**1. "Hybrid" RRF retrieval ≡ vector-only — identical at every k.**

| Mode | R@1 | R@2 | R@3 | R@5 |
|---|---|---|---|---|
| vector-only | 0.657 | 0.839 | 0.922 | 0.964 |
| hybrid + RRF | 0.657 | 0.839 | 0.922 | 0.964 |

The "hybrid retrieval" mode was, on this benchmark, **pure vector retrieval** —
byte-identical recall at every k, even at R@1 where there's ample headroom.

**2. The FTS half was a dead no-op (a bug), and fixing it still doesn't justify the hybrid.**
`_search_fts` joined query terms with implicit **AND**, so every token (incl.
stopwords) had to match → FTS-only recall was **0.0**. After fixing it to
stopword-filtered **OR** (BM25), FTS-only R@1 rose to **0.608** — but hybrid+RRF
(R@1 **0.609**) *still* doesn't beat vector-only (**0.657**); it's a wash and slightly
worse at top rank. Equal-weight RRF lets a weaker sparse signal pollute a stronger
dense ranking. **Conclusion: the hybrid needs confidence-weighted or query-routed
fusion to justify itself — not equal RRF.**

**3. Version-chain "update detection" gives zero retrieval benefit on knowledge-update.**
Indexing with supersession-detection on vs. off yields **identical** recall at every k
on the knowledge-update category (R@1 0.50 / R@2 0.92 / R@3 1.0 / R@5 1.0, both ways).
It may help a reader pick the latest value; it does **not** change retrieval ranking.

**4. Retrieval saturates at R@5 ≈ 0.96** on this subset — headroom is at top-rank
precision (R@1), not coverage.

## Honest scope (what this is NOT)

- **Small n** (≈12 questions/type, one LongMemEval_S subset). These are
  *characterizations*, not benchmark scores; treat magnitudes as indicative.
- **Retrieval-only.** This measures gold-session recall, not end-to-end answer
  correctness. A separate cloud-reader QA number exists elsewhere and is **not** what
  these scripts measure — don't conflate them.
- **One embedding model** (bge-base), on-device.
- The retrieval *mechanisms* (vector + FTS5 + RRF over sqlite-vec) are **standard**;
  nothing here claims a novel retrieval method. The contribution is the
  **evaluation** — per-category ablation and the honest nulls, reproducible end to end.
