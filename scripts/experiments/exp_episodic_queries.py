#!/usr/bin/env python3
"""Ecologically-valid test: does episodic memory answer 'when/where' queries?

The earlier ablation tested abstractive FACT QA (semantic memory). This tests the
thing episodic memory is actually for: temporal-contextual recall over a stream of
dated work sessions — "the 3 most recent sessions", "sessions in March", "the
session before the deploy", "the auth session in February".

Key property: these queries have **automatic ground truth from the timestamps**,
so no relevance labeling is needed — and the same harness runs on real usage data.

We compare, per query type:
  - flat     : engine.recall(query_text) over turn-level memories (Charon today)
  - episodic : time-structural retrieval over first-class Episodes
The point isn't a close race — it's that flat retrieval *structurally cannot*
serve time queries (no content overlap with "last Tuesday"), so this measures a
capability gap, not a tuning delta.

  PYTHONPATH=src CHARON_EMBED_BACKEND=local \
    python scripts/experiments/exp_episodic_queries.py --seeds 3
"""
import argparse
import json
import random
import statistics
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from charon.memory.memory_engine import MemoryEngine  # noqa: E402
from charon.memory import episodic as ep  # noqa: E402

# Realistic developer work-session content. Each topic recurs across months, so a
# time filter genuinely matters for topic-in-time queries.
TOPICS = {
    "auth": ["fixed the token refresh race condition", "added the OAuth PKCE flow",
             "rotated the JWT signing keys", "tightened the session timeout policy"],
    "api": ["added rate limiting to the public endpoints", "versioned the REST API",
            "fixed cursor pagination", "added request idempotency keys"],
    "deploy": ["rolled out the release to staging", "fixed the CI build cache",
               "added a canary deployment step", "automated the rollback path"],
    "database": ["added an index on the orders table", "ran the schema migration",
                 "fixed a transaction deadlock", "tuned the connection pool"],
    "caching": ["added a Redis cache layer", "tuned the cache TTLs",
                "fixed a cache stampede", "added cache invalidation on write"],
    "ui": ["redesigned the settings page", "fixed the dark-mode toggle",
           "added keyboard shortcuts", "reworked the onboarding flow"],
}
TOPIC_LIST = list(TOPICS)


