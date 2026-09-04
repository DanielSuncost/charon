from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event

from charon.conversation import conversation_runtime
from charon.infra.queue_io import update_queue_atomic


def _enqueue(state_dir, correlation_id: str):
    return conversation_runtime.enqueue_agent_task(
        state_dir,
        owner_agent_id="agent-1",
        instruction="Run the routed task",
        correlation_id=correlation_id,
    )


def test_concurrent_enqueue_is_exactly_once_per_correlation(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(conversation_runtime, "_HAS_STORE", False)

    with ThreadPoolExecutor(max_workers=12) as pool:
        tasks = list(
            pool.map(
                lambda _: _enqueue(tmp_path, "graph/run/node/visit-1"),
                range(48),
            )
        )

    queue = conversation_runtime.load_queue(tmp_path)
    assert len(queue) == 1
    assert {task["id"] for task in tasks} == {queue[0]["id"]}
    assert queue[0]["correlation_id"] == "graph/run/node/visit-1"


def test_stale_queue_save_preserves_a_concurrent_append(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(conversation_runtime, "_HAS_STORE", False)
    first = _enqueue(tmp_path, "first")
    stale = conversation_runtime.load_queue(tmp_path)

    second = _enqueue(tmp_path, "second")
    stale[0]["status"] = "completed"
    conversation_runtime.save_queue(tmp_path, stale)

    queue = conversation_runtime.load_queue(tmp_path)
    by_id = {task["id"]: task for task in queue}
    assert set(by_id) == {first["id"], second["id"]}
    assert by_id[first["id"]]["status"] == "completed"
    assert by_id[second["id"]]["status"] == "pending"


def test_locked_queue_update_cannot_erase_a_concurrent_append(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(conversation_runtime, "_HAS_STORE", False)
    parent = _enqueue(tmp_path, "parent")
    entered = Event()
    release = Event()

    def update_parent(queue):
        entered.set()
        assert release.wait(timeout=5)
        queue[0]["status"] = "pending"
        queue[0]["woken"] = True
        return True

    with ThreadPoolExecutor(max_workers=2) as pool:
        update_future = pool.submit(
            update_queue_atomic,
            tmp_path / "queue.json",
            update_parent,
        )
        assert entered.wait(timeout=5)
        append_future = pool.submit(_enqueue, tmp_path, "graph-append")
        release.set()
        update_future.result(timeout=5)
        appended = append_future.result(timeout=5)

    queue = conversation_runtime.load_queue(tmp_path)
    by_id = {task["id"]: task for task in queue}
    assert set(by_id) == {parent["id"], appended["id"]}
    assert by_id[parent["id"]]["woken"] is True
