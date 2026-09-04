"""Guarded FSM kernel: legal transitions, replay, and durable metadata."""
from __future__ import annotations

import pytest

from charon.orchestration.fsm import (
    EventCollision,
    InvariantViolation,
    MachineDefinitionError,
    MachineInstance,
    MachineSpec,
    RevisionConflict,
    TransitionRejected,
    TransitionSpec,
    acknowledge_events,
    create_instance,
    dispatch,
    project_machine,
    validate_instance,
)


def _delivery_spec() -> MachineSpec:
    def has_artifact(ctx):
        if ctx.target == "completed" and not ctx.data.get("artifact"):
            return "completed delivery requires an artifact"
        return True

    return MachineSpec(
        name="delivery",
        states={"assembling", "verifying", "completed", "failed"},
        initial_state="assembling",
        terminal_states={"completed", "failed"},
        transitions=[
            TransitionSpec(
                "verify",
                "assembling",
                "verifying",
                guard=lambda ctx: bool(ctx.payload.get("candidate")),
                reducer=lambda ctx: {"artifact": ctx.payload["candidate"]},
            ),
            TransitionSpec("complete", "verifying", "completed"),
            TransitionSpec(
                "fail",
                {"assembling", "verifying"},
                "failed",
            ),
        ],
        invariants=[has_artifact],
    )


def test_dispatch_applies_guard_reducer_invariant_and_revision():
    spec = _delivery_spec()
    original = create_instance(spec, "delivery-1", now="2026-01-01T00:00:00+00:00")

    result = dispatch(
        spec,
        original,
        "verify",
        event_id="request-1",
        payload={"candidate": "report.html"},
        expected_revision=0,
        now="2026-01-01T00:00:01+00:00",
    )

    assert original.state == "assembling"
    assert original.revision == 0
    assert result.instance.state == "verifying"
    assert result.instance.data == {"artifact": "report.html"}
    assert result.instance.revision == 1
    assert result.instance.event_seq == 1
    assert result.event == result.instance.event_outbox[0]
    assert result.event["source"] == "assembling"
    assert result.event["target"] == "verifying"
    assert result.event["revision"] == 1

    completed = dispatch(
        spec,
        result.instance,
        "complete",
        event_id="request-2",
        expected_revision=1,
    ).instance
    assert completed.state == "completed"
    assert completed.revision == 2


def test_illegal_transition_and_guard_rejection_do_not_mutate_instance():
    spec = _delivery_spec()
    instance = create_instance(spec, "delivery-1")

    with pytest.raises(TransitionRejected, match="not legal"):
        dispatch(spec, instance, "complete", event_id="bad-order")
    with pytest.raises(TransitionRejected, match="guard"):
        dispatch(
            spec,
            instance,
            "verify",
            event_id="missing-artifact",
            payload={},
        )

    assert instance.state == "assembling"
    assert instance.revision == 0
    assert instance.processed_events == {}
    assert instance.event_outbox == ()


def test_post_transition_invariant_rejects_candidate_atomically():
    spec = MachineSpec(
        name="certificate",
        states={"verifying", "completed"},
        initial_state="verifying",
        terminal_states={"completed"},
        transitions=[TransitionSpec("complete", "verifying", "completed")],
        invariants=[
            lambda ctx: (
                "completion certificate missing"
                if ctx.target == "completed" and not ctx.data.get("certificate")
                else True
            )
        ],
    )
    instance = create_instance(spec, "certificate-1")

    with pytest.raises(InvariantViolation, match="certificate missing"):
        dispatch(spec, instance, "complete", event_id="complete-1")

    assert instance.state == "verifying"
    assert instance.revision == 0


def test_expected_revision_prevents_stale_unique_event():
    spec = _delivery_spec()
    instance = create_instance(spec, "delivery-1")
    advanced = dispatch(
        spec,
        instance,
        "verify",
        event_id="request-1",
        payload={"candidate": "report.html"},
    ).instance

    with pytest.raises(RevisionConflict, match="expected revision 0, found 1"):
        dispatch(
            spec,
            advanced,
            "complete",
            event_id="request-2",
            expected_revision=0,
        )


