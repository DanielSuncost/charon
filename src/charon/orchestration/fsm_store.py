"""File-backed durability adapter for :mod:`charon.orchestration.fsm`.

The instance snapshot is the transaction commit point.  It carries a staged
event outbox in the same atomic JSON document.  The JSONL audit projection is
then flushed idempotently and the outbox is acknowledged in a second atomic
snapshot.  Recovery performs the same flush before serving or dispatching.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Mapping

from charon.orchestration.fsm import (
    DispatchResult,
    MachineInstance,
    MachineSpec,
    acknowledge_events,
    create_instance,
    dispatch,
    validate_instance,
)


_LOCAL_LOCKS: dict[str, threading.RLock] = {}
_LOCAL_LOCKS_GUARD = threading.Lock()
_INSTANCE_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")


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
        raise RuntimeError(f"cannot read durable machine snapshot {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"durable machine snapshot {path} is not an object")
    return value


def _read_events(path: Path) -> tuple[list[dict[str, Any]], bool]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return [], False
    if not raw:
        return [], False
    rows: list[dict[str, Any]] = []
    repaired = False
    lines = raw.splitlines(keepends=True)
    for index, line in enumerate(lines):
        complete = line.endswith(b"\n")
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            if index != len(lines) - 1:
                raise RuntimeError(
                    f"machine event log contains invalid JSON before its tail: {path}"
                ) from exc
            repaired = True
            break
        if not isinstance(value, dict):
            raise RuntimeError(f"machine event log row is not an object: {path}")
        rows.append(value)
        if not complete:
            repaired = True
    return rows, repaired


def _atomic_write_events(path: Path, events: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(
        f".{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        with temp.open("x", encoding="utf-8") as handle:
            for event in events:
                handle.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def _local_lock(path: Path) -> threading.RLock:
    key = str(path)
    with _LOCAL_LOCKS_GUARD:
        return _LOCAL_LOCKS.setdefault(key, threading.RLock())


class DurableMachineStore:
    """Persist instances of one machine specification under a state root."""

    def __init__(self, state_dir: Path, spec: MachineSpec):
        self.state_dir = Path(state_dir)
        self.spec = spec

    @property
    def root(self) -> Path:
        path = self.state_dir / "orchestration" / "machines" / self.spec.name
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _instance_dir(self, instance_id: str) -> Path:
        if not isinstance(instance_id, str) or not _INSTANCE_ID_RE.fullmatch(instance_id):
            raise ValueError(f"invalid machine instance id {instance_id!r}")
        return self.root / instance_id

    def _snapshot_path(self, instance_id: str) -> Path:
        return self._instance_dir(instance_id) / "instance.json"

    def _events_path(self, instance_id: str) -> Path:
        return self._instance_dir(instance_id) / "events.jsonl"

    def _lock_path(self, instance_id: str) -> Path:
        directory = self.root / ".locks"
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"{self._instance_dir(instance_id).name}.lock"

    @contextmanager
    def locked(self, instance_id: str) -> Iterator[None]:
        """Serialize writers across both threads and local processes."""
        path = self._lock_path(instance_id)
        local = _local_lock(path)
        with local:
            with path.open("a+", encoding="utf-8") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _read(self, instance_id: str) -> MachineInstance | None:
        raw = _read_json(self._snapshot_path(instance_id))
        if raw is None:
            return None
        instance = MachineInstance.from_dict(raw)
        validate_instance(self.spec, instance)
        return instance

    def _write(self, instance: MachineInstance) -> None:
        _atomic_write_json(
            self._snapshot_path(instance.instance_id),
            instance.to_dict(),
        )

    def _flush_outbox(self, instance: MachineInstance) -> MachineInstance:
        """Publish missing committed events, repairing an incomplete tail."""
        path = self._events_path(instance.instance_id)
        existing, repaired = _read_events(path)
        if repaired:
            _atomic_write_events(path, existing)
        if not instance.event_outbox:
            return instance
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
        for event in instance.event_outbox:
            event_id = str(event.get("event_id") or "")
            seq = event.get("seq")
            if not event_id or not isinstance(seq, int) or seq < 1:
                raise RuntimeError("machine event outbox contains an invalid row")
            prior = by_id.get(event_id)
            if prior is not None:
                if prior != event:
                    raise RuntimeError(f"machine event id collision for {event_id!r}")
                continue
            prior_seq = by_seq.get(seq)
            if prior_seq is not None:
                raise RuntimeError(f"machine event sequence collision at {seq}")
            row = dict(event)
            append_rows.append(row)
            by_id[event_id] = row
            by_seq[seq] = row
        if append_rows:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                for event in append_rows:
                    handle.write(
                        json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n"
                    )
                handle.flush()
                os.fsync(handle.fileno())
        acknowledged = acknowledge_events(
            instance,
            [event["event_id"] for event in instance.event_outbox],
        )
        if acknowledged is not instance:
            self._write(acknowledged)
        return acknowledged

    def create(
        self,
        instance_id: str,
        *,
        data: Mapping[str, Any] | None = None,
        state: str | None = None,
    ) -> MachineInstance:
        """Create exactly one snapshot; an existing ID is never overwritten."""
        with self.locked(instance_id):
            if self._snapshot_path(instance_id).exists():
                raise ValueError(f"machine instance {instance_id!r} already exists")
            instance = create_instance(
                self.spec,
                instance_id,
                data=data,
                state=state,
            )
            self._write(instance)
            return instance

    def get(self, instance_id: str) -> MachineInstance | None:
        """Load an instance and recover any committed event outbox first."""
        with self.locked(instance_id):
            instance = self._read(instance_id)
            if instance is None:
                return None
            return self._flush_outbox(instance)

    def dispatch(
        self,
        instance_id: str,
        event: str,
        *,
        event_id: str,
        payload: Any = None,
        expected_revision: int | None = None,
    ) -> DispatchResult:
        """Lock, recover, transition, atomically commit, and publish one event."""
        with self.locked(instance_id):
            current = self._read(instance_id)
            if current is None:
                raise KeyError(f"machine instance {instance_id!r} does not exist")
            current = self._flush_outbox(current)
            result = dispatch(
                self.spec,
                current,
                event,
                event_id=event_id,
                payload=payload,
                expected_revision=expected_revision,
            )
            if result.duplicate:
                return result
            # The snapshot containing state + outbox is the commit point.
            self._write(result.instance)
            published = self._flush_outbox(result.instance)
            return DispatchResult(
                instance=published,
                event=result.event,
                duplicate=False,
            )

    def events(self, instance_id: str) -> list[dict[str, Any]]:
        """Return the ordered audit projection after recovering the outbox."""
        instance = self.get(instance_id)
        if instance is None:
            return []
        events, repaired = _read_events(self._events_path(instance_id))
        if repaired:
            # ``get`` already repaired under lock; this only covers an external
            # writer racing outside the contract and should not normally occur.
            with self.locked(instance_id):
                events, repaired = _read_events(self._events_path(instance_id))
                if repaired:
                    _atomic_write_events(self._events_path(instance_id), events)
        return events


__all__ = ["DurableMachineStore"]
