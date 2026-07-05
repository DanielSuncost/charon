#!/usr/bin/env python3
"""Replay REAL recorded task episodes through the current episodic/thread
pipeline — the reality check for the synthetic benchmarks (roadmap step: run on
real Charon traces).

Reads execution/task_episodes.jsonl from a real state dir (read-only), replays
each record through create_task_episode into a FRESH scratch state, then reports
what the memory subsystem actually produced: episodes, typed events, auto-
extracted decisions (decision_extract), and sample thread()/why() output for
topics found in the data.

Honest scope — this is DESCRIPTIVE, not a scored benchmark: real traces carry no
gold labels. Known limits of the recorded data (reported below when hit):
  - `agent_id` may be empty in real records → cross-agent attribution (the WHO)
    is structurally absent until capture is fixed; threads still form but agent
    columns are blank.
  - `response_preview` is truncated at 1200 chars and tool_calls are recorded as
    a name sequence only → decision extraction sees partial text; event timing
    is task-level (completion-derived capture, not per-turn streaming).

  PYTHONPATH=apps/core-daemon CHARON_EMBED_BACKEND=local \
    python scripts/exp_real_traces.py --state .charon_state
"""
import argparse
import json
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "core-daemon"))

from execution_memory import create_task_episode  # noqa: E402
from memory_engine import MemoryEngine  # noqa: E402
import episodic as ep  # noqa: E402
import threads as th  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--state", default=".charon_state",
                    help="real state dir holding execution/task_episodes.jsonl (read-only)")
    ap.add_argument("--queries", default="",
                    help="comma-separated topic queries for sample thread()/why() output")
    ap.add_argument("--out", default="results/exp_real_traces.json")
    args = ap.parse_args()

    src = Path(args.state) / "execution" / "task_episodes.jsonl"
    if not src.exists():
        print(f"no task episodes at {src}")
        return 1
    rows = [json.loads(l) for l in src.open(encoding="utf-8") if l.strip()]
    print(f"replaying {len(rows)} real task episodes from {src}")

    scratch = Path(tempfile.mkdtemp(prefix="charon-replay-"))
    empty_agents = 0
    for i, r in enumerate(rows):
        agent = r.get("agent_id") or ""
        if not agent:
            empty_agents += 1
            agent = f"unknown-agent"
        ts = r.get("ts")
        create_task_episode(
            scratch,
            session_id=r.get("session_id") or f"replay-{i}",
            agent_id=agent,
            project_root=r.get("project_root") or "unknown",
            provider=r.get("provider") or "unknown",
            objective=r.get("objective") or "",
            summary=r.get("summary") or "",
            # recorded as a name sequence; reconstruct minimal tool_call dicts
            tool_calls=[{"tool": t} for t in (r.get("tool_sequence") or []) if t],
            response_text=r.get("response_preview") or "",
            total_turns=r.get("turns") or 0,
            input_tokens=r.get("input_tokens") or 0,
            output_tokens=r.get("output_tokens") or 0,
        )

    eng = MemoryEngine(scratch)
    tags = Counter()
    all_eps = []
    for r in rows:
        pr = str(Path(r.get("project_root") or "unknown").resolve())
        tags[f"project:{pr}"] += 1
    report = {"replayed": len(rows), "empty_agent_id": empty_agents, "containers": {}}
    print(f"\n  records with EMPTY agent_id: {empty_agents}/{len(rows)}"
          + ("  <- real capture lacks the WHO; attribution blank until fixed"
         if empty_agents else ""))

    total_events, total_decisions, auto_decisions = 0, 0, []
    for tag in tags:
        eps = ep.list_episodes(eng, tag)
        all_eps.extend(eps)
        ev_types = Counter()
        for e in eps:
            for ev in ep.get_events(eng, e.id):
                ev_types[ev.event_type] += 1
                total_events += 1
                if ev.event_type == "decision":
                    total_decisions += 1
                    try:
                        meta = json.loads(ev.details or "{}")
                    except Exception:
                        meta = {}
                    if meta.get("auto"):
                        auto_decisions.append((ev.summary, meta.get("why", "")))
        report["containers"][tag] = {"episodes": len(eps), "events": dict(ev_types)}
        print(f"\n  container {tag}")
        print(f"    episodes: {len(eps)}   events: {dict(ev_types)}")

    print(f"\n  auto-extracted decisions from real responses: {len(auto_decisions)}")
    for s, w in auto_decisions[:10]:
        print(f"    - {s[:100]}" + (f"  [why: {w[:60]}]" if w else ""))
    report["auto_decisions"] = [{"what": s, "why": w} for s, w in auto_decisions]

    queries = [q.strip() for q in args.queries.split(",") if q.strip()]
    if not queries and all_eps:
        # fall back: derive sample queries from the most common objective words
        words = Counter()
        for r in rows:
            for w in (r.get("objective") or "").lower().split():
                if len(w) > 5:
                    words[w] += 1
        queries = [w for w, _n in words.most_common(3)]
    report["sample_threads"] = {}
    for q in queries:
        biggest = max(tags, key=lambda t: tags[t])
        items = th.thread(eng, q, container_tag=biggest, limit=6)
        print(f"\n  thread({q!r}) -> {len(items)} items")
        for it in items[:6]:
            print(f"    {it.ts[:10] if it.ts else '??'} [{it.agent or 'NO-AGENT'}/"
                  f"{it.event_type}] {it.what[:90]}")
        report["sample_threads"][q] = [
            {"ts": it.ts, "agent": it.agent, "type": it.event_type, "what": it.what}
            for it in items]

    eng.close()
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(report, indent=2))
    print(f"\nwrote {args.out}  (scratch replay state: {scratch})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
