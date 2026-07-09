"""Charon store adapter — single entry point for SQLite persistence.

All runtime modules import from here instead of directly reading/writing JSON.
The adapter opens the DB lazily on first access and provides a clean API.

Usage:
    from store_adapter import get_db, with_store

    # Direct DB access:
    db = get_db(state_dir)

    # Or via context manager:
    with with_store(state_dir) as db:
        ...

Migration from JSON happens automatically on first open of a state_dir.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

# Ensure libs/ is importable
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from libs.store import (  # noqa: E402
    DB,
    open_db,
    migrate_from_json,
    # agents
    agent_insert,
    agent_get,
    agent_list,
    agent_update,
    agent_count,
    # tasks
    task_insert,
    task_get,
    task_list,
    task_update,
    task_delete,
    task_pending,
    task_all,
    task_queue_stats,
    # events
    event_append,
    event_list,
    event_get_by_message,
    events_for_conversation,
    conversation_list,
    reconstruct_path,
    # shade contracts
    contract_insert,
    contract_get,
    contract_list,
    contract_update,
    # shade phase events
    shade_event_append,
    shade_event_list,
    # boundaries
    boundary_insert,
    boundary_get,
    boundary_list,
    boundary_update,
    boundary_pending_for_agent,
    # agent runtime
    agent_profile_upsert,
    agent_profile_get,
    agent_memory_upsert,
    agent_memory_get,
    agent_inbox_append,
    agent_inbox_list,
    agent_attempt_append,
    # goals
    goal_project_upsert,
    goal_project_get,
    goal_session_upsert,
    goal_session_get,
    goal_context_packet_upsert,
    goal_context_packet_get,
    # user model
    user_model_get,
    user_model_set,
    # onboarding
    onboarding_get,
    onboarding_set,
    # run log
    run_log_append,
    run_log_tail,
)


# ---------------------------------------------------------------------------
# Singleton DB registry — one DB per state_dir path
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_dbs: dict[str, DB] = {}
_migrated: set[str] = set()


def get_db(state_dir: Path | str) -> DB:
    """Get or create a DB handle for the given state directory.

    Thread-safe. Runs JSON migration on first open.
    """
    key = str(Path(state_dir).resolve())
    if key in _dbs:
        return _dbs[key]
    with _lock:
        # Double-check after acquiring lock
        if key in _dbs:
            return _dbs[key]
        db = open_db(Path(key))
        _dbs[key] = db
        # Auto-migrate JSON state on first open
        if key not in _migrated:
            _migrated.add(key)
            try:
                migrate_from_json(db, Path(key))
            except Exception:
                pass  # Migration is best-effort
        return db


def close_db(state_dir: Path | str) -> None:
    """Close and remove a DB handle. Safe to call if not open."""
    key = str(Path(state_dir).resolve())
    with _lock:
        db = _dbs.pop(key, None)
        if db:
            try:
                db.close()
            except Exception:
                pass


def reset_all() -> None:
    """Close all cached DB handles. Used in tests."""
    with _lock:
        for db in _dbs.values():
            try:
                db.close()
            except Exception:
                pass
        _dbs.clear()
        _migrated.clear()


class _StoreContext:
    """Context manager for get_db()."""

    def __init__(self, state_dir: Path | str):
        self.state_dir = state_dir

    def __enter__(self) -> DB:
        return get_db(self.state_dir)

    def __exit__(self, *exc):
        pass  # DB stays open (singleton)


def with_store(state_dir: Path | str) -> _StoreContext:
    """Context manager that returns a DB handle."""
    return _StoreContext(state_dir)


# Re-export everything from libs.store for convenience
__all__ = [
    'DB', 'get_db', 'close_db', 'reset_all', 'with_store',
    'open_db', 'migrate_from_json',
    # agents
    'agent_insert', 'agent_get', 'agent_list', 'agent_update', 'agent_count',
    # tasks
    'task_insert', 'task_get', 'task_list', 'task_update', 'task_delete',
    'task_pending', 'task_all', 'task_queue_stats',
    # events
    'event_append', 'event_list', 'event_get_by_message',
    'events_for_conversation', 'conversation_list', 'reconstruct_path',
    # shade contracts
    'contract_insert', 'contract_get', 'contract_list', 'contract_update',
    # shade phase events
    'shade_event_append', 'shade_event_list',
    # boundaries
    'boundary_insert', 'boundary_get', 'boundary_list', 'boundary_update',
    'boundary_pending_for_agent',
    # agent runtime
    'agent_profile_upsert', 'agent_profile_get',
    'agent_memory_upsert', 'agent_memory_get',
    'agent_inbox_append', 'agent_inbox_list',
    'agent_attempt_append',
    # goals
    'goal_project_upsert', 'goal_project_get',
    'goal_session_upsert', 'goal_session_get',
    'goal_context_packet_upsert', 'goal_context_packet_get',
    # user model
    'user_model_get', 'user_model_set',
    # onboarding
    'onboarding_get', 'onboarding_set',
    # run log
    'run_log_append', 'run_log_tail',
]
