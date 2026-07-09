#!/usr/bin/env python3
"""Cross-agent thread reconstruction — HARD variant.

The moderate benchmark (`exp_thread_reconstruction.py`, coverage 0.94) uses six
well-separated topics, three agents, one thread per month, and no noise — retrieval
barely has to discriminate. This variant closes those gaps:

  - Near-duplicate sibling topics: 6 families x 2 sibling threads (e.g. "user login
    authentication" vs "service-to-service authorization") with different decisions
    and rationales. Queries must pick the right sibling.
  - Interleaved timelines: all events across all threads share one 6-month window,
    so temporal locality cannot separate threads.
  - Six agents (planner, architect, security, implementer, reviewer, ops); five
    gold events per thread instead of three.
  - Distractor events: per-thread status noise that mentions the family's words,
    plus unrelated global chatter episodes.

Grading stays structural (no LLM judge): every gold event's exact (episode_id,
summary) is recorded at construction; retrieved ThreadItems are matched against
that. Metrics per thread query (thread(q, limit=10)):

  - coverage        : fraction of the thread's 5 gold events retrieved
  - precision       : fraction of retrieved items that are this thread's gold events
  - sibling pull-in : fraction of retrieved items that are the SIBLING's gold events
  - attribution     : of retrieved gold events, right agent (structural sanity)
  - ordering        : retrieved gold events chronological (structural sanity)
  - why top-1       : why() ranks THIS thread's decision rationale first
  - why in top-3    : this thread's rationale anywhere in why()'s top 3

A perfect score here is a red flag, not a win (house rule); the point of this
benchmark is a real, lower number than the moderate one.

  PYTHONPATH=apps/core-daemon CHARON_EMBED_BACKEND=local \
    python scripts/experiments/exp_thread_reconstruction_hard.py --seeds 3
"""
import argparse
import json
import random
import statistics
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "apps" / "core-daemon"))

from memory_engine import MemoryEngine  # noqa: E402
import episodic as ep  # noqa: E402
import threads as th  # noqa: E402

# 6 families x 2 near-duplicate siblings. Each sibling: a distinct decision +
# rationale, phrasing for the 5 gold events, and a paraphrase query that never
# uses the thread's literal key terms but does disambiguate it from its sibling.
FAMILIES = {
    "auth": {
        "user-login": {
            "subject": "user login authentication",
            "choice": "JWT sessions",
            "why": "stateless tokens let any node validate a login without shared session state",
            "query": "what did we settle on for how end users sign in and prove who they are",
        },
        "service-auth": {
            "subject": "service-to-service authorization",
            "choice": "mTLS with RBAC",
            "why": "mutual certificates give internal callers verified identity and scoped permissions",
            "query": "what did we settle on for how our internal backends verify each other's calls",
        },
    },
    "caching": {
        "api-cache": {
            "subject": "API response caching",
            "choice": "Redis",
            "why": "a shared cache keeps computed responses consistent across nodes",
            "query": "how are we storing computed backend results so repeat requests are fast",
        },
        "asset-cache": {
            "subject": "static asset caching",
            "choice": "a CDN",
            "why": "edge delivery cuts asset latency for users far from origin",
            "query": "how are we serving images scripts and styles quickly around the world",
        },
    },
    "storage": {
        "transactional-db": {
            "subject": "transactional database",
            "choice": "Postgres",
            "why": "we need transactions and strong consistency for core records",
            "query": "where do the system of record rows live that need atomic updates",
        },
        "analytics-store": {
            "subject": "analytics warehouse",
            "choice": "ClickHouse",
            "why": "columnar scans make large aggregate reports fast",
            "query": "where do we run the big number-crunching reports over historical data",
        },
    },
    "delivery": {
        "deploy-strategy": {
            "subject": "production deployment strategy",
            "choice": "blue-green deploys",
            "why": "two live environments give zero-downtime releases with instant rollback",
            "query": "how do we push new versions of the system live without an outage",
        },
        "rollout-gating": {
            "subject": "feature rollout gating",
            "choice": "feature flags",
            "why": "gradual exposure decouples shipping code from turning it on",
            "query": "how do we turn new capabilities on for a few people before everyone",
        },
    },
    "search": {
        "semantic-search": {
            "subject": "semantic document search",
            "choice": "a vector index",
            "why": "meaning-based recall beats keywords for how our users phrase things",
            "query": "how do we find documents that match what a person means not their words",
        },
        "log-search": {
            "subject": "log search and filtering",
            "choice": "an inverted index",
            "why": "exact term filters over huge log volumes need token-level lookup",
            "query": "how do operators dig through huge piles of runtime output for exact terms",
        },
    },
    "async": {
        "background-jobs": {
            "subject": "background job processing",
            "choice": "a work queue",
            "why": "queued jobs decouple producers from consumers under load",
            "query": "how do we hand slow work off so the request path does not wait on it",
        },
        "event-streaming": {
            "subject": "realtime event streaming",
            "choice": "a replayable event log",
            "why": "an ordered replayable log lets consumers rebuild state and catch up",
            "query": "how do downstream systems watch a live ordered feed of what happened",
        },
    },
}

