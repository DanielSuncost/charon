"""Durable, resumable step runtime — Phase 2 of the orchestration-runtime plan.

The problem it solves: Charon's impressive multi-agent systems (Libris, devop,
batch) run their control loops inside ephemeral `threading.Thread(daemon=True)`
supervisors that die with the process and orphan the operation on crash. Only
the queue + shade-contract + judge-tick substrates are crash-resumable, and each
hand-rolls its own sequencing/retry/termination.

This generalizes the one pattern that already works (the judge-loop S3 tick:
persisted state re-driven one step per daemon heartbeat) into a reusable engine:

- An **operation** is a typed state document persisted after every step
  (`orchestration/ops/<op_id>.json`). Because state is durable and the driver is
  re-invoked from the daemon tick, a crash just means the next tick reloads from
  disk and continues — no orphaning.
- Control flow is expressed as named **steps**; a step function inspects state
  and returns a **directive** (goto / suspend / done / fail / stay). Steps are
  registered per operation `kind` so they survive a restart (functions aren't
  serializable; the state that selects them is).
- **State updates merge through reducers** (declared per key), so parallel/
  accumulated results fan in deterministically instead of every caller
  hand-recomputing aggregate status.
- **Interrupt/resume is first-class**: `suspend()` parks an operation awaiting
  external input; `resume()` feeds the input and the step re-runs with it. This
  fixes the Libris "clarification is a dead-end" pattern for real.
- **Per-step retry with backoff**: a step that raises is retried up to a cap
  with exponential backoff, then the operation fails.
- **Spans are emitted per step** into the unified trace substrate.

Durability contract (same as LangGraph's): a step may re-run after a crash
mid-step, so **step bodies must be replay-safe** (idempotent up to their
directive). The canonical way to run external work is a `spawn` step that fires
it then `goto` a `wait` step that polls with `stay()` — exactly how Libris/judge
already work, now durable and reusable.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from charon.infra import orchestration_trace as _ot

try:
    from charon.infra.diagnostics import record as _diag
except Exception:  # diagnostics is best-effort and must never block import
    def _diag(*_a, **_k):  # type: ignore
        return None


# ── directives a step returns ──────────────────────────────────────────────

@dataclass
class Directive:
    kind: str                              # goto | suspend | done | fail | stay
    next_step: str | None = None
    updates: dict = field(default_factory=dict)
    reason: str = ""
    resume_key: str | None = None
    error: str = ""
    delay_sec: float = 0.0


def goto(next_step: str, **updates) -> Directive:
    """Advance to `next_step`, merging `updates` into state (through reducers)."""
    return Directive("goto", next_step=next_step, updates=updates)


def stay(*, delay_sec: float = 0.0, **updates) -> Directive:
    """Re-run the current step on a later tick (polling / barriers). Optional
    state updates and a minimum delay before the next attempt."""
    return Directive("stay", updates=updates, delay_sec=delay_sec)


def suspend(reason: str, *, resume_key: str | None = None, **updates) -> Directive:
    """Park the operation awaiting external input (HITL / clarification). Resume
    with `resume(state_dir, op_id, payload)`; the step re-runs with
    `ctx.resume_payload` set."""
    return Directive("suspend", reason=reason, resume_key=resume_key, updates=updates)


def done(**updates) -> Directive:
    """Terminal success."""
    return Directive("done", updates=updates)


def fail(error: str, **updates) -> Directive:
    """Terminal failure (without exhausting retries — a deliberate stop)."""
    return Directive("fail", error=error, updates=updates)


# ── step context ───────────────────────────────────────────────────────────

@dataclass
class StepContext:
    op_id: str
    kind: str
    step: str
    state: dict
    attempt: int
    state_dir: Path
    resume_payload: Any = None      # set on the tick that follows a resume()
    trace_id: str = ""


StepFn = Callable[[StepContext], Directive]


# ── registry ───────────────────────────────────────────────────────────────

@dataclass
class _KindSpec:
    steps: dict[str, StepFn]
    entry: str
    reducers: dict[str, Callable[[Any, Any], Any]]
    max_attempts: int
    backoff_base: float


_REGISTRY: dict[str, _KindSpec] = {}


def register_kind(kind: str, *, steps: dict[str, StepFn], entry: str,
                  reducers: dict[str, Callable[[Any, Any], Any]] | None = None,
                  max_attempts: int = 3, backoff_base: float = 2.0) -> None:
    """Register an operation kind: its named step functions, the entry step, and
    optional per-state-key reducers (merge fns `(current, incoming) -> merged`;
    keys without a reducer are overwritten, i.e. last-value)."""
    if entry not in steps:
        raise ValueError(f"entry step {entry!r} not in steps for kind {kind!r}")
    _REGISTRY[kind] = _KindSpec(steps=dict(steps), entry=entry,
                                reducers=dict(reducers or {}),
                                max_attempts=max_attempts, backoff_base=backoff_base)


def registered_kinds() -> list[str]:
    return list(_REGISTRY)


# ── persistence ────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ops_dir(state_dir: Path) -> Path:
    d = Path(state_dir) / "orchestration" / "ops"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _op_path(state_dir: Path, op_id: str) -> Path:
    return _ops_dir(state_dir) / f"{op_id}.json"


def _read_op(state_dir: Path, op_id: str) -> dict | None:
    p = _op_path(state_dir, op_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_op(state_dir: Path, op: dict) -> None:
    op["updated_at"] = _now_iso()
    p = _op_path(state_dir, op["op_id"])
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(op, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)  # atomic swap — never a torn state file


# ── lifecycle ──────────────────────────────────────────────────────────────

def start_operation(state_dir: Path, kind: str, *, initial_state: dict | None = None,
                    op_id: str | None = None, title: str = "") -> dict:
    """Create a durable operation of `kind`, positioned at its entry step."""
    if kind not in _REGISTRY:
        raise ValueError(f"unknown operation kind {kind!r}; register_kind first")
    spec = _REGISTRY[kind]
    op_id = op_id or f"op_{uuid.uuid4().hex[:16]}"
    op = {
        "op_id": op_id, "kind": kind, "title": title,
        "status": "running",           # running | suspended | done | failed
        "cursor": spec.entry,
        "state": dict(initial_state or {}),
        "attempt": 0,
        "not_before": 0.0,
        "suspended_reason": "", "resume_key": None, "resume_payload": None,
        "result": None, "error": "",
        "trace_id": f"tr_{op_id}",
        "history": [],                  # [{step, directive, at, ok}]
        "created_at": _now_iso(), "updated_at": _now_iso(),
    }
    _write_op(state_dir, op)
    _ot.record_span(state_dir, name=f"{kind} start", system="orchestration",
                    kind="operation", trace_id=op["trace_id"], operation_id=op_id,
                    status="ok", attributes={"kind": kind})
    return op


def get_operation(state_dir: Path, op_id: str) -> dict | None:
    return _read_op(state_dir, op_id)


def resume(state_dir: Path, op_id: str, payload: Any) -> dict | None:
    """Feed input to a suspended operation and make it runnable again. The step
    that suspended re-runs with `ctx.resume_payload = payload`."""
    op = _read_op(state_dir, op_id)
    if not op or op.get("status") != "suspended":
        return None
    op["status"] = "running"
    op["resume_payload"] = payload
    op["attempt"] = 0
    op["not_before"] = 0.0
    _write_op(state_dir, op)
    return op


def request_stop(state_dir: Path, op_id: str, reason: str = "") -> dict | None:
    op = _read_op(state_dir, op_id)
    if not op:
        return None
    op["status"] = "failed"
    op["error"] = f"stopped: {reason}"[:300]
    _write_op(state_dir, op)
    return op


def _merge_state(state: dict, updates: dict, reducers: dict) -> None:
    for k, v in (updates or {}).items():
        if k in reducers and k in state:
            try:
                state[k] = reducers[k](state[k], v)
                continue
            except Exception:
                pass
        state[k] = v


# ── the driver ─────────────────────────────────────────────────────────────

def tick_operation(state_dir: Path, op_id: str) -> dict:
    """Advance ONE operation by a single step. Returns an event dict describing
    what happened. Crash-safe: state is persisted before and after the step, so a
    re-invocation after a crash resumes from the same cursor.

    A step that raises is retried (exponential backoff via `not_before`) up to the
    kind's `max_attempts`, then the operation fails.
    """
    op = _read_op(state_dir, op_id)
    if not op:
        return {"op_id": op_id, "action": "missing"}
    if op.get("status") not in ("running",):
        return {"op_id": op_id, "action": "skipped", "status": op.get("status")}
    if (op.get("not_before") or 0.0) > time.time():
        return {"op_id": op_id, "action": "deferred"}

    spec = _REGISTRY.get(op["kind"])
    if not spec:
        op["status"] = "failed"
        op["error"] = f"kind {op['kind']!r} not registered in this process"
        _write_op(state_dir, op)
        return {"op_id": op_id, "action": "failed", "error": op["error"]}

    step_name = op["cursor"]
    step_fn = spec.steps.get(step_name)
    if not step_fn:
        op["status"] = "failed"
        op["error"] = f"step {step_name!r} not found for kind {op['kind']!r}"
        _write_op(state_dir, op)
        return {"op_id": op_id, "action": "failed", "error": op["error"]}

    ctx = StepContext(
        op_id=op_id, kind=op["kind"], step=step_name, state=dict(op["state"]),
        attempt=int(op.get("attempt") or 0), state_dir=Path(state_dir),
        resume_payload=op.get("resume_payload"), trace_id=op["trace_id"],
    )

    directive: Directive
    with _ot.span(state_dir, name=f"{op['kind']}:{step_name}", system="orchestration",
                  kind="step", trace_id=op["trace_id"], operation_id=op_id,
                  task_id=step_name, attributes={"attempt": ctx.attempt}) as sp:
        try:
            directive = step_fn(ctx)
        except Exception as e:  # noqa: BLE001 — retry/backoff, never crash the daemon
            sp.status = "error"
            attempt = int(op.get("attempt") or 0) + 1
            op["attempt"] = attempt
            if attempt >= spec.max_attempts:
                op["status"] = "failed"
                op["error"] = f"{type(e).__name__}: {e}"[:300]
                op["history"].append({"step": step_name, "directive": "error",
                                      "at": _now_iso(), "ok": False})
                _write_op(state_dir, op)
                _diag("orchestration", f"operation {op_id} step {step_name} failed after retries", error=e)
                return {"op_id": op_id, "action": "failed", "step": step_name,
                        "error": op["error"]}
            backoff = spec.backoff_base ** attempt
            op["not_before"] = time.time() + backoff
            _write_op(state_dir, op)
            return {"op_id": op_id, "action": "retry", "step": step_name,
                    "attempt": attempt, "backoff_sec": round(backoff, 2)}

    # step succeeded — the resume payload is single-use, clear it
    op["resume_payload"] = None
    op["attempt"] = 0
    op["not_before"] = 0.0
    _merge_state(op["state"], directive.updates, spec.reducers)
    op["history"].append({"step": step_name, "directive": directive.kind,
                          "at": _now_iso(), "ok": True})

    if directive.kind == "goto":
        op["cursor"] = directive.next_step
        action = "goto"
    elif directive.kind == "stay":
        if directive.delay_sec:
            op["not_before"] = time.time() + directive.delay_sec
        action = "stay"
    elif directive.kind == "suspend":
        op["status"] = "suspended"
        op["suspended_reason"] = directive.reason
        op["resume_key"] = directive.resume_key
        action = "suspend"
    elif directive.kind == "done":
        op["status"] = "done"
        op["result"] = op["state"]
        action = "done"
    elif directive.kind == "fail":
        op["status"] = "failed"
        op["error"] = directive.error
        action = "fail"
    else:
        op["status"] = "failed"
        op["error"] = f"unknown directive {directive.kind!r}"
        action = "fail"

    _write_op(state_dir, op)
    return {"op_id": op_id, "action": action, "step": step_name,
            "cursor": op["cursor"], "status": op["status"]}


def list_runnable(state_dir: Path) -> list[str]:
    """Op ids that are running and not deferred — candidates for the next tick."""
    out = []
    d = _ops_dir(state_dir)
    now = time.time()
    for p in sorted(d.glob("op_*.json")):
        try:
            op = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if op.get("status") == "running" and (op.get("not_before") or 0.0) <= now:
            out.append(op["op_id"])
    return out


def tick_operations(state_dir: Path, *, max_ops: int = 8) -> list[dict]:
    """Advance up to `max_ops` runnable operations by one step each. This is the
    single call the daemon heartbeat makes to drive ALL durable operations — the
    generalization of `tick_judge_loops`. No-op (returns []) when nothing is
    registered or runnable, so wiring it into the loop is safe."""
    events = []
    for op_id in list_runnable(state_dir)[:max_ops]:
        try:
            events.append(tick_operation(state_dir, op_id))
        except Exception as e:  # a bad op must never halt the daemon
            _diag("orchestration", f"tick_operation({op_id}) raised", error=e)
    return events


__all__ = [
    "Directive", "goto", "stay", "suspend", "done", "fail",
    "StepContext", "StepFn", "register_kind", "registered_kinds",
    "start_operation", "get_operation", "resume", "request_stop",
    "tick_operation", "tick_operations", "list_runnable",
]
