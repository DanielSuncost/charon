#!/usr/bin/env python3
"""Experiment: retrieval ablations on LongMemEval_S, per question type.

Holds the index fixed and retrieves three ways — vector-only, FTS-only, and
hybrid+RRF — to measure what RRF actually buys, broken out by question category
(the hard categories are multi-session / temporal-reasoning / knowledge-update).
Also a version-chain ablation on knowledge-update: does indexing with update
detection (is_latest chains) change recall on the category it's meant to help?

No API: pure on-device retrieval (bge-base + sqlite-vec + FTS5).

  PYTHONPATH=apps/core-daemon CHARON_EMBED_BACKEND=local \
    python scripts/exp_memory_ablation.py --per-type 10 --topk 5
"""
import argparse
import json
import statistics
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "core-daemon"))

from memory_engine import MemoryEngine, embed_one  # noqa: E402

DATA = ROOT / "data" / "longmemeval" / "longmemeval_s_cleaned.json"


def _index(engine, item, check_updates=False):
    id2sid = {}
    for session, date, sid in zip(item["haystack_sessions"], item["haystack_dates"],
                                  item["haystack_session_ids"]):
        dn = date.split(" ")[0].replace("/", "-") if date else None
        for ti, turn in enumerate(session):
            c = turn.get("content", "")
            if isinstance(c, list):
                c = " ".join(b.get("text", "") for b in c if isinstance(b, dict))
            if len(c) < 20:
                continue
            mem = engine.add(c[:2000], category="event", container_tag=item["question_id"],
                             event_date=dn, source_conv=sid, source_turn=ti, check_updates=check_updates)
            id2sid[mem.id] = sid
    return id2sid


def _sessions_from_ids(ids, id2sid):
    out, seen = [], set()
    for mid in ids:
        sid = id2sid.get(mid)
        if sid and sid not in seen:
            seen.add(sid); out.append(sid)
    return out


def _recall_at_k(ranked_sessions, gold, k):
    if not gold:
        return None
    top = set(ranked_sessions[:k])
    return len(set(gold) & top) / len(gold)


def retrieve_modes(engine, item, id2sid, topk_turns=40):
    qid, q = item["question_id"], item["question"]
    qv = embed_one(q, engine.state_dir)
    vec = _sessions_from_ids([i for i, _ in engine._search_vec(qv, container_tag=qid, limit=topk_turns)], id2sid)
    fts = _sessions_from_ids([i for i, _ in engine._search_fts(q, container_tag=qid, limit=topk_turns)], id2sid)
    rec = engine.recall(q, container_tag=qid, limit=topk_turns)
    hyb_sessions, seen = [], set()
    for sm in rec.memories:
        sid = id2sid.get(sm.memory.id) or sm.memory.source_conv
        if sid and sid not in seen:
            seen.add(sid); hyb_sessions.append(sid)
    return {"vector_only": vec, "fts_only": fts, "hybrid_rrf": hyb_sessions}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--per-type", type=int, default=10)
    ap.add_argument("--topk", type=int, default=5)
    ap.add_argument("--ks", default="1,2,3,5")
    ap.add_argument("--out", default="results/exp_memory_ablation.json")
    args = ap.parse_args()
    KS = [int(x) for x in args.ks.split(",")]

    data = json.loads(DATA.read_text())
    by_type = defaultdict(list)
    for q in data:
        by_type[q["question_type"]].append(q)
    sample = []
    for t, qs in by_type.items():
        sample.extend(qs[:args.per_type])

    # main ablation: per type -> mode -> k -> [recall]
    per = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    t0 = time.time()
    for i, item in enumerate(sample):
        eng = MemoryEngine(Path(tempfile.mkdtemp()))
        id2sid = _index(eng, item)
        modes = retrieve_modes(eng, item, id2sid)
        gold = item["answer_session_ids"]
        for mode, ranked in modes.items():
            for k in KS:
                r = _recall_at_k(ranked, gold, k)
                if r is not None:
                    per[item["question_type"]][mode][k].append(r)
        eng.close()
        if (i + 1) % 10 == 0:
            print(f"  {i+1}/{len(sample)} ({time.time()-t0:.0f}s)", flush=True)

    report = {"per_type": args.per_type, "ks": KS, "by_type": {}, "overall": {}}
    agg = defaultdict(lambda: defaultdict(list))
    for qtype, modes in per.items():
        report["by_type"][qtype] = {
            m: {f"recall@{k}": round(statistics.mean(v[k]), 3) for k in KS if v[k]}
            for m, v in modes.items()}
        for m, v in modes.items():
            for k in KS:
                agg[m][k].extend(v[k])
    report["overall"] = {m: {f"recall@{k}": round(statistics.mean(v[k]), 3) for k in KS if v[k]}
                         for m, v in agg.items()}

    # version-chain ablation on knowledge-update (smaller sample), at recall@1
    ku = by_type["knowledge-update"][:max(6, args.per_type)]
    vc = {"updates_off": defaultdict(list), "updates_on": defaultdict(list)}
    for item in ku:
        gold = item["answer_session_ids"]
        for flag, key in ((False, "updates_off"), (True, "updates_on")):
            eng = MemoryEngine(Path(tempfile.mkdtemp()))
            id2sid = _index(eng, item, check_updates=flag)
            rec = eng.recall(item["question"], container_tag=item["question_id"], limit=40)
            ranked, seen = [], set()
            for sm in rec.memories:
                sid = id2sid.get(sm.memory.id) or sm.memory.source_conv
                if sid and sid not in seen:
                    seen.add(sid); ranked.append(sid)
            for k in KS:
                r = _recall_at_k(ranked, gold, k)
                if r is not None:
                    vc[key][k].append(r)
            eng.close()
    report["version_chain_on_knowledge_update"] = {
        key: {f"recall@{k}": round(statistics.mean(v[k]), 3) for k in KS if v[k]}
        for key, v in vc.items()}

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(report, indent=2))
    print("overall:", report["overall"])
    print("version-chain (knowledge-update):", report["version_chain_on_knowledge_update"])
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
