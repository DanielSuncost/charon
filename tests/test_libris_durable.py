"""Libris on the durable runtime: full flow progression, the clarification
dead-end replaced by suspend/resume, and crash-recovery of a stalled researcher.

Agent spawning + liveness are stubbed (as in test_libris_clarification); the real
libris_runtime state helpers run on a temp state dir, and durable steps are
driven with tick_operation.
"""
import json
import shutil
from types import SimpleNamespace

import pytest

from charon.libris import libris_durable as ld
from charon.libris import libris_agents as la
from charon.libris import libris_runtime as lr
from charon.orchestration import runtime as rt


@pytest.fixture
def stub_agents(monkeypatch):
    """Stub agent spawning + liveness. Returns handles to control them."""
    spawned = []
    agent_status = {"default": "running"}

    def fake_spawn(state_dir, project_root, *, role, operation_id, topic_slug="",
                   user_goal="", parent_agent_id="", agent_id="",
                   restart_existing=False):
        aid = agent_id or f"AG-{role}-{len(spawned)}"
        spawned.append({"role": role, "topic_slug": topic_slug, "id": aid})
        return {"id": aid}

    def fake_status(agent_id):
        return agent_status.get(agent_id, agent_status["default"])

    monkeypatch.setattr(la, "spawn_libris_role", fake_spawn)
    monkeypatch.setattr(la, "_agent_status", fake_status)
    return {"spawned": spawned, "status": agent_status}


def _start(tmp_path, stub, prompt="research agent memory", **kw):
    return ld.start_durable_libris_research(tmp_path, tmp_path, prompt=prompt,
                                            budget={"max_topics": 1}, **kw)


def _mark_draft(tmp_path, op_id, slug):
    # topic state is filesystem-derived, so create the real draft file
    lr.save_report_draft(tmp_path, tmp_path, op_id, slug, markdown="# Draft\nbody")


def _mark_checkpoint(tmp_path, op_id, slug):
    lr.save_checkpoint(tmp_path, tmp_path, op_id, slug, report_markdown="r",
                       critique_markdown="c", summary_markdown="s", score=8.5)


def test_deterministic_coordinator_reuse_validates_launch_identity(
    tmp_path,
    monkeypatch,
):
    from charon.agents import agent_lifecycle

    coordinator_id = "libris-coordinator-fixed"
    expected = {
        "id": coordinator_id,
        "role": "coordinator",
        "project": str(tmp_path.resolve()),
        "goal": "research goal",
        "parent_agent_id": "AG-parent",
    }
    monkeypatch.setattr(agent_lifecycle, "load_agents", lambda _state_dir: [expected])
    monkeypatch.setattr(
        agent_lifecycle,
        "create_agent",
        lambda **_kwargs: pytest.fail("matching identity must be reused"),
    )

    reused = la.spawn_libris_role(
        tmp_path,
        tmp_path,
        role="coordinator",
        operation_id="rop-1",
        user_goal="research goal",
        parent_agent_id="AG-parent",
        agent_id=coordinator_id,
    )
    assert reused == expected

    monkeypatch.setattr(
        agent_lifecycle,
        "load_agents",
        lambda _state_dir: [{**expected, "project": str(tmp_path / "other")}],
    )
    with pytest.raises(ValueError, match="different Libris launch identity"):
        la.spawn_libris_role(
            tmp_path,
            tmp_path,
            role="coordinator",
            operation_id="rop-1",
            user_goal="research goal",
            parent_agent_id="AG-parent",
            agent_id=coordinator_id,
        )


