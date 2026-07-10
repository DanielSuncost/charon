#!/usr/bin/env python3
"""Retrieval eval over synthetic multi-session trajectories (on-device, free).

Generates controlled trajectories with `memeval_gen`, ingests each into a fresh
MemoryEngine, and measures session-level recall@k PER QUESTION TYPE and PER
DIFFICULTY — isolating exactly the axes an agentic-memory benchmark cares about:
single-session recall, knowledge-update (latest value), multi-session joins, and
temporal ordering. Because the data is authored with ground truth, we can crank
distractors/joins and watch which retrieval regime breaks.

  PYTHONPATH=src CHARON_EMBED_BACKEND=local \
    python scripts/experiments/exp_memeval.py --seeds 3
"""
import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path
import random
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts" / "experiments"))

from charon.memory.memory_engine import MemoryEngine, embed_one  # noqa: E402
from memeval_gen import Gen, PRESETS, validate  # noqa: E402

KS = [1, 2, 3, 5]
DIFFICULTIES = ["easy", "medium", "hard"]


def _rank_sessions(pairs, id2sid):
    """Memory ids (ranked) -> de-duplicated session ids in rank order."""
    out, seen = [], set()
    for mid in pairs:
        sid = id2sid.get(mid)
        if sid and sid not in seen:
            seen.add(sid)
            out.append(sid)
    return out


def _recall_at_k(ranked, gold, k):
    if not gold:
        return None
    return len(set(gold) & set(ranked[:k])) / len(gold)


def _index(engine, dataset, tag):
    """Ingest every turn of the trajectory; map memory id -> session id."""
    id2sid = {}
    for s in dataset["sessions"]:
        sid = s["session_id"]
        for ti, turn in enumerate(s["turns"]):
            if len(turn["text"]) < 12:
                continue
            mem = engine.add(turn["text"], category="event", container_tag=tag,
                             event_date=s["timestamp"].split("T")[0],
                             source_conv=sid, source_turn=ti)
            id2sid[mem.id] = sid
    return id2sid


def _retrieve(engine, query, tag, id2sid, limit=20):
    """Return ranked session lists for vector / fts / hybrid."""
    qv = embed_one(query, engine.state_dir)
    vec = _rank_sessions([i for i, _ in engine._search_vec(qv, container_tag=tag, limit=limit)], id2sid)
    fts = _rank_sessions([i for i, _ in engine._search_fts(query, container_tag=tag, limit=limit)], id2sid)
    rec = engine.recall(query, container_tag=tag, limit=limit)
    hyb = _rank_sessions([sm.memory.id for sm in rec.memories], id2sid)
    return {"vector": vec, "fts": fts, "hybrid": hyb}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=3, help="seeds per difficulty (averaged)")
    ap.add_argument("--out", default="results/memeval/exp_memeval.json")
    args = ap.parse_args()

    # (difficulty, type, mode, k) -> [recall...]
    cell = defaultdict(list)
    n_q = defaultdict(int)

    for diff in DIFFICULTIES:
        for seed in range(args.seeds):
            rng = random.Random(seed)
            dataset = Gen(rng, **PRESETS[diff]).build()
            assert not validate(dataset), f"invalid dataset {diff}/{seed}"
            tag = f"{diff}-{seed}"
            engine = MemoryEngine(Path(tempfile.mkdtemp()))
            id2sid = _index(engine, dataset, tag)
            for q in dataset["questions"]:
                modes = _retrieve(engine, q["question"], tag, id2sid)
                gold = q["gold_session_ids"]
                n_q[(diff, q["type"])] += 1
                for mode, ranked in modes.items():
                    for k in KS:
                        r = _recall_at_k(ranked, gold, k)
                        if r is not None:
                            cell[(diff, q["type"], mode, k)].append(r)
            engine.close()
        print(f"[{diff}] done ({args.seeds} seeds)", flush=True)

    # ---- report ----
    types = ["single_session", "knowledge_update", "multi_session_join", "temporal"]
    report = {"seeds": args.seeds, "ks": KS, "by_difficulty_type": {}}

    print("\n=== hybrid recall@k by difficulty × question type "
          "(n = questions across seeds) ===")
    hdr = f"{'difficulty':10}{'type':20}{'n':>4}" + "".join(f"  R@{k}" for k in KS)
    print(hdr)
    print("-" * len(hdr))
    for diff in DIFFICULTIES:
        for t in types:
            n = n_q[(diff, t)]
            if not n:
                continue
            vals = []
            for k in KS:
                v = cell[(diff, t, "hybrid", k)]
                vals.append(round(statistics.mean(v), 3) if v else None)
                report["by_difficulty_type"].setdefault(diff, {}).setdefault(t, {})[f"recall@{k}"] = vals[-1]
            print(f"{diff:10}{t:20}{n:>4}" + "".join(f"  {x:.2f}" if x is not None else "   -- " for x in vals))

    # mode comparison focused on the hard type (multi_session_join), recall@3
    print("\n=== retrieval mode on multi_session_join (recall@3) ===")
    for diff in DIFFICULTIES:
        line = f"{diff:10}"
        for mode in ("vector", "fts", "hybrid"):
            v = cell[(diff, "multi_session_join", mode, 3)]
            line += f"  {mode}={statistics.mean(v):.2f}" if v else f"  {mode}=--"
        print(line)
        report.setdefault("join_mode_recall@3", {})[diff] = {
            mode: round(statistics.mean(cell[(diff, "multi_session_join", mode, 3)]), 3)
            for mode in ("vector", "fts", "hybrid")
            if cell[(diff, "multi_session_join", mode, 3)]}

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2))
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