AGENTS = ["planner", "architect", "security", "implementer", "reviewer", "ops"]
WINDOW_DAYS = 180  # all threads interleave inside one shared 6-month window
GOLD_PER_THREAD = 5
THREAD_LIMIT = 10

GLOBAL_DISTRACTORS = [
    "standup notes: everyone shared yesterday's progress and today's plan",
    "reminder: submit vacation requests for the summer schedule",
    "office move: desks on the third floor relocate next week",
    "dependency bumps: routine version updates across the repos, no behavior change",
    "lint cleanup: formatting-only sweep, no functional changes",
    "onboarding: walked the new teammate through the repo layout",
]


def _date(day_index: int) -> str:
    """Day offset -> ISO date inside 2025-01-01 + WINDOW_DAYS."""
    month_lengths = [31, 28, 31, 30, 31, 30, 31]
    m, d = 0, day_index
    while d >= month_lengths[m]:
        d -= month_lengths[m]
        m += 1
    return f"2025-{m+1:02d}-{d+1:02d}"


def build(rng, tag):
    """12 interleaved threads (6 families x 2 siblings) across 6 agents, with
    per-thread noise events and unrelated global chatter. Returns the engine and
    gold: thread_id -> {events: [(key, agent, ts)], why, query, sibling}."""
    eng = MemoryEngine(Path(tempfile.mkdtemp()))
    gold = {}
    all_gold_keys = {}  # (episode_id, summary) -> thread_id

    def episode(thread_id, step, agent):
        e = ep.create_episode(eng, f"{thread_id} {step}", source_conv=f"{thread_id}-{step}",
                              source_agent=agent, container_tag=tag)
        return e.id

    for family, siblings in FAMILIES.items():
        for thread_id, spec in siblings.items():
            days = sorted(rng.sample(range(WINDOW_DAYS), GOLD_PER_THREAD + 2))
            ts = [_date(d) for d in days]
            subject, choice, why = spec["subject"], spec["choice"], spec["why"]
            events = []  # (key, agent, ts) in gold order

            # bind events/thread_id: closure is only called within this iteration (B023 false positive)
            def gold_event(ev, agent, events=events, thread_id=thread_id):
                key = (ev.episode_id, ev.summary)
                events.append((key, agent, ev.ts))
                all_gold_keys[key] = thread_id

            ev = ep.add_event(eng, episode(thread_id, "raise", "planner"),
                              event_type="user_message", actor="user",
                              summary=f"what should we use for {subject}?",
                              container_tag=tag, ts=ts[0])
            gold_event(ev, "planner")
            ev = ep.add_event(eng, episode(thread_id, "concern", "security"),
                              event_type="observation", actor="agent",
                              summary=f"reviewed the {subject} options and flagged the risks of each",
                              container_tag=tag, ts=ts[1])
            gold_event(ev, "security")
            ev = th.log_decision(eng, episode(thread_id, "decide", "architect"),
                                 what=f"use {choice} for {subject}", why=why,
                                 topic=subject, container_tag=tag, ts=ts[2])
            gold_event(ev, "architect")
            ev = ep.add_event(eng, episode(thread_id, "implement", "implementer"),
                              event_type="agent_message", actor="agent",
                              summary=f"implemented {choice} for {subject}",
                              container_tag=tag, ts=ts[3])
            gold_event(ev, "implementer")
            ev = ep.add_event(eng, episode(thread_id, "verify", "ops"),
                              event_type="agent_message", actor="agent",
                              summary=f"verified {subject} in staging: {choice} behaves as decided",
                              container_tag=tag, ts=ts[4])
            gold_event(ev, "ops")

            # per-thread noise: status chatter that mentions the family's words but
            # is not part of the thread's gold story (crowds the retrieval limit)
            ep.add_event(eng, episode(thread_id, "noise1", "reviewer"),
                         event_type="system_notification", actor="system",
                         summary=f"ticket sync: updated the {subject} tickets and reran the checks",
                         container_tag=tag, ts=ts[5], importance=20)
            ep.add_event(eng, episode(thread_id, "noise2", "reviewer"),
                         event_type="agent_message", actor="agent",
                         summary=f"posted a status summary about the ongoing {subject} discussions",
                         container_tag=tag, ts=ts[6], importance=20)

            gold[thread_id] = {"events": events, "why": why,
                               "query": spec["query"], "family": family}

    for thread_id, g in gold.items():
        g["sibling"] = next(t for t, o in gold.items()
                            if t != thread_id and o["family"] == g["family"])

    # unrelated global chatter
    for i, text in enumerate(GLOBAL_DISTRACTORS):
        eid = ep.create_episode(eng, f"chatter {i}", source_conv=f"chatter-{i}",
                                source_agent=rng.choice(AGENTS), container_tag=tag).id
        ep.add_event(eng, eid, event_type="system_notification", actor="system",
                     summary=text, container_tag=tag,
                     ts=_date(rng.randrange(WINDOW_DAYS)), importance=20)

    return eng, gold, all_gold_keys


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--seed-offset", type=int, default=0,
                    help="first seed (use held-out seeds when validating tuned params)")
    ap.add_argument("--importance-weight", type=float, default=0.5,
                    help="importance re-rank exponent (0 = pure content ranking); "
                         "default matches the library default in episodic.recall_events")
    ap.add_argument("--out", default="results/exp_thread_reconstruction_hard.json")
    args = ap.parse_args()
    iw = args.importance_weight

    cov, prec, sib, attr, order = [], [], [], [], []
    why_top1, why_top3 = [], []
    per_thread = {}

    for seed in range(args.seed_offset, args.seed_offset + args.seeds):
        eng, gold, all_gold_keys = build(random.Random(seed), tag=f"thrh-{seed}")
        for thread_id, g in gold.items():
            q = g["query"]
            items = th.thread(eng, q, container_tag=f"thrh-{seed}", limit=THREAD_LIMIT,
                              importance_weight=iw)
            keys = [(it.episode_id, it.what) for it in items]
            gold_keys = [k for k, _a, _t in g["events"]]
            agent_of = {k: a for k, a, _t in g["events"]}
            ts_of = {k: t for k, _a, t in g["events"]}

            hit = [k for k in keys if k in gold_keys]
            c = len(set(hit)) / len(gold_keys)
            cov.append(c)
            per_thread.setdefault(thread_id, []).append(c)
            if keys:
                prec.append(len(hit) / len(keys))
                sib.append(sum(1 for k in keys
                               if all_gold_keys.get(k) == g["sibling"]) / len(keys))
            # structural sanity on the retrieved gold subset
            matched = [(it, (it.episode_id, it.what)) for it in items
                       if (it.episode_id, it.what) in agent_of]
            if matched:
                attr.append(sum(1 for it, k in matched if it.agent == agent_of[k])
                            / len(matched))
                seq = [ts_of[k] for _it, k in matched]
                order.append(1.0 if seq == sorted(seq) else 0.0)
            # why discrimination: does why() rank THIS thread's rationale first?
            w = th.why(eng, q, container_tag=f"thrh-{seed}", limit=3,
                       importance_weight=iw)
            whys = [x["why"] or "" for x in w]
            why_top1.append(1.0 if whys and whys[0] == g["why"] else 0.0)
            why_top3.append(1.0 if g["why"] in whys else 0.0)
        eng.close()
        print(f"seed {seed} done", flush=True)

    def m(v):
        return round(statistics.mean(v), 3) if v else 0.0

    n_threads = sum(len(s) for s in FAMILIES.values())
    print(f"\n=== cross-agent thread reconstruction — HARD "
          f"({args.seeds} seeds x {n_threads} sibling threads, {len(AGENTS)} agents, "
          f"interleaved + noise) ===")
    print(f"  coverage (gold events retrieved)       {m(cov):.2f}")
    print(f"  precision (retrieved that are gold)    {m(prec):.2f}")
    print(f"  sibling pull-in (wrong twin retrieved) {m(sib):.2f}")
    print(f"  attribution (right agent | retrieved)  {m(attr):.2f}")
    print(f"  ordering (chronological | retrieved)   {m(order):.2f}")
    print(f"  why top-1 (right rationale first)      {m(why_top1):.2f}")
    print(f"  why in top-3                           {m(why_top3):.2f}")
    worst = sorted((statistics.mean(v), t) for t, v in per_thread.items())[:4]
    print("  hardest threads: " + ", ".join(f"{t} {c:.2f}" for c, t in worst))

    report = {
        "seeds": args.seeds, "threads": n_threads, "agents": len(AGENTS),
        "importance_weight": iw,
        "gold_events_per_thread": GOLD_PER_THREAD, "thread_limit": THREAD_LIMIT,
        "coverage": m(cov), "precision": m(prec), "sibling_pull_in": m(sib),
        "attribution": m(attr), "ordering": m(order),
        "why_top1": m(why_top1), "why_top3": m(why_top3),
        "per_thread_coverage": {t: round(statistics.mean(v), 3)
                                for t, v in sorted(per_thread.items())},
    }
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(report, indent=2))
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
