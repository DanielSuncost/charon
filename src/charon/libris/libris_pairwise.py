"""In-loop pairwise regression gate for the Libris revision loop.

The keep-if-better hill-climb (libris_runtime.select_best_checkpoint) is a pure,
deterministic guard: it prefers the incumbent on a score tie and vetoes rounds
that collapse depth for a marginal score gain. But the judge's *absolute* score
is coarse — two checkpoints can score the same while one is materially better.
This module adds the missing signal: a cheap, blind, order-swapped PAIRWISE
judgment of the newest checkpoint against the running best. If the new round
loses the head-to-head, it is demoted (pairwise_rejected) and the draft reverts
to the incumbent — catching regressions the absolute score cannot see.

Cheap by construction: the gate only spends LLM calls when the numeric selector
would actually deliver the newest checkpoint (i.e. it scored highest). If the
pure selector already prefers an earlier checkpoint, there is nothing to gate.
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Callable

try:
    from charon.infra.diagnostics import record as _diag
except Exception:  # diagnostics is best-effort and must never block import
    def _diag(*args, **kwargs):
        return None


_RUBRIC = """You are a rigorous scientific-review editor. Two research reports answer the SAME question:
"%s"

Decide which report is better overall, weighing coverage/depth, citation quality and
breadth, honesty about what is established vs contested, actionability, and clarity.
A report that is broader but noticeably shallower or thinner is NOT better.

