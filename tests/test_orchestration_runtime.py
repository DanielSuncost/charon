"""Durable step runtime: step sequencing, crash-resume, suspend/resume (HITL),
per-step retry with backoff, reducers, and span emission."""
import json
import multiprocessing
import os
import queue
import threading

import pytest

from charon.orchestration import runtime as rt
from charon.infra import orchestration_trace as ot


def _read_op_file(state_dir, op_id):
    p = state_dir / "orchestration" / "ops" / f"{op_id}.json"
    return json.loads(p.read_text())


def _cross_process_tick_worker(
    state_dir,
    entered,
    release,
    invocations,
    results,
):
    """Register the persisted kind in a fresh scheduler process and tick it."""
    from charon.orchestration import runtime as worker_rt

    def once(_ctx):
        invocations.put(os.getpid())
        entered.set()
        if not release.wait(10):
            raise TimeoutError("test did not release the durable step")
        return worker_rt.done(pid=os.getpid())

    worker_rt.register_kind(
        "cross_process_lock_test",
        steps={"once": once},
        entry="once",
    )
    try:
        event = worker_rt.tick_operation(state_dir, "op_cross_process_lock")
        results.put({"event": event})
    except Exception as exc:  # pragma: no cover - failure is asserted in parent
        results.put({"error": f"{type(exc).__name__}: {exc}"})


def test_sequence_runs_each_step_once_and_completes(tmp_path):
    def s_a(ctx): return rt.goto("b", trail=ctx.state.get("trail", []) + ["a"])
    def s_b(ctx): return rt.goto("c", trail=ctx.state["trail"] + ["b"])
    def s_c(ctx): return rt.done(trail=ctx.state["trail"] + ["c"])
    rt.register_kind("seq_test", steps={"a": s_a, "b": s_b, "c": s_c}, entry="a")

    op = rt.start_operation(tmp_path, "seq_test")
    for _ in range(5):
        ev = rt.tick_operation(tmp_path, op["op_id"])
        if ev["status"] in ("done", "failed"):
            break
    final = rt.get_operation(tmp_path, op["op_id"])
    assert final["status"] == "done"
    assert final["state"]["trail"] == ["a", "b", "c"]  # each step ran exactly once


def test_crash_resume_continues_from_persisted_cursor(tmp_path):
    # Each step appends to durable state; between ticks nothing is held in memory,
    # so re-invoking the driver (a "restart") must continue from the on-disk cursor.
    def s1(ctx): return rt.goto("s2", steps_run=ctx.state.get("steps_run", 0) + 1)
    def s2(ctx): return rt.goto("s3", steps_run=ctx.state["steps_run"] + 1)
    def s3(ctx): return rt.done(steps_run=ctx.state["steps_run"] + 1)
    rt.register_kind("crash_test", steps={"s1": s1, "s2": s2, "s3": s3}, entry="s1")

    op = rt.start_operation(tmp_path, "crash_test")
    rt.tick_operation(tmp_path, op["op_id"])                 # s1 -> s2
    mid = _read_op_file(tmp_path, op["op_id"])
    assert mid["cursor"] == "s2" and mid["state"]["steps_run"] == 1
    # "restart": no in-memory state; driver reloads from disk and continues
    rt.tick_operation(tmp_path, op["op_id"])                 # s2 -> s3
    rt.tick_operation(tmp_path, op["op_id"])                 # s3 -> done
    final = rt.get_operation(tmp_path, op["op_id"])
    assert final["status"] == "done" and final["state"]["steps_run"] == 3


