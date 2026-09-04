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

import fcntl
import hashlib
import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from charon.orchestration import runtime as rt

try:
    from charon.infra.diagnostics import record as _diag
except Exception:
    def _diag(*_a, **_k):
        return None

KIND = "libris_operation"
_BOOTSTRAP_KIND = "libris_durable_v1"
_BOOTSTRAP_KEY = "durable_bootstrap"
_BOOTSTRAP_RESUME_KEY = "libris_bootstrap"
_BOOTSTRAP_SUSPENDED_REASON = "Awaiting durable Libris bootstrap."
_PROCESS_INSTANCE_ID = uuid.uuid4().hex
_BOOTSTRAP_LOCKS: dict[str, threading.RLock] = {}
_BOOTSTRAP_LOCKS_GUARD = threading.Lock()

# a stale in-flight researcher (agent not running, no draft) is re-spawned at most
# this many times per topic — bounds crash-recovery churn.
_MAX_RESPAWN = 2
# A judge that exits without saving a checkpoint is retried, but never forever.
_MAX_JUDGE_ATTEMPTS = 3
# how long a topic may sit 'researching' with a dead agent before we re-spawn.
_STALE_SECONDS = 20
_TERMINAL_TOPIC_STATUSES = {
    "checkpointed",
    "ready_high_confidence",
    "plateaued",
    "judge_failed",
    "no_report",
    "excluded",
}


# ── helpers bound at call time (kept importable/stubbable for tests) ──────────

def _lr():
    from charon.libris import libris_runtime as lr
    return lr


def _agents():
    from charon.libris import libris_agents as la
    return la


def _durable_operation_id(operation_id: str) -> str:
    digest = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()[:24]
    return f"op_libris_{digest}"


def _coordinator_agent_id(operation_id: str) -> str:
    digest = hashlib.sha256(f"coordinator:{operation_id}".encode("utf-8")).hexdigest()[:20]
    return f"libris-coordinator-{digest}"


def _startup_checkpoint(_stage: str) -> None:
    """Fault-injection seam for durable-start commit-boundary tests."""


def _bootstrap_lock_path(state_dir: Path, operation_id: str) -> Path:
    digest = hashlib.sha256(operation_id.encode("utf-8")).hexdigest()
    path = Path(state_dir) / "orchestration" / "libris-bootstrap-locks" / f"{digest}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


@contextmanager
def _locked_bootstrap(state_dir: Path, operation_id: str) -> Iterator[None]:
    path = _bootstrap_lock_path(state_dir, operation_id)
    key = str(path.resolve())
    with _BOOTSTRAP_LOCKS_GUARD:
        local = _BOOTSTRAP_LOCKS.setdefault(key, threading.RLock())
    with local:
        with path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _operation_projection_path(
    state_dir: Path,
    project_root: Path,
    operation_id: str,
) -> Path:
    return _lr().operation_dir(state_dir, project_root, operation_id) / "operation.json"


def _read_operation_projection(
    state_dir: Path,
    project_root: Path,
    operation_id: str,
) -> dict[str, Any]:
    path = _operation_projection_path(state_dir, project_root, operation_id)
    try:
        row = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return row if isinstance(row, dict) else {}


def _update_bootstrap(
    state_dir: Path,
    project_root: Path,
    operation_id: str,
    **updates: Any,
) -> dict[str, Any]:
    from charon.libris import libris_lifecycle as lifecycle

    path = _operation_projection_path(state_dir, project_root, operation_id)

    def mutate(current: dict[str, Any]) -> dict[str, Any]:
        bootstrap = dict(current.get(_BOOTSTRAP_KEY) or {})
        bootstrap.update(updates)
        current[_BOOTSTRAP_KEY] = bootstrap
        return current

    return lifecycle.mutate_projection(path, mutate)


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
        coordinator = la.spawn_libris_role(
            sd, pr, role="coordinator", operation_id=op_id,
            user_goal=narrowed,
        )
        coordinator_id = str(coordinator.get("id") or "")
        lr.update_operation_runtime(
            sd, pr, op_id, coordinator_agent_id=coordinator_id,
            status="scouting", note="Re-scouting with user direction.",
        )
        return rt.stay(
            delay_sec=10, scout_ticks=0, prompt=narrowed,
            coordinator_id=coordinator_id,
        )

    coord_id = ctx.state.get("coordinator_id", "")
    scout_ticks = int(ctx.state.get("scout_ticks", 0)) + 1
    coord_running = la._agent_status(coord_id) == "running"

    # coordinator finished (or died) without topics, or we hit the wall cap ->
    # suspend for user direction instead of the old dead-end park.
    if (not coord_running or scout_ticks > 180):
        op = lr.get_operation_state(sd, pr, op_id) or {}
        if list(op.get("candidate_topics") or []):
            return rt.goto("fanout")
        _file_clarification(sd, pr, op_id, prompt, coord_id, ctx.op_id)
        lr.set_operation_status(sd, pr, op_id, "awaiting_clarification",
                                "No candidate topics; awaiting user direction.")
        return rt.suspend("Libris found no candidate topics; awaiting user direction.",
                          resume_key="missing_candidate_topics")
    return rt.stay(delay_sec=10, scout_ticks=scout_ticks)


