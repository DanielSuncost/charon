from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from charon.agents import agent_runtime
from charon.conversation.conversation_runtime import load_queue, save_queue
from charon.providers import ModelInfo
from charon.providers import model_registry
from charon.orchestration import graph_runtime
from charon.orchestration.fsm import EventCollision, RevisionConflict
from charon.orchestration.graph_runtime import (
    get_run,
    project_run,
    resume_run,
    start_run,
    stop_run,
    tick_run,
    tick_runs,
)
from charon.orchestration.graph_schema import (
    EdgeSpec,
    GraphDefinition,
    GraphValidationError,
    NodeSpec,
)


def _drive(state_dir, run_id, limit=30):
    for index in range(limit):
        event = tick_run(state_dir, run_id, now=time.time() + index + 100)
        if event.get("status") in {"completed", "failed", "stopped"}:
            return get_run(state_dir, run_id)
    raise AssertionError(f"run {run_id} did not settle")


def test_run_lifecycle_machine_is_canonical_and_projected(tmp_path):
    graph = GraphDefinition(
        graph_id="lifecycle",
        nodes=[NodeSpec("done", "noop", terminal=True)],
        edges=[],
        entry_nodes=["done"],
    )
    started = start_run(tmp_path, graph, run_id="lifecycle_run")
    assert started["lifecycle"]["state"] == "running"
    assert started["lifecycle"]["revision"] == 0

    tick_run(tmp_path, started["run_id"])
    projection = project_run(tmp_path, started["run_id"])
    assert projection["status"] == "completed"
    assert projection["revision"] == 1
    assert projection["lifecycle"] == {
        "machine": "graph_run",
        "machine_version": 1,
        "state": "completed",
        "revision": 1,
    }
    events = [
        json.loads(line)
        for line in (
            tmp_path
            / "orchestration"
            / "graph_runs"
            / started["run_id"]
            / "events.jsonl"
        ).read_text(encoding="utf-8").splitlines()
    ]
    completed = next(event for event in events if event["type"] == "run_completed")
    assert completed["fsm_revision"] == 1
    assert completed["source_status"] == "running"
    assert completed["target_status"] == "completed"


def test_suspend_resume_and_stop_use_guarded_revisions(tmp_path):
    graph = GraphDefinition(
        graph_id="lifecycle_control",
        nodes=[NodeSpec("gate", "human_gate", terminal=True)],
        edges=[],
        entry_nodes=["gate"],
    )
    run = start_run(tmp_path, graph, run_id="controlled_run")
    tick_run(tmp_path, run["run_id"])
    suspended = get_run(tmp_path, run["run_id"])
    assert suspended["status"] == "suspended"
    assert suspended["lifecycle"]["revision"] == 1

    with pytest.raises(RevisionConflict):
        resume_run(
            tmp_path,
            run["run_id"],
            {"approved": True},
            expected_revision=0,
        )
    resumed = resume_run(
        tmp_path,
        run["run_id"],
        {"approved": True},
        event_id="resume-request",
        expected_revision=1,
    )
    assert resumed["status"] == "running"
    assert resumed["lifecycle"]["revision"] == 2

    stopped = stop_run(
        tmp_path,
        run["run_id"],
        "operator request",
        event_id="stop-request",
        expected_revision=2,
    )
    assert stopped["status"] == "stopped"
    assert stopped["lifecycle"]["revision"] == 3


