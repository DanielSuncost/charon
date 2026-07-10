#!/usr/bin/env python3
"""Can Charon reconstruct a cross-agent decision/discussion thread? — objective eval.

The cross-agent thread feature comes with a clean, non-confounded benchmark: build
a multi-agent scenario with KNOWN threads (each topic raised by one agent, decided
by another with a known rationale, implemented by a third, over time), with several
topics acting as distractors for each other; then call `thread(topic)` and grade,
against ground truth derived from the construction:

  - coverage      : fraction of the topic's gold events retrieved
  - attribution   : of retrieved gold events, fraction attributed to the right agent
  - ordering      : retrieved gold events in correct chronological order
  - why           : the decision's rationale correctly surfaced (via why())

No LLM judge, no floored baseline, no legibility confound — gold is structural.

  PYTHONPATH=src CHARON_EMBED_BACKEND=local \
    python scripts/experiments/exp_thread_reconstruction.py --seeds 3
"""
import argparse
import json
import random
import statistics
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from charon.memory.memory_engine import MemoryEngine  # noqa: E402
from charon.memory import episodic as ep  # noqa: E402
from charon.agents import threads as th  # noqa: E402

TOPICS = {
    "authentication": ("JWT", "stateless tokens scale across the fleet"),
    "caching": ("Redis", "a shared cache avoids per-node staleness"),
    "database": ("Postgres", "we need transactions and strong consistency"),
    "deployment": ("blue-green", "zero-downtime releases with instant rollback"),
    "search": ("vector index", "semantic recall beats keyword for our queries"),
    "messaging": ("a queue", "decouples producers from consumers under load"),
}
AGENTS = ["planner", "architect", "implementer"]

# Paraphrased queries — a user asks by description, not the exact topic keyword, so
# retrieval must match on meaning rather than the literal word (the discriminating
# test; querying by the bare keyword is trivially perfect and only checks wiring).
PARAPHRASE = {
    "authentication": "how do users log in and prove who they are",
    "caching": "how do we avoid recomputing and store results for speed",
    "database": "how do we persist and query our data reliably",
    "deployment": "how do we ship releases to production safely",
    "search": "how do we find the most relevant items for a query",
    "messaging": "how do we pass work between services asynchronously",
}


def build(rng, tag):
    """One thread per topic across 3 agents over time; returns engine + gold."""
    eng = MemoryEngine(Path(tempfile.mkdtemp()))
    gold = {}
    topics = list(TOPICS)
    rng.shuffle(topics)
    for i, topic in enumerate(topics):
        choice, why = TOPICS[topic]
        d0, d1, d2 = f"2025-{i+1:02d}-03", f"2025-{i+1:02d}-07", f"2025-{i+1:02d}-12"
        # planner raises it
        m0 = eng.add(f"raise {topic} {i}", category="event", container_tag=tag,
                     source_conv=f"{topic}-plan", event_date=d0, check_updates=False)
        e0 = ep.create_episode(eng, f"{topic} planning", source_conv=f"{topic}-plan",
                               source_agent="planner", member_ids=[m0.id], container_tag=tag,
                               summary_memory_id=m0.id)
        ep.add_event(eng, e0.id, event_type="user_message", actor="user",
                     summary=f"what should we use for {topic}?", container_tag=tag, ts=d0)
        # architect decides
        m1 = eng.add(f"decide {topic} {i}", category="event", container_tag=tag,
                     source_conv=f"{topic}-dec", event_date=d1, check_updates=False)
        e1 = ep.create_episode(eng, f"{topic} decision", source_conv=f"{topic}-dec",
                               source_agent="architect", member_ids=[m1.id], container_tag=tag,
                               summary_memory_id=m1.id)
        th.log_decision(eng, e1.id, what=f"use {choice} for {topic}", why=why,
                        topic=topic, container_tag=tag, ts=d1)
        # implementer ships
        m2 = eng.add(f"ship {topic} {i}", category="event", container_tag=tag,
                     source_conv=f"{topic}-impl", event_date=d2, check_updates=False)
        e2 = ep.create_episode(eng, f"{topic} impl", source_conv=f"{topic}-impl",
                               source_agent="implementer", member_ids=[m2.id], container_tag=tag,
                               summary_memory_id=m2.id)
        ep.add_event(eng, e2.id, event_type="agent_message", actor="agent",
                     summary=f"implemented {choice} for {topic}", container_tag=tag, ts=d2)
        gold[topic] = {
            "choice": choice, "why": why,
            "events": [("planner", d0), ("architect", d1), ("implementer", d2)],
        }
    return eng, gold


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--out", default="results/exp_thread_reconstruction.json")
    args = ap.parse_args()

    cov, attr, order, whyrec = [], [], [], []
    for seed in range(args.seeds):
        eng, gold = build(random.Random(seed), tag=f"thr-{seed}")
        for topic, g in gold.items():
            q = PARAPHRASE[topic]   # query by description, not the literal topic word
            items = th.thread(eng, q, container_tag=f"thr-{seed}", limit=10)
            # a returned item matches a gold event if it mentions the topic
            matched = [it for it in items if topic in it.what.lower()]
            gold_agents = [a for a, _ in g["events"]]
            # coverage: distinct gold agents present among matched
            got_agents = [it.agent for it in matched]
            cov.append(sum(1 for a in gold_agents if a in got_agents) / len(gold_agents))
            # attribution: matched items whose agent is one of the gold agents (right owner)
            if matched:
                attr.append(sum(1 for it in matched if it.agent in gold_agents) / len(matched))
            # ordering: matched items chronological by ts
            ts_seq = [it.ts for it in matched]
            order.append(1.0 if ts_seq == sorted(ts_seq) else 0.0)
            # why: rationale surfaced via why()
            w = th.why(eng, q, container_tag=f"thr-{seed}", limit=3)
            whyrec.append(1.0 if any(g["why"][:20] in (x["why"] or "") for x in w) else 0.0)
        eng.close()
        print(f"seed {seed} done", flush=True)

    def m(v):
        return round(statistics.mean(v), 3) if v else 0.0
    print(f"\n=== cross-agent thread reconstruction ({args.seeds} seeds × {len(TOPICS)} topics) ===")
    print(f"  coverage (gold agents retrieved)     {m(cov):.2f}")
    print(f"  attribution (right agent)            {m(attr):.2f}")
    print(f"  ordering (chronological)             {m(order):.2f}")
    print(f"  why recall (rationale surfaced)      {m(whyrec):.2f}")
    report = {"seeds": args.seeds, "topics": len(TOPICS),
              "coverage": m(cov), "attribution": m(attr), "ordering": m(order), "why_recall": m(whyrec)}
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(report, indent=2))
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
