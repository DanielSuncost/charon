#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

# SQLite store adapter (optional)
try:
    from store_adapter import (
        get_db as _get_db,
        task_insert as _db_task_insert,
        task_get as _db_task_get,
        task_all as _db_task_all,
        task_pending as _db_task_pending,
    )
    _HAS_STORE = True
except ImportError:
    _HAS_STORE = False


def _use_store() -> bool:
    return _HAS_STORE and os.environ.get('CHARON_NO_SQLITE', '0') != '1'


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _task_id(prefix: str = 'convtask') -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _queue_path(state_dir: Path) -> Path:
    return state_dir / 'queue.json'


def _index_path(state_dir: Path) -> Path:
    return state_dir / 'conversation_index.json'


def load_queue(state_dir: Path) -> list[dict]:
    path = _queue_path(state_dir)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text())
        if isinstance(data, list):
            return data
    except Exception:
        pass
    return []


def save_queue(state_dir: Path, queue: list[dict]) -> None:
    path = _queue_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(queue, indent=2))


def enqueue_agent_message_task(
    state_dir: Path,
    *,
    actor_agent_id: str,
    conversation_id: str,
    message: str,
    parent_message_id: str | None = None,
    branch_label: str | None = None,
) -> dict:
    queue = load_queue(state_dir)
    ts = _now_iso()
    task = {
        'id': _task_id(),
        'title': f'agent_message:{conversation_id}',
        'status': 'pending',
        'task_type': 'agent_message',
        'conversation_id': conversation_id,
        'actor_agent_id': actor_agent_id,
        'message': message,
        'parent_message_id': parent_message_id,
        'branch_label': branch_label,
        'created_at': ts,
        'updated_at': ts,
    }
    queue.append(task)
    save_queue(state_dir, queue)
    if _use_store():
        try:
            db = _get_db(state_dir)
            if not _db_task_get(db, task['id']):
                _db_task_insert(db, dict(task))
        except Exception:
            pass
    return task


def enqueue_agent_intervention_task(
    state_dir: Path,
    *,
    actor_agent_id: str,
    conversation_id: str,
    intervention_of_message_id: str,
    message: str,
    parent_message_id: str | None = None,
) -> dict:
    queue = load_queue(state_dir)
    ts = _now_iso()
    task = {
        'id': _task_id(),
        'title': f'agent_intervention:{conversation_id}',
        'status': 'pending',
        'task_type': 'agent_intervention',
        'conversation_id': conversation_id,
        'actor_agent_id': actor_agent_id,
        'intervention_of_message_id': intervention_of_message_id,
        'parent_message_id': parent_message_id,
        'message': message,
        'created_at': ts,
        'updated_at': ts,
    }
    queue.append(task)
    save_queue(state_dir, queue)
    if _use_store():
        try:
            db = _get_db(state_dir)
            if not _db_task_get(db, task['id']):
                _db_task_insert(db, dict(task))
        except Exception:
            pass
    return task


def enqueue_agent_task(
    state_dir: Path,
    *,
    owner_agent_id: str,
    instruction: str,
    title: str | None = None,
    project: str | None = None,
    priority: str = 'normal',
    conversation_id: str | None = None,
    max_attempts: int = 3,
    scope: list[str] | None = None,
    deps: list[str] | None = None,
    correlation_id: str | None = None,
    constraints: list[str] | None = None,
    expected_outputs: list[str] | None = None,
    phase_plan: list[dict] | None = None,
    shade_phase: dict | None = None,
    interval_minutes: int | float | None = None,
    not_before: str | None = None,
) -> dict:
    queue = load_queue(state_dir)
    task_id = _task_id(prefix='task')
    ts = _now_iso()
    task = {
        'id': task_id,
        'title': title or f'agent_task:{owner_agent_id}',
        'instruction': instruction,
        'status': 'pending',
        'task_type': 'agent_task',
        'owner_agent_id': owner_agent_id,
        'actor_agent_id': owner_agent_id,
        'conversation_id': conversation_id or f'conv-{task_id}',
        'project': project,
        'priority': priority,
        'created_at': ts,
        'updated_at': ts,
        'attempt_count': 0,
        'max_attempts': max(1, int(max_attempts)),
        'result_summary': None,
        'scope': [s for s in (scope or []) if str(s).strip()],
        'deps': [d for d in (deps or []) if str(d).strip()],
        'correlation_id': correlation_id or task_id,
        'constraints': [c for c in (constraints or []) if str(c).strip()],
        'expected_outputs': [o for o in (expected_outputs or []) if str(o).strip()],
        'phase_plan': [p for p in (phase_plan or []) if isinstance(p, dict)],
        'shade_phase': shade_phase or None,
        'interval_minutes': interval_minutes if interval_minutes else None,
        'not_before': not_before,
        'boundary': {
            'status': 'unclaimed',
            'lease_owner': owner_agent_id,
            'lease_expires_at': None,
            'overlap_with': [],
        },
    }
    queue.append(task)
    save_queue(state_dir, queue)
    if _use_store():
        try:
            db = _get_db(state_dir)
            if not _db_task_get(db, task['id']):
                _db_task_insert(db, dict(task))
        except Exception:
            pass
    return task