def _file_clarification(sd, pr, op_id, prompt, coord_id, durable_op_id: str) -> None:
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
        res = execute_clarify({
            "action": "ask",
            "question": question,
            "choices": choices,
            "metadata": {
                "continuation": "libris",
                "operation_id": op_id,
                "durable_op_id": durable_op_id,
                "project_root": str(pr),
                "resume_key": "missing_candidate_topics",
            },
        }, cctx)
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

    # Repair legacy runs that were marked delivered with an empty bundle while
    # their durable supervisor was still active.
    if str(op.get("status") or "") == "delivered":
        manifest = lr.get_delivery_manifest(sd, pr, op_id, op=op)
        if not manifest.get("ready"):
            lr.adopt_legacy_incomplete_delivery(
                sd,
                pr,
                op_id,
                note="Repairing an incomplete legacy delivery.",
            )
            op["status"] = "researching"

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

        # Once convergence has been recorded, this topic is immutable from the
        # supervisor's perspective.  Reprocessing it every heartbeat used to
        # append duplicate convergence events indefinitely.
        if status in _TERMINAL_TOPIC_STATUSES:
            continue

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
            and draft_updated > latest_ckpt_at)

        if needs_judge:
            judge_running = bool(judge_id) and la._agent_status(judge_id) == "running"
            judge_spawned_at = float(topic.get("judge_spawned_at") or 0.0)
            if judge_running or (
                bool(judge_id)
                and judge_spawned_at > 0
                and (time.time() - judge_spawned_at) <= _STALE_SECONDS
            ):
                all_ready = False
                continue

            # Existing operations predate judge_attempts.  Count an already
            # stopped judge with no checkpoint as the first failed attempt.
            judge_attempts = int(topic.get("judge_attempts") or 0)
            if judge_attempts == 0 and judge_id and checkpoint_count == 0:
                judge_attempts = 1
            if judge_attempts >= _MAX_JUDGE_ATTEMPTS:
                lr.update_topic_runtime(
                    sd,
                    pr,
                    op_id,
                    slug,
                    status="judge_failed",
                    extras={
                        "judge_attempts": judge_attempts,
                        "judge_failure_reason": "Judge attempts ended without a checkpoint.",
                    },
                )
                lr.append_operation_event(
                    sd,
                    pr,
                    op_id,
                    "judge_retry_exhausted",
                    {"topic_slug": slug, "judge_attempts": judge_attempts},
                )
                continue

            judge = la.spawn_libris_role(sd, pr, role="judge", operation_id=op_id,
                                         topic_slug=slug, user_goal=prompt, parent_agent_id=coord_id)
            lr.update_topic_runtime(sd, pr, op_id, slug, status="judging",
                                    judge_agent_id=judge.get("id", ""),
                                    extras={
                                        "judge_round": judge_round + 1,
                                        "judge_attempts": judge_attempts + 1,
                                        "judge_spawned_at": time.time(),
                                    })
            event_type = "judge_retry_spawned" if judge_id else "judge_fanout_spawned"
            lr.append_operation_event(
                sd,
                pr,
                op_id,
                event_type,
                {
                    "topic_slug": slug,
                    "judge_agent_id": judge.get("id", ""),
                    "judge_attempt": judge_attempts + 1,
                },
            )
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

        # Pairwise regression gate (opt-in): the deterministic keep-if-better guard
        # is blind to quality differences the coarse judge score can't see (two
        # rounds can tie the score while one is materially better). When enabled,
        # blind-judge the newest checkpoint against the running best and demote it
        # if it loses. Off by default because it makes 2 synchronous LLM calls,
        # which would block the heartbeat tick; gated once per new checkpoint.
        if os.environ.get("CHARON_LIBRIS_PAIRWISE_GATE"):
            try:
                cks = lr.list_checkpoints(sd, pr, op_id, slug)
                latest_id = cks[-1].get("checkpoint_id") if cks else ""
                if latest_id and topic.get("pairwise_gated_ckpt") != latest_id:
                    from charon.libris import libris_pairwise as lp
                    g = lp.gate_latest_checkpoint(sd, pr, op_id, slug, question=prompt)
                    lr.update_topic_runtime(sd, pr, op_id, slug,
                                            extras={"pairwise_gated_ckpt": latest_id})
                    if g.get("rejected_latest"):
                        lr.append_operation_event(sd, pr, op_id, "checkpoint_pairwise_rejected",
                                                  {"topic_slug": slug, "checkpoint_id": latest_id})
            except Exception as e:
                _diag("libris_durable", "pairwise gate failed", error=e, topic_slug=slug)

        # Keep-if-better: if the latest checkpoint regressed below the best, discard
        # its draft and restore the best. This also makes the next revision start
        # from the high-water mark (revise-from-best) — the loop becomes a strict
        # hill-climb that cannot deliver a worse report than its best.
        try:
            lr.revert_topic_draft_to_best(sd, pr, op_id, slug)
        except Exception as e:
            _diag("libris_durable", "keep-if-better revert failed", error=e, topic_slug=slug)

        if plan.get("should_revise"):
            r = la.spawn_libris_role(sd, pr, role="researcher", operation_id=op_id,
                                     topic_slug=slug, user_goal=prompt, parent_agent_id=coord_id)
            lr.update_topic_runtime(sd, pr, op_id, slug, status="revising",
                                    researcher_agent_id=r.get("id", ""), judge_agent_id="",
                                    extras={"revision_round": revision_round + 1,
                                            "research_round": research_round + 1,
                                            "researcher_spawned_at": time.time(),
                                            "respawn_count": 0,
                                            "judge_attempts": 0,
                                            "judge_spawned_at": 0.0})
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
    operation = lr.get_operation_state(sd, pr, op_id) or {}
    operation_status = str(operation.get("status") or "")
    # A restarted finalize step may already have committed verification or the
    # terminal domain transition. Do not force either state backwards merely
    # because the compatibility scheduler is replaying its step.
    if operation_status not in {"verifying_delivery", "delivered"}:
        lr.update_operation_runtime(
            sd,
            pr,
            op_id,
            status="assembling_delivery",
            note="All terminal topics are being assembled for delivery.",
        )
    try:
        result = lr.finalize_operation_selection(sd, pr, op_id)
    except Exception as e:
        _diag("libris_durable", "finalize_operation_selection failed", error=e, operation_id=op_id)
        current = lr.get_operation_state(sd, pr, op_id) or {}
        if str(current.get("status") or "") != "delivered":
            lr.set_operation_status(sd, pr, op_id, "delivery_failed", str(e))
        return rt.fail(f"delivery finalization failed: {e}")

    if result.get("ready"):
        return rt.done(
            outcome="reports_ready",
            delivery_manifest=result.get("delivery_manifest") or {},
        )
    if result.get("status") == "waiting":
        lr.update_operation_runtime(
            sd,
            pr,
            op_id,
            status="researching",
            note="Delivery preflight found active topic work; returning to supervision.",
        )
        return rt.goto("supervise")

    reason = str(result.get("reason") or "No validated delivery was produced.")
    current = lr.get_operation_state(sd, pr, op_id) or {}
    if str(current.get("status") or "") == "delivered":
        # Completion history is immutable. Surface present-day artifact damage
        # through the manifest while allowing the compatibility scheduler to
        # settle instead of attempting an illegal terminal-state rewrite.
        return rt.done(
            outcome="delivered_with_integrity_error",
            delivery_manifest=result.get("delivery_manifest") or {},
            integrity_error=reason,
        )
    lr.set_operation_status(sd, pr, op_id, "delivery_failed", reason)
    return rt.fail(reason)