@pytest.mark.parametrize(
    "boundary",
    [
        "operation_projection_persisted",
        "operation_persisted",
        "continuation_persisted",
        "coordinator_spawned",
        "coordinator_linked",
        "bootstrap_ready",
        "before_response",
    ],
)
def test_durable_start_recovers_each_commit_boundary(
    tmp_path,
    monkeypatch,
    stub_agents,
    boundary,
):
    class InjectedCrash(RuntimeError):
        pass

    def crash_runtime(stage, _operation_id):
        if stage == boundary:
            raise InjectedCrash(stage)

    def crash_durable(stage):
        if stage == boundary:
            raise InjectedCrash(stage)

    monkeypatch.setattr(lr, "_operation_init_checkpoint", crash_runtime)
    monkeypatch.setattr(ld, "_startup_checkpoint", crash_durable)

    with pytest.raises(InjectedCrash, match=boundary):
        ld.start_durable_libris_research(
            tmp_path,
            tmp_path,
            prompt="recoverable launch",
            budget={"max_topics": 1},
            parent_agent_id="AG-parent",
        )

    operation_paths = list(
        tmp_path.glob("projects/*/research/operations/*/operation.json")
    )
    assert len(operation_paths) == 1
    before = json.loads(operation_paths[0].read_text(encoding="utf-8"))
    operation_id = before["operation_id"]
    expected_durable_id = ld._durable_operation_id(operation_id)
    expected_coordinator_id = ld._coordinator_agent_id(operation_id)

    monkeypatch.setattr(lr, "_operation_init_checkpoint", lambda *_args: None)
    monkeypatch.setattr(ld, "_startup_checkpoint", lambda *_args: None)
    monkeypatch.setattr(ld, "_PROCESS_INSTANCE_ID", "restarted-process")

    ld.register(tmp_path)
    assert ld.recover_pending_libris_bootstraps(tmp_path) == []

    durable_rows = []
    for path in (tmp_path / "orchestration" / "ops").glob("op_*.json"):
        row = json.loads(path.read_text(encoding="utf-8"))
        if (row.get("state") or {}).get("operation_id") == operation_id:
            durable_rows.append(row)
    assert len(durable_rows) == 1
    assert durable_rows[0]["op_id"] == expected_durable_id
    assert durable_rows[0]["kind"] == ld.KIND
    assert durable_rows[0]["status"] == "running"
    assert durable_rows[0]["resume_key"] is None
    assert durable_rows[0]["suspended_reason"] == ""

    repaired = json.loads(operation_paths[0].read_text(encoding="utf-8"))
    bootstrap = repaired["durable_bootstrap"]
    assert bootstrap["phase"] == "ready"
    assert bootstrap["durable_op_id"] == expected_durable_id
    assert bootstrap["coordinator_agent_id"] == expected_coordinator_id
    assert repaired["coordinator_agent_id"] == expected_coordinator_id
    assert {row["id"] for row in stub_agents["spawned"]} == {
        expected_coordinator_id,
    }

    from charon.libris import libris_lifecycle

    lifecycle = libris_lifecycle.load_lifecycle(
        tmp_path,
        operation_id,
        entity_type="operation",
    )
    assert lifecycle is not None
    events = [
        json.loads(line)
        for line in (operation_paths[0].parent / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert sum(event["type"] == "operation_started" for event in events) == 1
    index = json.loads(
        (operation_paths[0].parents[2] / "index.json").read_text(encoding="utf-8")
    )
    assert any(row["operation_id"] == operation_id for row in index["operations"])


def test_failed_coordinator_provision_stays_suspended_until_recovery(
    tmp_path,
    monkeypatch,
    stub_agents,
):
    original_spawn = la.spawn_libris_role

    def fail_before_provision(*_args, **_kwargs):
        raise RuntimeError("coordinator unavailable")

    monkeypatch.setattr(la, "spawn_libris_role", fail_before_provision)
    with pytest.raises(RuntimeError, match="coordinator unavailable"):
        ld.start_durable_libris_research(
            tmp_path,
            tmp_path,
            prompt="recover after coordinator outage",
        )

    operation_path = next(
        tmp_path.glob("projects/*/research/operations/*/operation.json")
    )
    operation = json.loads(operation_path.read_text(encoding="utf-8"))
    operation_id = operation["operation_id"]
    durable_id = ld._durable_operation_id(operation_id)
    durable = rt.get_operation(tmp_path, durable_id)
    assert durable["status"] == "suspended"
    assert durable["resume_key"] == ld._BOOTSTRAP_RESUME_KEY
    assert durable["suspended_reason"] == ld._BOOTSTRAP_SUSPENDED_REASON

    # Heartbeats cannot run scout or file a misleading clarification while the
    # coordinator launch is incomplete.
    assert rt.tick_operations(tmp_path) == []
    assert not (tmp_path / "clarifications.json").exists()

    monkeypatch.setattr(la, "spawn_libris_role", original_spawn)
    recovered = ld.recover_pending_libris_bootstraps(tmp_path)
    assert len(recovered) == 1

    durable = rt.get_operation(tmp_path, durable_id)
    assert durable["status"] == "running"
    assert durable["resume_key"] is None
    assert durable["suspended_reason"] == ""
    repaired = json.loads(operation_path.read_text(encoding="utf-8"))
    assert repaired["durable_bootstrap"]["phase"] == "ready"
    assert repaired["coordinator_agent_id"] == ld._coordinator_agent_id(operation_id)


def test_durable_start_response_hides_internal_bootstrap(tmp_path, stub_agents):
    result = ld.start_durable_libris_research(
        tmp_path,
        tmp_path,
        prompt="public response contract",
    )

    assert set(result) == {"operation", "coordinator", "durable_op_id"}
    assert "durable_bootstrap" not in result["operation"]
    assert result["coordinator"]["id"]
    assert result["durable_op_id"]
    durable = rt.get_operation(tmp_path, result["durable_op_id"])
    assert durable["status"] == "running"
    assert durable["resume_key"] is None


def test_finalize_step_does_not_rewind_verification_on_restart(
    tmp_path,
    monkeypatch,
):
    op = lr.init_operation(tmp_path, tmp_path, prompt="resume verification")
    op_id = op["operation_id"]
    lr.set_operation_status(tmp_path, tmp_path, op_id, "assembling_delivery")
    lr.set_operation_status(tmp_path, tmp_path, op_id, "verifying_delivery")
    monkeypatch.setattr(
        lr,
        "finalize_operation_selection",
        lambda *_args, **_kwargs: {
            "ready": True,
            "delivery_manifest": {"ready": True},
        },
    )
    context = SimpleNamespace(
        state={
            "state_dir": str(tmp_path),
            "project_root": str(tmp_path),
            "operation_id": op_id,
            "prompt": "resume verification",
        }
    )

    directive = ld._step_finalize(context)

    assert directive.kind == "done"
    assert lr.get_operation_state(
        tmp_path,
        tmp_path,
        op_id,
    )["status"] == "verifying_delivery"


def test_full_flow_scout_fanout_supervise_finalize(tmp_path, monkeypatch, stub_agents):
    # converge immediately once a checkpoint exists (convergence tested elsewhere)
    from charon.libris import libris_convergence as lc
    monkeypatch.setattr(lc, "should_request_additional_revision",
                        lambda *a, **k: {"should_revise": False,
                                         "reasons": ["quality_good_enough"], "metrics": {}})
    res = _start(tmp_path, stub_agents)
    op_id, dop = res["operation"]["operation_id"], res["durable_op_id"]
    # coordinator produced candidate topics
    lr.save_candidate_topics(tmp_path, tmp_path, op_id,
                             topics=[{"title": "Agent memory datasets",
                                      "recommended_action": "deep_research"}])

    rt.tick_operation(tmp_path, dop)                         # scout -> fanout
    assert rt.get_operation(tmp_path, dop)["cursor"] == "fanout"
    rt.tick_operation(tmp_path, dop)                         # fanout -> supervise (spawns researcher)
    assert rt.get_operation(tmp_path, dop)["cursor"] == "supervise"
    assert any(s["role"] == "researcher" for s in stub_agents["spawned"])
    slug = (lr.get_operation_state(tmp_path, tmp_path, op_id)["topics"])[0]["slug"]

    rt.tick_operation(tmp_path, dop)                         # no draft yet -> stay
    assert rt.get_operation(tmp_path, dop)["status"] == "running"

    _mark_draft(tmp_path, op_id, slug)
    _reset_delay(tmp_path, dop)
    rt.tick_operation(tmp_path, dop)                         # draft -> spawn judge
    assert any(s["role"] == "judge" for s in stub_agents["spawned"])

    _mark_checkpoint(tmp_path, op_id, slug)
    _reset_delay(tmp_path, dop)
    rt.tick_operation(tmp_path, dop)                         # checkpoint + converged -> finalize
    assert rt.get_operation(tmp_path, dop)["cursor"] == "finalize"
    rt.tick_operation(tmp_path, dop)                         # finalize -> done
    final = rt.get_operation(tmp_path, dop)
    assert final["status"] == "done" and final["state"]["outcome"] == "reports_ready"


def test_clarification_suspends_and_resumes(tmp_path, stub_agents):
    # coordinator finished with NO candidate topics -> suspend (not dead-end park)
    res = _start(tmp_path, stub_agents)
    op_id, dop = res["operation"]["operation_id"], res["durable_op_id"]
    coord_id = res["coordinator"]["id"]
    stub_agents["status"][coord_id] = "stopped"             # coordinator done, no topics

    rt.tick_operation(tmp_path, dop)
    d = rt.get_operation(tmp_path, dop)
    assert d["status"] == "suspended"
    assert lr.get_operation_state(tmp_path, tmp_path, op_id)["status"] == "awaiting_clarification"
    # ticking a suspended op is a no-op (the old code returned and orphaned here)
    assert rt.tick_operation(tmp_path, dop)["action"] == "skipped"

    # user answers -> resume; scout re-spawns coordinator and keeps going
    rt.resume(tmp_path, dop, "focus on conversation-disentanglement corpora")
    rt.tick_operation(tmp_path, dop)
    assert rt.get_operation(tmp_path, dop)["status"] == "running"
    assert any(s["role"] == "coordinator" for s in stub_agents["spawned"][1:])  # re-spawned

    # the re-scout produces topics -> flow proceeds past the old dead-end
    lr.save_candidate_topics(tmp_path, tmp_path, op_id,
                             topics=[{"title": "IRC disentanglement",
                                      "recommended_action": "deep_research"}])
    _reset_delay(tmp_path, dop)
    rt.tick_operation(tmp_path, dop)
    assert rt.get_operation(tmp_path, dop)["cursor"] == "fanout"


def test_answering_linked_clarification_resumes_and_verifies(tmp_path, stub_agents):
    from charon import tools as tools_mod
    from charon.tools import clarify_tool as cl

    res = _start(tmp_path, stub_agents)
    op_id, dop = res["operation"]["operation_id"], res["durable_op_id"]
    stub_agents["status"][res["coordinator"]["id"]] = "stopped"
    rt.tick_operation(tmp_path, dop)  # files linked clarification + suspends

    cctx = tools_mod.ToolContext(project_root=tmp_path, agent_id="AG-user", state_dir=tmp_path)
    pending = cl.execute_clarify({"action": "list"}, cctx).details["items"]
    assert len(pending) == 1
    row = pending[0]
    assert row["metadata"]["continuation"] == "libris"
    assert row["metadata"]["operation_id"] == op_id
    assert row["metadata"]["durable_op_id"] == dop

    result = cl.execute_clarify({
        "action": "answer",
        "clarification_id": row["clarification_id"],
        "answer": "focus on conversation-disentanglement corpora",
    }, cctx)
    assert not result.is_error
    assert "Libris resumed operation" in result.content
    assert result.details["applied_result"]["resumed"] is True
    durable = rt.get_operation(tmp_path, dop)
    assert durable["status"] == "running"
    assert durable["state"]["coordinator_id"] != res["coordinator"]["id"]
    assert lr.get_operation_state(tmp_path, tmp_path, op_id)["status"] == "scouting"


def test_legacy_clarification_discovers_durable_continuation(tmp_path, stub_agents):
    from charon import tools as tools_mod
    from charon.tools import clarify_tool as cl

    res = _start(tmp_path, stub_agents)
    op_id, dop = res["operation"]["operation_id"], res["durable_op_id"]
    stub_agents["status"][res["coordinator"]["id"]] = "stopped"
    rt.tick_operation(tmp_path, dop)

    cctx = tools_mod.ToolContext(project_root=tmp_path, agent_id="AG-user", state_dir=tmp_path)
    row = cl.execute_clarify({"action": "list"}, cctx).details["items"][0]
    # Simulate a record created by the old code before continuation metadata.
    data = cl._load(tmp_path)
    data["items"][0].pop("metadata", None)
    cl._save(tmp_path, data)

    result = cl.execute_clarify({
        "action": "answer",
        "clarification_id": row["clarification_id"],
        "answer": "focus narrowly on the named topic",
    }, cctx)
    assert not result.is_error
    assert result.details["metadata"]["legacy_discovered"] is True
    assert result.details["applied_result"]["durable_op_id"] == dop
    assert rt.get_operation(tmp_path, dop)["status"] == "running"
    assert lr.get_operation_state(tmp_path, tmp_path, op_id)["status"] == "scouting"


def test_legacy_controller_clarification_is_adopted_into_durable_runtime(
    tmp_path,
    stub_agents,
    monkeypatch,
):
    from charon import tools as tools_mod
    from charon.tools import clarify_tool as cl

    original_start = rt.start_operation
    initial_statuses = []

    def record_initial_status(*args, **kwargs):
        created = original_start(*args, **kwargs)
        persisted = rt.get_operation(args[0], created["op_id"])
        initial_statuses.append(persisted["status"])
        return created

    def reject_follow_up_write(*_args, **_kwargs):
        pytest.fail("adoption must not rewrite a scheduler-visible running operation")

    monkeypatch.setattr(rt, "start_operation", record_initial_status)
    monkeypatch.setattr(rt, "_write_op", reject_follow_up_write)

    op = lr.init_operation(tmp_path, tmp_path, prompt="legacy topic", coordinator_agent_id="AG-old")
    op_id = op["operation_id"]
    lr.set_operation_status(tmp_path, tmp_path, op_id, "awaiting_clarification")
    cctx = tools_mod.ToolContext(project_root=tmp_path, agent_id="AG-old", state_dir=tmp_path)
    ask = cl.execute_clarify({
        "action": "ask", "question": "What should it research?",
        "metadata": {
            "continuation": "libris", "operation_id": op_id,
            "project_root": str(tmp_path),
        },
    }, cctx)

    result = cl.execute_clarify({
        "action": "answer", "clarification_id": ask.details["clarification_id"],
        "answer": "the named topic",
    }, cctx)
    assert not result.is_error
    applied = result.details["applied_result"]
    assert applied["resumed"] is True
    assert initial_statuses == ["suspended"]
    adopted = rt.get_operation(tmp_path, applied["durable_op_id"])
    assert adopted["status"] == "running"
    assert lr.get_operation_state(tmp_path, tmp_path, op_id)["status"] == "scouting"


def test_answering_linked_clarification_reports_resume_failure(tmp_path):
    from charon import tools as tools_mod
    from charon.tools import clarify_tool as cl

    cctx = tools_mod.ToolContext(project_root=tmp_path, agent_id="AG-user", state_dir=tmp_path)
    ask = cl.execute_clarify({
        "action": "ask",
        "question": "What should Libris research?",
        "metadata": {
            "continuation": "libris",
            "operation_id": "rop_missing",
            "durable_op_id": "op_missing",
            "project_root": str(tmp_path),
        },
    }, cctx)
    result = cl.execute_clarify({
        "action": "answer",
        "clarification_id": ask.details["clarification_id"],
        "answer": "the named topic",
    }, cctx)
    assert result.is_error
    assert "FAILED to resume" in result.content
    assert result.details["status"] == "failed"
    assert "no durable Libris continuation" in result.details["apply_error"]
    pending = cl.execute_clarify({"action": "list"}, cctx).details["items"]
    assert pending[0]["clarification_id"] == ask.details["clarification_id"]


def test_crash_recovery_respawns_stalled_researcher(tmp_path, monkeypatch, stub_agents):
    res = _start(tmp_path, stub_agents)
    op_id, dop = res["operation"]["operation_id"], res["durable_op_id"]
    lr.save_candidate_topics(tmp_path, tmp_path, op_id,
                             topics=[{"title": "T", "recommended_action": "deep_research"}])
    rt.tick_operation(tmp_path, dop)                         # scout -> fanout
    rt.tick_operation(tmp_path, dop)                         # fanout -> supervise (researcher spawned)
    slug = lr.get_operation_state(tmp_path, tmp_path, op_id)["topics"][0]["slug"]
    researcher_id = [s for s in stub_agents["spawned"] if s["role"] == "researcher"][0]["id"]

    # simulate a crash: the researcher thread died (agent no longer running),
    # no draft, and it was spawned long enenough ago to be considered stalled.
    stub_agents["status"][researcher_id] = "stopped"
    lr.update_topic_runtime(tmp_path, tmp_path, op_id, slug,
                            extras={"researcher_spawned_at": 0.0})  # far in the past

    n_before = sum(1 for s in stub_agents["spawned"] if s["role"] == "researcher")
    _reset_delay(tmp_path, dop)
    rt.tick_operation(tmp_path, dop)                         # supervise detects stall -> re-spawn
    n_after = sum(1 for s in stub_agents["spawned"] if s["role"] == "researcher")
    assert n_after == n_before + 1
    topic = lr.get_operation_state(tmp_path, tmp_path, op_id)["topics"][0]
    assert int(topic.get("respawn_count") or 0) == 1


def test_writer_fallback_when_researcher_finishes_without_draft(tmp_path, stub_agents):
    res = _start(tmp_path, stub_agents)
    op_id, dop = res["operation"]["operation_id"], res["durable_op_id"]
    lr.save_candidate_topics(tmp_path, tmp_path, op_id,
                             topics=[{"title": "T", "recommended_action": "deep_research"}])
    rt.tick_operation(tmp_path, dop)                         # scout -> fanout
    rt.tick_operation(tmp_path, dop)                         # fanout -> supervise
    slug = lr.get_operation_state(tmp_path, tmp_path, op_id)["topics"][0]["slug"]
    researcher_id = [s for s in stub_agents["spawned"] if s["role"] == "researcher"][0]["id"]

    # researcher finished, saved a source, but never wrote a draft
    lr.add_source(tmp_path, tmp_path, topic_slug=slug, title="A paper",
                  url="https://arxiv.org/abs/1", operation_id=op_id)
    stub_agents["status"][researcher_id] = "stopped"
    lr.update_topic_runtime(tmp_path, tmp_path, op_id, slug,
                            extras={"researcher_spawned_at": 0.0})

    _reset_delay(tmp_path, dop)
    rt.tick_operation(tmp_path, dop)                         # -> writer fallback
    assert any(s["role"] == "writer" for s in stub_agents["spawned"])
    assert lr.get_operation_state(tmp_path, tmp_path, op_id)["topics"][0]["status"] == "writing"


def test_stopped_judge_without_checkpoint_is_retried(tmp_path, stub_agents):
    res = _start(tmp_path, stub_agents)
    op_id, dop = res["operation"]["operation_id"], res["durable_op_id"]
    lr.save_candidate_topics(
        tmp_path,
        tmp_path,
        op_id,
        topics=[{"title": "T", "recommended_action": "deep_research"}],
    )
    rt.tick_operation(tmp_path, dop)  # scout -> fanout
    rt.tick_operation(tmp_path, dop)  # fanout -> supervise
    slug = lr.get_operation_state(tmp_path, tmp_path, op_id)["topics"][0]["slug"]
    _mark_draft(tmp_path, op_id, slug)
    _reset_delay(tmp_path, dop)
    rt.tick_operation(tmp_path, dop)  # spawn first judge
    first_judge = [s for s in stub_agents["spawned"] if s["role"] == "judge"][-1]["id"]
    stub_agents["status"][first_judge] = "stopped"
    lr.update_topic_runtime(
        tmp_path,
        tmp_path,
        op_id,
        slug,
        extras={"judge_spawned_at": 0.0},
    )

    _reset_delay(tmp_path, dop)
    rt.tick_operation(tmp_path, dop)

    judges = [s for s in stub_agents["spawned"] if s["role"] == "judge"]
    assert len(judges) == 2
    topic = lr.get_operation_state(tmp_path, tmp_path, op_id)["topics"][0]
    assert topic["status"] == "judging"
    assert topic["judge_attempts"] == 2
    assert topic["judge_agent_id"] == judges[-1]["id"]


def test_judge_retry_cap_terminates_topic_instead_of_hanging(tmp_path, stub_agents):
    res = _start(tmp_path, stub_agents)
    op_id, dop = res["operation"]["operation_id"], res["durable_op_id"]
    lr.save_candidate_topics(
        tmp_path,
        tmp_path,
        op_id,
        topics=[{"title": "T", "recommended_action": "deep_research"}],
    )
    rt.tick_operation(tmp_path, dop)
    rt.tick_operation(tmp_path, dop)
    slug = lr.get_operation_state(tmp_path, tmp_path, op_id)["topics"][0]["slug"]
    _mark_draft(tmp_path, op_id, slug)
    lr.update_topic_runtime(
        tmp_path,
        tmp_path,
        op_id,
        slug,
        status="judging",
        judge_agent_id="AG-dead-judge",
        extras={"judge_attempts": ld._MAX_JUDGE_ATTEMPTS, "judge_spawned_at": 0.0},
    )
    stub_agents["status"]["AG-dead-judge"] = "stopped"

    before = len([s for s in stub_agents["spawned"] if s["role"] == "judge"])
    _reset_delay(tmp_path, dop)
    rt.tick_operation(tmp_path, dop)
    after = len([s for s in stub_agents["spawned"] if s["role"] == "judge"])

    assert after == before
    topic = lr.get_operation_state(tmp_path, tmp_path, op_id)["topics"][0]
    assert topic["status"] == "judge_failed"
    assert rt.get_operation(tmp_path, dop)["cursor"] == "finalize"


def test_legacy_empty_delivery_recovers_and_finishes(tmp_path, monkeypatch, stub_agents):
    from charon.libris import libris_convergence as lc

    monkeypatch.setattr(
        lc,
        "should_request_additional_revision",
        lambda *a, **k: {
            "should_revise": False,
            "reasons": ["quality_good_enough"],
            "metrics": {},
        },
    )
    res = _start(tmp_path, stub_agents)
    op_id, dop = res["operation"]["operation_id"], res["durable_op_id"]
    lr.save_candidate_topics(
        tmp_path,
        tmp_path,
        op_id,
        topics=[{"title": "T", "recommended_action": "deep_research"}],
    )
    rt.tick_operation(tmp_path, dop)
    rt.tick_operation(tmp_path, dop)
    slug = lr.get_operation_state(tmp_path, tmp_path, op_id)["topics"][0]["slug"]
    _mark_draft(tmp_path, op_id, slug)
    lr.update_topic_runtime(
        tmp_path,
        tmp_path,
        op_id,
        slug,
        status="judging",
        judge_agent_id="AG-legacy-judge",
        extras={"judge_round": 1},
    )
    stub_agents["status"]["AG-legacy-judge"] = "stopped"
    lr.build_operation_delivery_bundle(tmp_path, tmp_path, op_id, [])
    # Simulate a pre-FSM projection that falsely claimed delivery. A generic
    # status writer can no longer create this state.
    from charon.libris import libris_lifecycle as lifecycle

    machine_path = lifecycle.lifecycle_path(tmp_path, op_id, entity_type="operation")
    shutil.rmtree(machine_path.parent)
    op_path = lr.operation_dir(tmp_path, tmp_path, op_id) / "operation.json"
    legacy = json.loads(op_path.read_text(encoding="utf-8"))
    legacy["status"] = "delivered"
    for key in (
        "lifecycle_state",
        "lifecycle_revision",
        "lifecycle_event_seq",
        "lifecycle_updated_at",
    ):
        legacy.pop(key, None)
    op_path.write_text(json.dumps(legacy), encoding="utf-8")

    _reset_delay(tmp_path, dop)
    rt.tick_operation(tmp_path, dop)

    recovered = lr.get_operation_state(tmp_path, tmp_path, op_id)
    topic = recovered["topics"][0]
    assert recovered["status"] == "researching"
    assert topic["judge_attempts"] == 2
    assert topic["judge_agent_id"] != "AG-legacy-judge"

    _mark_checkpoint(tmp_path, op_id, slug)
    _reset_delay(tmp_path, dop)
    rt.tick_operation(tmp_path, dop)
    assert rt.get_operation(tmp_path, dop)["cursor"] == "finalize"
    rt.tick_operation(tmp_path, dop)

    final = rt.get_operation(tmp_path, dop)
    assert final["status"] == "done"
    assert final["state"]["outcome"] == "reports_ready"
    manifest = lr.get_delivery_manifest(tmp_path, tmp_path, op_id)
    assert manifest["ready"] is True
    assert manifest["topic_count"] == 1


def test_terminal_topic_is_not_reprocessed_on_each_supervise_tick(tmp_path, stub_agents):
    res = ld.start_durable_libris_research(
        tmp_path,
        tmp_path,
        prompt="research two topics",
        budget={"max_topics": 2},
    )
    op_id, dop = res["operation"]["operation_id"], res["durable_op_id"]
    lr.save_candidate_topics(
        tmp_path,
        tmp_path,
        op_id,
        topics=[
            {"title": "Finished", "recommended_action": "deep_research"},
            {"title": "Still running", "recommended_action": "deep_research"},
        ],
    )
    rt.tick_operation(tmp_path, dop)
    rt.tick_operation(tmp_path, dop)
    topics = lr.get_operation_state(tmp_path, tmp_path, op_id)["topics"]
    finished = next(t for t in topics if t["title"] == "Finished")
    lr.update_topic_runtime(
        tmp_path,
        tmp_path,
        op_id,
        finished["slug"],
        status="checkpointed",
    )
    events_path = lr.operation_dir(tmp_path, tmp_path, op_id) / "events.jsonl"

    _reset_delay(tmp_path, dop)
    rt.tick_operation(tmp_path, dop)
    first = events_path.read_text()
    _reset_delay(tmp_path, dop)
    rt.tick_operation(tmp_path, dop)
    second = events_path.read_text()

    assert second.count(
        f'"topic_slug": "{finished["slug"]}", "status": "checkpointed"'
    ) == first.count(
        f'"topic_slug": "{finished["slug"]}", "status": "checkpointed"'
    )


def _reset_delay(tmp_path, dop):
    """Clear a stay()'s not_before so the next tick runs immediately (no wall wait)."""
    op = rt.get_operation(tmp_path, dop)
    op["not_before"] = 0.0
    rt._write_op(tmp_path, op)


def test_revert_topic_draft_to_best_keeps_best(tmp_path):
    """keep-if-better: when the latest checkpoint regressed, the working draft is
    restored to the best-scoring checkpoint's report."""
    op = lr.init_operation(tmp_path, tmp_path, prompt="p", coordinator_agent_id="c")
    op_id = op["operation_id"]
    topic = lr.init_topic(tmp_path, tmp_path, op_id, title="T")
    slug = topic["slug"]
    # round 1: good report -> high-scoring checkpoint
    lr.save_report_draft(tmp_path, tmp_path, op_id, slug, markdown="GOOD draft (round 1)")
    lr.save_checkpoint(tmp_path, tmp_path, op_id, slug, report_markdown="GOOD draft (round 1)",
                       critique_markdown="c", summary_markdown="s", score=8.1)
    # round 2: worse, thinner report -> low-scoring checkpoint, now the draft
    lr.save_report_draft(tmp_path, tmp_path, op_id, slug, markdown="worse thin draft (round 2)")
    lr.save_checkpoint(tmp_path, tmp_path, op_id, slug, report_markdown="worse thin draft (round 2)",
                       critique_markdown="c", summary_markdown="s", score=7.3)
    draft = lr.topic_dir(tmp_path, tmp_path, op_id, slug) / "draft-report.md"
    assert "worse thin" in draft.read_text()          # regressed draft is current
    changed = lr.revert_topic_draft_to_best(tmp_path, tmp_path, op_id, slug)
    assert changed is True
    assert "GOOD draft (round 1)" in draft.read_text()  # restored to the best
    # idempotent: reverting again is a no-op (latest best already in place)
    assert lr.revert_topic_draft_to_best(tmp_path, tmp_path, op_id, slug) is False
