#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from charon.infra.project_registry_loader import load_ensure_project

_ensure_project = load_ensure_project(__file__, 'goal_runtime')

# SQLite store adapter (optional)
try:
    from charon.infra.store_adapter import (
        get_db as _get_db,
        goal_project_upsert as _db_project_upsert,
        goal_project_get as _db_project_get,  # noqa: F401 — availability probe: full adapter API must import
        goal_session_upsert as _db_session_upsert,
        goal_session_get as _db_session_get,  # noqa: F401 — availability probe
        goal_context_packet_upsert as _db_context_packet_upsert,
        goal_context_packet_get as _db_context_packet_get,
    )
    _HAS_STORE = True
except ImportError:
    _HAS_STORE = False


def _use_store() -> bool:
    return _HAS_STORE and os.environ.get('CHARON_NO_SQLITE', '0') != '1'


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_id(text: str, prefix: str) -> str:
    raw = ''.join(ch.lower() if ch.isalnum() else '-' for ch in str(text or ''))
    raw = '-'.join([part for part in raw.split('-') if part])
    if not raw:
        raw = f"{prefix}-{uuid.uuid4().hex[:8]}"
    return raw[:96]


def _goals_root(state_dir: Path) -> Path:
    return state_dir / 'goals'


def _project_path(state_dir: Path, project_id: str) -> Path:
    project_doc_path = state_dir / 'projects' / project_id / 'goals.json'
    if project_doc_path.parent.exists() or (state_dir / 'projects').exists():
        return project_doc_path
    return _goals_root(state_dir) / 'projects' / f'{project_id}.json'


def _session_path(state_dir: Path, session_id: str) -> Path:
    return _goals_root(state_dir) / 'sessions' / f'{session_id}.json'


def _context_packet_path(state_dir: Path, agent_id: str) -> Path:
    return state_dir / 'context_packets' / f'{agent_id}.json'


def _read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        data = json.loads(path.read_text())
        return data
    except Exception:
        return default


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))


def _default_project_doc(project_id: str) -> dict:
    return {
        'project_id': project_id,
        'goals': [],
        'created_at': _now_iso(),
        'updated_at': _now_iso(),
    }


def _default_session_doc(session_id: str, project_id: str, agent_id: str) -> dict:
    return {
        'session_id': session_id,
        'project_id': project_id,
        'agent_id': agent_id,
        'goals': [],
        'active_goal_id': None,
        'created_at': _now_iso(),
        'updated_at': _now_iso(),
    }


def _goal_node(*, title: str, project_id: str, session_id: str, conversation_id: str, parent_goal_id: str | None = None, intent_type: str = 'user_intent') -> dict:
    gid = f"goal-{uuid.uuid4().hex[:10]}"
    return {
        'goal_id': gid,
        'parent_goal_id': parent_goal_id,
        'title': str(title or '').strip()[:240],
        'intent_type': intent_type,
        'constraints': [],
        'acceptance_criteria': [],
        'status': 'active',
        'priority': 'normal',
        'linked_tasks': [],
        'linked_messages': [],
        'evidence': [],
        'project_id': project_id,
        'session_id': session_id,
        'conversation_id': conversation_id,
        'created_at': _now_iso(),
        'updated_at': _now_iso(),
    }


def ingest_user_intent(
    state_dir: Path,
    *,
    agent_id: str,
    project: str,
    session_id: str,
    conversation_id: str,
    text: str,
) -> dict:
    project_doc = _ensure_project(state_dir, Path(project or '.'))
    project_id = str(project_doc.get('id') or _safe_id(project or 'default-project', 'project'))
    session_id = _safe_id(session_id or f'session-{agent_id}', 'session')

    project_path = _project_path(state_dir, project_id)
    session_path = _session_path(state_dir, session_id)

    proj = _read_json(project_path, _default_project_doc(project_id))
    ses = _read_json(session_path, _default_session_doc(session_id, project_id, agent_id))

    parent_goal_id = ses.get('active_goal_id')
    goal = _goal_node(
        title=text,
        project_id=project_id,
        session_id=session_id,
        conversation_id=conversation_id,
        parent_goal_id=parent_goal_id,
    )

    proj_goals = list(proj.get('goals') or [])
    ses_goals = list(ses.get('goals') or [])
    proj_goals.append(goal)
    ses_goals.append(goal)

    proj['goals'] = proj_goals[-500:]
    ses['goals'] = ses_goals[-500:]
    ses['active_goal_id'] = goal.get('goal_id')
    proj['updated_at'] = _now_iso()
    ses['updated_at'] = _now_iso()

    _write_json(project_path, proj)
    _write_json(session_path, ses)

    if _use_store():
        try:
            db = _get_db(state_dir)
            _db_project_upsert(db, project_id, proj)
            _db_session_upsert(db, session_id, project_id, ses)
        except Exception:
            pass

    return {
        'goal': goal,
        'project_id': project_id,
        'session_id': session_id,
    }


