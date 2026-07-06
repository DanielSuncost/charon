#!/usr/bin/env python3
from __future__ import annotations
import json
import os
from datetime import datetime, timezone

import subprocess
import importlib.util
import sys
import shutil
import re
from pathlib import Path

# SQLite store adapter (optional — gracefully degrades to JSON)
try:
    from store_adapter import get_db as _get_db, agent_insert as _db_agent_insert, agent_get as _db_agent_get, agent_list as _db_agent_list, agent_update as _db_agent_update
    _HAS_STORE = True
except ImportError:
    _HAS_STORE = False


def _tmux_available() -> bool:
    return bool(shutil.which('tmux'))


def _ensure_tmux_session(name: str, command: str | None = None) -> tuple[bool, str | None]:
    if not _tmux_available():
        return False, 'tmux binary not found on PATH'

    try:
        existing = subprocess.run(['tmux', 'has-session', '-t', name], capture_output=True)
        if existing.returncode == 0:
            return True, None
    except Exception as e:
        return False, f'tmux has-session failed: {e}'

    cmd = ['tmux', 'new-session', '-d', '-s', name]
    if command:
        cmd.extend([command])
    try:
        created = subprocess.run(cmd, capture_output=True)
        if created.returncode != 0:
            stderr = (created.stderr or b'').decode('utf-8', errors='ignore').strip()
            return False, f'tmux new-session failed (exit {created.returncode}): {stderr or "unknown error"}'
        return True, None
    except Exception as e:
        return False, f'tmux new-session failed: {e}'


def _slug(text: str) -> str:
    value = re.sub(r'[^a-z0-9]+', '-', (text or '').lower()).strip('-')
    return value or 'project'


def _project_suffix(project: str | None) -> str:
    if not project:
        return 'general'
    try:
        return _slug(Path(project).name)
    except Exception:
        return 'general'


def _next_charon_name(agents: list[dict], project: str | None) -> str:
    suffix = _project_suffix(project)
    # Avoid stutter: charon-charon-01 → charon-01
    if suffix == 'charon':
        prefix = 'charon-'
    else:
        prefix = f'charon-{suffix}-'
    max_n = 0
    for agent in agents:
        name = str(agent.get('name') or '')
        if not name.startswith(prefix):
            continue
        tail = name[len(prefix):]
        if tail.isdigit():
            max_n = max(max_n, int(tail))
    return f'{prefix}{max_n + 1:02d}'


ROOT = Path(__file__).resolve().parents[2]
STATE_DIR = ROOT / '.charon_state'
AGENTS_FILE = STATE_DIR / 'agents.json'
INTERVENTIONS_FILE = STATE_DIR / 'interventions.jsonl'

_THIS_DIR = Path(__file__).resolve().parent
_INTERVENTION_GRAPH_PATH = _THIS_DIR / 'intervention_graph.py'
_spec = importlib.util.spec_from_file_location('charon_intervention_graph', _INTERVENTION_GRAPH_PATH)
intervention_graph = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = intervention_graph
_spec.loader.exec_module(intervention_graph)


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _use_store() -> bool:
    """Check if SQLite store should be used."""
    return _HAS_STORE and os.environ.get('CHARON_NO_SQLITE', '0') != '1'


def load_agents(state_dir: Path | None = None) -> list[dict]:
    sd = state_dir or STATE_DIR
    if _use_store():
        try:
            db = _get_db(sd)
            return _db_agent_list(db)
        except Exception:
            pass
    af = (sd / 'agents.json') if state_dir else AGENTS_FILE
    if not af.exists():
        return []
    try:
        return json.loads(af.read_text())
    except Exception:
        return []


def save_agents(agents: list[dict], state_dir: Path | None = None) -> None:
    sd = state_dir or STATE_DIR
    sd.mkdir(parents=True, exist_ok=True)
    # Always write JSON as backup/export
    af = (sd / 'agents.json') if state_dir else AGENTS_FILE
    af.write_text(json.dumps(agents, indent=2))


def _slugify_id(text: str) -> str:
    """Turn user input into a valid agent ID slug."""
    value = re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')
    return value[:40] if value else ''


def _existing_ids(agents: list[dict]) -> set[str]:
    """Collect all agent IDs (case-insensitive) from the list."""
    return {a.get('id', '').lower() for a in agents if a.get('id')}


def next_id(agents: list[dict], *, custom_id: str | None = None) -> str:
    """Generate or validate an agent ID. Enforces uniqueness.

    If custom_id is provided, slugifies it and checks for collisions.
    Otherwise, finds the highest AG-NNNN and increments.
    """
    existing = _existing_ids(agents)

    if custom_id:
        slug = _slugify_id(custom_id)
        if not slug:
            raise ValueError(f'invalid agent ID: {custom_id!r}')
        if slug.lower() in existing:
            raise ValueError(f'agent ID already exists: {slug}')
        return slug

    # Auto-generate: find max AG-NNNN and increment
    max_n = 0
    for aid in existing:
        if aid.startswith('ag-') and aid[3:].isdigit():
            max_n = max(max_n, int(aid[3:]))
    return f"AG-{max_n + 1:04d}"