def test_resume_and_stop_command_replays_are_idempotent_and_collision_safe(
    tmp_path,
):
    graph = GraphDefinition(
        graph_id="lifecycle_replay",
        nodes=[NodeSpec("gate", "human_gate", terminal=True)],
        edges=[],
        entry_nodes=["gate"],
    )
    run = start_run(tmp_path, graph, run_id="replayed_control_run")
    tick_run(tmp_path, run["run_id"])

    first_resume = resume_run(
        tmp_path,
        run["run_id"],
        {"approved": True},
        event_id="resume-request",
        expected_revision=1,
    )
    replayed_resume = resume_run(
        tmp_path,
        run["run_id"],
        {"approved": True},
        event_id="resume-request",
        expected_revision=1,
    )

    assert replayed_resume["status"] == "running"
    assert replayed_resume["lifecycle"]["revision"] == 2
    assert replayed_resume["active_nodes"] == first_resume["active_nodes"]
    with pytest.raises(EventCollision):
        resume_run(
            tmp_path,
            run["run_id"],
            {"approved": False},
            event_id="resume-request",
            expected_revision=1,
        )

    first_stop = stop_run(
        tmp_path,
        run["run_id"],
        "operator request",
        event_id="stop-request",
        expected_revision=2,
    )
    replayed_stop = stop_run(
        tmp_path,
        run["run_id"],
        "operator request",
        event_id="stop-request",
        expected_revision=2,
    )

    assert replayed_stop["status"] == "stopped"
    assert replayed_stop["lifecycle"]["revision"] == 3
    assert replayed_stop["completed_at"] == first_stop["completed_at"]
    with pytest.raises(EventCollision):
        stop_run(
            tmp_path,
            run["run_id"],
            "different request",
            event_id="stop-request",
            expected_revision=2,
        )


def test_embedded_lifecycle_overrides_stale_status_projection(tmp_path):
    graph = GraphDefinition(
        graph_id="canonical_lifecycle",
        nodes=[NodeSpec("work", "noop")],
        edges=[],
        entry_nodes=["work"],
    )
    run = start_run(tmp_path, graph, run_id="canonical_run")
    path = (
        tmp_path
        / "orchestration"
        / "graph_runs"
        / run["run_id"]
        / "run.json"
    )
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["status"] = "failed"
    graph_runtime._atomic_write_json(path, raw)

    restored = get_run(tmp_path, run["run_id"])

    assert restored["status"] == "running"
    assert restored["lifecycle"]["state"] == "running"


def test_validation_is_strict_but_allows_reachable_cycles():
    graph = GraphDefinition(
        graph_id="cycle",
        nodes=[NodeSpec("a", "noop"), NodeSpec("b", "noop")],
        edges=[
            EdgeSpec("a_to_b", "a", "b"),
            EdgeSpec("b_to_a", "b", "a"),
        ],
        entry_nodes=["a"],
    )
    assert graph.to_dict()["entry_nodes"] == ["a"]
    assert GraphDefinition.from_dict(graph.to_dict()) == graph

    with pytest.raises(GraphValidationError, match="missing endpoint"):
        GraphDefinition(
            graph_id="bad",
            nodes=[NodeSpec("a", "noop")],
            edges=[EdgeSpec("bad_edge", "a", "missing")],
            entry_nodes=["a"],
        )
    with pytest.raises(GraphValidationError, match="unreachable"):
        GraphDefinition(
            graph_id="bad",
            nodes=[NodeSpec("a", "noop"), NodeSpec("orphan", "noop")],
            edges=[],
            entry_nodes=["a"],
        )
    with pytest.raises(GraphValidationError, match="join"):
        NodeSpec("bad", "noop", join="sometimes")
    raw = graph.to_dict()
    raw["surprise"] = True
    with pytest.raises(GraphValidationError, match="unknown field"):
        GraphDefinition.from_dict(raw)


def test_conditional_branch_activates_only_selected_route(tmp_path):
    graph = GraphDefinition(
        graph_id="branch",
        nodes=[
            NodeSpec("choose", "fixture", config={"result": {"route": "left"}}),
            NodeSpec("left", "noop", terminal=True),
            NodeSpec("right", "noop", terminal=True),
        ],
        edges=[
            EdgeSpec("go_left", "choose", "left", route="left"),
            EdgeSpec("go_right", "choose", "right", route="right"),
        ],
        entry_nodes=["choose"],
    )
    run = start_run(tmp_path, graph)
    first = tick_run(tmp_path, run["run_id"])
    assert first["active_nodes"] == ["left"]
    final = _drive(tmp_path, run["run_id"])
    assert final["status"] == "completed"
    assert final["node_states"]["left"]["visits"] == 1
    assert final["node_states"]["right"]["visits"] == 0


