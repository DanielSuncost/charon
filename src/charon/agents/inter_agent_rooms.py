from __future__ import annotations

import json
import re
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

try:
    from charon.infra.diagnostics import record as _diag
except Exception:  # diagnostics is best-effort and must never block import
    def _diag(*args, **kwargs):
        return None


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


def _normalize_room(room: dict[str, Any]) -> dict[str, Any]:
    item = dict(room or {})
    meta = item.get('meta') if isinstance(item.get('meta'), dict) else {}
    meta.setdefault('runner_state', {})
    meta.setdefault('pending_injections', [])
    item['meta'] = meta
    return item


def create_room(state_dir: Path, *, kind: str, title: str, project: str = '', participants: list[dict[str, Any]] | None = None, meta: dict[str, Any] | None = None) -> dict[str, Any]:
    room_id = f"{kind}-{slugify(title)}-{uuid.uuid4().hex[:6]}"
    room = _normalize_room({
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
    })
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
        return _normalize_room(data) if isinstance(data, dict) else None
    except Exception as e:
        _diag('inter_agent_rooms', 'room.json unreadable; room treated as missing', error=e, room_id=room_id)
        return None


def update_room(state_dir: Path, room_id: str, **fields: Any) -> dict[str, Any] | None:
    room = load_room(state_dir, room_id)
    if not room:
        return None
    room.update(fields)
    room = _normalize_room(room)
    room['updated_at'] = _now_iso()
    if 'last_activity' not in fields:
        room['last_activity'] = _now_iso()
    p = _room_path(state_dir, room_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(room, indent=2, ensure_ascii=False))
    return room


def update_room_meta(state_dir: Path, room_id: str, **meta_fields: Any) -> dict[str, Any] | None:
    room = load_room(state_dir, room_id)
    if not room:
        return None
    meta = dict(room.get('meta') or {})
    meta.update(meta_fields)
    return update_room(state_dir, room_id, meta=meta)


def save_runner_state(state_dir: Path, room_id: str, state: dict[str, Any]) -> dict[str, Any] | None:
    room = load_room(state_dir, room_id)
    if not room:
        return None
    meta = dict(room.get('meta') or {})
    current = meta.get('runner_state') if isinstance(meta.get('runner_state'), dict) else {}
    current.update(dict(state or {}))
    meta['runner_state'] = current
    return update_room(state_dir, room_id, meta=meta)


def load_runner_state(state_dir: Path, room_id: str) -> dict[str, Any]:
    room = load_room(state_dir, room_id) or {}
    meta = room.get('meta') if isinstance(room.get('meta'), dict) else {}
    state = meta.get('runner_state') if isinstance(meta.get('runner_state'), dict) else {}
    return dict(state)


def set_room_status(state_dir: Path, room_id: str, status: str, *, summary: str | None = None, append_status_event: bool = False, reason: str = '') -> dict[str, Any] | None:
    room = update_room(state_dir, room_id, status=status, summary=(summary if summary is not None else load_room(state_dir, room_id).get('summary', '')))
    if room and append_status_event:
        event = {'type': 'room_status_changed', 'status': status}
        if reason:
            event['reason'] = reason
        append_event(state_dir, room_id, event)
    return room


def _participant_delivery_key(participant: dict[str, Any] | None, speaker_role: str = '') -> str:
    if isinstance(participant, dict):
        for key in ('id', 'session', 'name', 'role'):
            val = str(participant.get(key) or '').strip().lower()
            if val:
                return val
    return str(speaker_role or '').strip().lower() or 'participant'


def _target_matches(target: str, speaker_role: str, participant: dict[str, Any] | None) -> bool:
    t = str(target or 'whole').strip().lower()
    if t in ('', 'whole', 'room', 'all', '*'):
        return True
    role = str(speaker_role or '').strip().lower()
    if t == role:
        return True
    if not isinstance(participant, dict):
        return False
    for key in ('id', 'name', 'session', 'role'):
        val = str(participant.get(key) or '').strip().lower()
        if val and (t == val or t in val):
            return True
    return False