def test_suspend_and_resume_hitl(tmp_path):
    # The Libris "clarification is a dead-end" fix: a step suspends awaiting input,
    # resume() feeds it, and the step re-runs with the payload and proceeds.
    def ask(ctx):
        if ctx.resume_payload is None:
            return rt.suspend("need a topic", resume_key="clarify-1")
        return rt.done(answer=ctx.resume_payload)
    rt.register_kind("hitl_test", steps={"ask": ask}, entry="ask")

    op = rt.start_operation(tmp_path, "hitl_test")
    ev = rt.tick_operation(tmp_path, op["op_id"])
    assert ev["action"] == "suspend"
    assert rt.get_operation(tmp_path, op["op_id"])["status"] == "suspended"
    # ticking a suspended op does nothing
    assert rt.tick_operation(tmp_path, op["op_id"])["action"] == "skipped"
    # resume with the human's answer
    resumed = rt.resume(tmp_path, op["op_id"], "reinforcement learning")
    assert resumed["suspended_reason"] == ""
    assert resumed["resume_key"] is None
    rt.tick_operation(tmp_path, op["op_id"])
    final = rt.get_operation(tmp_path, op["op_id"])
    assert final["status"] == "done"
    assert final["state"]["answer"] == "reinforcement learning"


def test_start_operation_can_persist_suspended_state_atomically(tmp_path):
    rt.register_kind(
        "initial_suspend_test",
        steps={"ask": lambda _ctx: rt.done()},
        entry="ask",
    )
    op = rt.start_operation(
        tmp_path,
        "initial_suspend_test",
        suspended=True,
        suspended_reason="waiting for input",
        resume_key="clarify-1",
    )

    persisted = rt.get_operation(tmp_path, op["op_id"])
    assert persisted["status"] == "suspended"
    assert persisted["suspended_reason"] == "waiting for input"
    assert persisted["resume_key"] == "clarify-1"
    assert persisted["history"] == []


def test_explicit_operation_id_is_create_only_by_default(tmp_path):
    rt.register_kind(
        "create_only_test",
        steps={"once": lambda _ctx: rt.done()},
        entry="once",
    )
    original = rt.start_operation(
        tmp_path,
        "create_only_test",
        op_id="op_create_only",
        initial_state={"operation_id": "domain-1"},
    )

    with pytest.raises(ValueError, match="already exists"):
        rt.start_operation(
            tmp_path,
            "create_only_test",
            op_id="op_create_only",
            initial_state={"operation_id": "domain-2"},
        )

    assert rt.get_operation(tmp_path, "op_create_only") == original


def test_idempotent_start_validates_canonical_launch_identity(tmp_path):
    rt.register_kind(
        "identity_test",
        steps={"once": lambda _ctx: rt.done()},
        entry="once",
    )
    identity = {"operation_id": "domain-1", "project_root": "/project/a"}
    original = rt.start_operation(
        tmp_path,
        "identity_test",
        op_id="op_identity",
        initial_state={**identity, "mutable": "initial"},
    )
    assert rt.tick_operation(tmp_path, original["op_id"])["action"] == "done"

    reused = rt.start_operation(
        tmp_path,
        "identity_test",
        op_id="op_identity",
        initial_state={**identity, "mutable": "would reset"},
        idempotent=True,
        idempotency_identity=identity,
    )
    assert reused["status"] == "done"
    assert reused["state"]["mutable"] == "initial"

    with pytest.raises(ValueError, match="identity does not match"):
        rt.start_operation(
            tmp_path,
            "identity_test",
            op_id="op_identity",
            initial_state={"operation_id": "domain-1", "project_root": "/project/b"},
            idempotent=True,
            idempotency_identity={
                "operation_id": "domain-1",
                "project_root": "/project/b",
            },
        )

    rt.register_kind(
        "identity_collision_test",
        steps={"once": lambda _ctx: rt.done()},
        entry="once",
    )
    with pytest.raises(ValueError, match="already exists with kind"):
        rt.start_operation(
            tmp_path,
            "identity_collision_test",
            op_id="op_identity",
            initial_state=identity,
            idempotent=True,
            idempotency_identity=identity,
        )