# ── registration + startup recovery ──────────────────────────────────────

_TERMINAL_OPERATION_STATUSES = {
    "delivered",
    "idle",
    "budget_exhausted",
    "stopped",
    "failed",
    "delivery_failed",
    "lifecycle_error",
}


def _provision_coordinator_unlocked(
    state_dir: Path,
    project_root: Path,
    operation_id: str,
    projection: dict[str, Any],
) -> dict[str, Any]:
    bootstrap = dict(projection.get(_BOOTSTRAP_KEY) or {})
    expected_id = str(
        bootstrap.get("coordinator_agent_id")
        or _coordinator_agent_id(operation_id)
    )
    linked_id = str(projection.get("coordinator_agent_id") or "")
    if linked_id:
        if linked_id != expected_id:
            raise RuntimeError(
                f"Libris bootstrap coordinator mismatch for {operation_id}: "
                f"{linked_id!r} != {expected_id!r}"
            )
        coordinator: dict[str, Any] = {"id": linked_id}
        phase = str(bootstrap.get("phase") or "intent_persisted")
        spawn_owner = str(bootstrap.get("spawn_owner") or "")
        if phase != "ready" and spawn_owner != _PROCESS_INSTANCE_ID:
            coordinator = _agents().spawn_libris_role(
                state_dir,
                project_root,
                role="coordinator",
                operation_id=operation_id,
                user_goal=str(projection.get("prompt") or ""),
                parent_agent_id=str(bootstrap.get("parent_agent_id") or ""),
                agent_id=expected_id,
                restart_existing=True,
            )
        _update_bootstrap(
            state_dir,
            project_root,
            operation_id,
            phase="ready",
            coordinator_agent_id=expected_id,
            spawn_owner=(
                _PROCESS_INSTANCE_ID
                if phase != "ready" and spawn_owner != _PROCESS_INSTANCE_ID
                else spawn_owner
            ),
        )
        return coordinator

    phase = str(bootstrap.get("phase") or "intent_persisted")
    spawn_owner = str(bootstrap.get("spawn_owner") or "")
    coordinator: dict[str, Any]
    if phase == "coordinator_spawned" and spawn_owner == _PROCESS_INSTANCE_ID:
        # The launch returned in this process and its identity was committed;
        # only the projection link was interrupted.
        coordinator = {"id": expected_id}
    else:
        _update_bootstrap(
            state_dir,
            project_root,
            operation_id,
            phase="coordinator_spawning",
            coordinator_agent_id=expected_id,
            spawn_owner=_PROCESS_INSTANCE_ID,
        )
        coordinator = _agents().spawn_libris_role(
            state_dir,
            project_root,
            role="coordinator",
            operation_id=operation_id,
            user_goal=str(projection.get("prompt") or ""),
            parent_agent_id=str(bootstrap.get("parent_agent_id") or ""),
            agent_id=expected_id,
            restart_existing=bool(spawn_owner and spawn_owner != _PROCESS_INSTANCE_ID),
        )
        if str(coordinator.get("id") or "") != expected_id:
            raise RuntimeError(
                f"coordinator launch returned {coordinator.get('id')!r}; "
                f"expected {expected_id!r}"
            )
        _update_bootstrap(
            state_dir,
            project_root,
            operation_id,
            phase="coordinator_spawned",
            coordinator_agent_id=expected_id,
            spawn_owner=_PROCESS_INSTANCE_ID,
        )
        _startup_checkpoint("coordinator_spawned")

    _lr().update_operation_runtime(
        state_dir,
        project_root,
        operation_id,
        coordinator_agent_id=expected_id,
        status="scouting",
        note="Durable coordinator spawned.",
    )
    _startup_checkpoint("coordinator_linked")
    _update_bootstrap(
        state_dir,
        project_root,
        operation_id,
        phase="ready",
        coordinator_agent_id=expected_id,
        spawn_owner=_PROCESS_INSTANCE_ID,
    )
    return coordinator


