"""Libris on the durable runtime: full flow progression, the clarification
dead-end replaced by suspend/resume, and crash-recovery of a stalled researcher.

Agent spawning + liveness are stubbed (as in test_libris_clarification); the real
libris_runtime state helpers run on a temp state dir, and durable steps are
driven with tick_operation.
"""
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
                   user_goal="", parent_agent_id=""):
        aid = f"AG-{role}-{len(spawned)}"
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


def _reset_delay(tmp_path, dop):
    """Clear a stay()'s not_before so the next tick runs immediately (no wall wait)."""
    op = rt.get_operation(tmp_path, dop)
    op["not_before"] = 0.0
    rt._write_op(tmp_path, op)