def queue_injection(
    state_dir: Path,
    room_id: str,
    *,
    message: str,
    target: str = 'whole',
    when: str = 'next',
    sender: str = 'user',
    interrupt: bool | None = None,
) -> dict[str, Any] | None:
    room = load_room(state_dir, room_id)
    if not room:
        return None
    meta = dict(room.get('meta') or {})
    pending = list(meta.get('pending_injections') or [])
    when_norm = str(when or 'next').strip().lower()
    if when_norm in ('immediate', 'now', 'interrupt'):
        when_norm = 'immediate'
    else:
        when_norm = 'next'
    item = {
        'id': f'inject-{uuid.uuid4().hex[:8]}',
        'message': str(message or '').strip(),
        'target': str(target or 'whole').strip() or 'whole',
        'when': when_norm,
        'interrupt': bool(interrupt if interrupt is not None else when_norm == 'immediate'),
        'sender': str(sender or 'user').strip() or 'user',
        'created_at': _now_iso(),
        'delivered_to': [],
    }
    if item['when'] == 'immediate':
        pending.insert(0, item)
    else:
        pending.append(item)
    meta['pending_injections'] = pending
    room = update_room(state_dir, room_id, meta=meta)
    append_event(state_dir, room_id, {
        'type': 'room_injection_queued',
        'injection_id': item['id'],
        'target': item['target'],
        'when': item['when'],
        'sender': item['sender'],
        'summary': item['message'][:240],
        'message': item['message'],
    })
    return item if room else None


def consume_injections(
    state_dir: Path,
    room_id: str,
    *,
    speaker_role: str,
    participant: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    room = load_room(state_dir, room_id)
    if not room:
        return []
    meta = dict(room.get('meta') or {})
    pending = list(meta.get('pending_injections') or [])
    participants = list(room.get('participants') or [])
    participant_key = _participant_delivery_key(participant, speaker_role)
    matched: list[dict[str, Any]] = []
    kept: list[dict[str, Any]] = []
    changed = False
    for item in pending:
        if not isinstance(item, dict):
            continue
        current = dict(item)
        target = str(current.get('target') or 'whole').strip().lower()
        delivered_to = [str(x).strip().lower() for x in (current.get('delivered_to') or []) if str(x).strip()]
        if not _target_matches(target, speaker_role, participant):
            kept.append(current)
            continue
        if participant_key and participant_key in delivered_to:
            kept.append(current)
            continue
        matched.append(dict(current))
        changed = True
        append_event(state_dir, room_id, {
            'type': 'room_injection_delivered',
            'injection_id': str(current.get('id') or ''),
            'target': target,
            'speaker_role': speaker_role,
            'participant': str((participant or {}).get('name') or (participant or {}).get('id') or speaker_role),
            'summary': str(current.get('message') or '')[:240],
        })
        if target in ('', 'whole', 'room', 'all', '*'):
            delivered_to.append(participant_key)
            current['delivered_to'] = delivered_to
            participant_keys = {
                _participant_delivery_key(p, str(p.get('role') or ''))
                for p in participants if isinstance(p, dict)
            }
            participant_keys = {k for k in participant_keys if k}
            if participant_keys and participant_keys.issubset(set(delivered_to)):
                continue
            kept.append(current)
            continue
        current['delivered_to'] = delivered_to + ([participant_key] if participant_key else [])
        # targeted injections are removed after first successful delivery
    if changed:
        meta['pending_injections'] = kept
        update_room(state_dir, room_id, meta=meta)
    return matched


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
            out.append(_normalize_room(data))
    out.sort(key=lambda r: str(r.get('last_activity') or r.get('updated_at') or r.get('created_at') or ''), reverse=True)
    return out[:limit]


def list_events(state_dir: Path, room_id: str, limit: int = 200) -> list[dict[str, Any]]:
    p = _events_path(state_dir, room_id)
    if not p.exists():
        return []
    try:
        lines = p.read_text().splitlines()[-limit:]
    except Exception as e:
        _diag('inter_agent_rooms', 'events.jsonl unreadable; room event log returned empty', error=e, room_id=room_id)
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


def delete_room(state_dir: Path, room_id: str) -> bool:
    room_dir = _room_dir(state_dir, room_id)
    if not room_dir.exists():
        return False
    trash_dir = state_dir / 'deleted_rooms'
    trash_dir.mkdir(parents=True, exist_ok=True)
    target = trash_dir / f'{room_id}-{int(time.time())}'
    try:
        shutil.move(str(room_dir), str(target))
        return True
    except Exception as e:
        _diag('inter_agent_rooms', 'room move to deleted_rooms failed; delete reported as False', error=e, room_id=room_id)
        return False