def test_unmatched_success_route_fails_instead_of_silently_completing(tmp_path):
    graph = GraphDefinition(
        graph_id="strict_route",
        nodes=[
            NodeSpec(
                "choose",
                "fixture",
                config={"result": {"route": "typo"}},
            ),
            NodeSpec("left", "noop", terminal=True),
            NodeSpec("right", "noop", terminal=True),
        ],
        edges=[
            EdgeSpec("go_left", "choose", "left", route="left"),
            EdgeSpec("go_right", "choose", "right", route="right"),
        ],
        entry_nodes=["choose"],
    )
    run = start_run(tmp_path, graph)

    event = tick_run(tmp_path, run["run_id"])
    final = get_run(tmp_path, run["run_id"])

    assert event["action"] == "failed"
    assert final["status"] == "failed"
    assert "unmatched route" in final["error"]
    assert final["node_states"]["left"]["visits"] == 0
    assert final["node_states"]["right"]["visits"] == 0


def test_fanout_and_join_all_barrier(tmp_path):
    graph = GraphDefinition(
        graph_id="fanout",
        nodes=[
            NodeSpec("fork", "noop"),
            NodeSpec("a", "noop"),
            NodeSpec("b", "noop"),
            NodeSpec("join", "noop", join="all", terminal=True),
        ],
        edges=[
            EdgeSpec("fork_a", "fork", "a"),
            EdgeSpec("fork_b", "fork", "b"),
            EdgeSpec("a_join", "a", "join"),
            EdgeSpec("b_join", "b", "join"),
        ],
        entry_nodes=["fork"],
    )
    run = start_run(tmp_path, graph)
    event = tick_run(tmp_path, run["run_id"])
    assert set(event["active_nodes"]) == {"a", "b"}
    projection = project_run(tmp_path, run["run_id"])
    assert set(projection["active_edge_ids"]) == {"fork_a", "fork_b"}

    tick_run(tmp_path, run["run_id"])
    assert "join" not in get_run(tmp_path, run["run_id"])["active_nodes"]
    tick_run(tmp_path, run["run_id"])
    assert get_run(tmp_path, run["run_id"])["active_nodes"] == ["join"]
    assert _drive(tmp_path, run["run_id"])["status"] == "completed"


def test_waiting_branch_yields_to_ready_sibling(tmp_path):
    graph = GraphDefinition(
        graph_id="fair_fanout",
        nodes=[
            NodeSpec("fork", "noop"),
            NodeSpec(
                "poller",
                "fixture",
                config={
                    "result": {
                        "status": "wait",
                        "delay_sec": 0,
                    }
                },
            ),
            NodeSpec("ready", "noop", terminal=True),
        ],
        edges=[
            EdgeSpec("fork_poller", "fork", "poller"),
            EdgeSpec("fork_ready", "fork", "ready"),
        ],
        entry_nodes=["fork"],
    )
    run = start_run(tmp_path, graph)
    tick_run(tmp_path, run["run_id"])

    assert tick_run(tmp_path, run["run_id"])["node_id"] == "poller"
    completed = tick_run(tmp_path, run["run_id"])
    final = get_run(tmp_path, run["run_id"])

    assert completed["node_id"] == "ready"
    assert final["status"] == "completed"
    assert final["node_states"]["poller"]["calls"] == 1
    assert final["node_states"]["ready"]["calls"] == 1


def test_tick_runs_is_fair_and_skips_deferred_runs(tmp_path):
    waiting = GraphDefinition(
        graph_id="waiting",
        nodes=[
            NodeSpec(
                "wait",
                "fixture",
                config={
                    "result": {
                        "status": "wait",
                        "delay_sec": 10_000,
                    }
                },
                terminal=True,
            )
        ],
        edges=[],
        entry_nodes=["wait"],
    )
    for index in range(8):
        start_run(tmp_path, waiting, run_id=f"waiting_{index}")
    ready = GraphDefinition(
        graph_id="ready",
        nodes=[NodeSpec("done", "noop", terminal=True)],
        edges=[],
        entry_nodes=["done"],
    )
    start_run(tmp_path, ready, run_id="ready_later")

    tick_runs(tmp_path, max_runs=8, now=100)
    second = tick_runs(tmp_path, max_runs=8, now=101)

    assert any(
        event["run_id"] == "ready_later" and event["status"] == "completed"
        for event in second
    )
    assert get_run(tmp_path, "ready_later")["node_states"]["done"]["calls"] == 1