def _recover_libris_bootstrap(
    state_dir: Path,
    project_root: Path,
    operation_id: str,
) -> dict[str, Any]:
    state_dir = Path(state_dir)
    project_root = Path(project_root).resolve()
    with _locked_bootstrap(state_dir, operation_id):
        projection = _lr().reconcile_operation_initialization(
            state_dir,
            project_root,
            operation_id,
        )
        bootstrap = dict(projection.get(_BOOTSTRAP_KEY) or {})
        if bootstrap.get("kind") != _BOOTSTRAP_KIND:
            raise ValueError(f"operation {operation_id!r} has no durable bootstrap intent")
        stored_root = Path(str(bootstrap.get("project_root") or project_root)).resolve()
        if stored_root != project_root:
            raise ValueError(
                f"operation {operation_id!r} bootstrap project does not match"
            )

        durable_id = str(
            bootstrap.get("durable_op_id")
            or _durable_operation_id(operation_id)
        )
        coordinator_id = str(
            bootstrap.get("coordinator_agent_id")
            or _coordinator_agent_id(operation_id)
        )
        initial_state = {
            "state_dir": str(state_dir),
            "project_root": str(project_root),
            "operation_id": operation_id,
            "prompt": str(projection.get("prompt") or ""),
            "coordinator_id": coordinator_id,
            "max_topics_default": int(bootstrap.get("max_topics_default") or 3),
            "parent_agent_id": str(bootstrap.get("parent_agent_id") or ""),
        }
        durable = rt.start_operation(
            state_dir,
            KIND,
            title=f"Libris: {str(projection.get('prompt') or '')[:60]}",
            initial_state=initial_state,
            op_id=durable_id,
            suspended=True,
            suspended_reason=_BOOTSTRAP_SUSPENDED_REASON,
            resume_key=_BOOTSTRAP_RESUME_KEY,
            idempotent=True,
            idempotency_identity={
                "operation_id": operation_id,
                "project_root": str(project_root),
            },
        )
        if str(bootstrap.get("phase") or "intent_persisted") in {
            "",
            "intent_persisted",
        }:
            projection = _update_bootstrap(
                state_dir,
                project_root,
                operation_id,
                phase="continuation_persisted",
                durable_op_id=durable_id,
                coordinator_agent_id=coordinator_id,
            )
        _startup_checkpoint("continuation_persisted")
        projection = _read_operation_projection(state_dir, project_root, operation_id)
        coordinator = _provision_coordinator_unlocked(
            state_dir,
            project_root,
            operation_id,
            projection,
        )
        recovered = {
            "operation_id": operation_id,
            "durable": durable,
            "durable_op_id": durable_id,
            "coordinator": coordinator,
        }

    # Never expose a runnable scout until its coordinator identity is durably
    # linked. Resume outside the bootstrap lock so scheduler and bootstrap
    # mutations cannot acquire their file locks in opposite orders.
    _startup_checkpoint("bootstrap_ready")
    current = rt.get_operation(state_dir, durable_id)
    if current and current.get("status") == "suspended":
        if current.get("resume_key") != _BOOTSTRAP_RESUME_KEY:
            raise RuntimeError(
                f"durable operation {durable_id!r} is suspended for "
                f"{current.get('resume_key')!r}, not Libris bootstrap"
            )
        current = rt.resume(state_dir, durable_id, None) or rt.get_operation(
            state_dir,
            durable_id,
        )
    if not current or current.get("status") != "running":
        raise RuntimeError(
            f"durable Libris continuation {durable_id!r} did not become runnable"
        )
    recovered["durable"] = current
    return recovered