Respond ONLY with JSON: {"winner":"A|B|tie","reason":"one sentence"}"""


async def _judge_once(engine, question: str, report_A: str, report_B: str) -> dict:
    prompt = (_RUBRIC % question
              + "\n\n=== REPORT A ===\n" + (report_A or "")[:14000]
              + "\n\n=== REPORT B ===\n" + (report_B or "")[:14000]
              + "\n\nReturn the JSON verdict now.")
    resp, _ = await engine.submit_and_collect(prompt)
    m = re.search(r"\{.*\}", resp or "", re.DOTALL)
    if not m:
        raise RuntimeError(f"no JSON in pairwise judge response: {(resp or '')[:200]}")
    v = json.loads(m.group(0))
    w = str(v.get("winner") or "tie").strip().upper()
    v["winner"] = w if w in ("A", "B") else "TIE"
    return v


def default_engine_factory(state_dir: Path, project_root: Path):
    """A lightweight shade-tier engine for pairwise judging (imported lazily so the
    pure runtime path never pays the conversation-engine import cost)."""
    from charon.conversation.conversation_engine import ConversationEngine
    from charon.providers.model_registry import get_shade_provider_and_model
    provider, model, _ = get_shade_provider_and_model(state_dir, phase_name="research",
                                                      task_complexity="complex")
    return ConversationEngine(
        provider=provider, model=model, project_root=project_root,
        agent_id="libris-pairwise-gate", agent_name="pairwise",
        system_prompt="You are a precise, unbiased scientific-review editor. Output only JSON.",
        state_dir=state_dir, max_tokens=1024, max_turns=2,
    )


async def judge_pair(question: str, report_old: str, report_new: str, *,
                     engine_factory: Callable, swap: bool = True) -> dict:
    """Blind pairwise verdict of NEW vs OLD. When swap=True the pair is judged
    twice with A/B flipped to cancel position bias; NEW must strictly win the
    aggregate for 'new', OLD must strictly win for 'old', else 'tie'."""
    v1 = await _judge_once(engine_factory(), question, report_old, report_new)  # A=old, B=new
    verdicts = [v1]
    new_wins = 1 if v1["winner"] == "B" else 0
    old_wins = 1 if v1["winner"] == "A" else 0
    if swap:
        v2 = await _judge_once(engine_factory(), question, report_new, report_old)  # A=new, B=old
        verdicts.append(v2)
        new_wins += 1 if v2["winner"] == "A" else 0
        old_wins += 1 if v2["winner"] == "B" else 0

    if new_wins > old_wins:
        winner = "new"
    elif old_wins > new_wins:
        winner = "old"
    else:
        winner = "tie"
    return {
        "winner": winner,
        "new_wins": new_wins,
        "old_wins": old_wins,
        "reasons": [str(v.get("reason") or "") for v in verdicts],
    }


def _topic_question(state_dir: Path, project_root: Path, operation_id: str, topic_slug: str) -> str:
    from charon.libris.libris_runtime import topic_dir, _read_json
    tj = _read_json(topic_dir(state_dir, project_root, operation_id, topic_slug) / "topic.json", {})
    fq = tj.get("focus_questions") or []
    return str(tj.get("title") or (fq[0] if fq else "") or topic_slug)


async def agate_latest_checkpoint(state_dir: Path, project_root: Path, operation_id: str,
                                  topic_slug: str, *, question: str = "",
                                  engine_factory: Callable | None = None,
                                  swap: bool = True) -> dict:
    """Gate the newest checkpoint against the running best. Returns a dict with
    'ran' (whether an LLM judgment happened) and 'rejected_latest' (whether the
    newest checkpoint was demoted and the draft reverted to the incumbent)."""
    from charon.libris.libris_runtime import (
        list_checkpoints, select_best_checkpoint, mark_checkpoint_pairwise_rejected,
        revert_topic_draft_to_best, _safe_read_text,
    )

    items = list_checkpoints(state_dir, project_root, operation_id, topic_slug)
    if len(items) < 2:
        return {"ran": False, "reason": "need_two_checkpoints", "rejected_latest": False}

    latest = items[-1]
    # Cheap-skip: if the deterministic selector already prefers an earlier
    # checkpoint, the newest one won't be delivered — no LLM call needed.
    current_best = select_best_checkpoint(state_dir, project_root, operation_id, topic_slug)
    if current_best.get("checkpoint_id") != latest.get("checkpoint_id"):
        return {"ran": False, "reason": "latest_not_selected", "rejected_latest": False,
                "kept_checkpoint_id": current_best.get("checkpoint_id")}

    # Compare the newest checkpoint to the best of the PRIOR checkpoints.
    prior = [it for it in items if it.get("checkpoint_id") != latest.get("checkpoint_id")]
    incumbent = select_best_checkpoint  # reuse selection semantics over the prior set
    # select_best_checkpoint reads from disk (all checkpoints); pick incumbent from
    # the prior subset directly to avoid re-selecting the latest.
    def _key(it):
        from charon.libris.libris_runtime import _ckpt_norm_score
        return (_ckpt_norm_score(it), -int(it.get("iteration") or 0))
    incumbent = sorted([it for it in prior if not it.get("pairwise_rejected")] or prior,
                       key=_key, reverse=True)[0]

    old_md = _safe_read_text(str(incumbent.get("report_path") or ""))
    new_md = _safe_read_text(str(latest.get("report_path") or ""))
    if not old_md.strip() or not new_md.strip():
        return {"ran": False, "reason": "missing_report_text", "rejected_latest": False}

    q = question or _topic_question(state_dir, project_root, operation_id, topic_slug)
    factory = engine_factory or (lambda: default_engine_factory(state_dir, project_root))
    try:
        verdict = await judge_pair(q, old_md, new_md, engine_factory=factory, swap=swap)
    except Exception as e:
        _diag("libris_pairwise", "pairwise gate failed; leaving deterministic selection", error=e)
        return {"ran": False, "reason": f"judge_error:{type(e).__name__}", "rejected_latest": False}

    result = {"ran": True, "rejected_latest": False, "verdict": verdict,
              "incumbent_checkpoint_id": incumbent.get("checkpoint_id"),
              "latest_checkpoint_id": latest.get("checkpoint_id")}
    if verdict["winner"] == "old":
        mark_checkpoint_pairwise_rejected(
            state_dir, project_root, operation_id, topic_slug, latest.get("checkpoint_id"),
            winner="old", reason="lost blind pairwise vs running best",
            detail={"new_wins": verdict["new_wins"], "old_wins": verdict["old_wins"]})
        reverted = revert_topic_draft_to_best(state_dir, project_root, operation_id, topic_slug)
        result["rejected_latest"] = True
        result["reverted_draft"] = reverted
    return result


def gate_latest_checkpoint(state_dir: Path, project_root: Path, operation_id: str,
                           topic_slug: str, *, question: str = "",
                           engine_factory: Callable | None = None, swap: bool = True) -> dict:
    """Sync wrapper around agate_latest_checkpoint for the tick-based/CLI loops."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(agate_latest_checkpoint(
            state_dir, project_root, operation_id, topic_slug,
            question=question, engine_factory=engine_factory, swap=swap))
    # Already inside an event loop: run in a dedicated loop on a worker thread.
    import concurrent.futures
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(lambda: asyncio.run(agate_latest_checkpoint(
            state_dir, project_root, operation_id, topic_slug,
            question=question, engine_factory=engine_factory, swap=swap))).result()