def test_cycle_stops_at_node_visit_budget(tmp_path):
    graph = GraphDefinition(
        graph_id="bounded_cycle",
        nodes=[NodeSpec("again", "noop")],
        edges=[EdgeSpec("loop", "again", "again")],
        entry_nodes=["again"],
        max_node_visits=2,
        max_run_steps=20,
    )
    run = start_run(tmp_path, graph)
    final = _drive(tmp_path, run["run_id"])
    assert final["status"] == "failed"
    assert final["node_states"]["again"]["visits"] == 2
    assert "visit budget" in final["error"]


def test_retry_backoff_is_durable_then_succeeds(tmp_path):
    graph = GraphDefinition(
        graph_id="retry",
        nodes=[
            NodeSpec(
                "flaky",
                "fixture",
                config={
                    "sequence": [
                        {"raise": "transient"},
                        {"status": "success", "output": "ok"},
                    ]
                },
                max_attempts=2,
                backoff_base_sec=0,
                terminal=True,
            )
        ],
        edges=[],
        entry_nodes=["flaky"],
    )
    run = start_run(tmp_path, graph)
    first = tick_run(tmp_path, run["run_id"])
    assert first["action"] == "retry"
    persisted = get_run(tmp_path, run["run_id"])
    assert persisted["node_states"]["flaky"]["attempt"] == 1
    final = _drive(tmp_path, run["run_id"])
    assert final["status"] == "completed"
    assert final["node_states"]["flaky"]["calls"] == 2
    assert final["node_states"]["flaky"]["output"] == "ok"


def test_suspend_resume_feeds_payload_to_same_node(tmp_path):
    graph = GraphDefinition(
        graph_id="approval",
        nodes=[NodeSpec("gate", "human_gate", terminal=True)],
        edges=[],
        entry_nodes=["gate"],
    )
    run = start_run(tmp_path, graph)
    event = tick_run(tmp_path, run["run_id"])
    assert event["action"] == "suspend"
    assert get_run(tmp_path, run["run_id"])["status"] == "suspended"

    resumed = resume_run(
        tmp_path, run["run_id"], {"approved": True}, node_id="gate"
    )
    assert resumed["status"] == "running"
    final = _drive(tmp_path, run["run_id"])
    assert final["status"] == "completed"
    assert final["node_states"]["gate"]["output"] == {"approved": True}


def test_failure_edge_recovers_without_failing_run(tmp_path):
    graph = GraphDefinition(
        graph_id="fallback",
        nodes=[
            NodeSpec(
                "work",
                "fixture",
                config={"result": {"status": "failure", "error": "nope"}},
            ),
            NodeSpec("recover", "noop", terminal=True),
        ],
        edges=[EdgeSpec("fallback", "work", "recover", on="failure")],
        entry_nodes=["work"],
    )
    run = start_run(tmp_path, graph)
    first = tick_run(tmp_path, run["run_id"])
    assert first["action"] == "failure_routed"
    final = _drive(tmp_path, run["run_id"])
    assert final["status"] == "completed"
    assert final["node_states"]["work"]["status"] == "failed"