def test_retry_with_backoff_then_succeeds(tmp_path):
    def flaky(ctx):
        if ctx.attempt < 1:            # fail the first attempt
            raise RuntimeError("transient")
        return rt.done(ok=True)
    rt.register_kind("retry_test", steps={"flaky": flaky}, entry="flaky",
                     max_attempts=3, backoff_base=2.0)

    op = rt.start_operation(tmp_path, "retry_test")
    ev = rt.tick_operation(tmp_path, op["op_id"])
    assert ev["action"] == "retry" and ev["attempt"] == 1
    on_disk = _read_op_file(tmp_path, op["op_id"])
    assert on_disk["not_before"] > 0                       # backoff scheduled
    on_disk["not_before"] = 0.0                            # simulate time passing
    rt._write_op(tmp_path, on_disk)
    ev2 = rt.tick_operation(tmp_path, op["op_id"])
    assert ev2["action"] == "done"


def test_retry_exhausts_to_failed(tmp_path):
    def always_bad(ctx):
        raise ValueError("nope")
    rt.register_kind("fail_test", steps={"x": always_bad}, entry="x",
                     max_attempts=2, backoff_base=1.0)
    op = rt.start_operation(tmp_path, "fail_test")
    for _ in range(5):
        o = _read_op_file(tmp_path, op["op_id"])
        o["not_before"] = 0.0
        rt._write_op(tmp_path, o)
        ev = rt.tick_operation(tmp_path, op["op_id"])
        if ev["action"] in ("failed",):
            break
    final = rt.get_operation(tmp_path, op["op_id"])
    assert final["status"] == "failed" and "nope" in final["error"]


def test_reducer_accumulates_fan_in(tmp_path):
    # A reducer merges state updates instead of overwriting — deterministic fan-in.
    def collect(ctx):
        n = ctx.state.get("count", 0)
        if n >= 3:
            return rt.done()
        return rt.stay(results=[f"r{n}"], count=n + 1)   # stay + accumulate
    rt.register_kind("reduce_test", steps={"collect": collect}, entry="collect",
                     reducers={"results": lambda cur, inc: (cur or []) + (inc or [])})
    op = rt.start_operation(tmp_path, "reduce_test", initial_state={"results": []})
    for _ in range(6):
        if rt.tick_operation(tmp_path, op["op_id"])["status"] == "done":
            break
    final = rt.get_operation(tmp_path, op["op_id"])
    assert final["state"]["results"] == ["r0", "r1", "r2"]  # accumulated, not overwritten


def test_steps_emit_spans_into_trace(tmp_path):
    def a(ctx): return rt.goto("b")
    def b(ctx): return rt.done()
    rt.register_kind("span_test", steps={"a": a, "b": b}, entry="a")
    op = rt.start_operation(tmp_path, "span_test")
    rt.tick_operation(tmp_path, op["op_id"])
    rt.tick_operation(tmp_path, op["op_id"])
    spans = ot.read_spans(tmp_path, trace_id=op["trace_id"])
    kinds = {s["kind"] for s in spans}
    assert "operation" in kinds and "step" in kinds        # start + per-step spans
    step_names = {s["name"] for s in spans if s["kind"] == "step"}
    assert step_names == {"span_test:a", "span_test:b"}


def test_tick_operations_drives_many_and_noops_when_idle(tmp_path):
    assert rt.tick_operations(tmp_path) == []               # nothing registered/runnable
    def one(ctx): return rt.done()
    rt.register_kind("multi_test", steps={"one": one}, entry="one")
    ids = [rt.start_operation(tmp_path, "multi_test")["op_id"] for _ in range(3)]
    evs = rt.tick_operations(tmp_path)
    assert {e["op_id"] for e in evs} == set(ids)
    assert all(rt.get_operation(tmp_path, i)["status"] == "done" for i in ids)
    assert rt.tick_operations(tmp_path) == []               # all done -> idle again


