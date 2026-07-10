from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_message_id() -> str:
    return f"msg-{uuid.uuid4().hex[:12]}"


def _new_event_id() -> str:
    return f"evt-{uuid.uuid4().hex[:12]}"


def load_events(log_path: Path) -> list[dict]:
    if not log_path.exists():
        return []
    out: list[dict] = []
    for line in log_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            continue
    return out


def _append_event(log_path: Path, event: dict) -> dict:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open('a') as f:
        f.write(json.dumps(event) + '\n')
    return event


def append_message(
    log_path: Path,
    *,
    conversation_id: str,
    actor_agent_id: str,
    content: str,
    parent_message_id: str | None = None,
    branch_label: str | None = None,
    correlation_id: str | None = None,
) -> dict:
    message_id = _new_message_id()
    event = {
        'schema_version': '1.0',
        'id': _new_event_id(),
        'ts': _utc_now_iso(),
        'event_type': 'agent_message',
        'actor_type': 'agent',
        'actor_id': actor_agent_id,
        'correlation_id': correlation_id or conversation_id,
        'causation_id': parent_message_id,
        'conversation_id': conversation_id,
        'message_id': message_id,
        'parent_message_id': parent_message_id,
        'intervention_of_message_id': None,
        'branch_label': branch_label,
        'payload': {'content': content},
        'signature': None,
    }
    return _append_event(log_path, event)


def append_intervention(
    log_path: Path,
    *,
    conversation_id: str,
    actor_agent_id: str,
    content: str,
    intervention_of_message_id: str,
    parent_message_id: str | None = None,
    correlation_id: str | None = None,
) -> dict:
    parent = parent_message_id or intervention_of_message_id
    message_id = _new_message_id()
    event = {
        'schema_version': '1.0',
        'id': _new_event_id(),
        'ts': _utc_now_iso(),
        'event_type': 'agent_intervention',
        'actor_type': 'agent',
        'actor_id': actor_agent_id,
        'correlation_id': correlation_id or conversation_id,
        'causation_id': intervention_of_message_id,
        'conversation_id': conversation_id,
        'message_id': message_id,
        'parent_message_id': parent,
        'intervention_of_message_id': intervention_of_message_id,
        'branch_label': 'intervention',
        'payload': {'content': content},
        'signature': None,
    }
    return _append_event(log_path, event)


def reconstruct_path(log_path: Path, *, conversation_id: str, message_id: str) -> list[dict]:
    events = [
        e
        for e in load_events(log_path)
        if e.get('conversation_id') == conversation_id and e.get('message_id')
    ]
    by_message = {e['message_id']: e for e in events}
    if message_id not in by_message:
        return []

    chain: list[dict] = []
    cursor = by_message[message_id]
    visited: set[str] = set()
    while cursor:
        mid = cursor.get('message_id')
        if not mid or mid in visited:
            break
        visited.add(mid)
        chain.append(cursor)
        parent_id = cursor.get('parent_message_id')
        cursor = by_message.get(parent_id) if parent_id else None

    chain.reverse()
    return chain