def create_agent(
    name: str | None,
    mode: str,
    goal: str,
    project: str | None = None,
    *,
    agent_id: str | None = None,
    role: str = 'charon',
    visibility: str = 'user',
    parent_agent_id: str | None = None,
    require_tmux: bool | None = None,
    specialization: str = '',
    charter: str = '',
) -> dict:
    agents = load_agents()
    agent_id = next_id(agents, custom_id=agent_id)

    if require_tmux is None:
        require_tmux = os.environ.get('CHARON_REQUIRE_TMUX', '1') == '1'

    final_name = (name or '').strip()
    if not final_name and role == 'charon':
        final_name = _next_charon_name(agents, project)
    if not final_name:
        final_name = f'{role}-{agent_id.lower()}'

    tmux_session = f"charon-{agent_id}" if require_tmux else None
    if require_tmux:
        ok, err = _ensure_tmux_session(tmux_session)
        if not ok:
            raise RuntimeError(f'failed to create tmux session {tmux_session}: {err}')

    a = {
        'id': agent_id,
        'name': final_name,
        'mode': mode,  # temp | persistent
        'goal': goal,
        'project': project or str(ROOT),
        'status': 'running',
        'created_at': now(),
        'last_active': now(),
        'tmux_session': tmux_session,
        'role': role,
        'visibility': visibility,
        'parent_agent_id': parent_agent_id,
    }
    if specialization.strip():
        # User-assigned specialization is authoritative: lock it so the
        # soft-specialization auto-labeler never overwrites it.
        a['specialization'] = specialization.strip()
        a['specialization_locked'] = True
    if charter.strip():
        a['charter'] = charter.strip()
    agents.append(a)
    save_agents(agents)
    # Also write to SQLite store
    if _use_store():
        try:
            db = _get_db(STATE_DIR)
            if not _db_agent_get(db, agent_id):
                _db_agent_insert(db, dict(a))
        except Exception:
            pass
    return a


def list_agents() -> list[dict]:
    return load_agents()


def assign_specialization(agent_id: str, specialization: str,
                          charter: str | None = None) -> dict | None:
    """Give an existing agent a user-assigned specialization (and optional role
    charter). Locks the specialization so soft_specialization's auto-derived
    labels never overwrite it. Pass specialization='' to clear and unlock."""
    agents = load_agents()
    for a in agents:
        if a.get('id') == agent_id:
            spec = (specialization or '').strip()
            if spec:
                a['specialization'] = spec
                a['specialization_locked'] = True
            else:
                a.pop('specialization', None)
                a.pop('specialization_locked', None)
            if charter is not None:
                if charter.strip():
                    a['charter'] = charter.strip()
                else:
                    a.pop('charter', None)
            a['last_active'] = now()
            save_agents(agents)
            if _use_store():
                try:
                    db = _get_db(STATE_DIR)
                    _db_agent_update(
                        db, agent_id,
                        specialization=a.get('specialization', ''),
                        specialization_locked=a.get('specialization_locked', False),
                        charter=a.get('charter', ''),
                    )
                except Exception:
                    pass
            return a
    return None


def set_status(agent_id: str, status: str) -> dict | None:
    agents = load_agents()
    for a in agents:
        if a.get('id') == agent_id:
            a['status'] = status
            a['last_active'] = now()
            save_agents(agents)
            if _use_store():
                try:
                    db = _get_db(STATE_DIR)
                    _db_agent_update(db, agent_id, status=status)
                except Exception:
                    pass
            return a
    return None


def post_agent_message(
    agent_id: str,
    conversation_id: str,
    content: str,
    *,
    parent_message_id: str | None = None,
    branch_label: str | None = None,
) -> dict:
    return intervention_graph.append_message(
        INTERVENTIONS_FILE,
        conversation_id=conversation_id,
        actor_agent_id=agent_id,
        content=content,
        parent_message_id=parent_message_id,
        branch_label=branch_label,
    )


def intervene(
    agent_id: str,
    conversation_id: str,
    content: str,
    *,
    intervention_of_message_id: str,
    parent_message_id: str | None = None,
) -> dict:
    return intervention_graph.append_intervention(
        INTERVENTIONS_FILE,
        conversation_id=conversation_id,
        actor_agent_id=agent_id,
        content=content,
        intervention_of_message_id=intervention_of_message_id,
        parent_message_id=parent_message_id,
    )


def backtrack(conversation_id: str, message_id: str) -> list[dict]:
    return intervention_graph.reconstruct_path(
        INTERVENTIONS_FILE,
        conversation_id=conversation_id,
        message_id=message_id,
    )
