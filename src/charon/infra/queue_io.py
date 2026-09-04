"""Atomic, cross-process operations for Charon's JSON task queue."""
from __future__ import annotations

import copy
import fcntl
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

from charon.infra.fileio import read_json_or_quarantine, write_json_atomic


_LOCAL_LOCKS: dict[str, threading.RLock] = {}
_LOCAL_LOCKS_GUARD = threading.Lock()


def _local_lock(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _LOCAL_LOCKS_GUARD:
        return _LOCAL_LOCKS.setdefault(key, threading.RLock())


@contextmanager
def queue_lock(queue_file: Path) -> Iterator[None]:
    """Serialize queue read-modify-write operations across threads/processes."""
    queue_file = Path(queue_file)
    queue_file.parent.mkdir(parents=True, exist_ok=True)
    lock_file = queue_file.with_name(f".{queue_file.name}.lock")
    with _local_lock(lock_file):
        with lock_file.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _read_unlocked(queue_file: Path) -> list[dict]:
    value = read_json_or_quarantine(
        queue_file,
        [],
        component="queue_io",
    )
    if not isinstance(value, list):
        return []
    return [row for row in value if isinstance(row, dict)]


def load_queue_atomic(queue_file: Path) -> list[dict]:
    with queue_lock(queue_file):
        return copy.deepcopy(_read_unlocked(Path(queue_file)))


def merge_queue_atomic(queue_file: Path, queue: list[dict]) -> list[dict]:
    """Persist caller updates without dropping tasks concurrently appended."""
    if not isinstance(queue, list) or not all(
        isinstance(row, dict) for row in queue
    ):
        raise TypeError("queue must be a list of task objects")
    queue_file = Path(queue_file)
    with queue_lock(queue_file):
        current = _read_unlocked(queue_file)
        merged = copy.deepcopy(queue)
        incoming_ids = {
            str(row.get("id"))
            for row in merged
            if row.get("id") is not None
        }
        incoming_correlations = {
            str(row.get("correlation_id"))
            for row in merged
            if row.get("correlation_id")
        }
        for row in current:
            task_id = str(row.get("id")) if row.get("id") is not None else ""
            correlation_id = str(row.get("correlation_id") or "")
            if task_id and task_id in incoming_ids:
                continue
            if correlation_id and correlation_id in incoming_correlations:
                continue
            merged.append(copy.deepcopy(row))
        write_json_atomic(
            queue_file,
            merged,
            ensure_ascii=False,
        )
        return copy.deepcopy(merged)


def enqueue_unique_atomic(
    queue_file: Path,
    task: dict,
    *,
    correlation_id: str,
) -> tuple[dict, bool]:
    """Append exactly one task for a correlation ID under the queue lock."""
    if not isinstance(task, dict):
        raise TypeError("task must be an object")
    correlation_id = str(correlation_id or "").strip()
    if not correlation_id:
        raise ValueError("correlation_id is required")
    queue_file = Path(queue_file)
    with queue_lock(queue_file):
        queue = _read_unlocked(queue_file)
        existing = next(
            (
                row
                for row in queue
                if str(row.get("correlation_id") or "") == correlation_id
            ),
            None,
        )
        if existing is not None:
            return copy.deepcopy(existing), False
        row = copy.deepcopy(task)
        row["correlation_id"] = correlation_id
        queue.append(row)
        write_json_atomic(
            queue_file,
            queue,
            ensure_ascii=False,
        )
        return copy.deepcopy(row), True


def update_queue_atomic(
    queue_file: Path,
    updater: Callable[[list[dict]], bool],
) -> tuple[list[dict], bool]:
    """Run one queue mutation while holding the cross-process queue lock."""
    if not callable(updater):
        raise TypeError("updater must be callable")
    queue_file = Path(queue_file)
    with queue_lock(queue_file):
        queue = _read_unlocked(queue_file)
        changed = bool(updater(queue))
        if not isinstance(queue, list) or not all(
            isinstance(row, dict) for row in queue
        ):
            raise TypeError("queue updater must preserve a list of task objects")
        if changed:
            write_json_atomic(
                queue_file,
                queue,
                ensure_ascii=False,
            )
        return copy.deepcopy(queue), changed


__all__ = [
    "enqueue_unique_atomic",
    "load_queue_atomic",
    "merge_queue_atomic",
    "queue_lock",
    "update_queue_atomic",
]
