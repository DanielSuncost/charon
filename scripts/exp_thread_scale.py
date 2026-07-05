#!/usr/bin/env python3
"""Thread reconstruction at scale + decision supersession + flat-RAG baseline.

Scales the cross-agent thread benchmark from 12 hand-written threads to N
programmatically generated ones (domain x aspect subjects, e.g. "payments rate
limiting"), all interleaved in one 18-month window, with per-thread noise. Adds:

  - SUPERSESSION: 25% of threads get a later decision that overrides the first
    ("switch from X to Y"). Query: what is the CURRENT choice? Gold = the newer
    decision. This is the query class that separates decision memory from plain
    retrieval — and where recency-blind ranking should honestly struggle.
  - FLAT-RAG BASELINE: the same event texts stored as plain memories (no
    episodes, no types, no importance), queried with engine.recall. Comparing
    coverage tells us what the episodic/thread STRUCTURE buys beyond retrieval;
    identical coverage would be an honest null for structure-as-retrieval-aid
    (structure still buys attribution/why/typing, which flat storage cannot
    answer at all).
  - Per-query latency at each scale.

Honest scope: subjects and queries are template-generated, so this measures
crowding/discrimination as the corpus grows — NOT deep paraphrase matching
(that's exp_thread_reconstruction_hard.py, hand-written). Near-duplicate
pressure emerges naturally from shared domain/aspect words at higher N.

  PYTHONPATH=apps/core-daemon CHARON_EMBED_BACKEND=local \
    python scripts/exp_thread_scale.py --sizes 24,96,288 --seeds 2
"""
import argparse
import json
import random
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "core-daemon"))

from memory_engine import MemoryEngine  # noqa: E402
import episodic as ep  # noqa: E402
import threads as th  # noqa: E402

DOMAINS = [
    "payments", "onboarding", "search", "billing", "notifications", "exports",
    "authentication", "logging", "analytics", "media uploads", "profile sync",
    "backups", "scheduling", "permissions", "reporting", "webhooks", "invoicing",
    "session handling", "email delivery", "feature flags", "audit trail",
    "localization", "rate limits", "checkout",
]
ASPECTS = [
    "storage engine", "caching layer", "delivery pipeline", "validation rules",
    "monitoring setup", "batching policy", "retry policy", "encryption scheme",
    "queue backend", "schema design", "rollout plan", "index strategy",
]
CHOICES = [
    ("Postgres", "SQLite"), ("Redis", "an in-process LRU"), ("Kafka", "a cron sweep"),
    ("a token bucket", "a fixed window"), ("protobuf", "JSON"), ("S3", "local disk"),
    ("blue-green", "rolling updates"), ("JWT", "server sessions"),
    ("a vector index", "FTS"), ("terraform", "hand-rolled scripts"),
    ("a work queue", "inline processing"), ("feature flags", "branch deploys"),
]
REASONS = [
    "it holds up under our peak load", "it removes an operational dependency",
    "it keeps the failure modes simple", "the team already knows how to run it",
    "it makes rollbacks trivial", "it cuts tail latency where users feel it",
    "it keeps costs predictable at our scale", "it fails loudly instead of silently",
]
SUPERSEDE_REASONS = [
    "the first choice fell over in production", "the cost curve got ugly at scale",
    "a security review ruled the old approach out", "operational burden was too high",
]
AGENTS = ["planner", "architect", "security", "implementer", "reviewer", "ops"]
WINDOW_DAYS = 540
GOLD_PER_THREAD = 5
THREAD_LIMIT = 10
SUPERSEDE_FRACTION = 0.25


def _date(day: int) -> str:
    y, rem = 2024 + day // 360, day % 360
    return f"{y}-{rem // 30 + 1:02d}-{rem % 30 + 1:02d}"


