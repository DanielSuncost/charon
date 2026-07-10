#!/usr/bin/env python3
"""LongMemEval_S benchmark runner for Charon's semantic memory engine.

The benchmark has two independent stages:
  1. RETRIEVAL — our memory engine indexes sessions and retrieves relevant ones.
     This is what we're testing. No LLM needed (just embeddings).
  2. READING — a fixed LLM (GPT-4o) answers the question given retrieved context.
     This is a controlled variable — same model for everyone.

This separation means the score measures our MEMORY SYSTEM quality,
not our LLM quality.

Usage:
    # Full pipeline (retrieve + read with GPT-4o):
    python scripts/experiments/bench_longmemeval.py --reader-provider openai --reader-model gpt-4o

    # Retrieval-only (outputs retrieval metrics, no reader LLM needed):
    python scripts/experiments/bench_longmemeval.py --retrieval-only

    # Smoke test (2 questions):
    python scripts/experiments/bench_longmemeval.py --limit 2 --retrieval-only
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

DATA_URL = "https://huggingface.co/datasets/xiaowu0162/longmemeval-cleaned/resolve/main/longmemeval_s_cleaned.json"
DATA_DIR = ROOT / "data" / "longmemeval"
DATA_FILE = DATA_DIR / "longmemeval_s_cleaned.json"
RESULTS_DIR = ROOT / "results" / "longmemeval"


def download_data():
    if DATA_FILE.exists():
        return
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    print("Downloading LongMemEval_S...")
    import httpx
    resp = httpx.get(DATA_URL, follow_redirects=True, timeout=120)
    resp.raise_for_status()
    DATA_FILE.write_bytes(resp.content)
    print(f"Downloaded {len(resp.content) / 1024 / 1024:.1f} MB")


def load_data() -> list[dict]:
    return json.loads(DATA_FILE.read_text())


# ── Stage 1: Retrieval (embedding-only, no LLM) ────────────────────

def index_and_retrieve(item: dict, engine_class, limit: int = 30) -> dict:
    """Index all sessions as raw turns, then retrieve for the question.

    This is a PURE RETRIEVAL benchmark — no LLM extraction.
    We index every user turn as a memory (with its session date),
    then use hybrid search to find the most relevant ones.

    Returns retrieval results with session-level scoring.
    """
    import tempfile
    qid = item["question_id"]
    question = item["question"]
    sessions = item["haystack_sessions"]
    dates = item["haystack_dates"]
    session_ids = item["haystack_session_ids"]

    state_dir = Path(tempfile.mkdtemp())
    engine = engine_class(state_dir)

    # Index: every user turn, tagged with its session ID
    turn_to_session = {}
    for _si, (session, date, sid) in enumerate(zip(sessions, dates, session_ids, strict=False)):
        date_normalized = date.split(" ")[0].replace("/", "-") if date else None
        for ti, turn in enumerate(session):
            content = turn.get("content", "")
            if isinstance(content, list):
                content = " ".join(b.get("text", "") for b in content if isinstance(b, dict))
            if len(content) < 20:
                continue

            mem = engine.add(
                content[:2000],
                category="event",
                container_tag=qid,
                event_date=date_normalized,
                source_conv=sid,
                source_turn=ti,
                check_updates=False,  # skip version detection for speed
            )
            turn_to_session[mem.id] = sid

    # Retrieve
    result = engine.recall(question, container_tag=qid, limit=limit)

    # Score sessions by their best-ranked turn
    session_scores: dict[str, float] = {}
    for sm in result.memories:
        sid = turn_to_session.get(sm.memory.id) or sm.memory.source_conv
        if sid and (sid not in session_scores or sm.score > session_scores[sid]):
            session_scores[sid] = sm.score

    ranked_sessions = sorted(session_scores.items(), key=lambda x: x[1], reverse=True)

    engine.close()

    return {
        "question_id": qid,
        "ranked_sessions": [sid for sid, _ in ranked_sessions],
        "session_scores": {sid: score for sid, score in ranked_sessions},
        "num_indexed": engine.count(qid) if hasattr(engine, '_db') and engine._db else len(turn_to_session),
        "recall_ms": result.timing_ms,
    }


def evaluate_retrieval(items: list[dict], retrieval_results: dict[str, dict]) -> dict:
    """Compute retrieval recall@K metrics."""
    ks = [1, 3, 5, 10, 20]
    recall_at_k = {k: [] for k in ks}
    by_type = {}

    for item in items:
        qid = item["question_id"]
        qtype = item["question_type"]
        gold_sessions = set(item["answer_session_ids"])

        if qid not in retrieval_results:
            continue

        ranked = retrieval_results[qid]["ranked_sessions"]

        for k in ks:
            retrieved_set = set(ranked[:k])
            if gold_sessions:
                recall = len(gold_sessions & retrieved_set) / len(gold_sessions)
            else:
                recall = 1.0 if not retrieved_set else 0.0
            recall_at_k[k].append(recall)

            by_type.setdefault(qtype, {k2: [] for k2 in ks})
            by_type[qtype][k].append(recall)

    import numpy as np
    summary = {}
    for k in ks:
        vals = recall_at_k[k]
        summary[f"recall@{k}"] = float(np.mean(vals)) if vals else 0.0

    type_summary = {}
    for qtype, kvals in by_type.items():
        type_summary[qtype] = {
            f"recall@{k}": float(np.mean(v)) if v else 0.0
            for k, v in kvals.items()
        }

    return {"overall": summary, "by_type": type_summary}


# ── Stage 2: Reading (fixed LLM, controlled variable) ──────────────

READER_PROMPTS = {
    "default": """I will give you several history chats between you and a user, sorted in chronological order. Please answer the question based on the relevant chat history.