def test_concurrent_thread_ticks_execute_step_once(tmp_path):
    entered = threading.Event()
    release = threading.Event()
    duplicate_entered = threading.Event()
    second_started = threading.Event()
    calls = []
    results = []
    errors = []
    calls_lock = threading.Lock()

    def once(_ctx):
        with calls_lock:
            calls.append(threading.get_ident())
            if len(calls) > 1:
                duplicate_entered.set()
        entered.set()
        if not release.wait(5):
            raise TimeoutError("test did not release the durable step")
        return rt.done()

    rt.register_kind("thread_lock_test", steps={"once": once}, entry="once")
    op = rt.start_operation(tmp_path, "thread_lock_test")

    def tick(*, announce=False):
        if announce:
            second_started.set()
        try:
            results.append(rt.tick_operation(tmp_path, op["op_id"]))
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    first = threading.Thread(target=tick)
    second = threading.Thread(target=tick, kwargs={"announce": True})
    first.start()
    try:
        assert entered.wait(2)
        second.start()
        assert second_started.wait(2)
        assert not duplicate_entered.wait(0.4)
    finally:
        release.set()
        first.join(timeout=5)
        if second.ident is not None:
            second.join(timeout=5)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert len(calls) == 1
    assert {event["action"] for event in results} == {"done", "skipped"}
    assert len(rt.get_operation(tmp_path, op["op_id"])["history"]) == 1


def test_concurrent_process_ticks_execute_step_once(tmp_path):
    rt.register_kind(
        "cross_process_lock_test",
        steps={"once": lambda _ctx: rt.done()},
        entry="once",
    )
    rt.start_operation(
        tmp_path,
        "cross_process_lock_test",
        op_id="op_cross_process_lock",
    )

    context = multiprocessing.get_context("spawn")
    entered = context.Event()
    release = context.Event()
    invocations = context.Queue()
    results = context.Queue()
    args = (str(tmp_path), entered, release, invocations, results)
    first = context.Process(target=_cross_process_tick_worker, args=args)
    second = context.Process(target=_cross_process_tick_worker, args=args)

    first.start()
    try:
        assert entered.wait(5)
        assert invocations.get(timeout=2) == first.pid
        second.start()
        with pytest.raises(queue.Empty):
            invocations.get(timeout=0.6)
    finally:
        release.set()
        first.join(timeout=10)
        if second.pid is not None:
            second.join(timeout=10)
        for process in (first, second):
            if process.is_alive():
                process.terminate()
                process.join(timeout=2)

    assert first.exitcode == 0
    assert second.exitcode == 0
    child_results = [results.get(timeout=2), results.get(timeout=2)]
    assert all("error" not in result for result in child_results)
    assert {
        result["event"]["action"] for result in child_results
    } == {"done", "skipped"}
    final = rt.get_operation(tmp_path, "op_cross_process_lock")
    assert final["status"] == "done"
    assert len(final["history"]) == 1


def test_resume_and_stop_share_operation_lock(tmp_path):
    def ask(_ctx):
        return rt.suspend("need input")

    rt.register_kind("lifecycle_lock_test", steps={"ask": ask}, entry="ask")
    op = rt.start_operation(tmp_path, "lifecycle_lock_test")
    assert rt.tick_operation(tmp_path, op["op_id"])["action"] == "suspend"

    started = [threading.Event(), threading.Event()]
    finished = [threading.Event(), threading.Event()]
    results = [None, None]

    def do_resume():
        started[0].set()
        results[0] = rt.resume(tmp_path, op["op_id"], "answer")
        finished[0].set()

    def do_stop():
        started[1].set()
        results[1] = rt.request_stop(tmp_path, op["op_id"], "test stop")
        finished[1].set()

    workers = [threading.Thread(target=do_resume), threading.Thread(target=do_stop)]
    with rt._locked_op(tmp_path, op["op_id"]):
        for worker in workers:
            worker.start()
        assert all(event.wait(2) for event in started)
        assert not any(event.wait(0.2) for event in finished)

    for worker in workers:
        worker.join(timeout=5)
        assert not worker.is_alive()

    assert results[1] is not None
    final = rt.get_operation(tmp_path, op["op_id"])
    assert final["status"] == "failed"
    assert final["error"] == "stopped: test stop"


