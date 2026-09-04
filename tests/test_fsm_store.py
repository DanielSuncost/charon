"""Durable FSM store: locking, commit/outbox recovery, and replay."""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from charon.orchestration.fsm import (
    MachineSpec,
    RevisionConflict,
    TransitionSpec,
)
from charon.orchestration.fsm_store import DurableMachineStore


def _spec() -> MachineSpec:
    return MachineSpec(
        name="durable_test",
        states={"ready", "running", "done"},
        initial_state="ready",
        terminal_states={"done"},
        transitions=[
            TransitionSpec("start", "ready", "running"),
            TransitionSpec("finish", "running", "done"),
        ],
    )


def test_store_commits_snapshot_and_event_projection(tmp_path):
    store = DurableMachineStore(tmp_path, _spec())
    store.create("work-1", data={"input": 7})

    result = store.dispatch(
        "work-1",
        "start",
        event_id="request-1",
        expected_revision=0,
    )

    assert result.instance.state == "running"
    assert result.instance.revision == 1
    assert result.instance.event_outbox == ()
    events = store.events("work-1")
    assert [event["event_id"] for event in events] == ["request-1"]
    assert events[0]["source"] == "ready"
    assert events[0]["target"] == "running"
    snapshot_path = (
        tmp_path
        / "orchestration"
        / "machines"
        / "durable_test"
        / "work-1"
        / "instance.json"
    )
    raw = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert raw["state"] == "running"
    assert raw["event_outbox"] == []


def test_committed_outbox_recovers_after_publish_crash(tmp_path, monkeypatch):
    store = DurableMachineStore(tmp_path, _spec())
    store.create("work-1")
    original_flush = store._flush_outbox
    crashed = False

    def crash_after_snapshot(instance):
        nonlocal crashed
        if instance.event_outbox and not crashed:
            crashed = True
            raise RuntimeError("simulated publish crash")
        return original_flush(instance)

    monkeypatch.setattr(store, "_flush_outbox", crash_after_snapshot)
    with pytest.raises(RuntimeError, match="simulated publish crash"):
        store.dispatch("work-1", "start", event_id="request-1")

    snapshot = json.loads(
        (
            tmp_path
            / "orchestration"
            / "machines"
            / "durable_test"
            / "work-1"
            / "instance.json"
        ).read_text(encoding="utf-8")
    )
    assert snapshot["state"] == "running"
    assert [event["event_id"] for event in snapshot["event_outbox"]] == [
        "request-1"
    ]

    monkeypatch.setattr(store, "_flush_outbox", original_flush)
    recovered = store.get("work-1")
    assert recovered.state == "running"
    assert recovered.event_outbox == ()
    assert [event["event_id"] for event in store.events("work-1")] == [
        "request-1"
    ]
    replay = store.dispatch("work-1", "start", event_id="request-1")
    assert replay.duplicate is True
    assert len(store.events("work-1")) == 1


def test_store_lock_and_revision_allow_only_one_stale_writer(tmp_path):
    store = DurableMachineStore(tmp_path, _spec())
    store.create("work-1")

    def attempt(index):
        try:
            return store.dispatch(
                "work-1",
                "start",
                event_id=f"request-{index}",
                expected_revision=0,
            )
        except RevisionConflict as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, [1, 2]))

    assert sum(not isinstance(result, Exception) for result in results) == 1
    assert sum(isinstance(result, RevisionConflict) for result in results) == 1
    assert store.get("work-1").revision == 1
    assert len(store.events("work-1")) == 1


def test_partial_event_tail_is_repaired_during_recovery(tmp_path):
    store = DurableMachineStore(tmp_path, _spec())
    store.create("work-1")
    store.dispatch("work-1", "start", event_id="request-1")
    path = (
        tmp_path
        / "orchestration"
        / "machines"
        / "durable_test"
        / "work-1"
        / "events.jsonl"
    )
    with path.open("ab") as handle:
        handle.write(b'{"event_id":"partial"')

    recovered = store.get("work-1")

    assert recovered.state == "running"
    assert [event["event_id"] for event in store.events("work-1")] == [
        "request-1"
    ]
    assert path.read_bytes().endswith(b"\n")


def test_store_never_overwrites_existing_instance(tmp_path):
    store = DurableMachineStore(tmp_path, _spec())
    store.create("work-1")
    with pytest.raises(ValueError, match="already exists"):
        store.create("work-1")