def ingest_idea(
    state_dir: Path,
    *,
    agent_id: str,
    project: str,
    text: str,
    priority: str = 'normal',
) -> dict:
    """Capture a quick idea/feature into the goal backlog.

    Unlike ingest_user_intent, this does NOT create a task or set the
    idea as the active goal. It just stores it in the project's goal
    list with status='backlog' for later prioritization.
    """
    project_doc = _ensure_project(state_dir, Path(project or '.'))
    project_id = str(project_doc.get('id') or _safe_id(project or 'default-project', 'project'))
    session_id = _safe_id(f'ideas-{agent_id}', 'session')

    project_path = _project_path(state_dir, project_id)
    session_path = _session_path(state_dir, session_id)

    proj = _read_json(project_path, _default_project_doc(project_id))
    ses = _read_json(session_path, _default_session_doc(session_id, project_id, agent_id))

    goal = _goal_node(
        title=text,
        project_id=project_id,
        session_id=session_id,
        conversation_id=f'ideas-{project_id}',
        intent_type='idea',
    )
    goal['status'] = 'backlog'
    goal['priority'] = priority

    proj_goals = list(proj.get('goals') or [])
    ses_goals = list(ses.get('goals') or [])
    proj_goals.append(goal)
    ses_goals.append(goal)

    proj['goals'] = proj_goals[-500:]
    ses['goals'] = ses_goals[-500:]
    proj['updated_at'] = _now_iso()
    ses['updated_at'] = _now_iso()

    _write_json(project_path, proj)
    _write_json(session_path, ses)

    if _use_store():
        try:
            db = _get_db(state_dir)
            _db_project_upsert(db, project_id, proj)
            _db_session_upsert(db, session_id, project_id, ses)
        except Exception:
            pass

    return {
        'goal': goal,
        'project_id': project_id,
        'session_id': session_id,
    }


def list_goals(
    state_dir: Path,
    *,
    project: str,
    status: str | None = None,
) -> list[dict]:
    """List goals for a project, optionally filtered by status.

    status: 'active', 'backlog', 'blocked', 'completed', or None for all.
    """
    project_doc = _ensure_project(state_dir, Path(project or '.'))
    project_id = str(project_doc.get('id') or _safe_id(project or 'default-project', 'project'))
    proj = _read_json(_project_path(state_dir, project_id), _default_project_doc(project_id))
    goals = [g for g in (proj.get('goals') or []) if isinstance(g, dict)]
    if status:
        goals = [g for g in goals if g.get('status') == status]
    return goals


def promote_idea(
    state_dir: Path,
    *,
    project: str,
    goal_id: str,
) -> dict | None:
    """Move a backlog idea to active status."""
    project_doc = _ensure_project(state_dir, Path(project or '.'))
    project_id = str(project_doc.get('id') or _safe_id(project or 'default-project', 'project'))
    ppath = _project_path(state_dir, project_id)
    proj = _read_json(ppath, _default_project_doc(project_id))

    found = None
    for g in (proj.get('goals') or []):
        if isinstance(g, dict) and g.get('goal_id') == goal_id:
            g['status'] = 'active'
            g['updated_at'] = _now_iso()
            found = g
            break

    if found:
        proj['updated_at'] = _now_iso()
        _write_json(ppath, proj)
        if _use_store():
            try:
                _db_project_upsert(_get_db(state_dir), project_id, proj)
            except Exception:
                pass

    return found


def _update_goal_doc_goal_list(goals: list[dict], goal_id: str, updater) -> list[dict]:
    out = []
    for g in goals:
        if isinstance(g, dict) and g.get('goal_id') == goal_id:
            rec = dict(g)
            updater(rec)
            rec['updated_at'] = _now_iso()
            out.append(rec)
        else:
            out.append(g)
    return out


def attach_task(
    state_dir: Path,
    *,
    project_id: str,
    session_id: str,
    goal_id: str,
    task_id: str,
) -> None:
    ppath = _project_path(state_dir, project_id)
    spath = _session_path(state_dir, session_id)
    proj = _read_json(ppath, _default_project_doc(project_id))
    ses = _read_json(spath, _default_session_doc(session_id, project_id, ''))

    def _attach(rec: dict):
        links = list(rec.get('linked_tasks') or [])
        if task_id not in links:
            links.append(task_id)
        rec['linked_tasks'] = links[-100:]

    proj['goals'] = _update_goal_doc_goal_list(list(proj.get('goals') or []), goal_id, _attach)
    ses['goals'] = _update_goal_doc_goal_list(list(ses.get('goals') or []), goal_id, _attach)
    proj['updated_at'] = _now_iso()
    ses['updated_at'] = _now_iso()
    _write_json(ppath, proj)
    _write_json(spath, ses)
    if _use_store():
        try:
            db = _get_db(state_dir)
            _db_project_upsert(db, project_id, proj)
            _db_session_upsert(db, session_id, project_id, ses)
        except Exception:
            pass


