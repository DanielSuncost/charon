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
        boundary_insert as _db_boundary_insert,
        boundary_get as _db_boundary_get,
        boundary_list as _db_boundary_list,
        boundary_update as _db_boundary_update,
        boundary_pending_for_agent as _db_boundary_pending,
    )
    _HAS_STORE = True
except ImportError:
    _HAS_STORE = False


def _use_store() -> bool:
    return _HAS_STORE and os.environ.get('CHARON_NO_SQLITE', '0') != '1'


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _boundary_id() -> str:
    return f"bnd-{uuid.uuid4().hex[:10]}"


def _path(state_dir: Path) -> Path:
    return state_dir / 'boundaries.json'


def load_boundaries(state_dir: Path) -> list[dict]:
    if _use_store():
        try:
            return _db_boundary_list(_get_db(state_dir))
        except Exception:
            pass
    p = _path(state_dir)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text())
        if isinstance(data, list):
            return data
    except Exception:
        pass
    return []


def save_boundaries(state_dir: Path, boundaries: list[dict]) -> None:
    # Always write JSON as backup
    p = _path(state_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(boundaries, indent=2))


def create_proposal(
    state_dir: Path,
    *,
    proposer_agent_id: str,
    target_agent_id: str,
    project: str,
    scope: list[str],
    reason: str,
    source_task_id: str | None = None,
    correlation_id: str | None = None,
) -> dict:
    boundaries = load_boundaries(state_dir)
    rec = {
        'id': _boundary_id(),
        'status': 'proposed',
        'proposer_agent_id': proposer_agent_id,
        'target_agent_id': target_agent_id,
        'project': project,
        'scope': [s for s in (scope or []) if str(s).strip()],
        'reason': reason,
        'source_task_id': source_task_id,
        'correlation_id': correlation_id,
        'created_at': _now_iso(),
        'updated_at': _now_iso(),
        'resolved_at': None,
        'resolved_by': None,
        'resolution_reason': '',
    }
    boundaries.append(rec)
    save_boundaries(state_dir, boundaries)
    if _use_store():
        try:
            db = _get_db(state_dir)
            if not _db_boundary_get(db, rec['id']):
                _db_boundary_insert(db, dict(rec))
        except Exception:
            pass
    return rec


def resolve_proposal(
    state_dir: Path,
    *,
    proposal_id: str,
    resolver_agent_id: str,
    decision: str,
    reason: str = '',
) -> dict | None:
    decision = str(decision or '').lower().strip()
    if decision not in ('accept', 'reject'):
        raise ValueError('decision must be accept|reject')

    boundaries = load_boundaries(state_dir)
    for rec in boundaries:
        if rec.get('id') != proposal_id:
            continue
        rec['status'] = 'accepted' if decision == 'accept' else 'rejected'
        rec['resolved_at'] = _now_iso()
        rec['resolved_by'] = resolver_agent_id
        rec['resolution_reason'] = reason
        rec['updated_at'] = _now_iso()
        save_boundaries(state_dir, boundaries)
        if _use_store():
            try:
                _db_boundary_update(_get_db(state_dir), rec['id'],
                                    status=rec['status'], resolved_at=rec['resolved_at'],
                                    resolved_by=resolver_agent_id, resolution_reason=reason)
            except Exception:
                pass
        return rec
    return None


def pending_for_agent(state_dir: Path, agent_id: str) -> list[dict]:
    return [
        b for b in load_boundaries(state_dir)
        if b.get('target_agent_id') == agent_id and b.get('status') == 'proposed'
    ]