def _date(day: int) -> str:
    # monotonic dates across ~6 months from a fixed epoch (no wall clock)
    y, m, d = 2025, 1 + (day // 28), 1 + (day % 28)
    return f"{y:04d}-{m:02d}-{d:02d}"


def gen_sessions(rng, n=60):
    """n dated work sessions, ~every 3 days, topics recurring across months.

    Each turn carries a unique ref token so the engine's dedup doesn't collapse
    recurring phrasings across sessions (which would corrupt episode membership).
    The token is time-neutral — it must NOT leak the date into content, or flat
    content retrieval could cheat on the time queries."""
    sessions = []
    for i in range(n):
        topic = TOPIC_LIST[i % len(TOPIC_LIST)] if i < len(TOPIC_LIST) else rng.choice(TOPIC_LIST)
        acts = rng.sample(TOPICS[topic], k=2)
        day = i * 3
        def ref():
            return f"(ref {rng.randrange(10 ** 9):09d})"
        turns = [f"Worked on {topic}: {a}. {ref()}" for a in acts]
        turns.append(f"Standup, code review, and email triage. {ref()}")  # distractor
        sessions.append({"sid": f"S{i:02d}", "topic": topic, "date": _date(day),
                         "month": _date(day)[:7], "turns": turns})
    return sessions


def flat_sessions(engine, query, tag, id2sid, limit=5):
    res = engine.recall(query, container_tag=tag, limit=limit * 3)
    out, seen = [], set()
    for sm in res.memories:
        sid = id2sid.get(sm.memory.id)
        # don't let an episode-summary memory leak the answer to the flat baseline
        if sm.memory.category == "episode_summary":
            continue
        if sid and sid not in seen:
            seen.add(sid)
            out.append(sid)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--out", default="results/memeval/exp_episodic_queries.json")
    args = ap.parse_args()

    acc = defaultdict(lambda: defaultdict(list))  # qtype -> method -> [score]

    for seed in range(args.seeds):
        rng = random.Random(seed)
        sessions = gen_sessions(rng, n=60)
        tag = f"wk-{seed}"
        eng = MemoryEngine(Path(tempfile.mkdtemp()))
        # index turns; create one episode per session with member ids + dates
        id2sid = {}
        for s in sessions:
            mids = []
            for ti, text in enumerate(s["turns"]):
                mem = eng.add(text, category="event", container_tag=tag,
                              event_date=s["date"], source_conv=s["sid"], source_turn=ti)
                id2sid[mem.id] = s["sid"]
                mids.append(mem.id)
            ep.create_episode(eng, " ".join(s["turns"][:-1]), source_conv=s["sid"],
                              member_ids=mids, container_tag=tag, title=s["sid"])

        # ---- Q1 recency: "the 3 most recent sessions" ----
        gold = {s["sid"] for s in sorted(sessions, key=lambda x: x["date"])[-3:]}
        epi = {e.source_conv for e in ep.recent_episodes(eng, tag, n=3)}
        flat = set(flat_sessions(eng, "the 3 most recent sessions", tag, id2sid, limit=3))
        acc["recency_top3"]["episodic"].append(len(epi & gold) / 3)
        acc["recency_top3"]["flat"].append(len(flat & gold) / 3)

        # ---- Q2 time-range: "sessions in <month>" ----
        for month in sorted({s["month"] for s in sessions})[1:4]:
            gold = {s["sid"] for s in sessions if s["month"] == month}
            epi = {e.source_conv for e in ep.episodes_in_range(eng, month + "-01", month + "-28", tag)}
            flat = set(flat_sessions(eng, f"sessions from {month}", tag, id2sid, limit=len(gold) + 2))
            acc["time_range_month"]["episodic"].append(len(epi & gold) / len(gold))
            acc["time_range_month"]["flat"].append(len(flat & gold) / len(gold))

        # ---- Q3 before/after: "the session before session X" ----
        ordered = sorted(sessions, key=lambda x: x["date"])
        for idx in (10, 25, 40):
            anchor = ordered[idx]
            gold_sid = ordered[idx - 1]["sid"]
            anchor_ep = next(e for e in ep.list_episodes(eng, tag) if e.source_conv == anchor["sid"])
            before = ep.episode_before(eng, anchor_ep.id, tag)
            epi_ok = 1.0 if before and before.source_conv == gold_sid else 0.0
            flat = flat_sessions(eng, f"the session before the {anchor['topic']} work", tag, id2sid, limit=1)
            flat_ok = 1.0 if flat and flat[0] == gold_sid else 0.0
            acc["before_session"]["episodic"].append(epi_ok)
            acc["before_session"]["flat"].append(flat_ok)

        # ---- Q4 topic-in-time: "the <topic> session in <month>" ----
        # pick (topic, month) pairs that occur exactly once
        from collections import Counter
        tm = Counter((s["topic"], s["month"]) for s in sessions)
        singles = [(t, m) for (t, m), c in tm.items() if c == 1][:6]
        for topic, month in singles:
            gold_sid = next(s["sid"] for s in sessions if s["topic"] == topic and s["month"] == month)
            epi_hits = ep.recall_episodes(eng, f"{topic} work", container_tag=tag,
                                          temporal_range=(month + "-01", month + "-28"), limit=3)
            epi_ok = 1.0 if gold_sid in {e.source_conv for e, _ in epi_hits} else 0.0
            flat = flat_sessions(eng, f"the {topic} session in {month}", tag, id2sid, limit=3)
            flat_ok = 1.0 if gold_sid in set(flat) else 0.0
            acc["topic_in_month"]["episodic"].append(epi_ok)
            acc["topic_in_month"]["flat"].append(flat_ok)

        eng.close()
        print(f"seed {seed} done", flush=True)

    print("\n=== episodic 'when/where' queries: episodic retrieval vs flat content retrieval ===")
    print(f"{'query type':20}{'n':>4}{'flat':>9}{'episodic':>11}")
    print("-" * 44)
    report = {}
    for qt in ["recency_top3", "time_range_month", "before_session", "topic_in_month"]:
        n = len(acc[qt]["episodic"])
        f = round(statistics.mean(acc[qt]["flat"]), 3)
        e = round(statistics.mean(acc[qt]["episodic"]), 3)
        print(f"{qt:20}{n:>4}{f:>9.2f}{e:>11.2f}")
        report[qt] = {"n": n, "flat": f, "episodic": e}
    print("\n(score = fraction of the time-defined gold correctly retrieved)")
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2))
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