def recover_pending_libris_bootstraps(state_dir: Path) -> list[dict[str, Any]]:
    """Repair marked nonterminal domain operations into one continuation each."""
    state_dir = Path(state_dir)
    recovered: list[dict[str, Any]] = []
    pattern = "projects/*/research/operations/*/operation.json"
    for path in sorted(state_dir.glob(pattern)):
        try:
            projection = json.loads(path.read_text(encoding="utf-8"))
            bootstrap = dict(projection.get(_BOOTSTRAP_KEY) or {})
            if bootstrap.get("kind") != _BOOTSTRAP_KIND:
                continue
            if str(projection.get("status") or "") in _TERMINAL_OPERATION_STATUSES:
                continue
            durable_id = str(
                bootstrap.get("durable_op_id")
                or _durable_operation_id(str(projection.get("operation_id") or path.parent.name))
            )
            existing = rt.get_operation(state_dir, durable_id)
            bootstrap_suspended = bool(
                existing
                and existing.get("status") == "suspended"
                and existing.get("resume_key") == _BOOTSTRAP_RESUME_KEY
            )
            if (
                str(bootstrap.get("phase") or "") == "ready"
                and existing is not None
                and not bootstrap_suspended
            ):
                continue
            project_root_raw = str(bootstrap.get("project_root") or "").strip()
            operation_id = str(projection.get("operation_id") or path.parent.name)
            if not project_root_raw:
                continue
            project_root = Path(project_root_raw)
            recovered.append(
                _recover_libris_bootstrap(state_dir, project_root, operation_id)
            )
        except Exception as exc:
            _diag(
                "libris_durable",
                "durable bootstrap recovery failed",
                error=exc,
                operation_path=str(path),
            )
    return recovered


