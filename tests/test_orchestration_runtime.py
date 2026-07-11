"""Durable step runtime: step sequencing, crash-resume, suspend/resume (HITL),
per-step retry with backoff, reducers, and span emission."""
import json

from charon.orchestration import runtime as rt
from charon.infra import orchestration_trace as ot


def _read_op_file(state_dir, op_id):
    p = state_dir / "orchestration" / "ops" / f"{op_id}.json"
    return json.loads(p.read_text())


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
    rt.resume(tmp_path, op["op_id"], "reinforcement learning")
    rt.tick_operation(tmp_path, op["op_id"])
    final = rt.get_operation(tmp_path, op["op_id"])
    assert final["status"] == "done"
    assert final["state"]["answer"] == "reinforcement learning"


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