def record_result(
    state_dir: Path,
    *,
    project_id: str,
    session_id: str,
    goal_id: str,
    summary: str,
    status: str,
) -> None:
    ppath = _project_path(state_dir, project_id)
    spath = _session_path(state_dir, session_id)
    proj = _read_json(ppath, _default_project_doc(project_id))
    ses = _read_json(spath, _default_session_doc(session_id, project_id, ''))

    def _apply(rec: dict):
        rec['status'] = status
        evidence = list(rec.get('evidence') or [])
        evidence.append({'ts': _now_iso(), 'summary': str(summary or '')[:600]})
        rec['evidence'] = evidence[-30:]

    proj['goals'] = _update_goal_doc_goal_list(list(proj.get('goals') or []), goal_id, _apply)
    ses['goals'] = _update_goal_doc_goal_list(list(ses.get('goals') or []), goal_id, _apply)
    if status in ('completed', 'failed') and ses.get('active_goal_id') == goal_id:
        ses['active_goal_id'] = None
    proj['updated_at'] = _now_iso()
    ses['updated_at'] = _now_iso()
    _write_json(ppath, proj)
    _write_json(spath, ses)
    if _use_store():
        try:
            db = _get_db(state_dir)
            _db_project_upsert(db, project_id, proj)
            _db_session_upsert(db, session_id, project_id, ses)
        except Exception:
            pass


def build_context_packet(
    state_dir: Path,
    *,
    agent_id: str,
    project_id: str,
    session_id: str,
) -> dict:
    proj = _read_json(_project_path(state_dir, project_id), _default_project_doc(project_id))
    ses = _read_json(_session_path(state_dir, session_id), _default_session_doc(session_id, project_id, agent_id))

    session_goals = [g for g in (ses.get('goals') or []) if isinstance(g, dict)]
    active = [g for g in session_goals if g.get('status') == 'active']
    blocked = [g for g in session_goals if g.get('status') == 'blocked']
    recent = sorted(session_goals, key=lambda g: g.get('updated_at') or '')[-5:]

    packet = {
        'agent_id': agent_id,
        'project_id': project_id,
        'session_id': session_id,
        'active_goal_id': ses.get('active_goal_id'),
        'active_goals': [
            {
                'goal_id': g.get('goal_id'),
                'title': g.get('title'),
                'linked_tasks': list(g.get('linked_tasks') or [])[-3:],
            }
            for g in active[-5:]
        ],
        'blocked_goals': [
            {
                'goal_id': g.get('goal_id'),
                'title': g.get('title'),
            }
            for g in blocked[-5:]
        ],
        'recent_goal_updates': [
            {
                'goal_id': g.get('goal_id'),
                'status': g.get('status'),
                'title': g.get('title'),
                'updated_at': g.get('updated_at'),
            }
            for g in recent
        ],
        'goal_count_project': len(list(proj.get('goals') or [])),
        'goal_count_session': len(session_goals),
        'updated_at': _now_iso(),
    }

    _write_json(_context_packet_path(state_dir, agent_id), packet)
    if _use_store():
        try:
            _db_context_packet_upsert(_get_db(state_dir), agent_id, packet)
        except Exception:
            pass
    return packet


def load_context_packet(state_dir: Path, agent_id: str) -> dict:
    if _use_store():
        try:
            p = _db_context_packet_get(_get_db(state_dir), agent_id)
            if p:
                return p
        except Exception:
            pass
    return _read_json(_context_packet_path(state_dir, agent_id), {})


def show_goals(state_dir: Path, *, session_id: str | None = None, project_id: str | None = None) -> dict:
    if session_id:
        sid = _safe_id(session_id, 'session')
        return _read_json(_session_path(state_dir, sid), _default_session_doc(sid, 'default-project', ''))
    if project_id:
        pid = _safe_id(project_id, 'project')
        return _read_json(_project_path(state_dir, pid), _default_project_doc(pid))
    return {'error': 'session_id or project_id required'}


__all__ = [
    'ingest_user_intent',
    'ingest_idea',
    'list_goals',
    'promote_idea',
    'attach_task',
    'record_result',
    'build_context_packet',
    'load_context_packet',
    'show_goals',
]
