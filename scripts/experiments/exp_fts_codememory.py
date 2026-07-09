#!/usr/bin/env python3
"""Where does FTS earn its keep? Terse factoids vs fluent distractors.

LongMemEval is conversational QA — the wrong domain for a coding agent's memory.
Here we model the real failure mode of dense retrieval: a coding-memory store
mixes terse key/value factoids (the answers) with fluent prose discussing the
same topic (no answer). Dense embeddings have a verbosity/semantic bias and rank
the fluent-but-answerless passage above the terse-but-correct factoid; FTS pins
the exact key/value. Each query has one gold factoid among fluent distractors.

  PYTHONPATH=apps/core-daemon CHARON_EMBED_BACKEND=local \
    python scripts/experiments/exp_fts_codememory.py
"""
import json
import statistics
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "apps" / "core-daemon"))

from memory_engine import MemoryEngine, embed_one  # noqa: E402

# Each cluster: (query, terse gold factoid, [fluent answerless distractors]).
CLUSTERS = [
    ("what port does the embedding worker listen on?",
     "embedding_worker port = 8731",
     ["The embedding worker is a subprocess that listens for HTTP requests and serves "
      "sentence-transformer embeddings to the memory engine over a local socket.",
      "We start the embedding worker lazily; it loads the bge-base model once and keeps "
      "it warm so that every recall does not pay the model-load cost again."]),
    ("what did we set busy_timeout to on the sqlite connection?",
     "sqlite busy_timeout = 5000",
     ["The sqlite connection for the memory database is opened with WAL journaling and a "
      "busy timeout so that concurrent writers wait for the lock instead of erroring out.",
      "Without a busy timeout the memory database would raise 'database is locked' under "
      "concurrent access from the execution-memory and bridge code paths."]),
    ("what is RRF_K set to in the retrieval fusion?",
     "RRF_K = 60",
     ["Reciprocal rank fusion merges the vector and FTS result lists by summing the "
      "reciprocal of each item's rank plus a constant, so neither retriever dominates.",
      "The hybrid retriever runs a vector search and a keyword search and then fuses the "
      "two ranked lists with reciprocal rank fusion before returning the top memories."]),
    ("what cosine value is VERSION_MATCH_THRESHOLD?",
     "VERSION_MATCH_THRESHOLD = 0.80",
     ["Version chains detect when a stored fact has been superseded by a newer one and "
      "mark the old memory as not-latest so the chain can be walked later.",
      "When a new memory is added we compare it by cosine similarity against existing "
      "memories to decide whether it is a duplicate, an update, or a genuinely new fact."]),
    ("what max_tokens does the shade conversation engine use?",
     "shade engine max_tokens = 16384",
     ["A shade is an ephemeral worker agent that runs its own conversation engine in a "
      "background thread and is restricted to the files in its contract scope.",
      "The shade orchestrator drives each phase of a contract, spawning the conversation "
      "engine and feeding it the phase instruction until the contract completes."]),
    ("what default does CHARON_HEARTBEAT_INTERVAL have?",
     "CHARON_HEARTBEAT_INTERVAL default = 30",
     ["On each heartbeat the daemon checks whether consolidation should run, refreshes "
      "soft specialization, and advances any running judge loops by a single step.",
      "The heartbeat is how background work is scheduled in the main loop without "
      "blocking task processing; it fires every so many cycles of the loop."]),
    ("which file holds the changed_paths_under helper?",
     "changed_paths_under is in checkpoint_manager.py",
     ["The frozen-path gate diffs the post-implementation checkpoint against the best "
      "checkpoint and rejects the iteration if any frozen path changed.",
      "Checkpoints use a shadow git repository with a separate GIT_DIR so the user's own "
      "working tree and .git are never touched by snapshot or rollback."]),
    ("what is STOCHASTIC_JUDGE_MIN_DELTA?",
     "STOCHASTIC_JUDGE_MIN_DELTA = 0.5",
     ["Stochastic LLM judges have a measurable score-noise floor, so the minimum "
      "improvement delta must exceed it or the loop will hill-climb judge noise.",
      "The aesthetic judge scores code against a rubric using an LLM, which means its "
      "scores vary run to run unlike the deterministic quantitative judge."]),
    ("what version did we pin sqlite-vec to?",
     "sqlite-vec pinned >= 0.1.6",
     ["sqlite-vec is the extension that adds a vec0 virtual table for approximate nearest "
      "neighbor search directly inside sqlite, which is how recall stays on-device.",
      "If the sqlite-vec extension fails to load, the schema creation for the vector table "
      "is skipped and recall silently degrades to FTS-only keyword search."]),
    ("what port does the lmstudio provider use by default?",
     "lmstudio provider port = 1234",
     ["The lmstudio provider speaks the OpenAI-compatible streaming protocol over httpx "
      "with no SDK dependency, parsing SSE chunks and inline think blocks itself.",
      "Local models are served through the lmstudio provider, which points at a local "
      "OpenAI-compatible endpoint instead of a cloud API."]),
]

