"""Durable executor for declarative orchestration graphs.

The existing step runtime remains independent.  This module persists one
immutable definition snapshot and one atomically replaced run document per
run, with an append-only event stream beside them.
"""
from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from charon.orchestration.graph_schema import (
    EdgeSpec,
    GraphDefinition,
    GraphValidationError,
    NodeSpec,
)
from charon.orchestration.fsm import (
    DispatchResult as MachineDispatchResult,
    MachineInstance,
    MachineSpec,
    TransitionSpec,
    acknowledge_events as acknowledge_machine_events,
    create_instance as create_machine_instance,
    dispatch as dispatch_machine,
    validate_instance as validate_machine_instance,
)


_RUN_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")
_TERMINAL = frozenset({"completed", "failed", "stopped"})
_EXECUTORS: dict[str, ExecutorFn] = {}
_EXECUTOR_LOCK = threading.RLock()
_LOCAL_LOCKS: dict[str, threading.RLock] = {}
_LOCAL_LOCKS_GUARD = threading.Lock()


_GRAPH_RUN_MACHINE = MachineSpec(
    name="graph_run",
    states={"running", "suspended", "completed", "failed", "stopped"},
    initial_state="running",
    terminal_states={"completed", "failed", "stopped"},
    transitions=[
        TransitionSpec("run_suspended", "running", "suspended"),
        TransitionSpec("run_resumed", "suspended", "running"),
        TransitionSpec("run_completed", "running", "completed"),
        TransitionSpec(
            "run_failed", {"running", "suspended"}, "failed"
        ),
        TransitionSpec("run_quarantined", "running", "failed"),
        TransitionSpec("run_deadlocked", "running", "failed"),
        TransitionSpec("route_unmatched", "running", "failed"),
        TransitionSpec("budget_exceeded", "running", "failed"),
        TransitionSpec(
            "run_stopped", {"running", "suspended"}, "stopped"
        ),
    ],
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json_copy(value: Any, field_name: str) -> Any:
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be JSON-serializable: {exc}") from exc
    return json.loads(encoded)


@dataclass(frozen=True)
class NodeContext:
    """Data passed to a registered node executor."""

    state_dir: Path
    run_id: str
    graph_id: str
    node_id: str
    visit: int
    call: int
    attempt: int
    config: dict[str, Any]
    state: dict[str, Any]
    node_state: dict[str, Any]
    resume_payload: Any = None

    @property
    def idempotency_key(self) -> str:
        return f"graph:{self.run_id}:{self.node_id}:{self.visit}"


@dataclass(frozen=True)
class ExecutorResult:
    """Normalized result returned by a node executor."""

    status: str = "success"  # success | wait | suspend | failure
    output: Any = None
    state_updates: dict[str, Any] = field(default_factory=dict)
    route: str | tuple[str, ...] | list[str] | None = None
    delay_sec: float = 0.0
    reason: str = ""
    resume_key: str | None = None
    error: str = ""
    retryable: bool = False

    @classmethod
    def success(
        cls,
        output: Any = None,
        *,
        route: str | tuple[str, ...] | list[str] | None = None,
        state_updates: dict[str, Any] | None = None,
    ) -> ExecutorResult:
        return cls(
            "success",
            output=output,
            route=route,
            state_updates=dict(state_updates or {}),
        )

    @classmethod
    def wait(
        cls,
        *,
        delay_sec: float = 0.0,
        output: Any = None,
        state_updates: dict[str, Any] | None = None,
    ) -> ExecutorResult:
        return cls(
            "wait",
            output=output,
            delay_sec=delay_sec,
            state_updates=dict(state_updates or {}),
        )

    @classmethod
    def suspend(
        cls,
        reason: str,
        *,
        resume_key: str | None = None,
        output: Any = None,
        state_updates: dict[str, Any] | None = None,
    ) -> ExecutorResult:
        return cls(
            "suspend",
            output=output,
            reason=reason,
            resume_key=resume_key,
            state_updates=dict(state_updates or {}),
        )

    @classmethod
    def failure(
        cls,
        error: str,
        *,
        retryable: bool = False,
        route: str | tuple[str, ...] | list[str] | None = None,
        output: Any = None,
        state_updates: dict[str, Any] | None = None,
    ) -> ExecutorResult:
        return cls(
            "failure",
            output=output,
            error=error,
            retryable=retryable,
            route=route,
            state_updates=dict(state_updates or {}),
        )


NodeResult = ExecutorResult
ExecutorFn = Callable[[NodeContext], Any]


def register_executor(
    name: str,
    executor: ExecutorFn | None = None,
    *,
    replace: bool = False,
) -> ExecutorFn | Callable[[ExecutorFn], ExecutorFn]:
    """Register a process-local implementation for a persisted handler name.

    It can be used directly or as ``@register_executor("handler_name")``.
    """
    if not isinstance(name, str) or not _RUN_ID_RE.fullmatch(name):
        raise ValueError(f"invalid executor name {name!r}")

    def install(fn: ExecutorFn) -> ExecutorFn:
        if not callable(fn):
            raise TypeError("executor must be callable")
        with _EXECUTOR_LOCK:
            current = _EXECUTORS.get(name)
            if current is not None and current is not fn and not replace:
                raise ValueError(f"executor {name!r} is already registered")
            _EXECUTORS[name] = fn
        return fn

    if executor is None:
        return install
    return install(executor)


def _resolve_executor(name: str) -> ExecutorFn:
    with _EXECUTOR_LOCK:
        found = _EXECUTORS.get(name)
    if found is not None:
        return found
    # Built-ins are imported only when first needed; custom executors can be
    # registered by the host before ticking a restored run.
    from charon.orchestration.graph_executors import register_builtin_executors

    register_builtin_executors()
    with _EXECUTOR_LOCK:
        found = _EXECUTORS.get(name)
    if found is None:
        raise LookupError(f"no executor registered for handler {name!r}")
    return found


def _runs_dir(state_dir: Path) -> Path:
    path = Path(state_dir) / "orchestration" / "graph_runs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _validate_run_id(run_id: str) -> str:
    if not isinstance(run_id, str) or not _RUN_ID_RE.fullmatch(run_id):
        raise ValueError(f"invalid run_id {run_id!r}")
    return run_id


def _run_dir(state_dir: Path, run_id: str) -> Path:
    return _runs_dir(state_dir) / _validate_run_id(run_id)


def _run_path(state_dir: Path, run_id: str) -> Path:
    return _run_dir(state_dir, run_id) / "run.json"


def _definition_path(state_dir: Path, run_id: str) -> Path:
    return _run_dir(state_dir, run_id) / "definition.json"


def _events_path(state_dir: Path, run_id: str) -> Path:
    return _run_dir(state_dir, run_id) / "events.jsonl"


def _locks_dir(state_dir: Path) -> Path:
    path = _runs_dir(state_dir) / ".locks"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        with temp.open("x", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read durable graph state {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"durable graph state {path} is not an object")
    return value


def _read_run(state_dir: Path, run_id: str) -> dict[str, Any] | None:
    return _read_json(_run_path(state_dir, run_id))


def _atomic_write_events(path: Path, events: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        with temp.open("x", encoding="utf-8") as handle:
            for event in events:
                handle.write(
                    json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n"
                )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def _read_events(path: Path) -> tuple[list[dict[str, Any]], bool]:
    """Read valid JSONL events and report whether the tail needed repair."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return [], False
    if not raw:
        return [], False
    lines = raw.splitlines(keepends=True)
    events: list[dict[str, Any]] = []
    repaired = False
    for index, line in enumerate(lines):
        complete = line.endswith(b"\n")
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            if index != len(lines) - 1:
                raise RuntimeError(
                    f"event log contains invalid JSON before its tail: {path}"
                ) from exc
            repaired = True
            break
        if not isinstance(event, dict):
            raise RuntimeError(f"event log row is not an object: {path}")
        events.append(event)
        if not complete:
            repaired = True
    return events, repaired


def _prepare_event_state(
    state_dir: Path,
    run: dict[str, Any],
) -> tuple[list[dict[str, Any]], bool]:
    """Load/migrate the durable event outbox and repair a partial JSONL tail."""
    path = _events_path(state_dir, run["run_id"])
    existing, repaired = _read_events(path)
    changed = False
    if "event_outbox" not in run:
        # Legacy runs wrote events before committing run.json. Keep only the
        # last row for each sequence that run.json says was committed.
        committed_seq = int(run.get("event_seq") or 0)
        by_seq: dict[int, dict[str, Any]] = {}
        for event in existing:
            seq = event.get("seq")
            if isinstance(seq, int) and 0 < seq <= committed_seq:
                by_seq[seq] = event
        existing = [by_seq[seq] for seq in sorted(by_seq)]
        run["event_outbox"] = []
        run["events_flushed_through"] = max(by_seq, default=0)
        changed = True
        repaired = True
    if not isinstance(run.get("event_outbox"), list):
        raise RuntimeError("durable graph event_outbox is not a list")
    if repaired:
        _atomic_write_events(path, existing)
    return existing, changed


def _flush_event_outbox(
    state_dir: Path,
    run: dict[str, Any],
    *,
    existing: list[dict[str, Any]] | None = None,
) -> bool:
    """Idempotently append committed outbox rows, repairing a partial tail."""
    if existing is None:
        existing, changed = _prepare_event_state(state_dir, run)
    else:
        changed = False
    outbox = list(run.get("event_outbox") or [])
    if not outbox:
        return changed

    path = _events_path(state_dir, run["run_id"])
    flushed_through = int(run.get("events_flushed_through") or 0)
    by_id = {
        str(event.get("event_id")): event
        for event in existing
        if event.get("event_id")
    }
    by_seq = {
        int(event["seq"]): event
        for event in existing
        if isinstance(event.get("seq"), int)
    }
    append_rows: list[dict[str, Any]] = []
    for event in outbox:
        seq = event.get("seq")
        event_id = str(event.get("event_id") or "")
        if not isinstance(seq, int) or seq < 1 or not event_id:
            raise RuntimeError("durable graph event outbox contains an invalid row")
        prior = by_id.get(event_id)
        if prior is not None:
            if prior != event:
                raise RuntimeError(f"event id collision for {event_id!r}")
            flushed_through = max(flushed_through, seq)
            continue
        same_seq = by_seq.get(seq)
        if same_seq is not None:
            if seq <= flushed_through:
                raise RuntimeError(
                    f"committed event sequence collision at {seq}"
                )
            # A legacy/uncommitted row was ahead of run.json. Replace the
            # uncommitted tail with the authoritative outbox.
            existing = [
                row
                for row in existing
                if not isinstance(row.get("seq"), int) or int(row["seq"]) < seq
            ]
            _atomic_write_events(path, existing)
            by_id = {
                str(row.get("event_id")): row
                for row in existing
                if row.get("event_id")
            }
            by_seq = {
                int(row["seq"]): row
                for row in existing
                if isinstance(row.get("seq"), int)
            }
        append_rows.append(event)
        existing.append(event)
        by_id[event_id] = event
        by_seq[seq] = event
        flushed_through = max(flushed_through, seq)

    if append_rows:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            for event in append_rows:
                handle.write(
                    json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n"
                )
            handle.flush()
            os.fsync(handle.fileno())
    run["events_flushed_through"] = flushed_through
    run["event_outbox"] = []
    return True


def _recover_event_outbox(state_dir: Path, run: dict[str, Any]) -> None:
    existing, changed = _prepare_event_state(state_dir, run)
    changed = _flush_event_outbox(
        state_dir,
        run,
        existing=existing,
    ) or changed
    if changed:
        _atomic_write_json(_run_path(state_dir, run["run_id"]), run)


def _write_run(state_dir: Path, run: dict[str, Any]) -> None:
    _ensure_run_lifecycle(run)
    _prepare_event_state(state_dir, run)
    run["updated_at"] = _now_iso()
    # run.json is the commit point. It contains scheduler state and an event
    # outbox in the same atomic document, so the JSONL projection can never get
    # ahead of an uncommitted transition.
    _atomic_write_json(_run_path(state_dir, run["run_id"]), run)
    if _flush_event_outbox(state_dir, run):
        _atomic_write_json(_run_path(state_dir, run["run_id"]), run)


def _definition_digest(raw: dict[str, Any]) -> str:
    canonical = json.dumps(
        raw,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _load_definition(
    state_dir: Path,
    run_id: str,
    *,
    expected_sha256: str | None = None,
) -> GraphDefinition:
    raw = _read_json(_definition_path(state_dir, run_id))
    if raw is None:
        raise RuntimeError(f"definition snapshot missing for run {run_id!r}")
    if expected_sha256 and _definition_digest(raw) != expected_sha256:
        raise RuntimeError(
            f"definition integrity check failed for run {run_id!r}"
        )
    return GraphDefinition.from_dict(raw)


def _append_event(
    state_dir: Path,
    run: dict[str, Any],
    event_type: str,
    *,
    event_id: str | None = None,
    **payload: Any,
) -> dict[str, Any]:
    run["event_seq"] = int(run.get("event_seq") or 0) + 1
    event = {
        "event_id": event_id or f"gevt_{uuid.uuid4().hex[:16]}",
        "seq": run["event_seq"],
        "ts": _now_iso(),
        "run_id": run["run_id"],
        "graph_id": run["graph_id"],
        "type": event_type,
        **_json_copy(payload, "event payload"),
    }
    outbox = run.setdefault("event_outbox", [])
    if not isinstance(outbox, list):
        raise RuntimeError("durable graph event_outbox is not a list")
    outbox.append(event)
    run["last_event"] = event
    return event


def _ensure_run_lifecycle(run: dict[str, Any]) -> MachineInstance:
    """Load the canonical embedded lifecycle, adopting legacy run status.

    ``status`` remains at the top level for compatibility, but it is a
    projection of this machine once the embedded instance exists.
    """
    raw = run.get("lifecycle")
    if raw is None:
        instance = create_machine_instance(
            _GRAPH_RUN_MACHINE,
            str(run["run_id"]),
            state=str(run.get("status") or "running"),
            now=str(run.get("created_at") or _now_iso()),
        )
    else:
        instance = MachineInstance.from_dict(raw)
        validate_machine_instance(_GRAPH_RUN_MACHINE, instance)
        if instance.instance_id != str(run.get("run_id") or ""):
            raise RuntimeError(
                "embedded graph lifecycle instance does not match run_id"
            )
        if instance.event_outbox:
            raise RuntimeError(
                "embedded graph lifecycle has an unbridged event outbox"
            )
    run["lifecycle"] = instance.to_dict()
    run["status"] = instance.state
    return instance


def _transition_run_lifecycle(
    state_dir: Path,
    run: dict[str, Any],
    event_type: str,
    *,
    event_id: str | None = None,
    expected_revision: int | None = None,
    **payload: Any,
) -> MachineDispatchResult:
    """Dispatch under the run lock and bridge its event into the run outbox.

    The caller's eventual ``_write_run`` atomically commits the canonical
    lifecycle instance and the graph event outbox in the same ``run.json``.
    """
    instance = _ensure_run_lifecycle(run)
    stable_event_id = event_id or f"gevt_{uuid.uuid4().hex[:16]}"
    result = dispatch_machine(
        _GRAPH_RUN_MACHINE,
        instance,
        event_type,
        event_id=stable_event_id,
        payload=payload,
        expected_revision=expected_revision,
    )
    if not result.duplicate:
        _append_event(
            state_dir,
            run,
            event_type,
            event_id=stable_event_id,
            fsm_revision=result.instance.revision,
            source_status=result.event["source"],
            target_status=result.event["target"],
            **payload,
        )
    bridged = acknowledge_machine_events(result.instance, [stable_event_id])
    run["lifecycle"] = bridged.to_dict()
    run["status"] = bridged.state
    return MachineDispatchResult(
        instance=bridged,
        event=result.event,
        duplicate=result.duplicate,
    )


def _local_lock(path: Path) -> threading.RLock:
    key = str(path)
    with _LOCAL_LOCKS_GUARD:
        return _LOCAL_LOCKS.setdefault(key, threading.RLock())


@contextmanager
def _locked_run(state_dir: Path, run_id: str) -> Iterator[None]:
    run_id = _validate_run_id(run_id)
    lock_path = _locks_dir(state_dir) / f"{run_id}.lock"
    local = _local_lock(lock_path)
    with local:
        with lock_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _public_run(
    state_dir: Path, run: dict[str, Any], *, include_definition: bool = True
) -> dict[str, Any]:
    _ensure_run_lifecycle(run)
    value = copy.deepcopy(run)
    value.pop("event_outbox", None)
    if include_definition:
        value["definition"] = _load_definition(
            state_dir,
            run["run_id"],
            expected_sha256=run.get("definition_sha256"),
        ).to_dict()
    return value


def _initial_node_state() -> dict[str, Any]:
    return {
        "status": "pending",
        "visits": 0,
        "calls": 0,
        "attempt": 0,
        "poll_count": 0,
        "not_before": 0.0,
        "output": None,
        "error": "",
        "route": None,
        "resume_payload": None,
        "resume_key": None,
        "activated_via": [],
        "started_at": None,
        "completed_at": None,
    }


def _node_map(definition: GraphDefinition) -> dict[str, NodeSpec]:
    return {node.id: node for node in definition.nodes}


def _incoming(definition: GraphDefinition) -> dict[str, list[EdgeSpec]]:
    result = {node.id: [] for node in definition.nodes}
    for edge in definition.edges:
        result[edge.target].append(edge)
    return result


def _activate_node(
    state_dir: Path,
    definition: GraphDefinition,
    run: dict[str, Any],
    node_id: str,
    *,
    via_edge: str | None = None,
) -> bool:
    state = run["node_states"][node_id]
    if node_id in run["active_nodes"]:
        return True
    node = _node_map(definition)[node_id]
    limit = node.max_visits or definition.max_node_visits
    next_visit = int(state.get("visits") or 0) + 1
    if next_visit > limit:
        _fail_run(
            state_dir,
            run,
            f"node visit budget exceeded for {node_id!r} ({limit})",
            event_type="budget_exceeded",
            node_id=node_id,
        )
        return False
    state.update(
        {
            "status": "active",
            "visits": next_visit,
            "attempt": 0,
            "poll_count": 0,
            "not_before": 0.0,
            "error": "",
            "route": None,
            "resume_payload": None,
            "resume_key": None,
            "activated_via": (
                [part for part in str(via_edge).split(",") if part]
                if via_edge
                else []
            ),
            "started_at": None,
            "completed_at": None,
        }
    )
    run["active_nodes"].append(node_id)
    _append_event(
        state_dir,
        run,
        "node_activated",
        node_id=node_id,
        visit=next_visit,
        via_edge=via_edge,
    )
    return True


def _fail_run(
    state_dir: Path,
    run: dict[str, Any],
    error: str,
    *,
    event_type: str = "run_failed",
    **payload: Any,
) -> None:
    run["error"] = str(error)[:1000]
    run["completed_at"] = _now_iso()
    for node_id in list(run.get("active_nodes") or []):
        node_state = run["node_states"][node_id]
        if node_state.get("status") not in {"failed", "succeeded"}:
            node_state["status"] = "cancelled"
    run["active_nodes"] = []
    _transition_run_lifecycle(
        state_dir,
        run,
        event_type,
        error=run["error"],
        **payload,
    )


def _complete_run(
    state_dir: Path,
    run: dict[str, Any],
    *,
    terminal_node: str | None = None,
) -> None:
    run["completed_at"] = _now_iso()
    run["result"] = {
        "state": copy.deepcopy(run["state"]),
        "outputs": {
            node_id: copy.deepcopy(node_state.get("output"))
            for node_id, node_state in run["node_states"].items()
            if node_state.get("status") == "succeeded"
        },
    }
    if terminal_node:
        for node_id in list(run.get("active_nodes") or []):
            if node_id != terminal_node:
                run["node_states"][node_id]["status"] = "cancelled"
        run["active_nodes"] = []
        run["pending_tokens"] = {}
    _transition_run_lifecycle(
        state_dir,
        run,
        "run_completed",
        terminal_node=terminal_node,
    )


def _offer_edges(
    state_dir: Path,
    definition: GraphDefinition,
    run: dict[str, Any],
    node_id: str,
    *,
    success: bool,
    routes: set[str],
) -> list[EdgeSpec]:
    mode = "success" if success else "failure"
    selected = []
    for edge in definition.edges:
        if edge.source != node_id or edge.on not in {mode, "always"}:
            continue
        if edge.route is not None and edge.route not in routes:
            continue
        selected.append(edge)
        target_tokens = run["pending_tokens"].setdefault(edge.target, {})
        target_tokens[edge.id] = int(target_tokens.get(edge.id) or 0) + 1
        traversal = {
            "edge_id": edge.id,
            "source": edge.source,
            "target": edge.target,
            "on": edge.on,
            "route": edge.route,
            "source_visit": run["node_states"][node_id]["visits"],
            "ts": _now_iso(),
        }
        run["traversed_edges"].append(traversal)
        run["edge_counts"][edge.id] = int(run["edge_counts"].get(edge.id) or 0) + 1
        _append_event(state_dir, run, "edge_traversed", **traversal)
    return selected


def _eligible_edges(
    definition: GraphDefinition,
    node_id: str,
    *,
    success: bool,
) -> list[EdgeSpec]:
    mode = "success" if success else "failure"
    return [
        edge
        for edge in definition.edges
        if edge.source == node_id and edge.on in {mode, "always"}
    ]


def _drain_ready_nodes(
    state_dir: Path, definition: GraphDefinition, run: dict[str, Any]
) -> None:
    incoming = _incoming(definition)
    nodes = _node_map(definition)
    changed = True
    while changed and run["status"] == "running":
        changed = False
        for node_id in [node.id for node in definition.nodes]:
            if node_id in run["active_nodes"]:
                continue
            edges = incoming[node_id]
            if not edges:
                continue
            tokens = run["pending_tokens"].setdefault(node_id, {})
            consumed: list[str] = []
            if nodes[node_id].join == "all":
                if all(int(tokens.get(edge.id) or 0) > 0 for edge in edges):
                    consumed = [edge.id for edge in edges]
            else:
                consumed = [
                    edge.id
                    for edge in edges
                    if int(tokens.get(edge.id) or 0) > 0
                ][:1]
            if not consumed:
                continue
            for edge_id in consumed:
                tokens[edge_id] -= 1
                if tokens[edge_id] <= 0:
                    tokens.pop(edge_id, None)
            if _activate_node(
                state_dir,
                definition,
                run,
                node_id,
                via_edge=",".join(consumed),
            ):
                changed = True


def _settle(
    state_dir: Path, definition: GraphDefinition, run: dict[str, Any]
) -> None:
    if run["status"] != "running" or run["active_nodes"]:
        return
    pending = any(
        int(count) > 0
        for by_edge in run.get("pending_tokens", {}).values()
        for count in by_edge.values()
    )
    if pending:
        _fail_run(
            state_dir,
            run,
            "graph is blocked at an unsatisfied join",
            event_type="run_deadlocked",
        )
    else:
        _complete_run(state_dir, run)


def start_run(
    state_dir: Path,
    definition: GraphDefinition | dict[str, Any],
    *,
    initial_state: dict[str, Any] | None = None,
    inputs: dict[str, Any] | None = None,
    run_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist a definition snapshot and create a runnable graph instance."""
    if not isinstance(definition, GraphDefinition):
        definition = GraphDefinition.from_dict(definition)
    if initial_state is not None and inputs is not None:
        raise ValueError("pass either initial_state or inputs, not both")
    state = _json_copy(
        initial_state if initial_state is not None else (inputs or {}),
        "initial_state",
    )
    run_metadata = _json_copy(metadata or {}, "run metadata")
    run_id = _validate_run_id(run_id or f"grun_{uuid.uuid4().hex[:16]}")
    directory = _run_dir(state_dir, run_id)
    try:
        directory.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise ValueError(f"graph run {run_id!r} already exists") from exc

    snapshot = definition.to_dict()
    _atomic_write_json(directory / "definition.json", snapshot)
    now = _now_iso()
    lifecycle = create_machine_instance(
        _GRAPH_RUN_MACHINE,
        run_id,
        now=now,
    )
    run: dict[str, Any] = {
        "schema_version": 1,
        "run_id": run_id,
        "graph_id": definition.graph_id,
        "title": definition.title,
        "definition_sha256": _definition_digest(snapshot),
        "status": "running",
        "lifecycle": lifecycle.to_dict(),
        "state": state,
        "metadata": run_metadata,
        "node_states": {
            node.id: _initial_node_state() for node in definition.nodes
        },
        "active_nodes": [],
        "pending_tokens": {node.id: {} for node in definition.nodes},
        "traversed_edges": [],
        "edge_counts": {edge.id: 0 for edge in definition.edges},
        "run_steps": 0,
        "event_seq": 0,
        "event_outbox": [],
        "events_flushed_through": 0,
        "last_ticked_at": None,
        "suspension": None,
        "error": "",
        "result": None,
        "created_at": now,
        "updated_at": now,
        "completed_at": None,
        "last_event": None,
    }
    _append_event(state_dir, run, "run_started")
    for node_id in definition.entry_nodes:
        if run["status"] == "running":
            _activate_node(state_dir, definition, run, node_id)
    _write_run(state_dir, run)
    return _public_run(state_dir, run)


def get_run(
    state_dir: Path, run_id: str, *, include_definition: bool = True
) -> dict[str, Any] | None:
    with _locked_run(state_dir, run_id):
        run = _read_run(state_dir, run_id)
        if run is None:
            return None
        _recover_event_outbox(state_dir, run)
        return _public_run(
            state_dir,
            run,
            include_definition=include_definition,
        )


def list_runs(
    state_dir: Path,
    *,
    status: str | None = None,
    include_definition: bool = False,
) -> list[dict[str, Any]]:
    runs = []
    for path in sorted(_runs_dir(state_dir).glob("*/run.json")):
        run_id = path.parent.name
        try:
            with _locked_run(state_dir, run_id):
                run = _read_run(state_dir, run_id)
                if run is None or (
                    status is not None and run.get("status") != status
                ):
                    continue
                _recover_event_outbox(state_dir, run)
                runs.append(
                    _public_run(
                        state_dir,
                        run,
                        include_definition=include_definition,
                    )
                )
        except (OSError, RuntimeError, GraphValidationError, ValueError):
            # Enumeration is fault-isolated. tick_runs reports per-run errors
            # so a damaged snapshot cannot halt healthy workflows.
            continue
    return runs


def _normalize_result(value: Any) -> ExecutorResult:
    if value is None:
        return ExecutorResult.success()
    if isinstance(value, ExecutorResult):
        result = value
    elif isinstance(value, dict):
        raw = dict(value)
        status = raw.pop("status", raw.pop("action", "success"))
        aliases = {
            "done": "success",
            "completed": "success",
            "stay": "wait",
            "poll": "wait",
            "fail": "failure",
            "failed": "failure",
        }
        raw["status"] = aliases.get(status, status)
        try:
            result = ExecutorResult(**raw)
        except TypeError as exc:
            raise TypeError(f"invalid executor result: {exc}") from exc
    else:
        raise TypeError("executor must return ExecutorResult, dict, or None")
    if result.status not in {"success", "wait", "suspend", "failure"}:
        raise ValueError(f"unknown executor result status {result.status!r}")
    if not isinstance(result.state_updates, dict):
        raise TypeError("executor state_updates must be an object")
    if isinstance(result.delay_sec, bool) or not isinstance(
        result.delay_sec, (int, float)
    ):
        raise TypeError("executor delay_sec must be numeric")
    if float(result.delay_sec) < 0:
        raise ValueError("executor delay_sec must be non-negative")
    _json_copy(result.output, "executor output")
    _json_copy(result.state_updates, "executor state_updates")
    routes = result.route
    if routes is not None and not isinstance(routes, (str, tuple, list)):
        raise TypeError("executor route must be a string, list, tuple, or null")
    return result


def _routes(result: ExecutorResult) -> set[str]:
    if result.route is None:
        return set()
    if isinstance(result.route, str):
        return {result.route}
    if not all(isinstance(item, str) and item for item in result.route):
        raise ValueError("executor route labels must be non-empty strings")
    return set(result.route)


def _apply_updates(run: dict[str, Any], result: ExecutorResult) -> None:
    if result.state_updates:
        run["state"].update(_json_copy(result.state_updates, "state updates"))


def _handle_failure(
    state_dir: Path,
    definition: GraphDefinition,
    run: dict[str, Any],
    node: NodeSpec,
    result: ExecutorResult,
) -> str:
    node_state = run["node_states"][node.id]
    error = str(result.error or "node executor failed")[:1000]
    node_state["error"] = error
    _apply_updates(run, result)
    if result.output is not None:
        node_state["output"] = _json_copy(result.output, "executor output")
    if result.retryable:
        node_state["attempt"] = int(node_state.get("attempt") or 0) + 1
        if node_state["attempt"] < node.max_attempts:
            backoff = node.backoff_base_sec * (2 ** (node_state["attempt"] - 1))
            node_state["status"] = "waiting"
            node_state["not_before"] = time.time() + backoff
            _append_event(
                state_dir,
                run,
                "node_retry_scheduled",
                node_id=node.id,
                visit=node_state["visits"],
                attempt=node_state["attempt"],
                delay_sec=backoff,
                error=error,
            )
            return "retry"

    node_state["resume_payload"] = None
    node_state["status"] = "failed"
    node_state["completed_at"] = _now_iso()
    if node.id in run["active_nodes"]:
        run["active_nodes"].remove(node.id)
    selected = _offer_edges(
        state_dir,
        definition,
        run,
        node.id,
        success=False,
        routes=_routes(result),
    )
    _append_event(
        state_dir,
        run,
        "node_failed",
        node_id=node.id,
        visit=node_state["visits"],
        error=error,
        handled=bool(selected),
    )
    if selected:
        _drain_ready_nodes(state_dir, definition, run)
        _settle(state_dir, definition, run)
        return "failure_routed"
    _fail_run(state_dir, run, error, node_id=node.id)
    return "failed"


def tick_run(
    state_dir: Path,
    run_id: str,
    *,
    now: float | None = None,
) -> dict[str, Any]:
    """Execute at most one ready node call for a durable graph run."""
    clock = time.time() if now is None else float(now)
    with _locked_run(state_dir, run_id):
        try:
            run = _read_run(state_dir, run_id)
        except (OSError, RuntimeError, ValueError) as exc:
            return {
                "run_id": run_id,
                "action": "error",
                "status": "unreadable",
                "error": f"{type(exc).__name__}: {exc}",
            }
        if run is None:
            return {"run_id": run_id, "action": "missing"}
        try:
            _recover_event_outbox(state_dir, run)
            _ensure_run_lifecycle(run)
        except (OSError, RuntimeError, ValueError) as exc:
            return {
                "run_id": run_id,
                "action": "error",
                "status": "event_log_error",
                "error": f"{type(exc).__name__}: {exc}",
            }
        if run.get("status") != "running":
            return {
                "run_id": run_id,
                "action": "skipped",
                "status": run.get("status"),
            }
        try:
            definition = _load_definition(
                state_dir,
                run_id,
                expected_sha256=run.get("definition_sha256"),
            )
        except (OSError, RuntimeError, GraphValidationError, ValueError) as exc:
            error = f"definition unavailable or invalid: {type(exc).__name__}: {exc}"
            _fail_run(
                state_dir,
                run,
                error,
                event_type="run_quarantined",
            )
            _write_run(state_dir, run)
            return {
                "run_id": run_id,
                "action": "quarantined",
                "status": "failed",
                "error": error,
            }
        _drain_ready_nodes(state_dir, definition, run)
        if run["status"] != "running":
            _write_run(state_dir, run)
            return {
                "run_id": run_id,
                "action": "failed",
                "status": run["status"],
                "error": run.get("error", ""),
            }
        if not run["active_nodes"]:
            _settle(state_dir, definition, run)
            _write_run(state_dir, run)
            return {
                "run_id": run_id,
                "action": "settled",
                "status": run["status"],
            }
        if int(run.get("run_steps") or 0) >= definition.max_run_steps:
            _fail_run(
                state_dir,
                run,
                f"run step budget exceeded ({definition.max_run_steps})",
                event_type="budget_exceeded",
            )
            _write_run(state_dir, run)
            return {
                "run_id": run_id,
                "action": "failed",
                "status": "failed",
                "error": run["error"],
            }

        node_id = next(
            (
                candidate
                for candidate in run["active_nodes"]
                if float(
                    run["node_states"][candidate].get("not_before") or 0.0
                )
                <= clock
            ),
            None,
        )
        if node_id is None:
            next_time = min(
                float(run["node_states"][candidate].get("not_before") or 0.0)
                for candidate in run["active_nodes"]
            )
            return {
                "run_id": run_id,
                "action": "deferred",
                "status": "running",
                "not_before": next_time,
            }

        nodes = _node_map(definition)
        node = nodes[node_id]
        node_state = run["node_states"][node_id]
        node_state["status"] = "running"
        node_state["calls"] = int(node_state.get("calls") or 0) + 1
        node_state["started_at"] = node_state.get("started_at") or _now_iso()
        run["run_steps"] = int(run.get("run_steps") or 0) + 1
        run["last_ticked_at"] = _now_iso()
        _append_event(
            state_dir,
            run,
            "node_started",
            node_id=node_id,
            visit=node_state["visits"],
            call=node_state["calls"],
            attempt=int(node_state.get("attempt") or 0) + 1,
        )
        # Persist the claim before executing.  A crash can replay the call, so
        # side-effecting executors must use ctx.idempotency_key.
        _write_run(state_dir, run)

        context = NodeContext(
            state_dir=Path(state_dir),
            run_id=run_id,
            graph_id=definition.graph_id,
            node_id=node_id,
            visit=int(node_state["visits"]),
            call=int(node_state["calls"]),
            attempt=int(node_state.get("attempt") or 0) + 1,
            config=copy.deepcopy(node.config),
            state=copy.deepcopy(run["state"]),
            node_state=copy.deepcopy(node_state),
            resume_payload=copy.deepcopy(node_state.get("resume_payload")),
        )
        try:
            result = _normalize_result(_resolve_executor(node.handler)(context))
        except Exception as exc:  # noqa: BLE001 - failures become durable retries
            result = ExecutorResult.failure(
                f"{type(exc).__name__}: {exc}", retryable=True
            )

        action: str
        if result.status == "wait":
            node_state["resume_payload"] = None
            _apply_updates(run, result)
            node_state["status"] = "waiting"
            node_state["poll_count"] = int(node_state.get("poll_count") or 0) + 1
            node_state["not_before"] = clock + float(result.delay_sec)
            if result.output is not None:
                node_state["output"] = _json_copy(result.output, "executor output")
            _append_event(
                state_dir,
                run,
                "node_waiting",
                node_id=node_id,
                visit=node_state["visits"],
                delay_sec=float(result.delay_sec),
            )
            action = "wait"
        elif result.status == "suspend":
            node_state["resume_payload"] = None
            _apply_updates(run, result)
            node_state["status"] = "suspended"
            node_state["resume_key"] = result.resume_key
            if result.output is not None:
                node_state["output"] = _json_copy(result.output, "executor output")
            run["suspension"] = {
                "node_id": node_id,
                "reason": str(result.reason),
                "resume_key": result.resume_key,
                "at": _now_iso(),
            }
            _transition_run_lifecycle(
                state_dir,
                run,
                "run_suspended",
                node_id=node_id,
                reason=str(result.reason),
                resume_key=result.resume_key,
            )
            action = "suspend"
        elif result.status == "failure":
            action = _handle_failure(
                state_dir, definition, run, node, result
            )
        else:
            node_state["resume_payload"] = None
            _apply_updates(run, result)
            routes = _routes(result)
            node_state["status"] = "succeeded"
            node_state["attempt"] = 0
            node_state["not_before"] = 0.0
            node_state["route"] = sorted(routes) if len(routes) > 1 else (
                next(iter(routes)) if routes else None
            )
            node_state["output"] = _json_copy(result.output, "executor output")
            node_state["completed_at"] = _now_iso()
            if node_id in run["active_nodes"]:
                run["active_nodes"].remove(node_id)
            _append_event(
                state_dir,
                run,
                "node_succeeded",
                node_id=node_id,
                visit=node_state["visits"],
                route=sorted(routes),
            )
            if node.terminal:
                _complete_run(state_dir, run, terminal_node=node_id)
            else:
                selected = _offer_edges(
                    state_dir,
                    definition,
                    run,
                    node_id,
                    success=True,
                    routes=routes,
                )
                eligible = _eligible_edges(
                    definition,
                    node_id,
                    success=True,
                )
                if eligible and not selected:
                    expected_routes = sorted({
                        edge.route
                        for edge in eligible
                        if edge.route is not None
                    })
                    _fail_run(
                        state_dir,
                        run,
                        (
                            f"node {node_id!r} emitted unmatched route(s) "
                            f"{sorted(routes)!r}; expected one of "
                            f"{expected_routes!r}"
                        ),
                        event_type="route_unmatched",
                        node_id=node_id,
                        routes=sorted(routes),
                        expected_routes=expected_routes,
                    )
                    action = "failed"
                else:
                    _drain_ready_nodes(state_dir, definition, run)
                    _settle(state_dir, definition, run)
                    action = "success"
            if node.terminal:
                action = "success"

        if action in {"wait", "retry"} and node_id in run["active_nodes"]:
            # Durable round-robin: a polling/retrying branch yields to ready
            # siblings instead of monopolizing every heartbeat.
            run["active_nodes"].remove(node_id)
            run["active_nodes"].append(node_id)

        _write_run(state_dir, run)
        return {
            "run_id": run_id,
            "action": action,
            "node_id": node_id,
            "status": run["status"],
            "active_nodes": list(run["active_nodes"]),
            "error": run.get("error", ""),
        }


def tick_runs(
    state_dir: Path, *, max_runs: int = 8, now: float | None = None
) -> list[dict[str, Any]]:
    """Fairly tick up to ``max_runs`` runnable graphs once each."""
    if isinstance(max_runs, bool) or not isinstance(max_runs, int) or max_runs < 1:
        raise ValueError("max_runs must be a positive integer")
    candidates: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    for path in sorted(_runs_dir(state_dir).glob("*/run.json")):
        run_id = path.parent.name
        try:
            run = _read_json(path)
        except (OSError, RuntimeError, ValueError) as exc:
            results.append({
                "run_id": run_id,
                "action": "error",
                "status": "unreadable",
                "error": f"{type(exc).__name__}: {exc}",
            })
            continue
        if run is not None and run.get("status") == "running":
            candidates.append(run)
    candidates.sort(
        key=lambda run: (
            str(run.get("last_ticked_at") or ""),
            str(run.get("created_at") or ""),
            str(run.get("run_id") or ""),
        )
    )
    advanced = 0
    for run in candidates:
        event = tick_run(state_dir, str(run["run_id"]), now=now)
        if event.get("action") == "deferred":
            continue
        results.append(event)
        if event.get("action") not in {
            "error",
            "missing",
            "quarantined",
            "skipped",
        }:
            advanced += 1
        if advanced >= max_runs:
            break
    return results


def resume_run(
    state_dir: Path,
    run_id: str,
    payload: Any = None,
    *,
    node_id: str | None = None,
    resume_key: str | None = None,
    event_id: str | None = None,
    expected_revision: int | None = None,
) -> dict[str, Any] | None:
    """Resume the suspended node matching ``node_id`` or ``resume_key``."""
    value = _json_copy(payload, "resume payload")
    resume_payload_sha256 = hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    with _locked_run(state_dir, run_id):
        run = _read_run(state_dir, run_id)
        if run is None:
            return None
        _recover_event_outbox(state_dir, run)
        lifecycle = _ensure_run_lifecycle(run)
        if event_id and event_id in lifecycle.processed_events:
            prior = lifecycle.processed_events[event_id].get("transition") or {}
            prior_payload = prior.get("payload") or {}
            _transition_run_lifecycle(
                state_dir,
                run,
                "run_resumed",
                event_id=event_id,
                expected_revision=expected_revision,
                node_id=prior_payload.get("node_id"),
                resume_key=prior_payload.get("resume_key"),
                requested_node_id=node_id,
                requested_resume_key=resume_key,
                resume_payload_sha256=resume_payload_sha256,
            )
            return _public_run(state_dir, run)
        if run.get("status") != "suspended":
            return None
        suspended = [
            candidate
            for candidate in run["active_nodes"]
            if run["node_states"][candidate].get("status") == "suspended"
        ]
        selected = next(
            (
                candidate
                for candidate in suspended
                if (node_id is None or candidate == node_id)
                and (
                    resume_key is None
                    or run["node_states"][candidate].get("resume_key")
                    == resume_key
                )
            ),
            None,
        )
        if selected is None:
            return None
        state = run["node_states"][selected]
        _transition_run_lifecycle(
            state_dir,
            run,
            "run_resumed",
            event_id=event_id,
            expected_revision=expected_revision,
            node_id=selected,
            resume_key=state.get("resume_key"),
            requested_node_id=node_id,
            requested_resume_key=resume_key,
            resume_payload_sha256=resume_payload_sha256,
        )
        state["status"] = "active"
        state["not_before"] = 0.0
        state["resume_payload"] = value
        run["suspension"] = None
        _write_run(state_dir, run)
        return _public_run(state_dir, run)


def stop_run(
    state_dir: Path,
    run_id: str,
    reason: str = "",
    *,
    event_id: str | None = None,
    expected_revision: int | None = None,
) -> dict[str, Any] | None:
    """Stop an active or suspended graph without executing more nodes."""
    with _locked_run(state_dir, run_id):
        run = _read_run(state_dir, run_id)
        if run is None:
            return None
        _recover_event_outbox(state_dir, run)
        lifecycle = _ensure_run_lifecycle(run)
        if event_id and event_id in lifecycle.processed_events:
            _transition_run_lifecycle(
                state_dir,
                run,
                "run_stopped",
                event_id=event_id,
                expected_revision=expected_revision,
                reason=str(reason),
            )
            return _public_run(state_dir, run)
        if run.get("status") in _TERMINAL:
            return _public_run(state_dir, run)
        run["error"] = f"stopped: {reason}".rstrip()
        run["completed_at"] = _now_iso()
        for node_id in run["active_nodes"]:
            run["node_states"][node_id]["status"] = "cancelled"
        run["active_nodes"] = []
        _transition_run_lifecycle(
            state_dir,
            run,
            "run_stopped",
            event_id=event_id,
            expected_revision=expected_revision,
            reason=str(reason),
        )
        _write_run(state_dir, run)
        return _public_run(state_dir, run)


def project_run(state_dir: Path, run_id: str) -> dict[str, Any] | None:
    """Return a stable node/edge snapshot suitable for graph UIs."""
    run = get_run(state_dir, run_id, include_definition=True)
    if run is None:
        return None
    definition = GraphDefinition.from_dict(run.pop("definition"))
    active_nodes = set(run.get("active_nodes") or [])
    traversed_ids = {
        item.get("edge_id") for item in run.get("traversed_edges") or []
    }
    active_edge_ids = {
        edge_id
        for node_id in active_nodes
        for edge_id in run["node_states"][node_id].get("activated_via", [])
    }
    nodes = []
    for node in definition.nodes:
        state = copy.deepcopy(run["node_states"][node.id])
        nodes.append(
            {
                "id": node.id,
                "label": node.title or node.id,
                "handler": node.handler,
                "join": node.join,
                "terminal": node.terminal,
                "status": state["status"],
                "active": node.id in active_nodes,
                "visits": state["visits"],
                "attempt": state["attempt"],
                "output": state["output"],
                "error": state["error"],
                "metadata": copy.deepcopy(node.metadata),
            }
        )
    edges = []
    for edge in definition.edges:
        edge_status = "inactive"
        if edge.id in active_edge_ids:
            edge_status = "active"
        elif edge.id in traversed_ids:
            edge_status = "traversed"
        edges.append(
            {
                "id": edge.id,
                "from": edge.source,
                "to": edge.target,
                "on": edge.on,
                "route": edge.route,
                "label": edge.title or edge.route or "",
                "status": edge_status,
                "traversal_count": int(run["edge_counts"].get(edge.id) or 0),
                "metadata": copy.deepcopy(edge.metadata),
            }
        )
    edge_by_id = {edge["id"]: edge for edge in edges}
    lifecycle = run.get("lifecycle") or {}
    return {
        "schema_version": 1,
        "run_id": run_id,
        "graph_id": definition.graph_id,
        "title": definition.title,
        "status": run["status"],
        "revision": int(lifecycle.get("revision") or 0),
        "lifecycle": {
            "machine": lifecycle.get("machine", _GRAPH_RUN_MACHINE.name),
            "machine_version": int(
                lifecycle.get("machine_version") or _GRAPH_RUN_MACHINE.version
            ),
            "state": lifecycle.get("state", run["status"]),
            "revision": int(lifecycle.get("revision") or 0),
        },
        "created_at": run["created_at"],
        "updated_at": run["updated_at"],
        "completed_at": run.get("completed_at"),
        "run_steps": run["run_steps"],
        "nodes": nodes,
        "edges": edges,
        "node_states": copy.deepcopy(run["node_states"]),
        "active_nodes": list(run["active_nodes"]),
        "active_edges": [
            copy.deepcopy(edge_by_id[edge_id])
            for edge_id in sorted(active_edge_ids)
            if edge_id in edge_by_id
        ],
        "traversed_edges": [
            copy.deepcopy(edge_by_id[edge_id])
            for edge_id in sorted(traversed_ids)
            if edge_id in edge_by_id
        ],
        "active_edge_ids": sorted(active_edge_ids),
        "traversed_edge_ids": sorted(
            edge_id for edge_id in traversed_ids if edge_id
        ),
        "error": run.get("error", ""),
        "suspension": copy.deepcopy(run.get("suspension")),
    }


__all__ = [
    "ExecutorFn",
    "ExecutorResult",
    "NodeContext",
    "NodeResult",
    "get_run",
    "list_runs",
    "project_run",
    "register_executor",
    "resume_run",
    "start_run",
    "stop_run",
    "tick_run",
    "tick_runs",
]