def test_snapshot_events_persistence_and_projection(tmp_path):
    graph = GraphDefinition(
        graph_id="snapshot",
        nodes=[NodeSpec("a", "noop"), NodeSpec("b", "noop", terminal=True)],
        edges=[EdgeSpec("a_b", "a", "b")],
        entry_nodes=["a"],
        metadata={"purpose": "test"},
    )
    run = start_run(tmp_path, graph, initial_state={"seed": 7}, run_id="known_run")
    root = tmp_path / "orchestration" / "graph_runs" / "known_run"
    definition_before = (root / "definition.json").read_bytes()
    tick_run(tmp_path, run["run_id"])
    projection = project_run(tmp_path, run["run_id"])
    assert projection["node_states"]["b"]["status"] == "active"
    assert projection["traversed_edge_ids"] == ["a_b"]
    assert projection["active_edge_ids"] == ["a_b"]
    assert (root / "definition.json").read_bytes() == definition_before

    events = [
        json.loads(line)
        for line in (root / "events.jsonl").read_text().splitlines()
    ]
    assert [event["seq"] for event in events] == list(
        range(1, len(events) + 1)
    )
    assert {"run_started", "node_started", "edge_traversed"} <= {
        event["type"] for event in events
    }
    restored = get_run(tmp_path, "known_run")
    assert restored["state"] == {"seed": 7}
    assert restored["definition"]["metadata"] == {"purpose": "test"}


def test_event_outbox_recovers_crash_without_duplicate_sequences(
    tmp_path,
    monkeypatch,
):
    graph = GraphDefinition(
        graph_id="outbox",
        nodes=[NodeSpec("done", "noop", terminal=True)],
        edges=[],
        entry_nodes=["done"],
    )
    run = start_run(tmp_path, graph, run_id="outbox_run")
    original_flush = graph_runtime._flush_event_outbox
    crashed = False

    def crash_after_claim_persist(state_dir, durable_run, **kwargs):
        nonlocal crashed
        if not crashed and any(
            event.get("type") == "node_started"
            for event in durable_run.get("event_outbox") or []
        ):
            crashed = True
            raise RuntimeError("simulated process crash")
        return original_flush(state_dir, durable_run, **kwargs)

    monkeypatch.setattr(
        graph_runtime,
        "_flush_event_outbox",
        crash_after_claim_persist,
    )
    with pytest.raises(RuntimeError, match="simulated process crash"):
        tick_run(tmp_path, run["run_id"])
    monkeypatch.setattr(
        graph_runtime,
        "_flush_event_outbox",
        original_flush,
    )

    claimed = get_run(tmp_path, run["run_id"])
    assert claimed["node_states"]["done"]["status"] == "running"
    final = _drive(tmp_path, run["run_id"])
    events_path = (
        tmp_path
        / "orchestration"
        / "graph_runs"
        / run["run_id"]
        / "events.jsonl"
    )
    events = [
        json.loads(line)
        for line in events_path.read_text(encoding="utf-8").splitlines()
    ]

    assert final["status"] == "completed"
    assert [event["seq"] for event in events] == list(
        range(1, len(events) + 1)
    )
    assert len({event["event_id"] for event in events}) == len(events)


def test_definition_integrity_failure_is_quarantined_and_does_not_block_others(
    tmp_path,
):
    graph = GraphDefinition(
        graph_id="integrity",
        nodes=[NodeSpec("done", "noop", terminal=True)],
        edges=[],
        entry_nodes=["done"],
    )
    start_run(tmp_path, graph, run_id="damaged")
    start_run(tmp_path, graph, run_id="healthy")
    definition_path = (
        tmp_path
        / "orchestration"
        / "graph_runs"
        / "damaged"
        / "definition.json"
    )
    raw = json.loads(definition_path.read_text(encoding="utf-8"))
    raw["nodes"][0]["config"] = {"output": "tampered"}
    definition_path.write_text(json.dumps(raw), encoding="utf-8")

    events = tick_runs(tmp_path, max_runs=1)

    assert any(event["action"] == "quarantined" for event in events)
    assert get_run(tmp_path, "damaged", include_definition=False)["status"] == "failed"
    assert get_run(tmp_path, "healthy")["status"] == "completed"


def test_missing_run_lock_does_not_reserve_future_run_id(tmp_path):
    assert tick_run(tmp_path, "future")["action"] == "missing"
    graph = GraphDefinition(
        graph_id="future",
        nodes=[NodeSpec("done", "noop", terminal=True)],
        edges=[],
        entry_nodes=["done"],
    )

    started = start_run(tmp_path, graph, run_id="future")

    assert started["run_id"] == "future"