def test_concurrent_resumes_accept_one_payload(tmp_path):
    rt.register_kind(
        "concurrent_resume_test",
        steps={"ask": lambda _ctx: rt.suspend("need input")},
        entry="ask",
    )
    op = rt.start_operation(tmp_path, "concurrent_resume_test")
    assert rt.tick_operation(tmp_path, op["op_id"])["action"] == "suspend"

    started = [threading.Event(), threading.Event()]
    finished = [threading.Event(), threading.Event()]
    results = [None, None]

    def resume(index, payload):
        started[index].set()
        results[index] = rt.resume(tmp_path, op["op_id"], payload)
        finished[index].set()

    workers = [
        threading.Thread(target=resume, args=(0, "first")),
        threading.Thread(target=resume, args=(1, "second")),
    ]
    with rt._locked_op(tmp_path, op["op_id"]):
        for worker in workers:
            worker.start()
        assert all(event.wait(2) for event in started)
        assert not any(event.wait(0.2) for event in finished)

    for worker in workers:
        worker.join(timeout=5)
        assert not worker.is_alive()

    accepted = [result for result in results if result is not None]
    assert len(accepted) == 1
    final = rt.get_operation(tmp_path, op["op_id"])
    assert final["status"] == "running"
    assert final["resume_payload"] in {"first", "second"}
    assert final["resume_payload"] == accepted[0]["resume_payload"]


def test_stop_cannot_be_overwritten_by_in_flight_tick(tmp_path):
    entered = threading.Event()
    release = threading.Event()
    stop_started = threading.Event()
    stop_finished = threading.Event()

    def once(_ctx):
        entered.set()
        if not release.wait(5):
            raise TimeoutError("test did not release the durable step")
        return rt.done()

    rt.register_kind("stop_tick_lock_test", steps={"once": once}, entry="once")
    op = rt.start_operation(tmp_path, "stop_tick_lock_test")
    tick_result = []
    stop_result = []

    ticker = threading.Thread(
        target=lambda: tick_result.append(rt.tick_operation(tmp_path, op["op_id"])),
    )

    def stop():
        stop_started.set()
        stop_result.append(rt.request_stop(tmp_path, op["op_id"], "cancelled"))
        stop_finished.set()

    stopper = threading.Thread(target=stop)
    ticker.start()
    try:
        assert entered.wait(2)
        stopper.start()
        assert stop_started.wait(2)
        assert not stop_finished.wait(0.3)
    finally:
        release.set()
        ticker.join(timeout=5)
        if stopper.ident is not None:
            stopper.join(timeout=5)

    assert not ticker.is_alive()
    assert not stopper.is_alive()
    assert tick_result[0]["action"] == "done"
    assert stop_result[0]["status"] == "failed"
    final = rt.get_operation(tmp_path, op["op_id"])
    assert final["status"] == "failed"
    assert final["error"] == "stopped: cancelled"


def test_compatibility_writer_shares_operation_lock(tmp_path):
    rt.register_kind(
        "compatibility_writer_lock_test",
        steps={"once": lambda _ctx: rt.done()},
        entry="once",
    )
    op = rt.start_operation(tmp_path, "compatibility_writer_lock_test")
    op["title"] = "updated through compatibility writer"
    started = threading.Event()
    finished = threading.Event()

    def write():
        started.set()
        rt._write_op(tmp_path, op)
        finished.set()

    worker = threading.Thread(target=write)
    with rt._locked_op(tmp_path, op["op_id"]):
        worker.start()
        assert started.wait(2)
        assert not finished.wait(0.2)
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert rt.get_operation(tmp_path, op["op_id"])["title"] == op["title"]
    ops_dir = tmp_path / "orchestration" / "ops"
    assert not [path for path in ops_dir.iterdir() if path.suffix == ".tmp"]
