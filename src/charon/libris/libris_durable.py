"""Libris on the durable orchestration runtime (Phase 3).

Ports Libris's autonomous control flow (`_run_operation_controller`, an ephemeral
daemon thread that orphans the operation on crash) onto
`charon.orchestration.runtime` as durable steps: scout -> fanout -> supervise ->
finalize. Because the controller's state now persists after every step and is
re-driven by the daemon heartbeat, a crash no longer freezes the operation — the
next tick reloads and continues. Two concrete wins over the thread controller:

- The "no candidate topics" case is a first-class SUSPEND (awaiting user
  direction) that `resume()` un-parks — the real fix for the old dead-end park,
  where answering the clarification had no consumer.
- After a crash, an in-flight researcher whose thread died is detected (agent no
  longer running, no draft) and re-spawned, so the operation actually completes.

The heavy agent work still runs in the existing role threads (`spawn_libris_role`
/ `_run_libris_role`); only the *orchestration* is now durable steps that poll
saved topic state — exactly what the old supervisor loop did, but resumable.

This path is opt-in (`start_durable_libris_research`); the existing thread
controller is untouched.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from charon.orchestration import runtime as rt

try:
    from charon.infra.diagnostics import record as _diag
except Exception:
    def _diag(*_a, **_k):
        return None

KIND = "libris_operation"

# a stale in-flight researcher (agent not running, no draft) is re-spawned at most
# this many times per topic — bounds crash-recovery churn.
_MAX_RESPAWN = 2
# how long a topic may sit 'researching' with a dead agent before we re-spawn.
_STALE_SECONDS = 20


# ── helpers bound at call time (kept importable/stubbable for tests) ──────────

def _lr():
    from charon.libris import libris_runtime as lr
    return lr


def _agents():
    from charon.libris import libris_agents as la
    return la


def _budget_exhausted(op: dict, sd: Path, pr: Path, op_id: str) -> tuple[bool, str]:
    lr = _lr()
    budget = op.get("budget_status") or lr.get_budget_status(sd, pr, op_id)
    if not budget.get("continue_running", True):
        return True, ", ".join(budget.get("reasons") or [])
    return False, ""


def _ctx_paths(ctx) -> tuple[Path, Path, str, str]:
    st = ctx.state
    return (Path(st["state_dir"]), Path(st["project_root"]),
            st["operation_id"], st.get("prompt", ""))


def _topic_has_sources(sd: Path, pr: Path, op_id: str, slug: str) -> bool:
    """True if the researcher saved any sources for this topic — used to decide
    crash recovery: no sources -> re-spawn researcher (crashed before gathering);
    sources present but no draft -> spawn a writer to synthesize the draft."""
    lr = _lr()
    try:
        path = lr.research_root(sd, pr) / "sources" / "sources.jsonl"
        if not path.exists():
            return False
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            if row.get("operation_id") == op_id and row.get("topic_slug") == slug:
                return True
    except Exception:
        pass
    return False


# ── steps ────────────────────────────────────────────────────────────────

def _step_scout(ctx) -> rt.Directive:
    sd, pr, op_id, prompt = _ctx_paths(ctx)
    lr, la = _lr(), _agents()
    op = lr.get_operation_state(sd, pr, op_id)
    if not op:
        return rt.fail("operation state vanished")

    exhausted, reasons = _budget_exhausted(op, sd, pr, op_id)
    if exhausted:
        lr.set_operation_status(sd, pr, op_id, "budget_exhausted", reasons)
        return rt.done(outcome="budget_exhausted")
    if op.get("stop_requested"):
        lr.set_operation_status(sd, pr, op_id, "stopped", "User requested stop.")
        return rt.done(outcome="stopped")

    topics = list(op.get("candidate_topics") or [])
    if topics:
        return rt.goto("fanout")

    # resume path: the user answered the clarification — re-scout with their steer
    if ctx.resume_payload:
        narrowed = f"{prompt}\n\nUser direction: {str(ctx.resume_payload)[:400]}"
        la.spawn_libris_role(sd, pr, role="coordinator", operation_id=op_id,
                             user_goal=narrowed)
        lr.set_operation_status(sd, pr, op_id, "scouting", "Re-scouting with user direction.")
        return rt.stay(delay_sec=10, scout_ticks=0, prompt=narrowed)

    coord_id = ctx.state.get("coordinator_id", "")
    scout_ticks = int(ctx.state.get("scout_ticks", 0)) + 1
    coord_running = la._agent_status(coord_id) == "running"

    # coordinator finished (or died) without topics, or we hit the wall cap ->
    # suspend for user direction instead of the old dead-end park.
    if (not coord_running or scout_ticks > 180):
        op = lr.get_operation_state(sd, pr, op_id) or {}
        if list(op.get("candidate_topics") or []):
            return rt.goto("fanout")
        _file_clarification(sd, pr, op_id, prompt, coord_id)
        lr.set_operation_status(sd, pr, op_id, "awaiting_clarification",
                                "No candidate topics; awaiting user direction.")
        return rt.suspend("Libris found no candidate topics; awaiting user direction.",
                          resume_key="missing_candidate_topics")
    return rt.stay(delay_sec=10, scout_ticks=scout_ticks)


def _file_clarification(sd, pr, op_id, prompt, coord_id) -> None:
    lr = _lr()
    try:
        from charon.tools import ToolContext
        from charon.tools.clarify_tool import execute_clarify
        question = ("Libris could not confidently derive candidate research topics from your "
                    f'request: "{prompt[:220]}". What should it research?')
        choices = [
            f"Focus strictly on the named topic: {prompt[:120]}",
            "Narrow to core definitions, key papers, and major methods only",
            "Narrow to one domain/application area before researching",
            "Rewrite the topic in my own words / give a custom direction",
        ]
        cctx = ToolContext(project_root=pr, agent_id=coord_id, state_dir=sd)
        res = execute_clarify({"action": "ask", "question": question, "choices": choices}, cctx)
        cid = str((res.details or {}).get("clarification_id") or "")
        lr.append_operation_event(sd, pr, op_id, "clarification_requested",
                                  {"clarification_id": cid, "reason": "missing_candidate_topics",
                                   "question": question, "choices": choices})
    except Exception as e:
        _diag("libris_durable", "clarification filing failed", error=e, operation_id=op_id)


def _step_fanout(ctx) -> rt.Directive:
    sd, pr, op_id, prompt = _ctx_paths(ctx)
    lr, la = _lr(), _agents()
    op = lr.get_operation_state(sd, pr, op_id) or {}
    topics = list(op.get("candidate_topics") or [])
    budget = (op.get("budget_status") or {}).get("budget") or {}
    max_topics = int(budget.get("max_topics") or 0) or int(ctx.state.get("max_topics_default", 3))

    selected = [t for t in topics
                if str(t.get("recommended_action") or "monitor") in ("deep_research", "monitor")][:max_topics]
    if not selected:
        lr.set_operation_status(sd, pr, op_id, "idle", "No promising topics were selected.")
        return rt.done(outcome="idle")

    lr.set_operation_status(sd, pr, op_id, "fanout", f"Selecting {len(selected)} topic(s).")
    spawned = []
    coord_id = ctx.state.get("coordinator_id", "")
    for t in selected:
        topic = lr.init_topic(
            sd, pr, op_id, title=str(t.get("title") or "Topic"),
            why_interesting=str(t.get("why_interesting") or ""),
            focus_questions=[
                f'What is new or notable about {str(t.get("title") or "this topic")}?',
                "Why might this matter to the user and broader project goals?",
                "What evidence supports practical importance or novelty?",
            ])
        researcher = la.spawn_libris_role(sd, pr, role="researcher", operation_id=op_id,
                                          topic_slug=topic["slug"], user_goal=prompt,
                                          parent_agent_id=coord_id)
        lr.update_topic_runtime(sd, pr, op_id, topic["slug"], status="researching",
                                researcher_agent_id=researcher.get("id", ""),
                                extras={"researcher_spawned_at": time.time()})
        lr.append_operation_event(sd, pr, op_id, "researcher_fanout_spawned",
                                  {"topic_slug": topic["slug"],
                                   "researcher_agent_id": researcher.get("id", "")})
        spawned.append(topic["slug"])
    lr.set_operation_status(sd, pr, op_id, "researching", f"Active topics: {len(spawned)}")
    return rt.goto("supervise", spawned_topics=spawned)


def _step_supervise(ctx) -> rt.Directive:
    sd, pr, op_id, prompt = _ctx_paths(ctx)
    lr, la = _lr(), _agents()
    op = lr.get_operation_state(sd, pr, op_id)
    if not op:
        return rt.fail("operation state vanished")

    exhausted, reasons = _budget_exhausted(op, sd, pr, op_id)
    if exhausted:
        lr.set_operation_status(sd, pr, op_id, "budget_exhausted", reasons)
        return rt.done(outcome="budget_exhausted")
    if op.get("stop_requested"):
        lr.set_operation_status(sd, pr, op_id, "stopped", "User requested stop.")
        return rt.done(outcome="stopped")

    coord_id = ctx.state.get("coordinator_id", "")
    all_ready = True
    for topic in op.get("topics") or []:
        slug = str(topic.get("slug") or "")
        if not slug:
            continue
        has_draft = bool(topic.get("draft_report_path"))
        checkpoint_count = int(topic.get("checkpoint_count") or 0)
        judge_id = str(topic.get("judge_agent_id") or "")
        judge_round = int(topic.get("judge_round") or 0)
        revision_round = int(topic.get("revision_round") or 0)
        research_round = int(topic.get("research_round") or 1)
        status = str(topic.get("status") or "")

        # No draft yet. While the active worker (researcher or writer) is still
        # running, just wait. Once it finishes without a draft, recover: if it
        # gathered sources but never wrote, synthesize with a WRITER (reliable);
        # if it produced nothing (crashed before gathering), re-spawn a
        # RESEARCHER. Both are bounded to avoid loops.
        if not has_draft and status in ("researching", "revising", "writing"):
            active_id = str(topic.get("writer_agent_id") or topic.get("researcher_agent_id") or "")
            active_running = bool(active_id) and la._agent_status(active_id) == "running"
            spawned_at = float(topic.get("researcher_spawned_at") or 0.0)
            if active_running or (time.time() - spawned_at) <= _STALE_SECONDS:
                all_ready = False
                continue
            if _topic_has_sources(sd, pr, op_id, slug):
                tries = int(topic.get("writer_tries") or 0)
                if tries < 2:
                    w = la.spawn_libris_role(sd, pr, role="writer", operation_id=op_id,
                                             topic_slug=slug, user_goal=prompt, parent_agent_id=coord_id)
                    lr.update_topic_runtime(sd, pr, op_id, slug, status="writing",
                                            extras={"writer_agent_id": w.get("id", ""),
                                                    "writer_tries": tries + 1,
                                                    "researcher_spawned_at": time.time()})
                    lr.append_operation_event(sd, pr, op_id, "writer_fallback_spawned",
                                              {"topic_slug": slug, "writer_tries": tries + 1})
                    all_ready = False
                else:
                    lr.update_topic_runtime(sd, pr, op_id, slug, status="no_report")
                continue  # not blocking once given up
            respawns = int(topic.get("respawn_count") or 0)
            if respawns < _MAX_RESPAWN:
                r = la.spawn_libris_role(sd, pr, role="researcher", operation_id=op_id,
                                         topic_slug=slug, user_goal=prompt, parent_agent_id=coord_id)
                lr.update_topic_runtime(sd, pr, op_id, slug, status="researching",
                                        researcher_agent_id=r.get("id", ""),
                                        extras={"respawn_count": respawns + 1,
                                                "researcher_spawned_at": time.time()})
                lr.append_operation_event(sd, pr, op_id, "researcher_respawned_after_stall",
                                          {"topic_slug": slug, "respawn_count": respawns + 1})
                all_ready = False
            else:
                lr.update_topic_runtime(sd, pr, op_id, slug, status="no_report")
            continue

        draft_updated = str(topic.get("draft_report_updated_at") or "")
        latest_ckpt_at = str((topic.get("latest_checkpoint") or {}).get("created_at") or "")
        needs_judge = (has_draft and checkpoint_count == 0) or (
            has_draft and draft_updated and latest_ckpt_at
            and draft_updated > latest_ckpt_at and judge_round < checkpoint_count + 1)

        if needs_judge and (not judge_id or judge_round < checkpoint_count + 1):
            judge = la.spawn_libris_role(sd, pr, role="judge", operation_id=op_id,
                                         topic_slug=slug, user_goal=prompt, parent_agent_id=coord_id)
            lr.update_topic_runtime(sd, pr, op_id, slug, status="judging",
                                    judge_agent_id=judge.get("id", ""),
                                    extras={"judge_round": judge_round + 1})
            lr.append_operation_event(sd, pr, op_id, "judge_fanout_spawned",
                                      {"topic_slug": slug, "judge_agent_id": judge.get("id", "")})
            all_ready = False
            continue

        if checkpoint_count == 0:
            all_ready = False
            continue

        plan = {"should_revise": False, "reasons": [], "metrics": {}}
        try:
            from charon.libris.libris_convergence import should_request_additional_revision
            plan = should_request_additional_revision(sd, pr, op_id, topic)
        except Exception as e:
            _diag("libris_durable", "revision decision failed", error=e, topic_slug=slug)

        if plan.get("should_revise"):
            r = la.spawn_libris_role(sd, pr, role="researcher", operation_id=op_id,
                                     topic_slug=slug, user_goal=prompt, parent_agent_id=coord_id)
            lr.update_topic_runtime(sd, pr, op_id, slug, status="revising",
                                    researcher_agent_id=r.get("id", ""), judge_agent_id="",
                                    extras={"revision_round": revision_round + 1,
                                            "research_round": research_round + 1,
                                            "researcher_spawned_at": time.time(), "respawn_count": 0})
            lr.append_operation_event(sd, pr, op_id, "research_revision_spawned",
                                      {"topic_slug": slug, "revision_round": revision_round + 1})
            all_ready = False
            continue

        # converged
        reasons = plan.get("reasons") or []
        final_status = "checkpointed"
        if "quality_good_enough" in reasons:
            final_status = "ready_high_confidence"
        elif "score_plateau" in reasons:
            final_status = "plateaued"
        lr.update_topic_runtime(sd, pr, op_id, slug, status=final_status,
                                extras={"convergence_reasons": reasons,
                                        "convergence_metrics": plan.get("metrics") or {}})
        lr.append_operation_event(sd, pr, op_id, "topic_convergence_decided",
                                  {"topic_slug": slug, "status": final_status, "reasons": reasons})

    if all_ready and (op.get("topics") or []):
        return rt.goto("finalize")
    return rt.stay(delay_sec=5)


def _step_finalize(ctx) -> rt.Directive:
    sd, pr, op_id, _ = _ctx_paths(ctx)
    lr = _lr()
    lr.update_operation_runtime(sd, pr, op_id, status="reports_ready",
                                note="All active topics completed the researcher/judge loop.")
    try:
        lr.finalize_operation_selection(sd, pr, op_id)
    except Exception as e:
        _diag("libris_durable", "finalize_operation_selection failed", error=e, operation_id=op_id)
    return rt.done(outcome="reports_ready")


# ── registration + entry point ────────────────────────────────────────────

def register() -> None:
    """Register the libris_operation kind with the durable runtime (idempotent)."""
    if KIND in rt.registered_kinds():
        return
    rt.register_kind(
        KIND,
        steps={"scout": _step_scout, "fanout": _step_fanout,
               "supervise": _step_supervise, "finalize": _step_finalize},
        entry="scout",
        max_attempts=3, backoff_base=2.0,
    )


def start_durable_libris_research(
    state_dir: Path, project_root: Path, *, prompt: str,
    budget: dict[str, Any] | None = None, model_policy: dict[str, Any] | None = None,
    max_topics_default: int = 3, parent_agent_id: str = "",
) -> dict[str, Any]:
    """Start a Libris research operation on the DURABLE runtime. Inits the Libris
    operation + spawns the coordinator, then creates a durable operation whose
    steps drive the run (advanced by the daemon heartbeat). Crash-resumable and
    suspend/resume-capable. Returns {operation, coordinator, durable_op_id}."""
    register()
    lr, la = _lr(), _agents()
    op = lr.init_operation(state_dir, project_root, prompt=prompt,
                           coordinator_agent_id="", budget=budget, model_policy=model_policy,
                           summary=f"Libris (durable): {prompt[:120]}")
    op_id = op["operation_id"]
    coordinator = la.spawn_libris_role(state_dir, project_root, role="coordinator",
                                       operation_id=op_id, user_goal=prompt,
                                       parent_agent_id=parent_agent_id)
    try:
        lr.update_operation_budget(state_dir, project_root, op_id,
                                   budget=budget or {}, model_policy=model_policy or {})
    except Exception:
        pass
    lr.update_operation_runtime(state_dir, project_root, op_id,
                                coordinator_agent_id=coordinator.get("id", ""),
                                status="scouting", note="Durable coordinator spawned.")
    durable = rt.start_operation(
        state_dir, KIND, title=f"Libris: {prompt[:60]}",
        initial_state={
            "state_dir": str(state_dir), "project_root": str(project_root),
            "operation_id": op_id, "prompt": prompt,
            "coordinator_id": coordinator.get("id", ""),
            "max_topics_default": max_topics_default,
        })
    return {"operation": op, "coordinator": coordinator, "durable_op_id": durable["op_id"]}


__all__ = ["KIND", "register", "start_durable_libris_research"]