FILLER = [
    "The user prefers dark mode terminals and tabs over spaces.",
    "The mascot image lives under the assets directory.",
    "Charon runs entirely on the local machine with no cloud recall.",
    "The TUI is written in Rust using crossterm and the vte crate.",
    "Harbor dispatches voyages to remote machines over SSH and ingests results.",
    "Conversation rooms let two or more agents discuss a topic with turn-taking.",
]


def top_ids(pairs):
    seen, out = set(), []
    for mid, _ in pairs:
        if mid not in seen:
            seen.add(mid)
            out.append(mid)
    return out


def main():
    eng = MemoryEngine(Path(tempfile.mkdtemp()))
    gold = {}
    for query, gold_text, distractors in CLUSTERS:
        m = eng.add(gold_text, container_tag="code", category="event", check_updates=False)
        gold[query] = m.id
        for d in distractors:
            eng.add(d, container_tag="code", category="event", check_updates=False)
    for f in FILLER:
        eng.add(f, container_tag="code", category="event", check_updates=False)

    modes = {"vector_only": [], "fts_only": [], "hybrid_rrf": []}
    detail = []
    for query, _gold_text, _ in CLUSTERS:
        g = gold[query]
        qv = embed_one(query, eng.state_dir)
        vec = top_ids(eng._search_vec(qv, container_tag="code", limit=20))
        fts = top_ids(eng._search_fts(query, container_tag="code", limit=20))
        hyb = top_ids([(sm.memory.id, sm.score) for sm in
                       eng.recall(query, container_tag="code", limit=20).memories])

        def at(ids, k, g=g):  # bind g: called within this iteration only (B023 false positive)
            return 1.0 if g in ids[:k] else 0.0
        modes["vector_only"].append((at(vec, 1), at(vec, 3)))
        modes["fts_only"].append((at(fts, 1), at(fts, 3)))
        modes["hybrid_rrf"].append((at(hyb, 1), at(hyb, 3)))
        detail.append({"query": query, "vec@1": at(vec, 1), "fts@1": at(fts, 1),
                       "hyb@1": at(hyb, 1), "vec_rank_gold": (vec.index(g) + 1) if g in vec else None})

    report = {"n_queries": len(CLUSTERS), "by_mode": {}, "detail": detail}
    for m, rows in modes.items():
        report["by_mode"][m] = {"recall@1": round(statistics.mean(r[0] for r in rows), 3),
                                "recall@3": round(statistics.mean(r[1] for r in rows), 3)}
    out = ROOT / "results" / "exp_fts_codememory.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2))
    print(f"{'mode':14} {'recall@1':>9} {'recall@3':>9}")
    for m, r in report["by_mode"].items():
        print(f"{m:14} {r['recall@1']:>9} {r['recall@3']:>9}")
    helped = [d["query"] for d in detail if d["vec@1"] == 0 and d["hyb@1"] == 1]
    print(f"\nvector missed @1, hybrid recovered @1: {len(helped)}/{len(detail)}")
    for d in detail:
        if d["vec@1"] == 0:
            print(f"  vec missed (gold rank {d['vec_rank_gold']}): {d['query']}")
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
