import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from charon.libris import libris_lifecycle as lifecycle
from charon.libris import libris_runtime as lr


def test_projection_reconciles_from_canonical_lifecycle_without_bumping_updated_at(tmp_path):
    op = lr.init_operation(tmp_path, tmp_path, prompt="reconcile")
    op_id = op["operation_id"]
    lr.set_operation_status(tmp_path, tmp_path, op_id, "scouting")
    path = lr.operation_dir(tmp_path, tmp_path, op_id) / "operation.json"
    projection = json.loads(path.read_text(encoding="utf-8"))
    original_updated_at = projection["updated_at"]
    projection["status"] = "running"
    projection["lifecycle_revision"] = 0
    path.write_text(json.dumps(projection), encoding="utf-8")

    restored = lr.get_operation_state(tmp_path, tmp_path, op_id)

    assert restored["status"] == "scouting"
    repaired = json.loads(path.read_text(encoding="utf-8"))
    assert repaired["status"] == "scouting"
    assert repaired["lifecycle_revision"] == 1
    assert repaired["updated_at"] == original_updated_at


def test_concurrent_topic_fanout_does_not_lose_selected_topic_ids(tmp_path):
    op = lr.init_operation(tmp_path, tmp_path, prompt="fan out")
    op_id = op["operation_id"]

    def create(index: int):
        return lr.init_topic(tmp_path, tmp_path, op_id, title=f"Topic {index}")

    with ThreadPoolExecutor(max_workers=8) as pool:
        topics = list(pool.map(create, range(16)))

    state = lr.get_operation_state(tmp_path, tmp_path, op_id)
    assert len(state["selected_topic_ids"]) == 16
    assert set(state["selected_topic_ids"]) == {topic["topic_id"] for topic in topics}


def test_record_usage_routes_budget_terminal_state_through_operation_machine(tmp_path):
    op = lr.init_operation(
        tmp_path,
        tmp_path,
        prompt="bounded",
        budget={"max_total_tokens": 10},
    )
    op_id = op["operation_id"]

    lr.record_usage(
        tmp_path,
        tmp_path,
        op_id,
        input_tokens=5,
        output_tokens=5,
    )

    state = lr.get_operation_state(tmp_path, tmp_path, op_id)
    machine = lifecycle.load_lifecycle(tmp_path, op_id, entity_type="operation")
    assert state["status"] == "budget_exhausted"
    assert machine is not None and machine.state == "budget_exhausted"


def test_researcher_recovery_can_move_writing_topic_back_to_researching(tmp_path):
    op = lr.init_operation(tmp_path, tmp_path, prompt="recover")
    topic = lr.init_topic(tmp_path, tmp_path, op["operation_id"], title="Topic")

    lr.update_topic_runtime(
        tmp_path,
        tmp_path,
        op["operation_id"],
        topic["slug"],
        status="writing",
    )
    recovered = lr.update_topic_runtime(
        tmp_path,
        tmp_path,
        op["operation_id"],
        topic["slug"],
        status="researching",
    )

    machine = lifecycle.load_lifecycle(
        tmp_path,
        topic["topic_id"],
        entity_type="topic",
    )
    assert recovered["status"] == "researching"
    assert machine is not None and machine.state == "researching"


def test_operation_timeline_event_ids_are_idempotent_and_collision_checked(tmp_path):
    op = lr.init_operation(tmp_path, tmp_path, prompt="events")
    op_id = op["operation_id"]
    payload = {"state": "scouting"}

    first = lr.append_operation_event(
        tmp_path,
        tmp_path,
        op_id,
        "fsm_projection",
        payload,
        event_id="stable-event",
    )
    replay = lr.append_operation_event(
        tmp_path,
        tmp_path,
        op_id,
        "fsm_projection",
        payload,
        event_id="stable-event",
    )

    assert replay == first
    events = lr._iter_jsonl(
        lr.operation_dir(tmp_path, tmp_path, op_id) / "events.jsonl"
    )
    assert sum(event.get("event_id") == "stable-event" for event in events) == 1
    with pytest.raises(ValueError, match="different content"):
        lr.append_operation_event(
            tmp_path,
            tmp_path,
            op_id,
            "fsm_projection",
            {"state": "fanout"},
            event_id="stable-event",
        )


def test_corrupt_operation_lifecycle_fails_closed_without_hiding_swarm(tmp_path):
    op = lr.init_operation(
        tmp_path,
        tmp_path,
        prompt="corrupt lifecycle",
        coordinator_agent_id="AG-coordinator",
    )
    op_id = op["operation_id"]
    lr.init_topic(
        tmp_path,
        tmp_path,
        op_id,
        title="Topic",
        researcher_agent_id="AG-researcher",
    )
    lr.emit_agent_comm(
        tmp_path,
        tmp_path,
        op_id,
        from_agent_id="AG-coordinator",
        to_agent_id="AG-researcher",
        from_role="coordinator",
        to_role="researcher",
        message_kind="assignment",
        summary="Investigate the topic.",
    )
    healthy = lr.get_libris_swarm_state(tmp_path, tmp_path, op_id)
    lifecycle_path = lifecycle.lifecycle_path(
        tmp_path,
        op_id,
        entity_type="operation",
    )
    lifecycle_path.write_text("{broken", encoding="utf-8")

    projected = lr.get_operation_state(tmp_path, tmp_path, op_id)
    manifest = lr.get_delivery_manifest(tmp_path, tmp_path, op_id, op=projected)
    swarm = lr.get_libris_swarm_state(tmp_path, tmp_path, op_id)

    assert projected["status"] == "lifecycle_error"
    assert manifest["status"] == "incomplete"
    assert manifest["ready"] is False
    assert manifest["integrity"]["lifecycle_valid"] is False
    assert "cannot read durable machine snapshot" in manifest["integrity"]["lifecycle_error"]
    assert swarm["status"] == "lifecycle_error"
    assert swarm["lifecycle"]["state"] == "lifecycle_error"
    assert swarm["lifecycle"]["integrity"]["lifecycle_valid"] is False
    assert [node["agent_id"] for node in swarm["nodes"]] == [
        node["agent_id"] for node in healthy["nodes"]
    ]
    assert [
        (edge["from_agent_id"], edge["to_agent_id"], edge["message_kind"])
        for edge in swarm["edges"]
    ] == [
        (edge["from_agent_id"], edge["to_agent_id"], edge["message_kind"])
        for edge in healthy["edges"]
    ]
    assert swarm["nodes"]
    assert swarm["edges"]
    assert lifecycle_path.read_text(encoding="utf-8") == "{broken"


def test_missing_known_lifecycle_is_not_reclassified_as_legacy(tmp_path):
    op = lr.init_operation(tmp_path, tmp_path, prompt="missing lifecycle")
    op_id = op["operation_id"]
    lifecycle_path = lifecycle.lifecycle_path(
        tmp_path,
        op_id,
        entity_type="operation",
    )
    lifecycle_path.unlink()

    manifest = lr.get_delivery_manifest(tmp_path, tmp_path, op_id)

    assert manifest["status"] == "incomplete"
    assert manifest["ready"] is False
    assert manifest["integrity"]["lifecycle_valid"] is False
    assert manifest["integrity"]["lifecycle_error"] == (
        "Authoritative lifecycle snapshot is missing."
    )
    assert not lifecycle_path.exists()