def enqueue_user_intent_task(
    state_dir: Path,
    *,
    actor_agent_id: str,
    message: str,
    project: str | None = None,
    session_id: str | None = None,
    conversation_id: str | None = None,
    correlation_id: str | None = None,
) -> dict:
    queue = load_queue(state_dir)
    task_id = _task_id(prefix='intent')
    ts = _now_iso()
    task = {
        'id': task_id,
        'title': f'user_intent:{actor_agent_id}',
        'instruction': message,
        'message': message,
        'status': 'pending',
        'task_type': 'user_intent',
        'owner_agent_id': actor_agent_id,
        'actor_agent_id': actor_agent_id,
        'project': project,
        'session_id': session_id or f'session-{actor_agent_id}',
        'conversation_id': conversation_id or f'conv-{task_id}',
        'correlation_id': correlation_id or task_id,
        'created_at': ts,
        'updated_at': ts,
        'attempt_count': 0,
        'max_attempts': 1,
    }
    queue.append(task)
    save_queue(state_dir, queue)
    if _use_store():
        try:
            db = _get_db(state_dir)
            if not _db_task_get(db, task['id']):
                _db_task_insert(db, dict(task))
        except Exception:
            pass
    return task


def enqueue_boundary_proposal_task(
    state_dir: Path,
    *,
    proposer_agent_id: str,
    target_agent_id: str,
    project: str,
    scope: list[str],
    reason: str,
    conversation_id: str | None = None,
    correlation_id: str | None = None,
) -> dict:
    queue = load_queue(state_dir)
    task_id = _task_id(prefix='bnd')
    ts = _now_iso()
    task = {
        'id': task_id,
        'title': f'boundary_proposal:{proposer_agent_id}->{target_agent_id}',
        'status': 'pending',
        'task_type': 'boundary_proposal',
        'actor_agent_id': proposer_agent_id,
        'target_agent_id': target_agent_id,
        'project': project,
        'scope': [s for s in (scope or []) if str(s).strip()],
        'reason': reason,
        'conversation_id': conversation_id or f'conv-{task_id}',
        'correlation_id': correlation_id or task_id,
        'created_at': ts,
        'updated_at': ts,
        'attempt_count': 0,
        'max_attempts': 2,
    }
    queue.append(task)
    save_queue(state_dir, queue)
    if _use_store():
        try:
            db = _get_db(state_dir)
            if not _db_task_get(db, task['id']):
                _db_task_insert(db, dict(task))
        except Exception:
            pass
    return task


def enqueue_boundary_resolution_task(
    state_dir: Path,
    *,
    resolver_agent_id: str,
    proposal_id: str,
    decision: str,
    reason: str = '',
    conversation_id: str | None = None,
    correlation_id: str | None = None,
) -> dict:
    queue = load_queue(state_dir)
    task_id = _task_id(prefix='bndres')
    ts = _now_iso()
    task = {
        'id': task_id,
        'title': f'boundary_resolution:{proposal_id}',
        'status': 'pending',
        'task_type': 'boundary_resolution',
        'actor_agent_id': resolver_agent_id,
        'proposal_id': proposal_id,
        'decision': decision,
        'reason': reason,
        'conversation_id': conversation_id or f'conv-{task_id}',
        'correlation_id': correlation_id or task_id,
        'created_at': ts,
        'updated_at': ts,
        'attempt_count': 0,
        'max_attempts': 2,
    }
    queue.append(task)
    save_queue(state_dir, queue)
    if _use_store():
        try:
            db = _get_db(state_dir)
            if not _db_task_get(db, task['id']):
                _db_task_insert(db, dict(task))
        except Exception:
            pass
    return task


def load_conversation_index(state_dir: Path) -> dict:
    path = _index_path(state_dir)
    if not path.exists():
        return {'conversations': {}}
    try:
        data = json.loads(path.read_text())
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {'conversations': {}}


def list_conversations(state_dir: Path) -> list[dict]:
    idx = load_conversation_index(state_dir)
    rows: list[dict] = []
    for cid, meta in (idx.get('conversations') or {}).items():
        if not isinstance(meta, dict):
            continue
        rows.append({
            'conversation_id': cid,
            'message_count': int(meta.get('message_count') or 0),
            'last_message_id': meta.get('last_message_id'),
            'agents': list(meta.get('agents') or []),
        })
    rows.sort(key=lambda x: (-x['message_count'], x['conversation_id']))
    return rows


__all__ = [
    'load_queue',
    'save_queue',
    'enqueue_agent_message_task',
    'enqueue_agent_intervention_task',
    'enqueue_agent_task',
    'enqueue_user_intent_task',
    'enqueue_boundary_proposal_task',
    'enqueue_boundary_resolution_task',
    'load_conversation_index',
    'list_conversations',
]