def register(state_dir: Path | None = None) -> None:
    """Register the durable kind and reconcile marked startup intents."""
    if KIND not in rt.registered_kinds():
        rt.register_kind(
            KIND,
            steps={"scout": _step_scout, "fanout": _step_fanout,
                   "supervise": _step_supervise, "finalize": _step_finalize},
            entry="scout",
            max_attempts=3, backoff_base=2.0,
        )
    if state_dir is not None:
        recover_pending_libris_bootstraps(Path(state_dir))


def discover_libris_clarification(
    state_dir: Path, clarification_id: str,
) -> dict[str, Any]:
    """Recover continuation metadata for pre-durable clarification records."""
    state_dir = Path(state_dir)
    for events_path in sorted(state_dir.glob("projects/*/research/operations/*/events.jsonl")):
        try:
            rows = events_path.read_text(encoding="utf-8").splitlines()
        except Exception:
            continue
        matched = False
        for line in rows:
            try:
                event = json.loads(line)
            except Exception:
                continue
            payload = event.get("payload") or {}
            if (event.get("type") == "clarification_requested"
                    and str(payload.get("clarification_id") or "") == clarification_id):
                matched = True
                break
        if not matched:
            continue
        operation_id = events_path.parent.name
        project_dir = events_path.parents[3]
        project_doc = {}
        try:
            project_doc = json.loads((project_dir / "project.json").read_text(encoding="utf-8"))
        except Exception:
            pass
        project_root = str(project_doc.get("root_path") or "").strip()
        durable = _find_durable_for_libris_operation(state_dir, operation_id)
        return {
            "continuation": "libris",
            "operation_id": operation_id,
            "durable_op_id": str((durable or {}).get("op_id") or ""),
            "project_root": project_root,
            "resume_key": "missing_candidate_topics",
            "legacy_discovered": True,
        }
    return {}


def _find_durable_for_libris_operation(state_dir: Path, operation_id: str) -> dict[str, Any] | None:
    """Locate a durable continuation by its Libris operation id.

    Scanning is intentionally only a compatibility fallback for clarifications
    created before continuation metadata was introduced.
    """
    ops_dir = Path(state_dir) / "orchestration" / "ops"
    for path in sorted(ops_dir.glob("op_*.json")):
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if row.get("kind") != KIND:
            continue
        if str((row.get("state") or {}).get("operation_id") or "") == operation_id:
            return row
    return None


