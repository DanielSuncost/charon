"""Authoritative finite-state machines for Libris operation and topic lifecycles.

The legacy ``operation.json`` and ``topic.json`` documents remain compatibility
projections. Their canonical lifecycle state is persisted by the shared durable
machine store, with atomic snapshots, revision checks, idempotent event ids, and
outbox recovery.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from charon.orchestration.fsm import (
    MachineInstance,
    MachineSpec,
    TransitionRejected,
    TransitionSpec,
    dispatch,
)
from charon.orchestration.fsm_store import DurableMachineStore


OPERATION_PROJECTION_TO_STATE = {
    "running": "running",
    "scouting": "scouting",
    "awaiting_clarification": "awaiting_clarification",
    "fanout": "fanout",
    "researching": "researching",
    "reports_ready": "reports_ready",
    "assembling_delivery": "assembling",
    "verifying_delivery": "verifying",
    "delivered": "completed",
    "idle": "idle",
    "budget_exhausted": "budget_exhausted",
    "stopped": "stopped",
    "failed": "failed",
    "delivery_failed": "delivery_failed",
}
OPERATION_STATE_TO_PROJECTION = {
    state: projection for projection, state in OPERATION_PROJECTION_TO_STATE.items()
}

TOPIC_STATES = frozenset({
    "researching",
    "writing",
    "judging",
    "revising",
    "checkpointed",
    "ready_high_confidence",
    "plateaued",
    "judge_failed",
    "no_report",
    "excluded",
})
TOPIC_TERMINAL_STATES = frozenset({
    "checkpointed",
    "ready_high_confidence",
    "plateaued",
    "judge_failed",
    "no_report",
    "excluded",
})


def _status_reducer(ctx) -> dict[str, Any]:
    payload = ctx.payload if isinstance(ctx.payload, dict) else {}
    patch: dict[str, Any] = {
        "last_transition_note": str(payload.get("note") or "")[:500],
        "last_transition_event_id": ctx.event_id,
    }
    if payload.get("reason"):
        patch["terminal_reason"] = str(payload.get("reason"))[:1000]
    return patch


def _completion_guard(ctx) -> bool | str:
    payload = ctx.payload if isinstance(ctx.payload, dict) else {}
    if payload.get("certificate_valid") is not True:
        return "delivery completion requires a validated completion certificate"
    digest = str(payload.get("certificate_sha256") or "")
    if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest.lower()):
        return "delivery completion requires a SHA-256 certificate digest"
    if int(payload.get("topic_count") or 0) < 1:
        return "delivery completion requires at least one certified topic"
    certificate_path = Path(str(payload.get("certificate_path") or ""))
    if not certificate_path.is_absolute():
        return "delivery completion requires an absolute certificate path"
    return True


def _completion_reducer(ctx) -> dict[str, Any]:
    payload = ctx.payload if isinstance(ctx.payload, dict) else {}
    return {
        "completion_certificate": {
            "path": str(payload.get("certificate_path") or ""),
            "sha256": str(payload.get("certificate_sha256") or ""),
            "topic_count": int(payload.get("topic_count") or 0),
        },
        "last_transition_note": str(payload.get("note") or "")[:500],
        "last_transition_event_id": ctx.event_id,
    }


def _completed_has_certificate(ctx) -> bool | str:
    if ctx.target != "completed":
        return True
    certificate = ctx.data.get("completion_certificate")
    if not isinstance(certificate, dict):
        return "completed operations must retain their completion certificate"
    if int(certificate.get("topic_count") or 0) < 1:
        return "completed operations must certify at least one topic"
    if len(str(certificate.get("sha256") or "")) != 64:
        return "completed operations must retain a certificate digest"
    return True


def _verify_completion_certificate_file(
    operation_id: str,
    claimed: dict[str, Any],
    *,
    operation_dir: Path,
) -> dict[str, Any]:
    """Independently verify the certificate and its artifact hashes."""
    path = Path(str(claimed.get("path") or ""))
    if not path.is_absolute():
        raise TransitionRejected("completion certificate path must be absolute")
    expected_path = Path(operation_dir).resolve() / "delivery" / "completion-certificate.json"
    if path.resolve() != expected_path:
        raise TransitionRejected("completion certificate path is outside this operation")
    try:
        stored = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TransitionRejected(f"completion certificate cannot be read: {exc}") from exc
    if not isinstance(stored, dict) or str(stored.get("operation_id") or "") != operation_id:
        raise TransitionRejected("completion certificate does not belong to this operation")
    if int(stored.get("schema_version") or 0) != 1:
        raise TransitionRejected("unsupported completion certificate schema")
    expected = str(stored.get("certificate_sha256") or "")
    body = {
        str(key): value
        for key, value in stored.items()
        if key not in {"certificate_sha256", "valid", "path", "reason"}
    }
    digest = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    if not expected or digest != expected or str(claimed.get("certificate_sha256") or "") != expected:
        raise TransitionRejected("completion certificate digest does not match")
    topic_count = int(stored.get("topic_count") or 0)
    if topic_count < 1 or topic_count != int(claimed.get("topic_count") or 0):
        raise TransitionRejected("completion certificate has no certified topics")
    topics = stored.get("topics") if isinstance(stored.get("topics"), list) else []
    checks = stored.get("checks") if isinstance(stored.get("checks"), dict) else {}
    artifacts = stored.get("artifacts") if isinstance(stored.get("artifacts"), list) else []
    required_checks = {
        "has_topics",
        "all_selected_topics_present",
        "all_topics_terminal",
        "has_deliverable_topics",
        "delivered_topics_match_deliverable_topics",
        "bundle_topic_count_matches",
        "bundle_topics_match_deliverable_topics",
        "topic_reports_nonempty",
        "bundle_nonempty",
        "summary_nonempty",
        "html_nonempty",
        "selection_nonempty",
    }
    if (
        len(topics) != topic_count
        or not required_checks.issubset(checks)
        or not all(checks.get(name) is True for name in required_checks)
    ):
        raise TransitionRejected("completion certificate does not prove all completion checks")
    if len(artifacts) < topic_count + 4:
        raise TransitionRejected("completion certificate is missing required artifact attestations")
    artifact_kinds = [str(artifact.get("kind") or "") for artifact in artifacts if isinstance(artifact, dict)]
    required_kinds = {"bundle", "summary", "report", "selection"}
    if (
        not required_kinds.issubset(artifact_kinds)
        or artifact_kinds.count("topic_report") != topic_count
    ):
        raise TransitionRejected("completion certificate is missing required artifact kinds")
    certificate_root = Path(operation_dir).resolve()
    seen_paths: set[str] = set()
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise TransitionRejected("completion certificate contains a malformed artifact")
        artifact_path = Path(str(artifact.get("path") or ""))
        try:
            artifact_path.resolve().relative_to(certificate_root)
            within_operation = True
        except (OSError, ValueError):
            within_operation = False
        if not artifact_path.is_absolute() or not within_operation:
            raise TransitionRejected("completion certificate contains an out-of-operation artifact path")
        resolved_path = str(artifact_path.resolve())
        if resolved_path in seen_paths:
            raise TransitionRejected("completion certificate contains duplicate artifact paths")
        seen_paths.add(resolved_path)
        try:
            data = artifact_path.read_bytes()
        except OSError as exc:
            raise TransitionRejected(f"certified artifact cannot be read: {artifact_path}") from exc
        if (
            not data
            or len(data) != int(artifact.get("size_bytes") or -1)
            or hashlib.sha256(data).hexdigest() != str(artifact.get("sha256") or "")
        ):
            raise TransitionRejected(f"certified artifact changed: {artifact_path}")
    return {
        **stored,
        "valid": True,
        "path": str(path),
    }


_ACTIVE_OPERATION_STATES = frozenset({
    "running",
    "scouting",
    "awaiting_clarification",
    "fanout",
    "researching",
    "reports_ready",
    "assembling",
    "verifying",
})
_OPERATION_TRANSITIONS = (
    TransitionSpec("enter.scouting", {"running", "awaiting_clarification", "scouting"}, "scouting", reducer=_status_reducer),
    TransitionSpec("enter.awaiting_clarification", {"running", "scouting", "awaiting_clarification"}, "awaiting_clarification", reducer=_status_reducer),
    TransitionSpec("enter.fanout", {"running", "scouting", "fanout"}, "fanout", reducer=_status_reducer),
    TransitionSpec("enter.researching", {"running", "scouting", "fanout", "researching", "reports_ready", "assembling", "verifying"}, "researching", reducer=_status_reducer),
    TransitionSpec("enter.reports_ready", {"researching", "reports_ready"}, "reports_ready", reducer=_status_reducer),
    TransitionSpec("enter.assembling", {"running", "researching", "reports_ready", "assembling"}, "assembling", reducer=_status_reducer),
    TransitionSpec("enter.verifying", {"assembling", "verifying"}, "verifying", reducer=_status_reducer),
    TransitionSpec("delivery.certified", "verifying", "completed", guard=_completion_guard, reducer=_completion_reducer),
    TransitionSpec("enter.idle", {"running", "scouting", "fanout"}, "idle", reducer=_status_reducer),
    TransitionSpec("enter.budget_exhausted", _ACTIVE_OPERATION_STATES, "budget_exhausted", reducer=_status_reducer),
    TransitionSpec("enter.stopped", _ACTIVE_OPERATION_STATES, "stopped", reducer=_status_reducer),
    TransitionSpec("enter.failed", _ACTIVE_OPERATION_STATES, "failed", reducer=_status_reducer),
    TransitionSpec("enter.delivery_failed", {"researching", "reports_ready", "assembling", "verifying"}, "delivery_failed", reducer=_status_reducer),
)

OPERATION_MACHINE = MachineSpec(
    name="libris.operation",
    states=frozenset(OPERATION_STATE_TO_PROJECTION),
    initial_state="running",
    transitions=_OPERATION_TRANSITIONS,
    terminal_states={"completed", "idle", "budget_exhausted", "stopped", "failed", "delivery_failed"},
    invariants=(_completed_has_certificate,),
    version=1,
    metadata={"projection": "operation.json"},
)

_ACTIVE_TOPIC_STATES = frozenset(TOPIC_STATES - TOPIC_TERMINAL_STATES)
_TOPIC_TRANSITIONS = (
    TransitionSpec("enter.researching", {"researching", "writing", "revising", "judging"}, "researching", reducer=_status_reducer),
    TransitionSpec("enter.writing", {"researching", "revising", "writing"}, "writing", reducer=_status_reducer),
    TransitionSpec("enter.judging", {"researching", "writing", "revising", "judging"}, "judging", reducer=_status_reducer),
    TransitionSpec("enter.revising", {"researching", "judging", "revising"}, "revising", reducer=_status_reducer),
    *tuple(
        TransitionSpec(f"enter.{target}", _ACTIVE_TOPIC_STATES, target, reducer=_status_reducer)
        for target in sorted(TOPIC_TERMINAL_STATES)
    ),
)
TOPIC_MACHINE = MachineSpec(
    name="libris.topic",
    states=TOPIC_STATES,
    initial_state="researching",
    transitions=_TOPIC_TRANSITIONS,
    terminal_states=TOPIC_TERMINAL_STATES,
    version=1,
    metadata={"projection": "topic.json"},
)


_LOCAL_LOCKS: dict[str, threading.RLock] = {}
_LOCAL_LOCKS_GUARD = threading.Lock()


def _local_lock(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _LOCAL_LOCKS_GUARD:
        return _LOCAL_LOCKS.setdefault(key, threading.RLock())


@contextmanager
def _locked_projection(projection_path: Path) -> Iterator[None]:
    lock_path = projection_path.with_name(f".{projection_path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    local = _local_lock(lock_path)
    with local:
        with lock_path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _atomic_write(path: Path, value: dict[str, Any]) -> None:
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


def _store(state_dir: Path, entity_type: str) -> DurableMachineStore:
    spec = OPERATION_MACHINE if entity_type == "operation" else TOPIC_MACHINE
    return DurableMachineStore(Path(state_dir), spec)


def lifecycle_path(state_dir: Path, entity_id: str, *, entity_type: str) -> Path:
    store = _store(state_dir, entity_type)
    return store.root / entity_id / "instance.json"


def load_lifecycle(
    state_dir: Path,
    entity_id: str,
    *,
    entity_type: str,
) -> MachineInstance | None:
    return _store(state_dir, entity_type).get(entity_id)


def initialize_lifecycle(
    state_dir: Path,
    *,
    entity_type: str,
    entity_id: str,
    projected_status: str,
    now: str | None = None,
    data: dict[str, Any] | None = None,
) -> MachineInstance:
    spec = OPERATION_MACHINE if entity_type == "operation" else TOPIC_MACHINE
    if entity_type == "operation":
        state = OPERATION_PROJECTION_TO_STATE.get(projected_status)
    else:
        state = projected_status if projected_status in TOPIC_STATES else None
    if state is None:
        raise ValueError(f"unknown Libris {entity_type} status: {projected_status!r}")
    store = DurableMachineStore(Path(state_dir), spec)
    existing = store.get(entity_id)
    if existing is not None:
        return existing
    payload = {"entity_type": entity_type, **dict(data or {})}
    if now:
        payload["projection_created_at"] = now
    try:
        return store.create(entity_id, state=state, data=payload)
    except ValueError:
        existing = store.get(entity_id)
        if existing is None:
            raise
        return existing


def projected_status(instance: MachineInstance) -> str:
    if instance.machine == OPERATION_MACHINE.name:
        return OPERATION_STATE_TO_PROJECTION[instance.state]
    if instance.machine == TOPIC_MACHINE.name:
        return instance.state
    raise ValueError(f"unsupported Libris lifecycle machine: {instance.machine}")


def transition_lifecycle(
    state_dir: Path,
    *,
    entity_type: str,
    entity_id: str,
    current_projected_status: str,
    target_projected_status: str,
    note: str = "",
    reason: str = "",
    event_id: str | None = None,
    expected_revision: int | None = None,
) -> tuple[MachineInstance, dict[str, Any]]:
    if entity_type == "operation":
        if target_projected_status == "delivered":
            raise TransitionRejected(
                "generic status updates cannot complete a Libris operation; "
                "a validated completion certificate is required"
            )
        target = OPERATION_PROJECTION_TO_STATE.get(target_projected_status)
        spec = OPERATION_MACHINE
    else:
        target = target_projected_status if target_projected_status in TOPIC_STATES else None
        spec = TOPIC_MACHINE
    if target is None:
        raise ValueError(f"unknown Libris {entity_type} status: {target_projected_status!r}")

    transition_id = event_id or f"lfc_{uuid.uuid4().hex}"
    store = DurableMachineStore(Path(state_dir), spec)
    instance = store.get(entity_id)
    if instance is None:
        initial = (
            OPERATION_PROJECTION_TO_STATE.get(current_projected_status)
            if entity_type == "operation"
            else current_projected_status
        )
        try:
            instance = store.create(
                entity_id,
                state=initial,
                data={"entity_type": entity_type, "adopted_legacy_projection": True},
            )
        except ValueError:
            instance = store.get(entity_id)
        if instance is None:
            raise RuntimeError(f"could not initialize Libris {entity_type} lifecycle")
    result = store.dispatch(
        entity_id,
        f"enter.{target}",
        event_id=transition_id,
        payload={"note": note[:500], "reason": reason[:1000]},
        expected_revision=expected_revision,
    )
    return result.instance, result.event


def complete_operation(
    state_dir: Path,
    *,
    operation_id: str,
    operation_dir: Path,
    current_projected_status: str,
    certificate: dict[str, Any],
    note: str = "",
    event_id: str | None = None,
) -> tuple[MachineInstance, dict[str, Any]]:
    store = DurableMachineStore(Path(state_dir), OPERATION_MACHINE)
    with store.locked(operation_id):
        instance = store._read(operation_id)
        if instance is None:
            raise RuntimeError("Libris operation lifecycle is unavailable")
        instance = store._flush_outbox(instance)
        # Validate the certificate and all artifact attestations while holding
        # the same operation lock used for the terminal lifecycle commit.
        certificate = _verify_completion_certificate_file(
            operation_id,
            certificate,
            operation_dir=operation_dir,
        )
        transition_id = event_id or (
            f"libris:{operation_id}:delivery-certified:"
            f"{str(certificate.get('certificate_sha256') or '')}"
        )
        payload = {
            "certificate_valid": certificate.get("valid") is True,
            "certificate_path": str(certificate.get("path") or ""),
            "certificate_sha256": str(certificate.get("certificate_sha256") or ""),
            "topic_count": int(certificate.get("topic_count") or 0),
            "note": note[:500],
        }
        result = dispatch(
            OPERATION_MACHINE,
            instance,
            "delivery.certified",
            event_id=transition_id,
            payload=payload,
            expected_revision=instance.revision,
        )
        if not result.duplicate:
            store._write(result.instance)
        published = store._flush_outbox(result.instance)
        return published, result.event


def adopt_legacy_incomplete_delivery(
    state_dir: Path,
    *,
    operation_id: str,
    note: str,
) -> MachineInstance:
    """Adopt a pre-FSM false-delivery record directly into active recovery.

    This is intentionally limited to operations with no lifecycle snapshot.  A
    certificate-backed ``completed`` machine remains terminal.
    """
    store = DurableMachineStore(Path(state_dir), OPERATION_MACHINE)
    existing = store.get(operation_id)
    if existing is not None:
        raise TransitionRejected(
            "an authoritative lifecycle already exists; completed operations are terminal"
        )
    return store.create(
        operation_id,
        state="researching",
        data={
            "entity_type": "operation",
            "adopted_legacy_projection": True,
            "legacy_status": "delivered",
            "last_transition_note": note[:500],
        },
    )


def adopt_legacy_completed_delivery(
    state_dir: Path,
    *,
    operation_id: str,
    operation_dir: Path,
    certificate: dict[str, Any],
) -> MachineInstance:
    """Adopt a fully validated legacy delivery without changing its timestamps."""
    certificate = _verify_completion_certificate_file(
        operation_id,
        certificate,
        operation_dir=operation_dir,
    )
    store = DurableMachineStore(Path(state_dir), OPERATION_MACHINE)
    existing = store.get(operation_id)
    if existing is not None:
        bound = (existing.data or {}).get("completion_certificate") or {}
        if existing.state == "completed":
            if (
                str(bound.get("path") or "") == str(certificate.get("path") or "")
                and str(bound.get("sha256") or "") == str(certificate.get("certificate_sha256") or "")
                and int(bound.get("topic_count") or 0) == int(certificate.get("topic_count") or 0)
            ):
                return existing
            raise TransitionRejected(
                "existing lifecycle is not bound to this legacy completion certificate"
            )
        if existing.state != "verifying" or not existing.data.get("adopted_legacy_projection"):
            raise TransitionRejected(
                "existing lifecycle cannot adopt this legacy completion certificate"
            )
    digest = str(certificate.get("certificate_sha256") or "")
    topic_count = int(certificate.get("topic_count") or 0)
    if certificate.get("valid") is not True or len(digest) != 64 or topic_count < 1:
        raise TransitionRejected("legacy completion adoption requires a valid certificate")
    if existing is None:
        try:
            store.create(
                operation_id,
                state="verifying",
                data={
                    "entity_type": "operation",
                    "adopted_legacy_projection": True,
                    "legacy_status": "delivered",
                },
            )
        except ValueError:
            pass
    completed, _ = complete_operation(
        state_dir,
        operation_id=operation_id,
        operation_dir=operation_dir,
        current_projected_status="verifying_delivery",
        certificate=certificate,
        note="Adopted a validated legacy delivery.",
    )
    return completed


def mutate_projection(
    projection_path: Path,
    mutator,
) -> dict[str, Any]:
    """Serialize and atomically persist one compatibility-projection mutation."""
    path = Path(projection_path)
    with _locked_projection(path):
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        if not isinstance(current, dict):
            raise RuntimeError(f"Libris projection is not an object: {path}")
        updated = mutator(dict(current))
        if not isinstance(updated, dict):
            raise TypeError("projection mutator must return an object")
        if updated == current:
            return current
        _atomic_write(path, updated)
        return updated


__all__ = [
    "OPERATION_MACHINE",
    "TOPIC_MACHINE",
    "TOPIC_TERMINAL_STATES",
    "adopt_legacy_completed_delivery",
    "adopt_legacy_incomplete_delivery",
    "complete_operation",
    "initialize_lifecycle",
    "lifecycle_path",
    "load_lifecycle",
    "mutate_projection",
    "projected_status",
    "transition_lifecycle",
]
