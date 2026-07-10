#!/usr/bin/env python3
"""When does FTS beat dense? As the store grows and keys are opaque ids.

A coding agent recalls things by exact id: task ids, commit hashes, request ids,
run ids. Such ids are out-of-vocabulary for a sentence embedder — they all embed
to nearly the same place — so as the store fills with similar records, dense
retrieval confuses them and recall@1 falls. Exact keyword match does not.

We build N records of the form "record <id>: <fact>", query by a held id, and
measure recall@1 for vector-only / FTS-only / hybrid+RRF as N grows.

  PYTHONPATH=src CHARON_EMBED_BACKEND=local \
    python scripts/experiments/exp_fts_scale.py
"""
import hashlib
import json
import statistics
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from charon.memory.memory_engine import MemoryEngine, embed_one  # noqa: E402

FACTS = [
    "rotated the api credentials and restarted the worker pool",
    "bumped the cache ttl and cleared the stale entries",
    "patched the retry backoff and re-ran the failing jobs",
    "migrated the schema and backfilled the missing rows",
    "tuned the batch size and reduced the p99 latency",
    "fixed the off-by-one in the pagination cursor",
    "disabled the flaky integration test and filed a ticket",
    "upgraded the dependency and pinned the new version",
]


def _id(i):
    # opaque, id-like keys (hash hex) — out-of-vocabulary for the embedder
    return "rec-" + hashlib.sha1(str(i).encode()).hexdigest()[:10]


def top_ids(pairs):
    seen, out = set(), []
    for mid, _ in pairs:
        if mid not in seen:
            seen.add(mid)
            out.append(mid)
    return out


def run_size(n, n_probe=20):
    eng = MemoryEngine(Path(tempfile.mkdtemp()))
    ids, golds = [], {}
    for i in range(n):
        rid = _id(i)
        fact = FACTS[i % len(FACTS)]
        m = eng.add(f"record {rid}: {fact}", container_tag="s", category="event", check_updates=False)
        ids.append(rid)
        golds[rid] = m.id
    # probe a spread of ids
    probes = [ids[int(j * (n - 1) / (n_probe - 1))] for j in range(min(n_probe, n))]
    res = {"vector_only": [], "fts_only": [], "hybrid_rrf": []}
    for rid in probes:
        g = golds[rid]
        q = f"what did record {rid} do?"
        qv = embed_one(q, eng.state_dir)
        vec = top_ids(eng._search_vec(qv, container_tag="s", limit=10))
        fts = top_ids(eng._search_fts(q, container_tag="s", limit=10))
        hyb = top_ids([(sm.memory.id, sm.score) for sm in
                       eng.recall(q, container_tag="s", limit=10).memories])
        res["vector_only"].append(1.0 if g in vec[:1] else 0.0)
        res["fts_only"].append(1.0 if g in fts[:1] else 0.0)
        res["hybrid_rrf"].append(1.0 if g in hyb[:1] else 0.0)
    eng.close()
    return {m: round(statistics.mean(v), 3) for m, v in res.items()}


def main():
    sizes = [20, 50, 100, 250, 500]
    report = {"recall@1_by_size": {}}
    print(f"{'N':>5} {'vector':>8} {'fts':>8} {'hybrid':>8}")
    for n in sizes:
        r = run_size(n)
        report["recall@1_by_size"][n] = r
        print(f"{n:>5} {r['vector_only']:>8} {r['fts_only']:>8} {r['hybrid_rrf']:>8}")
    out = ROOT / "results" / "exp_fts_scale.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2))
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