def resume_libris_clarification(
    state_dir: Path, *, clarification_id: str, answer: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resume and immediately verify a Libris clarification continuation."""
    register()
    meta = dict(metadata or {})
    operation_id = str(meta.get("operation_id") or "").strip()
    durable_op_id = str(meta.get("durable_op_id") or "").strip()
    durable = rt.get_operation(state_dir, durable_op_id) if durable_op_id else None
    if not durable and operation_id:
        durable = _find_durable_for_libris_operation(state_dir, operation_id)
        durable_op_id = str((durable or {}).get("op_id") or "")
    if not durable and operation_id and str(meta.get("project_root") or "").strip():
        # Compatibility migration for operations launched by the legacy daemon
        # controller. It exited after asking, so attach a suspended durable
        # continuation in-place rather than starting a duplicate research op.
        project_root = Path(str(meta.get("project_root")))
        existing = _lr().get_operation_state(state_dir, project_root, operation_id)
        if existing and existing.get("status") == "awaiting_clarification":
            adopted = rt.start_operation(
                state_dir, KIND, title=f"Libris continuation: {operation_id}",
                initial_state={
                    "state_dir": str(state_dir),
                    "project_root": str(project_root),
                    "operation_id": operation_id,
                    "prompt": str(existing.get("prompt") or ""),
                    "coordinator_id": str(existing.get("coordinator_agent_id") or ""),
                    "max_topics_default": int(((existing.get("budget") or {}).get("max_topics") or 0) or 3),
                },
                suspended=True,
                suspended_reason="Adopted legacy Libris clarification.",
                resume_key="missing_candidate_topics",
            )
            durable = adopted
            durable_op_id = adopted["op_id"]
    if not durable:
        return {
            "resumed": False,
            "operation_id": operation_id,
            "durable_op_id": durable_op_id,
            "error": "no durable Libris continuation is linked to this clarification",
        }
    if durable.get("status") != "suspended":
        return {
            "resumed": False,
            "operation_id": operation_id or str((durable.get("state") or {}).get("operation_id") or ""),
            "durable_op_id": durable_op_id,
            "status": durable.get("status"),
            "error": f'durable continuation is {durable.get("status")}, not suspended',
        }

    operation_id = operation_id or str((durable.get("state") or {}).get("operation_id") or "")
    project_root = Path(str(meta.get("project_root") or (durable.get("state") or {}).get("project_root") or ""))
    resumed = rt.resume(state_dir, durable_op_id, answer)
    if not resumed:
        return {"resumed": False, "operation_id": operation_id,
                "durable_op_id": durable_op_id, "error": "durable resume was rejected"}

    event = rt.tick_operation(state_dir, durable_op_id)
    verified = rt.get_operation(state_dir, durable_op_id) or {}
    status = str(verified.get("status") or "")
    error = str(verified.get("error") or "")
    ok = status == "running" and event.get("action") not in ("failed", "fail", "missing")
    if operation_id and str(project_root):
        lr = _lr()
        if ok:
            lr.update_operation_runtime(
                state_dir, project_root, operation_id, status="scouting",
                note="Resumed after user clarification.",
            )
            lr.append_operation_event(
                state_dir, project_root, operation_id, "clarification_resumed",
                {"clarification_id": clarification_id, "durable_op_id": durable_op_id,
                 "answer": answer[:500], "tick_action": event.get("action")},
            )
        else:
            lr.set_operation_status(state_dir, project_root, operation_id, "failed",
                                    f"Clarification resume failed: {error or event}")
            lr.append_operation_event(
                state_dir, project_root, operation_id, "clarification_resume_failed",
                {"clarification_id": clarification_id, "durable_op_id": durable_op_id,
                 "error": error or str(event)},
            )
    return {
        "resumed": ok,
        "operation_id": operation_id,
        "durable_op_id": durable_op_id,
        "status": status,
        "tick_action": event.get("action"),
        "error": "" if ok else (error or str(event)),
    }


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
    state_dir = Path(state_dir)
    project_root = Path(project_root).resolve()
    lr = _lr()
    op = lr.init_operation(
        state_dir,
        project_root,
        prompt=prompt,
        coordinator_agent_id="",
        budget=budget,
        model_policy=model_policy,
        summary=f"Libris (durable): {prompt[:120]}",
        durable_bootstrap={
            "kind": _BOOTSTRAP_KIND,
            "version": 1,
            "phase": "intent_persisted",
            "project_root": str(project_root),
            "max_topics_default": int(max_topics_default),
            "parent_agent_id": str(parent_agent_id or ""),
        },
    )
    op_id = op["operation_id"]
    _startup_checkpoint("operation_persisted")
    recovered = _recover_libris_bootstrap(state_dir, project_root, op_id)
    _startup_checkpoint("before_response")
    public_operation = dict(op)
    public_operation.pop(_BOOTSTRAP_KEY, None)
    return {
        "operation": public_operation,
        "coordinator": recovered["coordinator"],
        "durable_op_id": recovered["durable_op_id"],
    }


__all__ = [
    "KIND", "register", "start_durable_libris_research",
    "resume_libris_clarification", "discover_libris_clarification",
    "recover_pending_libris_bootstraps",
]