Read each session carefully — the answer may be mentioned briefly or in passing. Do not assume information is missing without checking every session.

History Chats:

{context}

Current Date: {question_date}
Question: {question}
Answer:""",

    "temporal-reasoning": """I will give you several history chats between you and a user, sorted in chronological order (earliest first). Please answer the question based on the relevant chat history.

IMPORTANT: This question involves dates and time. Follow these steps:
1. First, identify each relevant event and its exact date from the chat sessions.
2. List the events with their dates.
3. Then compute the answer (days between dates, chronological ordering, etc.).

Read each session carefully — dates and events may be mentioned in passing.

History Chats:

{context}

Current Date: {question_date}
Question: {question}
Answer (show your date reasoning step by step, then give the final answer):""",

    "multi-session": """I will give you several history chats between you and a user, sorted in chronological order. Please answer the question based on the relevant chat history.

IMPORTANT: The answer requires combining information from multiple sessions. Follow these steps:
1. First, identify and list every relevant fact from each session.
2. Then combine/count/aggregate them to answer the question.

Read each session carefully — relevant details may be mentioned in passing.

History Chats:

{context}

Current Date: {question_date}
Question: {question}
Answer (enumerate the relevant facts first, then give the final answer):""",

    "knowledge-update": """I will give you several history chats between you and a user, sorted in chronological order (earliest first). Please answer the question based on the relevant chat history.

IMPORTANT: Information may have been updated over time. The sessions are in chronological order — if a fact appears in an earlier session and then a different value appears in a later session, the LATER value is the current one. Always prefer the most recent information.

Read each session carefully — updates may be mentioned in passing.

History Chats:

{context}

Current Date: {question_date}
Question: {question}
Answer (use the most recent value):""",

    "single-session-preference": """I will give you several history chats between you and a user, sorted in chronological order. The user is asking for a personalized recommendation or advice.

IMPORTANT: Look carefully at the user's stated preferences, habits, tools, and interests throughout the chat history. Your response should be specifically tailored to what the user has told you about themselves. Reference their specific preferences in your answer.

History Chats:

{context}

Current Date: {question_date}
Question: {question}
Answer (personalize based on the user's stated preferences):""",
}


