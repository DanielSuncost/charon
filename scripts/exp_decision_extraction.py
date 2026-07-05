#!/usr/bin/env python3
"""Decision extraction — labeled precision/recall eval (roadmap §4.1, auto-capture).

`decision_extract.extract_decisions` claims to find committed decisions (and their
rationale) in agent output. This grades that claim on a hand-labeled corpus of
positive decision statements (varied phrasings, several beyond the extractor's
known patterns) and negatives including hard collisions ("picked apart", "went
with the flow", third-party "the vendor decided", negations, hedges, questions).

Honest scope: the corpus is small (50 sentences) and author-written — it measures
phrasing coverage and false-positive resistance of the heuristic, NOT performance
on the real distribution of agent output. The real-distribution check is step 4
(run over actual Charon traces) and the misses printed below.

  PYTHONPATH=apps/core-daemon python scripts/exp_decision_extraction.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "core-daemon"))

from decision_extract import extract_decisions  # noqa: E402

# (sentence, gold) — gold is None for negatives, else {"what": required substring
# of the extracted decision, "why": required substring of the extracted rationale
# or None when no explicit rationale clause exists}.
LABELED = [
    # ---- positives: committed decisions an agent might write ----
    ("We decided to use Postgres for the ledger because we need real transactions.",
     {"what": "Postgres", "why": "real transactions"}),
    ("After comparing the two, I went with tokio over async-std since the ecosystem is bigger.",
     {"what": "tokio", "why": "ecosystem is bigger"}),
    ("Settled on a monorepo layout.", {"what": "monorepo", "why": None}),
    ("Decision: cap retries at 3 — anything more just hides real outages.",
     {"what": "cap retries at 3", "why": None}),
    ("I chose SQLite for the cache layer, as it removes a service dependency.",
     {"what": "SQLite", "why": "removes a service dependency"}),
    ("Let's go with feature flags for the rollout.", {"what": "feature flags", "why": None}),
    ("We'll standardize on ruff for linting from now on.", {"what": "ruff", "why": None}),
    ("Opted for a two-space indent to match the existing files.",
     {"what": "two-space indent", "why": None}),
    ("We picked GitHub Actions over Jenkins because maintenance is someone else's problem.",
     {"what": "GitHub Actions", "why": "someone else's problem"}),
    ("Going with the event-sourcing approach for the audit log.",
     {"what": "event-sourcing", "why": None}),
    ("Final call: we ship the v2 endpoint behind a flag on Friday.",
     {"what": "v2 endpoint", "why": None}),
    ("We settled on bge-base embeddings; the larger model's gains didn't justify the latency.",
     {"what": "bge-base", "why": "didn't justify the latency"}),
    ("Decided against a rewrite — incremental refactors carry less risk.",
     {"what": "rewrite", "why": None}),
    ("We landed on 15-minute session timeouts.", {"what": "15-minute", "why": None}),
    ("The team agreed to freeze the schema until the migration completes.",
     {"what": "freeze the schema", "why": None}),
    ("We're adopting trunk-based development starting next sprint.",
     {"what": "trunk-based", "why": None}),
    ("I'm going to use the builder pattern here; the constructor had too many args.",
     {"what": "builder pattern", "why": "too many args"}),
    ("Resolved: all public APIs get versioned URLs.", {"what": "versioned URLs", "why": None}),
    ("We concluded that batching writes was the right call and implemented it.",
     {"what": "batching writes", "why": None}),
    ("Chose the smaller instance type to keep costs down.",
     {"what": "smaller instance type", "why": None}),
    # ---- held-out batch: added AFTER the extractor was frozen, phrasings not
    # ---- patterned against. Misses here are the honest recall picture.
    ("Postgres it is.", {"what": "Postgres", "why": None}),
    ("We're not going to use Kubernetes for this; docker-compose is enough.",
     {"what": "Kubernetes", "why": None}),
    ("In the end the queue won out over direct calls.", {"what": "queue", "why": None}),
    ("Sticking with make instead of just.", {"what": "make", "why": None}),
    ("Redis is the call here.", {"what": "Redis", "why": None}),
    ("Went back and forth on it, but JWT wins.", {"what": "JWT", "why": None}),
    ("The consensus was to keep the monolith.", {"what": "keep the monolith", "why": None}),
    ("K8s is overkill, so we're keeping docker-compose.",
     {"what": "docker-compose", "why": None}),
    # ---- negatives: questions, hedges, hypotheticals, third parties, collisions ----
    ("Should we use Redis or Memcached for this?", None),
    ("We could go with GraphQL, but REST also works.", None),
    ("I'm leaning toward Postgres, but let's benchmark first.", None),
    ("One option is to shard by tenant.", None),
    ("We might switch to pnpm at some point.", None),
    ("Considering moving the queue to SQS.", None),
    ("If we chose Rust, the rewrite would take a quarter.", None),
    ("What should we use for the cache?", None),
    ("The vendor decided to deprecate the v1 API.", None),
    ("Have we decided on the retry policy yet?", None),
    ("We haven't decided on the hosting region.", None),
    ("No decision yet on the queue technology.", None),
    ("The plan proposes using Kafka for ingestion.", None),
    ("It would be nice to use property-based tests here.", None),
    ("Maybe we adopt gRPC later.", None),
    ("Users can choose between dark and light themes.", None),
    ("This library was picked apart in the review.", None),
    ("We went with the flow of the existing design discussion.", None),
    ("Deciding factor analysis is still pending.", None),
    ("The settled science on this is clear.", None),
    ("I'll look into using Terraform next week.", None),
    ("Do you want me to switch the ORM?", None),
    ("We were choosing between three frameworks when the requirements changed.", None),
    ("Several teams opted out of the beta.", None),
    ("The benchmark results might justify moving to Rust.", None),
    ("Open question: how do we handle idempotency?", None),
    ("Alternatives include Redis, Memcached, and an in-process LRU.", None),
    ("He decided to take vacation in August.", None),
    ("Let's discuss the caching strategy tomorrow.", None),
    ("We use Postgres in production today.", None),
    # held-out hard negatives (added after freeze, same policy as above)
    ("We went with the assumption that traffic doubles yearly.", None),
    ("I picked up where the last session left off.", None),
    ("We agreed to disagree on formatting.", None),
    ("She chose to escalate.", None),
    ("The tests decided the matter for us.", None),
    ("We'll use the staging cluster to reproduce the bug.", None),
]


def main():
    tp, fp, fn = [], [], []
    what_ok, what_bad = 0, []
    why_hit, why_miss = 0, []
    n_why_gold = 0

    for text, gold in LABELED:
        found = extract_decisions(text)
        if gold is None:
            if found:
                fp.append((text, found[0]["what"]))
            continue
        if not found:
            fn.append(text)
            continue
        tp.append(text)
        d = found[0]
        if gold["what"].lower() in d["what"].lower():
            what_ok += 1
        else:
            what_bad.append((text, d["what"]))
        if gold["why"]:
            n_why_gold += 1
            if gold["why"].lower() in (d["why"] or "").lower():
                why_hit += 1
            else:
                why_miss.append((text, d["why"]))

    n_pos = sum(1 for _t, g in LABELED if g)
    n_neg = len(LABELED) - n_pos
    prec = len(tp) / (len(tp) + len(fp)) if (tp or fp) else 0.0
    rec = len(tp) / n_pos
    f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0

    print(f"=== decision extraction ({n_pos} positives, {n_neg} negatives) ===")
    print(f"  detection precision  {prec:.2f}")
    print(f"  detection recall     {rec:.2f}")
    print(f"  detection F1         {f1:.2f}")
    print(f"  what-span accuracy   {what_ok}/{len(tp)} of detected positives")
    print(f"  why recall           {why_hit}/{n_why_gold} of positives with an explicit rationale")
    if fn:
        print("\n  missed positives:")
        for t in fn:
            print(f"    - {t}")
    if fp:
        print("\n  false positives:")
        for t, w in fp:
            print(f"    - {t}  -> extracted: {w!r}")
    if what_bad:
        print("\n  wrong what-span:")
        for t, w in what_bad:
            print(f"    - {t}  -> extracted: {w!r}")
    if why_miss:
        print("\n  missed rationale:")
        for t, w in why_miss:
            print(f"    - {t}  -> extracted why: {w!r}")

    out = Path("results/exp_decision_extraction.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({
        "positives": n_pos, "negatives": n_neg,
        "precision": round(prec, 3), "recall": round(rec, 3), "f1": round(f1, 3),
        "what_span_accuracy": round(what_ok / len(tp), 3) if tp else 0.0,
        "why_recall": round(why_hit / n_why_gold, 3) if n_why_gold else None,
        "missed_positives": fn, "false_positives": [t for t, _ in fp],
    }, indent=2))
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