def test_event_replay_is_idempotent_even_with_stale_expected_revision():
    spec = _delivery_spec()
    instance = create_instance(spec, "delivery-1")
    first = dispatch(
        spec,
        instance,
        "verify",
        event_id="stable-request-id",
        payload={"candidate": "report.html"},
        expected_revision=0,
    )

    replay = dispatch(
        spec,
        first.instance,
        "verify",
        event_id="stable-request-id",
        payload={"candidate": "report.html"},
        expected_revision=0,
    )

    assert replay.duplicate is True
    assert replay.instance is first.instance
    assert replay.event == first.event
    assert replay.instance.revision == 1
    assert len(replay.instance.event_outbox) == 1


def test_reused_event_id_with_different_content_is_a_collision():
    spec = _delivery_spec()
    first = dispatch(
        spec,
        create_instance(spec, "delivery-1"),
        "verify",
        event_id="stable-request-id",
        payload={"candidate": "report.html"},
    )

    with pytest.raises(EventCollision, match="different content"):
        dispatch(
            spec,
            first.instance,
            "verify",
            event_id="stable-request-id",
            payload={"candidate": "other.html"},
        )


def test_instance_round_trip_and_outbox_acknowledgement():
    spec = _delivery_spec()
    transitioned = dispatch(
        spec,
        create_instance(spec, "delivery-1"),
        "verify",
        event_id="request-1",
        payload={"candidate": "report.html"},
    ).instance

    restored = MachineInstance.from_dict(transitioned.to_dict())
    validate_instance(spec, restored)
    acknowledged = acknowledge_events(restored, ["request-1"])

    assert acknowledged.event_outbox == ()
    assert acknowledged.revision == transitioned.revision
    assert acknowledged.processed_events == transitioned.processed_events
    replay = dispatch(
        spec,
        acknowledged,
        "verify",
        event_id="request-1",
        payload={"candidate": "report.html"},
    )
    assert replay.duplicate is True
    assert replay.instance.event_outbox == ()


def test_definition_rejects_ambiguity_and_terminal_outgoing_edges():
    duplicate = TransitionSpec("go", "ready", "running")
    with pytest.raises(MachineDefinitionError, match="ambiguous transition"):
        MachineSpec(
            name="ambiguous",
            states={"ready", "running"},
            initial_state="ready",
            transitions=[duplicate, duplicate],
        )
    with pytest.raises(MachineDefinitionError, match="terminal states cannot"):
        MachineSpec(
            name="terminal_loop",
            states={"ready", "done"},
            initial_state="ready",
            terminal_states={"done"},
            transitions=[TransitionSpec("again", "done", "ready")],
        )


def test_corrupted_restored_state_is_rejected():
    spec = _delivery_spec()
    instance = MachineInstance(
        machine="delivery",
        machine_version=1,
        instance_id="delivery-1",
        state="unknown",
    )
    with pytest.raises(InvariantViolation, match="unknown persisted state"):
        validate_instance(spec, instance)


def test_machine_projection_is_stable_and_presentation_neutral():
    spec = _delivery_spec()
    instance = dispatch(
        spec,
        create_instance(spec, "delivery-1"),
        "verify",
        event_id="request-1",
        payload={"candidate": "report.html"},
    ).instance

    projection = project_machine(spec, instance)

    assert projection["machine"] == "delivery"
    assert projection["current_state"] == "verifying"
    assert projection["revision"] == 1
    assert projection["allowed_events"] == ["complete", "fail"]
    assert next(
        node for node in projection["nodes"] if node["id"] == "verifying"
    )["current"] is True
    verify_edge = next(
        edge for edge in projection["edges"] if edge["event"] == "verify"
    )
    assert verify_edge == {
        "id": "assembling:verify:verifying",
        "from": "assembling",
        "to": "verifying",
        "event": "verify",
        "label": "verify",
        "guarded": True,
        "reduces_data": True,
        "metadata": {},
    }
    assert "data" not in projection
    assert "processed_events" not in projection