def build(rng, tag, n_threads):
    """N interleaved threads; ~25% get a superseding second decision. Returns
    (engine, gold, flat_engine, flat_gold_ids). The flat engine holds the SAME
    event texts as plain memories — the structure-free baseline."""
    eng = MemoryEngine(Path(tempfile.mkdtemp()))
    flat = MemoryEngine(Path(tempfile.mkdtemp()))
    subjects = [f"{d} {a}" for d in DOMAINS for a in ASPECTS]
    rng.shuffle(subjects)
    assert n_threads <= len(subjects), f"max {len(subjects)} threads"
    gold = {}

    def episode(tid, step, agent):
        return ep.create_episode(eng, f"{tid} {step}", source_conv=f"{tid}-{step}",
                                 source_agent=agent, container_tag=tag).id

    def flat_add(text, ts):
        return flat.add(text, category="event", container_tag=tag,
                        event_date=ts, check_updates=False).id

    for i in range(n_threads):
        subject = subjects[i]
        choice, alt = CHOICES[i % len(CHOICES)]
        reason = REASONS[i % len(REASONS)]
        superseded = rng.random() < SUPERSEDE_FRACTION
        n_days = GOLD_PER_THREAD + 2 + (1 if superseded else 0)
        ts = [_date(d) for d in sorted(rng.sample(range(WINDOW_DAYS), n_days))]
        tid = f"t{i:03d}"
        texts = [
            ("user_message", "planner", "user", f"what should we use for the {subject}?", ts[0], 50),
            ("observation", "security", "agent", f"reviewed the {subject} options and flagged the risks", ts[1], 50),
        ]
        events, flat_ids = [], []
        for et, agent, actor, summary, t, imp in texts:
            e = ep.add_event(eng, episode(tid, et, agent), event_type=et, actor=actor,
                             summary=summary, container_tag=tag, ts=t, importance=imp)
            events.append(((e.episode_id, e.summary), agent, t))
            flat_ids.append(flat_add(summary, t))
        dec = th.log_decision(eng, episode(tid, "decide", "architect"),
                              what=f"use {choice} for the {subject}", why=reason,
                              topic=subject, container_tag=tag, ts=ts[2])
        events.append(((dec.episode_id, dec.summary), "architect", ts[2]))
        flat_ids.append(flat_add(dec.summary, ts[2]))
        for et, agent, actor, summary, t in [
            ("agent_message", "implementer", "agent",
             f"implemented {choice} for the {subject}", ts[3]),
            ("agent_message", "ops", "agent",
             f"verified the {subject} in staging with {choice}", ts[4]),
        ]:
            e = ep.add_event(eng, episode(tid, et + agent, agent), event_type=et,
                             actor=actor, summary=summary, container_tag=tag, ts=t)
            events.append(((e.episode_id, e.summary), agent, t))
            flat_ids.append(flat_add(summary, t))
        # noise (not gold): status chatter using the subject's words
        for j, t in enumerate(ts[5:7]):
            ep.add_event(eng, episode(tid, f"noise{j}", "reviewer"),
                         event_type="system_notification", actor="system",
                         summary=f"ticket sync: updated the {subject} tickets",
                         container_tag=tag, ts=t, importance=20)
            flat_add(f"ticket sync: updated the {subject} tickets", t)

        current_choice, supersede_why = choice, None
        if superseded:
            supersede_why = SUPERSEDE_REASONS[i % len(SUPERSEDE_REASONS)]
            dec2 = th.log_decision(eng, episode(tid, "redecide", "architect"),
                                   what=f"switch the {subject} from {choice} to {alt}",
                                   why=supersede_why, topic=subject,
                                   container_tag=tag, ts=ts[7])
            events.append(((dec2.episode_id, dec2.summary), "architect", ts[7]))
            flat_ids.append(flat_add(dec2.summary, ts[7]))
            current_choice = alt

        gold[tid] = {
            "subject": subject, "events": events, "flat_ids": flat_ids,
            "query": f"what happened around the {subject} and what did we go with",
            "superseded": superseded, "first_choice": choice,
            "current_choice": current_choice, "why": reason,
            "supersede_why": supersede_why,
        }
    return eng, flat, gold


def run_size(n_threads, seeds):
    cov, flat_cov, prec, lat = [], [], [], []
    cur_acc, cur_stale = [], []
    for seed in seeds:
        rng = random.Random(seed)
        eng, flat, gold = build(rng, f"sc-{n_threads}-{seed}", n_threads)
        tag = f"sc-{n_threads}-{seed}"
        for tid, g in gold.items():
            t0 = time.monotonic()
            items = th.thread(eng, g["query"], container_tag=tag, limit=THREAD_LIMIT)
            lat.append(time.monotonic() - t0)
            keys = [(it.episode_id, it.what) for it in items]
            gold_keys = {k for k, _a, _t in g["events"]}
            hit = [k for k in keys if k in gold_keys]
            cov.append(len(set(hit)) / len(gold_keys))
            if keys:
                prec.append(len(hit) / len(keys))
            # flat baseline: same texts, plain memories, plain recall
            res = flat.recall(g["query"], container_tag=tag, limit=THREAD_LIMIT)
            got = {m.memory.id for m in res.memories}
            flat_cov.append(sum(1 for fid in g["flat_ids"] if fid in got)
                            / len(g["flat_ids"]))
            # supersession: does the top decision reflect the CURRENT choice?
            if g["superseded"]:
                w = th.why(eng, f"what is our current choice for the {g['subject']}",
                           container_tag=tag, limit=3)
                if w:
                    top = w[0]["decision"]
                    cur_acc.append(1.0 if f"to {g['current_choice']}" in top else 0.0)
                    cur_stale.append(1.0 if (f"use {g['first_choice']}" in top
                                             and f"to {g['current_choice']}" not in top)
                                     else 0.0)
        eng.close(); flat.close()
    m = lambda v: round(statistics.mean(v), 3) if v else None
    return {
        "threads": n_threads, "seeds": len(seeds),
        "coverage": m(cov), "precision": m(prec), "flat_coverage": m(flat_cov),
        "current_decision_acc": m(cur_acc), "stale_decision_rate": m(cur_stale),
        "supersession_queries": len(cur_acc),
        "mean_query_ms": round(statistics.mean(lat) * 1000, 1),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sizes", default="24,96,288")
    ap.add_argument("--seeds", type=int, default=2)
    ap.add_argument("--out", default="results/exp_thread_scale.json")
    args = ap.parse_args()
    sizes = [int(s) for s in args.sizes.split(",")]

    reports = []
    for n in sizes:
        seeds = list(range(args.seeds if n <= 100 else 1))  # big sizes: 1 seed
        r = run_size(n, seeds)
        reports.append(r)
        print(f"\n=== {n} threads ({r['seeds']} seed(s), "
              f"{r['supersession_queries']} supersession queries) ===")
        print(f"  thread coverage@{THREAD_LIMIT}      {r['coverage']:.2f}")
        print(f"  flat-RAG coverage@{THREAD_LIMIT}    {r['flat_coverage']:.2f}")
        print(f"  thread precision          {r['precision']:.2f}")
        print(f"  current-decision acc      {r['current_decision_acc']}")
        print(f"  stale-decision rate       {r['stale_decision_rate']}")
        print(f"  mean query latency        {r['mean_query_ms']} ms")

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(reports, indent=2))
    print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