def test_queue_agent_dispatch_is_idempotent_while_polling(tmp_path):
    graph = GraphDefinition(
        graph_id="queue",
        nodes=[
            NodeSpec(
                "delegate",
                "queue_agent",
                config={
                    "owner_agent_id": "agent-1",
                    "instruction": "perform the bounded task",
                    "poll_delay_sec": 0.01,
                },
                terminal=True,
            )
        ],
        edges=[],
        entry_nodes=["delegate"],
    )
    run = start_run(tmp_path, graph)
    assert tick_run(tmp_path, run["run_id"])["action"] == "wait"
    first_queue = load_queue(tmp_path)
    assert len(first_queue) == 1
    assert first_queue[0]["correlation_id"].startswith(
        f"graph:{run['run_id']}:delegate:1"
    )

    assert (
        tick_run(tmp_path, run["run_id"], now=time.time() + 60)["action"]
        == "wait"
    )
    assert len(load_queue(tmp_path)) == 1
    first_queue[0]["status"] = "completed"
    first_queue[0]["result_summary"] = "done"
    save_queue(tmp_path, first_queue)
    final = _drive(tmp_path, run["run_id"])
    assert final["status"] == "completed"
    assert final["node_states"]["delegate"]["output"]["task_id"] == first_queue[0]["id"]


def test_calibrated_route_flows_through_queue_and_reports_executed_endpoint(
    tmp_path,
):
    endpoint = {
        "candidate_id": "local-worker",
        "provider": "local",
        "model_id": "qwen3-30b-a3b",
        "capabilities": ["code", "tools"],
        "context_window": 65_536,
        "estimated_cost_usd": 0.01,
        "estimated_latency_ms": 2_000,
        "estimated_quality": 0.8,
        "estimated_reliability": 0.9,
    }
    graph = GraphDefinition(
        graph_id="routed_queue",
        nodes=[
            NodeSpec(
                "select",
                "route",
                config={
                    "policy": "balanced",
                    "state_key": "selected_route",
                    "task": {
                        "task_id": "routed-queue-task",
                        "required_capabilities": ["code", "tools"],
                    },
                    "candidates": [endpoint],
                },
            ),
            NodeSpec(
                "delegate",
                "queue_agent",
                config={
                    "owner_agent_id": "agent-1",
                    "instruction": "perform the routed task",
                    "route_from": "selected_route",
                    "poll_delay_sec": 0.01,
                },
                terminal=True,
            ),
        ],
        edges=[EdgeSpec("selected", "select", "delegate")],
        entry_nodes=["select"],
    )
    run = start_run(tmp_path, graph)

    assert tick_run(tmp_path, run["run_id"])["action"] == "success"
    assert tick_run(tmp_path, run["run_id"])["action"] == "wait"
    queue = load_queue(tmp_path)
    assert len(queue) == 1
    task = queue[0]
    assert task["model_route"] == {
        "candidate_id": "local-worker",
        "provider": "local",
        "model_id": "qwen3-30b-a3b",
        "context_window": 65_536,
    }
    assert task["routing_decision"]["selected_candidate_id"] == "local-worker"

    task["status"] = "completed"
    task["result_summary"] = "completed on the selected endpoint"
    task["executed_model"] = dict(task["model_route"])
    save_queue(tmp_path, queue)
    final = _drive(tmp_path, run["run_id"])
    output = final["node_states"]["delegate"]["output"]
    assert output["route_honored"] is True
    assert output["selected_model"] == output["executed_model"]