def make_reader_call(provider: str, model: str):
    """Create a reader LLM call function. This should be GPT-4o for fair comparison."""
    import httpx

    if provider == "openai":
        def call(messages: list[dict]) -> str:
            api_key = os.environ.get("OPENAI_API_KEY", "")
            resp = httpx.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={"model": model, "messages": messages, "temperature": 0, "max_tokens": 512},
                timeout=60,
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
        return call

    elif provider == "lmstudio":
        def call(messages: list[dict]) -> str:
            base = os.environ.get("LMSTUDIO_BASE_URL", "http://localhost:1234/v1")
            resp = httpx.post(
                f"{base}/chat/completions",
                json={"model": model, "messages": messages, "temperature": 0, "max_tokens": 512},
                timeout=60,
            )
            resp.raise_for_status()
            text = resp.json()["choices"][0]["message"]["content"]
            text = re.sub(r'<think>.*?</think>\s*', '', text, flags=re.DOTALL).strip()
            return text
        return call

    elif provider == "openrouter":
        def call(messages: list[dict]) -> str:
            api_key = os.environ.get("OPENROUTER_API_KEY", "")
            resp = httpx.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={"model": model, "messages": messages, "temperature": 0, "max_tokens": 512},
                timeout=120,
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
        return call

    elif provider == "anthropic":
        def call(messages: list[dict]) -> str:
            api_key = os.environ.get("ANTHROPIC_API_KEY", "")
            resp = httpx.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                json={"model": model, "messages": messages, "max_tokens": 512, "temperature": 0},
                timeout=60,
            )
            resp.raise_for_status()
            return resp.json()["content"][0]["text"]
        return call

    raise ValueError(f"Unknown provider: {provider}")


def format_retrieved_sessions(item: dict, ranked_session_ids: list[str], topk: int = 10) -> str:
    """Format top-K retrieved sessions, sorted chronologically."""
    sid_to_idx = {sid: i for i, sid in enumerate(item["haystack_session_ids"])}

    # Collect top-K sessions with their dates
    selected = []
    for sid in ranked_session_ids[:topk]:
        if sid not in sid_to_idx:
            continue
        idx = sid_to_idx[sid]
        selected.append((item["haystack_dates"][idx], idx, sid))

    # Sort by date (chronological order)
    selected.sort(key=lambda x: x[0])

    parts = []
    for date, idx, _sid in selected:
        session = item["haystack_sessions"][idx]
        session_lines = [f"[{date}]"]
        for turn in session:
            content = turn.get("content", "")
            if isinstance(content, list):
                content = " ".join(b.get("text", "") for b in content if isinstance(b, dict))
            session_lines.append(f"{turn['role']}: {content}")
        parts.append("\n".join(session_lines))
    return "\n\n---\n\n".join(parts)


# ── Main ────────────────────────────────────────────────────────────

