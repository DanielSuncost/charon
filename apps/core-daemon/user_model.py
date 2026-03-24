#!/usr/bin/env python3
from __future__ import annotations
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATE_DIR = ROOT / '.charon_state'
MODEL_FILE = STATE_DIR / 'user_model.json'


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default() -> dict:
    return {
        'version': 1,
        'updated_at': now(),
        'preferences': {},
        'projects': {},
        'cross_project_links': [],
        'notes': [],
    }


def load_model() -> dict:
    if not MODEL_FILE.exists():
        return _default()
    try:
        return json.loads(MODEL_FILE.read_text())
    except Exception:
        return _default()


def save_model(model: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    model['updated_at'] = now()
    MODEL_FILE.write_text(json.dumps(model, indent=2))


def set_preference(key: str, value: str) -> dict:
    m = load_model()
    m.setdefault('preferences', {})[key] = {'value': value, 'updated_at': now()}
    save_model(m)
    return m


def touch_project(project: str, note: str = '') -> dict:
    m = load_model()
    p = m.setdefault('projects', {}).setdefault(project, {})
    p['last_active'] = now()
    if note:
        p['last_note'] = note
    save_model(m)
    return m


def stale_projects(days: int = 7) -> list[tuple[str, float]]:
    m = load_model()
    out = []
    now_dt = datetime.now(timezone.utc)
    for name, meta in m.get('projects', {}).items():
        ts = meta.get('last_active')
        if not ts:
            continue
        try:
            dt = datetime.fromisoformat(ts)
            delta = (now_dt - dt).total_seconds() / 86400.0
            if delta >= days:
                out.append((name, delta))
        except Exception:
            continue
    return sorted(out, key=lambda x: x[1], reverse=True)


def suggest_cross_project_links() -> list[str]:
    m = load_model()
    names = sorted(m.get('projects', {}).keys())
    if len(names) < 2:
        return []
    # simple starter heuristic
    return [f'Consider whether work in {names[i]} can inform {names[i+1]}' for i in range(len(names)-1)]