def test_queue_projection_prefers_authoritative_endpoint_provenance(tmp_path):
    selected = {
        "provider": "local",
        "model_id": "qwen3-30b-a3b",
        "context_window": 65_536,
        "base_url": "http://127.0.0.1:1234/v1",
    }
    graph = GraphDefinition(
        graph_id="queue_provenance",
        nodes=[
            NodeSpec(
                "delegate",
                "queue_agent",
                config={
                    "owner_agent_id": "agent-1",
                    "instruction": "perform the routed task",
                    "model_route": selected,
                },
                terminal=True,
            ),
        ],
        edges=[],
        entry_nodes=["delegate"],
    )
    run = start_run(tmp_path, graph)
    assert tick_run(tmp_path, run["run_id"])["action"] == "wait"
    queue = load_queue(tmp_path)
    task = queue[0]
    task["status"] = "completed"
    task["model_route"] = {
        "provider": selected["provider"],
        "model_id": selected["model_id"],
    }
    task["executed_model"] = dict(task["model_route"])
    task["selected_endpoint"] = selected
    task["executed_endpoint"] = {
        **selected,
        "context_window": 32_768,
        "base_url": "http://127.0.0.1:11434/v1",
    }
    task["route_honored"] = False
    save_queue(tmp_path, queue)

    final = _drive(tmp_path, run["run_id"])
    output = final["node_states"]["delegate"]["output"]
    assert output["selected_model"] == selected
    assert output["executed_model"] == task["executed_endpoint"]
    assert output["route_honored"] is False


def test_registry_route_normalizes_endpoint_before_queue_dispatch(
    tmp_path,
    monkeypatch,
):
    provider = SimpleNamespace(
        _base_url="http://127.0.0.1:1234/v1",
    )
    model = ModelInfo(
        provider="local",
        model_id="qwen3-30b-a3b",
        context_window=65_536,
    )
    monkeypatch.setattr(
        "charon.providers.model_registry.get_shade_provider_and_model",
        lambda state_dir, phase_name, task_complexity: (
            provider,
            model,
            True,
        ),
    )
    graph = GraphDefinition(
        graph_id="registry_route",
        nodes=[
            NodeSpec(
                "select",
                "route",
                config={"state_key": "selected_route"},
            ),
            NodeSpec(
                "delegate",
                "queue_agent",
                config={
                    "owner_agent_id": "agent-1",
                    "instruction": "perform the registry-routed task",
                    "route_from": "selected_route",
                },
                terminal=True,
            ),
        ],
        edges=[
            EdgeSpec(
                "ready",
                "select",
                "delegate",
                route="ready",
            )
        ],
        entry_nodes=["select"],
    )
    run = start_run(tmp_path, graph)

    assert tick_run(tmp_path, run["run_id"])["action"] == "success"
    decision = get_run(tmp_path, run["run_id"])["state"]["selected_route"]
    assert decision["policy"] == "provider-registry"
    assert decision["selected_endpoint"] == {
        "candidate_id": "",
        "provider": "local",
        "model_id": "qwen3-30b-a3b",
        "context_window": 65_536,
        "base_url": "http://127.0.0.1:1234/v1",
        "resolver": "model_registry",
        "phase_name": "",
        "task_complexity": "normal",
    }
    assert tick_run(tmp_path, run["run_id"])["action"] == "wait"
    task = load_queue(tmp_path)[0]
    assert task["model_route"] == decision["selected_endpoint"]


