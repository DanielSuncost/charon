from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REGISTRY_VERSION = 1


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slug(text: str, fallback: str = 'project', max_len: int = 48) -> str:
    raw = ''.join(ch.lower() if ch.isalnum() else '-' for ch in str(text or ''))
    raw = '-'.join(part for part in raw.split('-') if part)
    raw = raw or fallback
    return raw[:max_len]


def _registry_path(state_dir: Path) -> Path:
    return state_dir / 'projects' / 'registry.json'


def _project_dir(state_dir: Path, project_id: str) -> Path:
    return state_dir / 'projects' / project_id


def _project_json_path(state_dir: Path, project_id: str) -> Path:
    return _project_dir(state_dir, project_id) / 'project.json'


def _read_json(path: Path, default: Any):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return default


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding='utf-8')


def _default_registry() -> dict[str, Any]:
    return {
        'version': REGISTRY_VERSION,
        'projects': [],
        'root_map': {},
        'updated_at': _now_iso(),
    }


def load_registry(state_dir: Path) -> dict[str, Any]:
    reg = _read_json(_registry_path(state_dir), _default_registry())
    if not isinstance(reg, dict):
        reg = _default_registry()
    reg.setdefault('version', REGISTRY_VERSION)
    reg.setdefault('projects', [])
    reg.setdefault('root_map', {})
    reg.setdefault('updated_at', _now_iso())
    return reg


def save_registry(state_dir: Path, registry: dict[str, Any]) -> None:
    registry = dict(registry or {})
    registry['version'] = REGISTRY_VERSION
    registry['updated_at'] = _now_iso()
    _write_json(_registry_path(state_dir), registry)


def derive_project_name(project_root: Path) -> str:
    return project_root.resolve().name or 'project'


def _normalize_root(path: Path | str) -> str:
    return str(Path(path).resolve())


def _iter_project_docs(state_dir: Path) -> list[dict[str, Any]]:
    base = state_dir / 'projects'
    out: list[dict[str, Any]] = []
    if not base.exists():
        return out
    for pj in base.glob('*/project.json'):
        doc = _read_json(pj, {})
        if isinstance(doc, dict) and doc.get('id'):
            out.append(doc)
    return out


def _find_by_root(registry: dict[str, Any], root: str) -> dict[str, Any] | None:
    pid = str((registry.get('root_map') or {}).get(root) or '').strip()
    if not pid:
        return None
    for proj in registry.get('projects') or []:
        if isinstance(proj, dict) and proj.get('id') == pid:
            return proj
    return None


def ensure_project(
    state_dir: Path,
    project_root: Path,
    *,
    name: str | None = None,
    kind: str | None = None,
    summary: str | None = None,
    provisional: bool = True,
) -> dict[str, Any]:
    root = _normalize_root(project_root)
    registry = load_registry(state_dir)

    existing = _find_by_root(registry, root)
    if existing:
        existing['updated_at'] = _now_iso()
        if name:
            existing['name'] = str(name).strip()[:120]
        if kind:
            existing['kind'] = str(kind).strip()[:40]
        if summary:
            existing['summary'] = str(summary).strip()[:500]
        linked = {root, *[str(x) for x in existing.get('roots') or [] if str(x).strip()]}
        existing['roots'] = sorted(linked)
        _write_json(_project_json_path(state_dir, existing['id']), existing)
        save_registry(state_dir, registry)
        return existing

    # Rehydrate from existing project docs if possible
    for doc in _iter_project_docs(state_dir):
        roots = {str(x) for x in doc.get('roots') or [] if str(x).strip()}
        root_path = str(doc.get('root_path') or '').strip()
        if root_path:
            roots.add(root_path)
        if root in roots:
            proj = dict(doc)
            proj['roots'] = sorted(roots)
            proj.setdefault('provisional', False)
            proj['updated_at'] = _now_iso()
            registry['projects'] = [p for p in registry.get('projects') or [] if p.get('id') != proj.get('id')]
            registry['projects'].append(proj)
            registry.setdefault('root_map', {})
            for r in proj['roots']:
                registry['root_map'][r] = proj['id']
            _write_json(_project_json_path(state_dir, proj['id']), proj)
            save_registry(state_dir, registry)
            return proj

    proj_name = str(name or derive_project_name(Path(root))).strip() or 'project'
    project_id = f"{_slug(proj_name)}-{uuid.uuid4().hex[:8]}"
    now = _now_iso()
    doc = {
        'id': project_id,
        'name': proj_name,
        'kind': str(kind or 'software'),
        'status': 'active',
        'root_path': root,
        'roots': [root],
        'aliases': [],
        'linked_paths': [],
        'summary': str(summary or '').strip()[:500],
        'provisional': bool(provisional),
        'created_at': now,
        'updated_at': now,
    }
    registry.setdefault('projects', []).append(doc)
    registry.setdefault('root_map', {})[root] = project_id
    _write_json(_project_json_path(state_dir, project_id), doc)
    save_registry(state_dir, registry)
    return doc


def get_project_by_root(state_dir: Path, project_root: Path) -> dict[str, Any] | None:
    root = _normalize_root(project_root)
    reg = load_registry(state_dir)
    return _find_by_root(reg, root)


def get_project(state_dir: Path, project_id: str) -> dict[str, Any] | None:
    if not project_id:
        return None
    path = _project_json_path(state_dir, project_id)
    doc = _read_json(path, {})
    return doc if isinstance(doc, dict) and doc.get('id') else None
