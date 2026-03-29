from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path
from typing import Any


def _rooms_dir(state_dir: Path) -> Path:
    return state_dir / 'rooms'


def _room_dir(state_dir: Path, room_id: str) -> Path:
    return _rooms_dir(state_dir) / room_id


def _room_path(state_dir: Path, room_id: str) -> Path:
    return _room_dir(state_dir, room_id) / 'room.json'


def _events_path(state_dir: Path, room_id: str) -> Path:
    return _room_dir(state_dir, room_id) / 'events.jsonl'


def _now_iso() -> str:
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def slugify(text: str) -> str:
    slug = re.sub(r'[^a-zA-Z0-9._-]+', '-', str(text or '').strip()).strip('-_.').lower()
    return slug[:80] or 'room'


def create_room(state_dir: Path, *, kind: str, title: str, project: str = '', participants: list[dict[str, Any]] | None = None, meta: dict[str, Any] | None = None) -> dict[str, Any]:
    room_id = f"{kind}-{slugify(title)}-{uuid.uuid4().hex[:6]}"
    room = {
        'id': room_id,
        'kind': kind,
        'title': title,
        'project': project,
        'status': 'active',
        'participants': participants or [],
        'meta': meta or {},
        'created_at': _now_iso(),
        'updated_at': _now_iso(),
        'last_activity': _now_iso(),
        'summary': '',
    }
    p = _room_path(state_dir, room_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(room, indent=2, ensure_ascii=False))
    append_event(state_dir, room_id, {
        'type': 'room_created',
        'title': title,
        'kind': kind,
        'participants': participants or [],
    })
    return load_room(state_dir, room_id) or room


def load_room(state_dir: Path, room_id: str) -> dict[str, Any] | None:
    p = _room_path(state_dir, room_id)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def update_room(state_dir: Path, room_id: str, **fields: Any) -> dict[str, Any] | None:
    room = load_room(state_dir, room_id)
    if not room:
        return None
    room.update(fields)
    room['updated_at'] = _now_iso()
    if 'last_activity' not in fields:
        room['last_activity'] = _now_iso()
    p = _room_path(state_dir, room_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(room, indent=2, ensure_ascii=False))
    return room


def append_event(state_dir: Path, room_id: str, event: dict[str, Any]) -> None:
    e = dict(event or {})
    e.setdefault('ts', _now_iso())
    p = _events_path(state_dir, room_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open('a', encoding='utf-8') as f:
        f.write(json.dumps(e, ensure_ascii=False) + '\n')
    update_room(state_dir, room_id, last_activity=e['ts'])


def list_rooms(state_dir: Path, limit: int = 50) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    d = _rooms_dir(state_dir)
    if not d.exists():
        return out
    for room_file in sorted(d.glob('*/room.json')):
        try:
            data = json.loads(room_file.read_text())
        except Exception:
            continue
        if isinstance(data, dict):
            out.append(data)
    out.sort(key=lambda r: str(r.get('last_activity') or r.get('updated_at') or r.get('created_at') or ''), reverse=True)
    return out[:limit]


def list_events(state_dir: Path, room_id: str, limit: int = 200) -> list[dict[str, Any]]:
    p = _events_path(state_dir, room_id)
    if not p.exists():
        return []
    try:
        lines = p.read_text().splitlines()[-limit:]
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for line in lines:
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    return out
