#!/usr/bin/env python3
"""Does a first-class episodic layer help retrieval? Measured, honestly.

Compares, per question type and difficulty, session-level recall@k for:
  - baseline          : flat turn-level retrieval (what Charon does today)
  - episodic(session) : + one indexed summary per session = the whole session
                        concatenated (conservative: includes distractor noise)
  - episodic(facts)   : + one indexed summary per session = only the fact-bearing
                        turns (optimistic proxy for a clean LLM summary)
A retrieved episode-summary counts its session as retrieved. Separate engines per
condition so summaries never contaminate the baseline.

Also tests recency-weighting on knowledge_update (the latest-value failure: stale
and current both retrievable; does a recency bonus surface the current one?).

  PYTHONPATH=src CHARON_EMBED_BACKEND=local \
    python scripts/experiments/exp_memeval_episodic.py --seeds 3
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
sys.path.insert(0, str(ROOT / "scripts" / "experiments"))

from charon.memory.memory_engine import MemoryEngine  # noqa: E402
from charon.memory import episodic as ep  # noqa: E402
from memeval_gen import Gen, PRESETS, validate  # noqa: E402

KS = [1, 2, 3, 5]
DIFFICULTIES = ["easy", "medium", "hard"]
TYPES = ["single_session", "knowledge_update", "multi_session_join", "temporal"]


def _index_turns(engine, dataset, tag):
    id2sid, by_sid = {}, defaultdict(list)
    for s in dataset["sessions"]:
        sid = s["session_id"]
        for ti, turn in enumerate(s["turns"]):
            if len(turn["text"]) < 12:
                continue
            mem = engine.add(turn["text"], category="event", container_tag=tag,
                             event_date=s["timestamp"].split("T")[0],
                             source_conv=sid, source_turn=ti)
            id2sid[mem.id] = sid
            by_sid[sid].append((mem.id, turn))
    return id2sid, by_sid


def _add_episode_summaries(engine, dataset, tag, by_sid, mode):
    """Create one episode per session; return summary_mem_id -> sid."""
    sum2sid = {}
    for s in dataset["sessions"]:
        sid = s["session_id"]
        items = by_sid.get(sid, [])
        if mode == "facts":
            texts = [t["text"] for _, t in items if t.get("fact")]
        else:  # session
            texts = [t["text"] for _, t in items]
        if not texts:
            continue
        summary = " ".join(texts)[:1500]
        e = ep.create_episode(engine, summary, source_conv=sid,
                              member_ids=[mid for mid, _ in items], container_tag=tag)
        if e.summary_memory_id:
            sum2sid[e.summary_memory_id] = sid
    return sum2sid


def _ranked_sessions(engine, query, tag, id2sid, sum2sid=None, recency_weight=0.0, limit=20):
    res = engine.recall(query, container_tag=tag, limit=limit, recency_weight=recency_weight)
    out, seen = [], set()
    for sm in res.memories:
        sid = id2sid.get(sm.memory.id) or (sum2sid or {}).get(sm.memory.id)
        if sid and sid not in seen:
            seen.add(sid)
            out.append(sid)
    return out


def _recall_at_k(ranked, gold, k):
    return len(set(gold) & set(ranked[:k])) / len(gold) if gold else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--out", default="results/memeval/exp_memeval_episodic.json")
    args = ap.parse_args()

    # (condition, type, k) -> [recall]; counts (type)->n
    cell = defaultdict(list)
    nq = defaultdict(int)
    ku = defaultdict(list)  # knowledge_update recency: (condition,k)->[recall]

    for diff in DIFFICULTIES:
        for seed in range(args.seeds):
            dataset = Gen(random.Random(seed), **PRESETS[diff]).build()
            assert not validate(dataset)
            tag = f"{diff}-{seed}"

            # baseline engine (turns only)
            eb = MemoryEngine(Path(tempfile.mkdtemp()))
            id2sid, by_sid = _index_turns(eb, dataset, tag)
            # episodic(session) engine
            es = MemoryEngine(Path(tempfile.mkdtemp()))
            id2sid_s, by_sid_s = _index_turns(es, dataset, tag)
            sum2sid_s = _add_episode_summaries(es, dataset, tag, by_sid_s, "session")
            # episodic(facts) engine
            ef = MemoryEngine(Path(tempfile.mkdtemp()))
            id2sid_f, by_sid_f = _index_turns(ef, dataset, tag)
            sum2sid_f = _add_episode_summaries(ef, dataset, tag, by_sid_f, "facts")

            for q in dataset["questions"]:
                gold, qt = q["gold_session_ids"], q["type"]
                nq[qt] += 1
                rb = _ranked_sessions(eb, q["question"], tag, id2sid)
                rs = _ranked_sessions(es, q["question"], tag, id2sid_s, sum2sid_s)
                rf = _ranked_sessions(ef, q["question"], tag, id2sid_f, sum2sid_f)
                for k in KS:
                    cell[("baseline", qt, k)].append(_recall_at_k(rb, gold, k))
                    cell[("episodic_session", qt, k)].append(_recall_at_k(rs, gold, k))
                    cell[("episodic_facts", qt, k)].append(_recall_at_k(rf, gold, k))
                # recency on knowledge_update (baseline engine, with/without bonus)
                if qt == "knowledge_update":
                    r0 = _ranked_sessions(eb, q["question"], tag, id2sid, recency_weight=0.0)
                    r1 = _ranked_sessions(eb, q["question"], tag, id2sid, recency_weight=1.0)
                    for k in KS:
                        ku[("no_recency", k)].append(_recall_at_k(r0, gold, k))
                        ku[("recency", k)].append(_recall_at_k(r1, gold, k))
            for e in (eb, es, ef):
                e.close()
        print(f"[{diff}] done", flush=True)

    def mean(vals):
        vals = [v for v in vals if v is not None]
        return round(statistics.mean(vals), 3) if vals else None

    print("\n=== session recall@k by type — baseline vs episodic (n = Qs across difficulty×seed) ===")
    hdr = f"{'type':20}{'n':>4}  " + "  ".join(f"{c:>17}" for c in ['baseline', 'episodic_session', 'episodic_facts'])
    print(hdr)
    print("-" * len(hdr))
    report = {"seeds": args.seeds, "ks": KS, "by_type": {}}
    for t in TYPES:
        n = nq[t]
        if not n:
            continue
        row = f"{t:20}{n:>4}  "
        for cond in ["baseline", "episodic_session", "episodic_facts"]:
            vals = [f"{mean(cell[(cond,t,k)]):.2f}" if mean(cell[(cond,t,k)]) is not None else " -- " for k in KS]
            row += f"  [{'/'.join(vals)}]".ljust(19)
            report["by_type"].setdefault(t, {})[cond] = {f"recall@{k}": mean(cell[(cond,t,k)]) for k in KS}
        print(row)
    print("(cells are R@1/R@2/R@3/R@5)")

    print("\n=== knowledge_update latest-value: recency weighting (baseline engine) ===")
    print(f"{'condition':14}" + "".join(f"  R@{k}" for k in KS))
    for cond in ["no_recency", "recency"]:
        print(f"{cond:14}" + "".join(f"  {mean(ku[(cond,k)]):.2f}" for k in KS))
    report["knowledge_update_recency"] = {
        cond: {f"recall@{k}": mean(ku[(cond,k)]) for k in KS} for cond in ["no_recency", "recency"]}

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2))
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