def run_benchmark(args):
    download_data()
    data = load_data()
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    if args.limit:
        data = data[:args.limit]

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    total = len(data)

    # Stage 1: Retrieval (skip if --retrieval-file provided)
    if args.retrieval_file:
        print(f"═══ Stage 1: Loading retrieval from {args.retrieval_file} ═══")
        saved = json.loads(Path(args.retrieval_file).read_text())
        retrieval_results = saved["results"]
        metrics = saved["metrics"]
        print(f"  Loaded {len(retrieval_results)} retrieval results")
        for k, v in metrics["overall"].items():
            print(f"  {k}: {v:.4f}")
    else:
        from charon.memory.memory_engine import MemoryEngine

        print(f"═══ Stage 1: Retrieval ({total} questions) ═══")
        print("Memory engine: embedding-only indexing + hybrid recall")
        print()

        retrieval_results = {}
        t0_total = time.monotonic()

        for qi, item in enumerate(data):
            qid = item["question_id"]
            qtype = item["question_type"]
            question = item["question"]
            n_sessions = len(item["haystack_sessions"])

            t0 = time.monotonic()
            result = index_and_retrieve(item, MemoryEngine, limit=30)
            elapsed = time.monotonic() - t0

            retrieval_results[qid] = result
            gold = set(item["answer_session_ids"])
            top5 = set(result["ranked_sessions"][:5])
            hit = "✓" if gold & top5 else "✗"

            print(f"  [{qi+1}/{total}] {hit} {qtype:30s} | {n_sessions} sess → {result['num_indexed']} indexed | "
                  f"recall: {result['recall_ms']:.0f}ms | total: {elapsed:.1f}s | Q: {question[:50]}...")

        retrieval_time = time.monotonic() - t0_total

        # Retrieval metrics
        metrics = evaluate_retrieval(data, retrieval_results)
        print(f"\n═══ Retrieval Results ({retrieval_time:.0f}s total) ═══")
        for k, v in metrics["overall"].items():
            print(f"  {k}: {v:.4f}")
        print()
        for qtype, kvals in sorted(metrics["by_type"].items()):
            r5 = kvals.get("recall@5", 0)
            r10 = kvals.get("recall@10", 0)
            print(f"  {qtype:35s} R@5={r5:.3f}  R@10={r10:.3f}")

        # Save retrieval results
        ret_file = RESULTS_DIR / f"retrieval_{timestamp}.json"
        ret_file.write_text(json.dumps({
            "metrics": metrics,
            "results": retrieval_results,
        }, indent=2))
        print(f"\nSaved to {ret_file}")

    if args.retrieval_only:
        return

    # Stage 2: Reading (fixed LLM)
    print(f"\n═══ Stage 2: Reading with {args.reader_provider}/{args.reader_model} ═══")
    reader_call = make_reader_call(args.reader_provider, args.reader_model)
    safe_model_name = args.reader_model.replace("/", "_")
    hyp_file = RESULTS_DIR / f"hyp_{safe_model_name}_{timestamp}.jsonl"

    # Resume: load existing hypotheses and skip already-completed questions
    existing_hyps = {}
    if args.resume and Path(args.resume).exists():
        hyp_file = Path(args.resume)
        for line in open(args.resume):
            h = json.loads(line)
            if not str(h.get("hypothesis", "")).startswith("Error:"):
                existing_hyps[h["question_id"]] = h
        print(f"  Resuming: {len(existing_hyps)} valid answers loaded, skipping those")

    hypotheses = list(existing_hyps.values())
    for qi, item in enumerate(data):
        qid = item["question_id"]
        if qid in existing_hyps:
            continue
        qtype = item["question_type"]
        ranked = retrieval_results[qid]["ranked_sessions"]
        context = format_retrieved_sessions(item, ranked, topk=args.topk)

        # Select type-aware prompt
        prompt_template = READER_PROMPTS.get(qtype, READER_PROMPTS["default"])
        prompt = prompt_template.format(
            context=context,
            question_date=item["question_date"],
            question=item["question"],
        )

        try:
            hypothesis = reader_call([{"role": "user", "content": prompt}])
        except Exception as e:
            hypothesis = f"Error: {e}"

        hypotheses.append({"question_id": qid, "hypothesis": hypothesis})
        print(f"  [{qi+1}/{total}] {item['question_type']:30s} | A: {hypothesis[:80]}...")

        # Write incrementally
        with open(hyp_file, "w") as f:
            for h in hypotheses:
                f.write(json.dumps(h) + "\n")

    print("\n═══ Done ═══")
    print(f"Hypotheses: {hyp_file}")
    print("\nTo evaluate:")
    print("  export OPENAI_API_KEY=...")
    print("  cd /tmp/LongMemEval/src/evaluation")
    print(f"  python evaluate_qa.py gpt-4o {hyp_file} {DATA_FILE}")


def main():
    parser = argparse.ArgumentParser(description="LongMemEval_S benchmark for Charon memory engine")
    parser.add_argument("--limit", type=int, default=None, help="Limit number of questions")
    parser.add_argument("--retrieval-only", action="store_true", help="Only run retrieval stage (no reader LLM needed)")
    parser.add_argument("--retrieval-file", type=str, default=None, help="Reuse retrieval results from a previous run")
    parser.add_argument("--reader-provider", default="openai", choices=["openai", "openrouter", "lmstudio", "anthropic"])
    parser.add_argument("--reader-model", default="gpt-4o")
    parser.add_argument("--topk", type=int, default=10, help="Number of sessions to feed to reader")
    parser.add_argument("--resume", type=str, default=None, help="Resume from an existing hypothesis file (skips valid answers)")
    args = parser.parse_args()
    run_benchmark(args)


if __name__ == "__main__":
    main()