@pytest.mark.parametrize(
    ("provider_name", "base_url", "expected_alias"),
    [
        ("lmstudio", "http://127.0.0.1:1234/v1", "lmstudio"),
        ("ollama", "http://127.0.0.1:11434/v1", "ollama"),
        ("openrouter", "https://router.example.test/v1", "api"),
    ],
)
def test_registry_route_queue_executes_exact_configured_provider(
    tmp_path,
    monkeypatch,
    provider_name,
    base_url,
    expected_alias,
):
    state_dir = tmp_path / "state"
    project = tmp_path / "project"
    state_dir.mkdir()
    project.mkdir()
    for name in (
        "_shared_main_provider",
        "_shared_main_model",
        "_shared_main_ready",
        "_shared_shade_provider",
        "_shared_shade_model",
        "_shared_shade_ready",
    ):
        monkeypatch.setattr(model_registry, name, None)
    agent_runtime._agent_engines.clear()
    (state_dir / "model_registry.json").write_text(
        json.dumps(
            {
                "shade_model_mode": "fixed",
                "shade_model": "registry-model",
                "shade_provider": provider_name,
                "shade_base_url": base_url,
                "shade_api_key": "registry-secret",
            }
        )
    )
    graph = GraphDefinition(
        graph_id=f"registry_{provider_name}",
        nodes=[
            NodeSpec(
                "select",
                "route",
                config={"state_key": "selected_route"},
            ),
            NodeSpec(
                "delegate",
                "queue_agent",
                config={
                    "owner_agent_id": "agent-1",
                    "instruction": "perform the registry-routed task",
                    "project": str(project),
                    "route_from": "selected_route",
                },
                terminal=True,
            ),
        ],
        edges=[EdgeSpec("ready", "select", "delegate", route="ready")],
        entry_nodes=["select"],
    )
    run = start_run(state_dir, graph)
    assert tick_run(state_dir, run["run_id"])["action"] == "success"
    assert tick_run(state_dir, run["run_id"])["action"] == "wait"
    task = load_queue(state_dir)[0]
    assert task["model_route"]["provider"] == expected_alias
    assert task["model_route"]["base_url"] == base_url
    assert task["model_route"]["resolver"] == "model_registry"

    monkeypatch.setattr(
        agent_runtime,
        "_build_task_system_prompt",
        lambda state, agent, task: "registry-routed task",
    )
    monkeypatch.setattr(
        agent_runtime,
        "_run_task_with_engine",
        lambda state, task, agent, engine: (
            True,
            {
                "status": "task_succeeded",
                "summary": "registry route completed",
                "attempt_id": "att-registry",
            },
        ),
    )
    ok, result = agent_runtime.run_task_tick(
        state_dir,
        task,
        agent={
            "id": "agent-1",
            "name": "registry-worker",
            "project": str(project),
            "role": "charon",
        },
    )

    assert ok is True
    assert result["route_honored"] is True
    assert result["selected_endpoint"]["provider"] == expected_alias
    assert result["executed_endpoint"]["provider"] == expected_alias
    assert result["executed_endpoint"]["base_url"] == base_url


def test_calibrated_route_persists_full_rationale(tmp_path):
    calibration = {
        "model_id": "reliable",
        "trial_count": 20,
        "success_count": 19,
        "success_rate": 0.95,
        "success_ci_low": 0.80,
        "success_ci_high": 0.99,
        "p50_latency_ms": 200,
        "p95_latency_ms": 300,
        "mean_cost_usd": 0.1,
        "mean_score": 0.96,
        "brier_score": 0.04,
        "expected_calibration_error": 0.03,
    }
    candidate = {
        "candidate_id": "reliable",
        "provider": "test",
        "model_id": "reliable",
        "capabilities": ["tools"],
        "context_window": 10_000,
        "estimated_cost_usd": 0.2,
        "estimated_latency_ms": 400,
        "estimated_quality": 0.7,
        "estimated_reliability": 0.7,
        "calibration": calibration,
    }
    graph = GraphDefinition(
        graph_id="calibrated_route",
        nodes=[
            NodeSpec(
                "select",
                "route",
                config={
                    "policy": "balanced",
                    "task": {
                        "task_id": "task-1",
                        "required_capabilities": ["tools"],
                        "context_tokens": 100,
                    },
                    "candidates": [candidate],
                },
            ),
            NodeSpec(
                "worker",
                "fixture",
                config={
                    "outcome": "success",
                    "summary": "checkpoint",
                    "model_from": "select",
                },
                terminal=True,
            ),
        ],
        edges=[EdgeSpec("selected", "select", "worker", route="reliable")],
        entry_nodes=["select"],
    )
    run = start_run(tmp_path, graph)
    final = _drive(tmp_path, run["run_id"])
    decision = final["node_states"]["select"]["output"]
    assert decision["selected_candidate_id"] == "reliable"
    assert decision["selection_reason"]
    assert decision["candidates"][0]["estimate"]["calibration_used"] is True
    assert final["state"]["select"] == decision
    assert final["node_states"]["worker"]["output"] == {
        "outcome": "success",
        "summary": "checkpoint",
        "model": "reliable",
    }
