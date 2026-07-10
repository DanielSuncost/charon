#!/usr/bin/env python3
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _empty_index() -> dict:
    return {
        'schema_version': '1.0',
        'updated_at': _now_iso(),
        'conversations': {},
        'messages': {},
        'children': {},
        'interventions_by_target': {},
    }


def load_index(index_path: Path) -> dict:
    if not index_path.exists():
        return _empty_index()
    try:
        data = json.loads(index_path.read_text())
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return _empty_index()


def save_index(index_path: Path, index: dict) -> None:
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index['updated_at'] = _now_iso()
    index_path.write_text(json.dumps(index, indent=2))


def _load_events(log_path: Path) -> list[dict]:
    if not log_path.exists():
        return []
    out: list[dict] = []
    for line in log_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if isinstance(obj, dict) and obj.get('message_id'):
            out.append(obj)
    return out


def rebuild_index(log_path: Path, index_path: Path | None = None) -> dict:
    index = _empty_index()
    events = _load_events(log_path)

    for e in events:
        conv_id = e.get('conversation_id')
        message_id = e.get('message_id')
        if not conv_id or not message_id:
            continue

        conv = index['conversations'].setdefault(conv_id, {
            'message_count': 0,
            'last_message_id': None,
            'agents': [],
        })
        conv['message_count'] += 1
        conv['last_message_id'] = message_id

        actor = e.get('actor_id')
        if actor and actor not in conv['agents']:
            conv['agents'].append(actor)

        index['messages'][message_id] = {
            'conversation_id': conv_id,
            'message_id': message_id,
            'parent_message_id': e.get('parent_message_id'),
            'intervention_of_message_id': e.get('intervention_of_message_id'),
            'event_type': e.get('event_type'),
            'actor_id': actor,
            'ts': e.get('ts'),
        }

        parent = e.get('parent_message_id')
        if parent:
            kids = index['children'].setdefault(parent, [])
            if message_id not in kids:
                kids.append(message_id)

        target = e.get('intervention_of_message_id')
        if target:
            refs = index['interventions_by_target'].setdefault(target, [])
            if message_id not in refs:
                refs.append(message_id)

    if index_path is not None:
        save_index(index_path, index)
    return index


def get_path(index: dict, *, conversation_id: str, message_id: str) -> list[str]:
    messages = index.get('messages', {})
    if message_id not in messages:
        return []
    if messages[message_id].get('conversation_id') != conversation_id:
        return []

    path: list[str] = []
    cur = message_id
    seen: set[str] = set()
    while cur and cur not in seen:
        node = messages.get(cur)
        if not node or node.get('conversation_id') != conversation_id:
            break
        seen.add(cur)
        path.append(cur)
        cur = node.get('parent_message_id')

    path.reverse()
    return path


__all__ = [
    'load_index',
    'save_index',
    'rebuild_index',
    'get_path',
]
