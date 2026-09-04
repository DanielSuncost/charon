from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import threading
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from charon.infra.diagnostics import record as _diag
except Exception:  # diagnostics is best-effort and must never block import
    def _diag(*args, **kwargs):
        return None


_EVENT_LOCKS: dict[str, threading.RLock] = {}
_EVENT_LOCKS_GUARD = threading.Lock()
_JSONL_CACHE_LIMIT = 64
_JSONL_CACHE_ROW_LIMIT = 100_000
_JSONL_CACHE: OrderedDict[
    str,
    tuple[tuple[int, int, int, int], tuple[dict[str, Any], ...]],
] = OrderedDict()
_JSONL_CACHE_LOCK = threading.RLock()


# ── Helpers ─────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _operation_init_checkpoint(_stage: str, _operation_id: str) -> None:
    """Fault-injection seam for operation-initialization recovery tests."""


def _slug(text: str, fallback: str = 'item', max_len: int = 80) -> str:
    raw = re.sub(r'[^a-z0-9]+', '-', (text or '').strip().lower())
    raw = re.sub(r'-+', '-', raw).strip('-')
    return (raw or fallback)[:max_len]


def _short_hash(text: str, length: int = 8) -> str:
    return hashlib.sha1(text.encode('utf-8', errors='replace')).hexdigest()[:length]


def _new_id(prefix: str) -> str:
    return f'{prefix}_{uuid.uuid4().hex[:12]}'


def _read_json(path: Path, default: Any):
    if not path.exists():
        return default
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        return data
    except Exception as e:
        _diag('libris_runtime', 'state JSON unreadable or corrupt; using default', error=e)
        return default


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding='utf-8')


def _write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f'.{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp')
    try:
        with temp.open('x', encoding='utf-8') as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as f:
        f.write(json.dumps(row, ensure_ascii=False) + '\n')


def _iter_jsonl(path: Path) -> list[dict[str, Any]]:
    path = Path(path)
    cache_key = str(path.resolve())
    try:
        before = path.stat()
    except FileNotFoundError:
        with _JSONL_CACHE_LOCK:
            _JSONL_CACHE.pop(cache_key, None)
        return []
    except OSError as e:
        _diag('libris_runtime', 'JSONL log cannot be inspected; yielding no rows', error=e)
        return []

    signature = (
        int(before.st_dev),
        int(before.st_ino),
        int(before.st_size),
        int(before.st_mtime_ns),
    )
    with _JSONL_CACHE_LOCK:
        cached = _JSONL_CACHE.get(cache_key)
        if cached and cached[0] == signature:
            _JSONL_CACHE.move_to_end(cache_key)
            # The cache owns its tuple. Callers may sort or slice the returned
            # list, or replace top-level fields, without changing the entry.
            return [dict(row) for row in cached[1]]

    rows: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding='utf-8', errors='replace').splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                if isinstance(row, dict):
                    rows.append(row)
            except Exception:
                continue
    except Exception as e:
        _diag('libris_runtime', 'JSONL log unreadable; yielding no rows', error=e)
        return []

    try:
        after = path.stat()
        after_signature = (
            int(after.st_dev),
            int(after.st_ino),
            int(after.st_size),
            int(after.st_mtime_ns),
        )
    except OSError:
        after_signature = None
    if after_signature == signature:
        with _JSONL_CACHE_LOCK:
            _JSONL_CACHE[cache_key] = (
                signature,
                tuple(dict(row) for row in rows),
            )
            _JSONL_CACHE.move_to_end(cache_key)
            while len(_JSONL_CACHE) > _JSONL_CACHE_LIMIT or (
                len(_JSONL_CACHE) > 1
                and sum(len(entry[1]) for entry in _JSONL_CACHE.values())
                > _JSONL_CACHE_ROW_LIMIT
            ):
                _JSONL_CACHE.popitem(last=False)
    return rows


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        if value in (None, ''):
            return default
        return int(value)
    except Exception:
        return default


def _coerce_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ''):
            return default
        return float(value)
    except Exception:
        return default


def _default_budget() -> dict[str, Any]:
    return {
        'max_wall_hours': 0,
        'max_total_tokens': 0,
        'max_total_cost_usd': 0.0,
        'max_topics': 0,
        'max_checkpoints_per_topic': 0,
        'max_concurrent_researchers': 0,
        'max_concurrent_shades': 0,
    }


def _default_model_policy() -> dict[str, Any]:
    return {
        'coordinator': 'strong',
        'judge': 'strong',
        'researcher': 'fast',
        'shade': 'cheap',
    }


def _default_usage() -> dict[str, Any]:
    return {
        'input_tokens': 0,
        'output_tokens': 0,
        'total_tokens': 0,
        'estimated_cost_usd': 0.0,
        # Billing basis for estimated_cost_usd: 'metered' (real $), 'subscription'
        # (OAuth flat-rate — $ is notional/not billed), 'local' (free), or '' when
        # unknown. Consumers suppress or label the $ figure accordingly.
        'cost_basis': '',
        'by_model': {},
        'by_role': {},
        'updated_at': _now_iso(),
    }


def _normalize_budget(budget: dict[str, Any] | None) -> dict[str, Any]:
    out = _default_budget()
    data = budget or {}
    for key in out:
        if key == 'max_total_cost_usd':
            out[key] = _coerce_float(data.get(key), out[key])
        else:
            out[key] = _coerce_int(data.get(key), out[key])
    return out


def _normalize_model_policy(model_policy: dict[str, Any] | None) -> dict[str, Any]:
    out = _default_model_policy()
    for key, value in (model_policy or {}).items():
        if value not in (None, ''):
            out[str(key)] = str(value)
    return out


def _estimate_cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    model_key = (model or '').strip().lower()
    pricing = {
        'cheap': (0.0, 0.0),
        'cheap_local': (0.0, 0.0),
        'local': (0.0, 0.0),
        'fast': (0.15, 0.60),
        'strong': (3.00, 15.00),
    }
    in_rate, out_rate = pricing.get(model_key, pricing['fast'])
    return round((input_tokens / 1_000_000.0) * in_rate + (output_tokens / 1_000_000.0) * out_rate, 6)


def _hours_since(ts: str | None) -> float:
    if not ts:
        return 0.0
    try:
        dt = datetime.fromisoformat(ts)
        return max(0.0, (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0)
    except Exception:
        return 0.0


def _evaluate_budget(operation: dict[str, Any]) -> dict[str, Any]:
    budget = _normalize_budget(operation.get('budget') or {})
    usage = dict(_default_usage())
    usage.update(operation.get('usage') or {})
    topics = operation.get('selected_topic_ids') or []
    reasons: list[str] = []

    if operation.get('stop_requested'):
        reasons.append('stop_requested')
    if budget['max_wall_hours'] and _hours_since(operation.get('created_at')) >= budget['max_wall_hours']:
        reasons.append('wall_time_exhausted')
    if budget['max_total_tokens'] and _coerce_int(usage.get('total_tokens')) >= budget['max_total_tokens']:
        reasons.append('token_budget_exhausted')
    if budget['max_total_cost_usd'] and _coerce_float(usage.get('estimated_cost_usd')) >= budget['max_total_cost_usd']:
        reasons.append('cost_budget_exhausted')
    advisory_reasons: list[str] = []
    if budget['max_topics'] and len(topics) >= budget['max_topics']:
        advisory_reasons.append('topic_budget_reached')

    continue_running = not reasons
    return {
        'continue_running': continue_running,
        'reasons': reasons,
        'advisory_reasons': advisory_reasons,
        'budget': budget,
        'usage': usage,
        'wall_hours_elapsed': round(_hours_since(operation.get('created_at')), 3),
    }


# ── Project paths / metadata ────────────────────────────────────────

from charon.infra.project_registry import get_project_by_root  # noqa: E402 — deliberate late import: section-local dependency
from charon.infra.project_registry_loader import load_ensure_project  # noqa: E402 — deliberate late import: section-local dependency

_ensure_project_registry = load_ensure_project(__file__, 'libris_runtime')


def derive_project_id(project_root: Path) -> str:
    """Backward-compatible project-id helper.

    New storage code must use ``resolve_project_id`` because deriving a registry
    location from ``project_root.parent`` can select a different Charon state
    tree than the caller's explicit ``state_dir``.
    """
    state_dir = project_root.parent / '.charon_state'
    return resolve_project_id(state_dir, project_root)


def resolve_project_id(state_dir: Path, project_root: Path) -> str:
    """Resolve the project id in the caller's state tree.

    Older Libris builds accidentally consulted ``project_root.parent/.charon_state``
    even when the runtime was using ``project_root/.charon_state``.  Preserve an
    existing research directory for the same root so historical operations do
    not disappear merely because a registry id changed.
    """
    state_dir = Path(state_dir)
    root = Path(project_root).resolve()
    try:
        # Project lookup is a read-heavy hot path. Do not rewrite both
        # registry files on every topic, event, and index lookup.
        proj = get_project_by_root(state_dir, root)
        if proj is None:
            proj = _ensure_project_registry(state_dir, root, provisional=True)
        canonical = str(proj.get('id') or '').strip()
        candidates: list[tuple[int, float, str]] = []
        projects_dir = state_dir / 'projects'
        for project_json in projects_dir.glob('*/project.json'):
            try:
                doc = _read_json(project_json, {})
                roots = {str(Path(x).resolve()) for x in (doc.get('roots') or []) if str(x).strip()}
                if doc.get('root_path'):
                    roots.add(str(Path(doc['root_path']).resolve()))
                if str(root) not in roots:
                    continue
                research = project_json.parent / 'research'
                op_count = len(list((research / 'operations').glob('*/operation.json')))
                if op_count:
                    candidates.append((op_count, research.stat().st_mtime, project_json.parent.name))
            except Exception:
                continue
        if candidates:
            # Prefer the canonical registry directory whenever it already holds
            # history. Otherwise fall back to the richest legacy directory so
            # old operations remain discoverable after an id migration.
            canonical_matches = [c for c in candidates if c[2] == canonical]
            if canonical_matches:
                return canonical
            return max(candidates)[2]
        if canonical:
            return canonical
    except Exception as e:
        _diag('libris_runtime', 'project registry lookup failed; using hash-derived project id', error=e)
    base = _slug(root.name or 'project', 'project', 48)
    return f'{base}-{_short_hash(str(root))}'


def project_dir(state_dir: Path, project_root: Path) -> Path:
    return Path(state_dir) / 'projects' / resolve_project_id(state_dir, project_root)


def project_json_path(state_dir: Path, project_root: Path) -> Path:
    return project_dir(state_dir, project_root) / 'project.json'


def ensure_project_metadata(
    state_dir: Path,
    project_root: Path,
    *,
    kind: str | None = None,
    research_mode: str | None = None,
    tags: list[str] | None = None,
    summary: str | None = None,
) -> dict[str, Any]:
    root = project_root.resolve()
    base = _ensure_project_registry(state_dir, root, kind=kind or 'software', summary=summary or '', provisional=True)
    pid = str(base.get('id') or derive_project_id(root))
    path = project_json_path(state_dir, root)
    existing = _read_json(path, {})
    now = _now_iso()

    doc = {
        'id': existing.get('id') or pid,
        'name': existing.get('name') or base.get('name') or (root.name or pid),
        'kind': existing.get('kind') or base.get('kind') or 'software',
        'research_mode': existing.get('research_mode'),
        'status': existing.get('status') or 'active',
        'root_path': str(root),
        'roots': existing.get('roots') or base.get('roots') or [str(root)],
        'linked_paths': existing.get('linked_paths') or [],
        'parent_project_id': existing.get('parent_project_id'),
        'tags': existing.get('tags') or [],
        'summary': existing.get('summary') or base.get('summary') or '',
        'provisional': existing.get('provisional', base.get('provisional', True)),
        'created_at': existing.get('created_at') or base.get('created_at') or now,
        'updated_at': now,
    }

    if kind:
        doc['kind'] = str(kind)
    if research_mode is not None:
        doc['research_mode'] = str(research_mode) if research_mode else None
    if tags:
        merged = {str(t).strip() for t in doc.get('tags') or [] if str(t).strip()}
        merged.update(str(t).strip() for t in tags if str(t).strip())
        doc['tags'] = sorted(merged)
    if summary:
        doc['summary'] = str(summary).strip()[:500]

    _write_json(path, doc)
    return doc


# ── Research paths ──────────────────────────────────────────────────


def research_root(state_dir: Path, project_root: Path) -> Path:
    return project_dir(state_dir, project_root) / 'research'


def operations_root(state_dir: Path, project_root: Path) -> Path:
    return research_root(state_dir, project_root) / 'operations'


def operation_dir(state_dir: Path, project_root: Path, operation_id: str) -> Path:
    return operations_root(state_dir, project_root) / operation_id


def topic_dir(state_dir: Path, project_root: Path, operation_id: str, topic_slug: str) -> Path:
    return operation_dir(state_dir, project_root, operation_id) / 'topics' / topic_slug


def _apply_lifecycle_projection(
    state_dir: Path,
    entity_id: str,
    document: dict[str, Any],
    *,
    entity_type: str,
    projection_path: Path | None = None,
) -> dict[str, Any]:
    """Overlay authoritative lifecycle state without mutating the projection."""
    try:
        from charon.libris import libris_lifecycle as lifecycle

        instance = lifecycle.load_lifecycle(
            state_dir,
            entity_id,
            entity_type=entity_type,
        )
    except Exception as exc:
        _diag(
            'libris_runtime',
            'authoritative lifecycle could not be loaded; using persisted projection',
            error=exc,
            entity_type=entity_type,
            entity_id=entity_id,
        )
        failed = dict(document)
        failed['status'] = 'lifecycle_error'
        failed['lifecycle_integrity'] = {
            'valid': False,
            'reason': str(exc),
        }
        return failed
    if instance is None:
        return document
    projected = dict(document)
    projected['status'] = lifecycle.projected_status(instance)
    projected['lifecycle_state'] = instance.state
    projected['lifecycle_revision'] = instance.revision
    projected['lifecycle_event_seq'] = instance.event_seq
    projected['lifecycle_updated_at'] = instance.updated_at
    projected['lifecycle_integrity'] = {'valid': True, 'reason': ''}
    if projection_path is not None:
        def reconcile(current: dict[str, Any]) -> dict[str, Any]:
            if int(current.get('lifecycle_revision') or -1) <= instance.revision:
                _persist_lifecycle_projection_fields(
                    current,
                    instance,
                    lifecycle.projected_status(instance),
                )
                current['lifecycle_integrity'] = {'valid': True, 'reason': ''}
            return current

        lifecycle.mutate_projection(projection_path, reconcile)
    return projected


def _persist_lifecycle_projection_fields(
    document: dict[str, Any],
    instance: Any,
    projected_status: str,
) -> None:
    document['status'] = projected_status
    document['lifecycle_state'] = instance.state
    document['lifecycle_revision'] = instance.revision
    document['lifecycle_event_seq'] = instance.event_seq
    document['lifecycle_updated_at'] = instance.updated_at


# ── Research tree setup ─────────────────────────────────────────────


def ensure_research_tree(state_dir: Path, project_root: Path) -> dict[str, str]:
    pdir = project_dir(state_dir, project_root)
    rroot = research_root(state_dir, project_root)
    paths = {
        'project_dir': pdir,
        'research_root': rroot,
        'sources_dir': rroot / 'sources',
        'snapshots_dir': rroot / 'sources' / 'snapshots',
        'briefs_dir': rroot / 'briefs',
        'provenance_dir': rroot / 'provenance',
        'operations_dir': rroot / 'operations',
        'topics_dir': rroot / 'topics',
    }
    for path in paths.values():
        Path(path).mkdir(parents=True, exist_ok=True)

    index_path = rroot / 'index.json'
    if not index_path.exists():
        _write_json(index_path, {
            'project_id': resolve_project_id(state_dir, project_root),
            'updated_at': _now_iso(),
            'operations': [],
            'topics': [],
            'source_counts': {},
            'claim_count': 0,
            'brief_count': 0,
        })

    for rel in ('dossier.md', 'questions.md', 'claims.jsonl', 'entities.jsonl', 'promising_sources.jsonl'):
        path = rroot / rel
        if not path.exists():
            path.write_text('', encoding='utf-8')

    return {k: str(v) for k, v in paths.items()}


# ── Event / index management ────────────────────────────────────────


def append_operation_event(
    state_dir: Path,
    project_root: Path,
    operation_id: str,
    event_type: str,
    payload: dict[str, Any] | None = None,
    *,
    event_id: str | None = None,
) -> dict[str, Any]:
    path = operation_dir(state_dir, project_root, operation_id) / 'events.jsonl'
    lock_path = path.with_name('.events.lock')
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_key = str(lock_path.resolve())
    with _EVENT_LOCKS_GUARD:
        local_lock = _EVENT_LOCKS.setdefault(lock_key, threading.RLock())
    row = {
        'event_id': event_id or _new_id('evt'),
        'operation_id': operation_id,
        'type': event_type,
        'timestamp': _now_iso(),
        'payload': payload or {},
    }
    with local_lock:
        with lock_path.open('a+', encoding='utf-8') as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            try:
                if event_id:
                    for existing in _iter_jsonl(path):
                        if str(existing.get('event_id') or '') != event_id:
                            continue
                        if (
                            existing.get('operation_id') != operation_id
                            or existing.get('type') != event_type
                            or (existing.get('payload') or {}) != (payload or {})
                        ):
                            raise ValueError(
                                f'operation event id {event_id!r} was reused for different content'
                            )
                        return existing
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open('a', encoding='utf-8') as event_handle:
                    event_handle.write(json.dumps(row, ensure_ascii=False) + '\n')
                    event_handle.flush()
                    os.fsync(event_handle.fileno())
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
    return row


def emit_agent_phase(
    state_dir: Path,
    project_root: Path,
    operation_id: str,
    *,
    agent_id: str,
    role: str,
    phase: str,
    status: str = 'running',
    topic_slug: str = '',
    summary: str = '',
) -> dict[str, Any]:
    return append_operation_event(state_dir, project_root, operation_id, 'agent_phase_changed', {
        'agent_id': agent_id,
        'role': role,
        'phase': phase,
        'status': status,
        'topic_slug': topic_slug,
        'summary': summary[:500],
    })


def emit_agent_comm(
    state_dir: Path,
    project_root: Path,
    operation_id: str,
    *,
    from_agent_id: str,
    to_agent_id: str,
    from_role: str,
    to_role: str,
    topic_slug: str = '',
    message_kind: str = 'handoff',
    summary: str = '',
) -> dict[str, Any]:
    return append_operation_event(state_dir, project_root, operation_id, 'agent_communication', {
        'from_agent_id': from_agent_id,
        'to_agent_id': to_agent_id,
        'from_role': from_role,
        'to_role': to_role,
        'topic_slug': topic_slug,
        'message_kind': message_kind,
        'summary': summary[:500],
    })


def rebuild_project_index(state_dir: Path, project_root: Path) -> dict[str, Any]:
    rroot = research_root(state_dir, project_root)
    idx_path = rroot / 'index.json'
    current = _read_json(idx_path, {})

    ops_dir = rroot / 'operations'
    topics: list[dict[str, Any]] = []
    operations: list[dict[str, Any]] = []
    source_counts: dict[str, int] = {}
    brief_count = 0
    claim_count = 0

    claims_path = rroot / 'claims.jsonl'
    if claims_path.exists():
        claim_count = len(_iter_jsonl(claims_path))

    sources_path = rroot / 'sources' / 'sources.jsonl'
    for src in _iter_jsonl(sources_path):
        st = str(src.get('source_type') or 'unknown')
        source_counts[st] = source_counts.get(st, 0) + 1

    if ops_dir.exists():
        for op_path in sorted(ops_dir.glob('*')):
            if not op_path.is_dir():
                continue
            op = _read_json(op_path / 'operation.json', {})
            if op:
                op = _apply_lifecycle_projection(
                    state_dir,
                    str(op.get('operation_id') or op_path.name),
                    op,
                    entity_type='operation',
                )
                operations.append({
                    'operation_id': op.get('operation_id'),
                    'status': op.get('status'),
                    'mode': op.get('mode'),
                    'created_at': op.get('created_at'),
                    'updated_at': op.get('updated_at'),
                    'prompt': str(op.get('prompt') or '')[:160],
                })
            op_topics = op_path / 'topics'
            if op_topics.exists():
                for topic_path in sorted(op_topics.glob('*')):
                    if not topic_path.is_dir():
                        continue
                    topic = _read_json(topic_path / 'topic.json', {})
                    if topic:
                        topic = _apply_lifecycle_projection(
                            state_dir,
                            str(topic.get('topic_id') or topic_path.name),
                            topic,
                            entity_type='topic',
                        )
                        topics.append({
                            'topic_id': topic.get('topic_id'),
                            'operation_id': topic.get('operation_id'),
                            'slug': topic.get('slug'),
                            'title': topic.get('title'),
                            'status': topic.get('status'),
                            'checkpoint_count': int(topic.get('checkpoint_count') or 0),
                            'best_checkpoint_id': topic.get('best_checkpoint_id'),
                            'updated_at': topic.get('updated_at'),
                        })
                    brief_count += len(list((topic_path / 'checkpoints').glob('*-report.md')))

    idx = {
        'project_id': resolve_project_id(state_dir, project_root),
        'updated_at': _now_iso(),
        'operations': operations,
        'topics': topics,
        'source_counts': source_counts,
        'claim_count': claim_count,
        'brief_count': brief_count,
    }
    if current.get('created_at'):
        idx['created_at'] = current['created_at']
    else:
        idx['created_at'] = _now_iso()
    _write_json(idx_path, idx)
    return idx


# ── Operation lifecycle ─────────────────────────────────────────────


def init_operation(
    state_dir: Path,
    project_root: Path,
    *,
    prompt: str,
    mode: str = 'autonomous_research_operation',
    coordinator_agent_id: str = '',
    kind: str = 'research',
    research_mode: str = 'exploratory',
    summary: str = '',
    budget: dict[str, Any] | None = None,
    model_policy: dict[str, Any] | None = None,
    durable_bootstrap: dict[str, Any] | None = None,
) -> dict[str, Any]:
    ensure_project_metadata(
        state_dir,
        project_root,
        kind=kind,
        research_mode=research_mode,
        tags=['libris', 'research'],
        summary=summary or 'Libris research-enabled project',
    )
    ensure_research_tree(state_dir, project_root)

    # Twelve random hex digits keep the legacy-readable timestamp prefix while
    # making cross-project collisions in the shared lifecycle store negligible.
    op_id = f'rop_{datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")}_{uuid.uuid4().hex[:12]}'
    op_dir = operation_dir(state_dir, project_root, op_id)
    (op_dir / 'coordinator').mkdir(parents=True, exist_ok=True)
    (op_dir / 'topics').mkdir(parents=True, exist_ok=True)

    now = _now_iso()
    doc = {
        'operation_id': op_id,
        'project_id': resolve_project_id(state_dir, project_root),
        'prompt': str(prompt).strip(),
        'mode': mode,
        'status': 'running',
        'coordinator_agent_id': coordinator_agent_id,
        'created_at': now,
        'updated_at': now,
        'stop_requested': False,
        'selected_topic_ids': [],
        'delivered_topic_ids': [],
        'budget': _normalize_budget(budget),
        'model_policy': _normalize_model_policy(model_policy),
        'usage': _default_usage(),
    }
    if durable_bootstrap:
        bootstrap = dict(durable_bootstrap)
        bootstrap['operation_started_payload'] = {
            'prompt': str(prompt).strip()[:500],
            'mode': mode,
            'coordinator_agent_id': coordinator_agent_id,
            'budget': doc['budget'],
            'model_policy': doc['model_policy'],
        }
        doc['durable_bootstrap'] = bootstrap
    _write_json_atomic(op_dir / 'operation.json', doc)
    if durable_bootstrap:
        _operation_init_checkpoint('operation_projection_persisted', op_id)
    return reconcile_operation_initialization(state_dir, project_root, op_id)


def reconcile_operation_initialization(
    state_dir: Path,
    project_root: Path,
    operation_id: str,
) -> dict[str, Any]:
    """Idempotently finish lifecycle, start-event, and index initialization."""
    op_dir = operation_dir(state_dir, project_root, operation_id)
    path = op_dir / 'operation.json'
    doc = _read_json(path, {})
    if not doc:
        return {}
    from charon.libris import libris_lifecycle as lifecycle

    machine = lifecycle.initialize_lifecycle(
        state_dir,
        entity_type='operation',
        entity_id=operation_id,
        projected_status=str(doc.get('status') or 'running'),
        now=str(doc.get('created_at') or _now_iso()),
        data={'operation_id': operation_id},
    )

    def reconcile_projection(current: dict[str, Any]) -> dict[str, Any]:
        _persist_lifecycle_projection_fields(
            current,
            machine,
            lifecycle.projected_status(machine),
        )
        return current

    doc = lifecycle.mutate_projection(path, reconcile_projection)
    bootstrap = dict(doc.get('durable_bootstrap') or {})
    start_payload = dict(bootstrap.get('operation_started_payload') or {})
    if not start_payload:
        start_payload = {
            'prompt': str(doc.get('prompt') or '').strip()[:500],
            'mode': str(doc.get('mode') or ''),
            'coordinator_agent_id': str(doc.get('coordinator_agent_id') or ''),
            'budget': doc.get('budget') or {},
            'model_policy': doc.get('model_policy') or {},
        }
    append_operation_event(
        state_dir,
        project_root,
        operation_id,
        'operation_started',
        start_payload,
        event_id=f'libris-operation-started:{operation_id}',
    )
    rebuild_project_index(state_dir, project_root)
    return doc


def get_operation_state(state_dir: Path, project_root: Path, operation_id: str) -> dict[str, Any]:
    op_dir = operation_dir(state_dir, project_root, operation_id)
    op = _read_json(op_dir / 'operation.json', {})
    if not op:
        return {}
    op = _apply_lifecycle_projection(
        state_dir,
        operation_id,
        op,
        entity_type='operation',
        projection_path=op_dir / 'operation.json',
    )
    op['candidate_topics'] = _read_json(op_dir / 'coordinator' / 'candidate-topics.json', [])
    op['events_tail'] = _iter_jsonl(op_dir / 'events.jsonl')[-20:]
    topics: list[dict[str, Any]] = []
    topic_root = op_dir / 'topics'
    for topic_path in sorted(topic_root.glob('*')):
        slug = topic_path.name
        topic = get_topic_state(state_dir, project_root, operation_id, slug)
        if topic:
            topics.append(topic)
    op['topics'] = topics
    op['budget_status'] = _evaluate_budget(op)
    return op


def request_stop(state_dir: Path, project_root: Path, operation_id: str, reason: str = '') -> dict[str, Any]:
    from charon.libris import libris_lifecycle as lifecycle

    path = operation_dir(state_dir, project_root, operation_id) / 'operation.json'
    if not path.exists():
        return {}

    def mark_stop(current: dict[str, Any]) -> dict[str, Any]:
        current['stop_requested'] = True
        current['updated_at'] = _now_iso()
        return current

    op = lifecycle.mutate_projection(path, mark_stop)
    append_operation_event(state_dir, project_root, operation_id, 'operation_stop_requested', {'reason': reason[:500]})
    rebuild_project_index(state_dir, project_root)
    return op


def save_candidate_topics(
    state_dir: Path,
    project_root: Path,
    operation_id: str,
    topics: list[dict[str, Any]],
    plan_markdown: str = '',
) -> dict[str, Any]:
    op_dir = operation_dir(state_dir, project_root, operation_id)
    clean: list[dict[str, Any]] = []
    for topic in topics:
        title = str(topic.get('title') or topic.get('topic') or '').strip()
        if not title:
            continue
        clean.append({
            'topic_id': str(topic.get('topic_id') or _new_id('top')),
            'title': title,
            'slug': str(topic.get('slug') or _slug(title, 'topic')),
            'summary': str(topic.get('summary') or '').strip(),
            'why_interesting': str(topic.get('why_interesting') or '').strip(),
            'relevance_to_user': str(topic.get('relevance_to_user') or '').strip(),
            'evidence_strength': str(topic.get('evidence_strength') or 'unknown'),
            'novelty': str(topic.get('novelty') or 'unknown'),
            'recommended_action': str(topic.get('recommended_action') or 'monitor'),
        })
    _write_json(op_dir / 'coordinator' / 'candidate-topics.json', clean)
    if plan_markdown:
        (op_dir / 'coordinator' / 'plan.md').write_text(plan_markdown, encoding='utf-8')

    append_operation_event(state_dir, project_root, operation_id, 'candidate_topics_written', {
        'count': len(clean),
    })
    return {'operation_id': operation_id, 'count': len(clean), 'topics': clean}


def init_topic(
    state_dir: Path,
    project_root: Path,
    operation_id: str,
    *,
    title: str,
    why_interesting: str = '',
    researcher_agent_id: str = '',
    judge_agent_id: str = '',
    focus_questions: list[str] | None = None,
    topic_budget: dict[str, Any] | None = None,
    model_policy_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    op_path = operation_dir(state_dir, project_root, operation_id) / 'operation.json'
    op = _read_json(op_path, {})
    if not op:
        raise ValueError(f'Unknown operation_id: {operation_id}')

    budget_status = _evaluate_budget(op)
    if not budget_status.get('continue_running', True):
        raise ValueError(f'Operation cannot continue: {", ".join(budget_status.get("reasons") or [])}')

    slug = _slug(title, 'topic')
    tdir = topic_dir(state_dir, project_root, operation_id, slug)
    (tdir / 'evidence').mkdir(parents=True, exist_ok=True)
    (tdir / 'checkpoints').mkdir(parents=True, exist_ok=True)
    (tdir / 'final').mkdir(parents=True, exist_ok=True)

    topic_id = _new_id('top')
    now = _now_iso()
    doc = {
        'topic_id': topic_id,
        'operation_id': operation_id,
        'slug': slug,
        'title': title.strip(),
        'why_interesting': why_interesting.strip(),
        'status': 'researching',
        'researcher_agent_id': researcher_agent_id,
        'judge_agent_id': judge_agent_id,
        'focus_questions': list(focus_questions or []),
        'checkpoint_count': 0,
        'best_checkpoint_id': None,
        'research_round': 1,
        'judge_round': 0,
        'revision_round': 0,
        'budget': _normalize_budget(topic_budget),
        'model_policy_override': _normalize_model_policy(model_policy_override),
        'usage': _default_usage(),
        'created_at': now,
        'updated_at': now,
    }
    _write_json(tdir / 'topic.json', doc)
    from charon.libris import libris_lifecycle as lifecycle

    machine = lifecycle.initialize_lifecycle(
        state_dir,
        entity_type='topic',
        entity_id=topic_id,
        projected_status='researching',
        now=now,
        data={'operation_id': operation_id, 'topic_slug': slug},
    )
    _persist_lifecycle_projection_fields(doc, machine, lifecycle.projected_status(machine))
    _write_json(tdir / 'topic.json', doc)

    def add_selected_topic(current: dict[str, Any]) -> dict[str, Any]:
        selected = list(current.get('selected_topic_ids') or [])
        if topic_id not in selected:
            selected.append(topic_id)
        current['selected_topic_ids'] = selected
        current['updated_at'] = _now_iso()
        return current

    lifecycle.mutate_projection(op_path, add_selected_topic)

    append_operation_event(state_dir, project_root, operation_id, 'topic_selected', {
        'topic_id': topic_id,
        'slug': slug,
        'title': title,
        'researcher_agent_id': researcher_agent_id,
        'judge_agent_id': judge_agent_id,
    })
    rebuild_project_index(state_dir, project_root)
    return doc


def get_topic_state(state_dir: Path, project_root: Path, operation_id: str, topic_slug: str) -> dict[str, Any]:
    tdir = topic_dir(state_dir, project_root, operation_id, topic_slug)
    topic = _read_json(tdir / 'topic.json', {})
    if not topic:
        return {}
    topic = _apply_lifecycle_projection(
        state_dir,
        str(topic.get('topic_id') or topic_slug),
        topic,
        entity_type='topic',
        projection_path=tdir / 'topic.json',
    )
    checkpoints = []
    meta_items = []
    cdir = tdir / 'checkpoints'
    for meta_path in sorted(cdir.glob('*-meta.json')):
        meta = _read_json(meta_path, {})
        if meta:
            meta_items.append(meta)
    if not meta_items:
        for summary in sorted(cdir.glob('*-summary.md')):
            checkpoints.append({'summary_path': str(summary)})
    else:
        for meta in meta_items:
            checkpoints.append({
                'checkpoint_id': meta.get('checkpoint_id'),
                'iteration': meta.get('iteration'),
                'summary_path': meta.get('summary_path'),
                'critique_path': meta.get('critique_path'),
                'report_path': meta.get('report_path'),
                'score': meta.get('score'),
                'created_at': meta.get('created_at'),
            })
    draft_path = tdir / 'draft-report.md'
    draft_meta = _read_json(tdir / 'draft-report.json', {})
    topic['checkpoints'] = checkpoints
    topic['draft_report_path'] = str(draft_path) if draft_path.exists() else ''
    topic['draft_report_updated_at'] = draft_meta.get('updated_at', '') if draft_meta else ''
    topic['latest_checkpoint'] = checkpoints[-1] if checkpoints else {}
    return topic


def update_topic_runtime(
    state_dir: Path,
    project_root: Path,
    operation_id: str,
    topic_slug: str,
    *,
    status: str | None = None,
    researcher_agent_id: str | None = None,
    judge_agent_id: str | None = None,
    extras: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from charon.libris import libris_lifecycle as lifecycle

    tdir = topic_dir(state_dir, project_root, operation_id, topic_slug)
    path = tdir / 'topic.json'
    topic = _read_json(path, {})
    if not topic:
        return {}
    authoritative = _apply_lifecycle_projection(
        state_dir,
        str(topic.get('topic_id') or topic_slug),
        topic,
        entity_type='topic',
    )
    lifecycle_event: dict[str, Any] = {}
    machine = None
    if status is not None:
        machine, lifecycle_event = lifecycle.transition_lifecycle(
            state_dir,
            entity_type='topic',
            entity_id=str(topic.get('topic_id') or topic_slug),
            current_projected_status=str(authoritative.get('status') or topic.get('status') or 'researching'),
            target_projected_status=str(status),
        )

    def mutate_projection(current: dict[str, Any]) -> dict[str, Any]:
        if machine is not None and int(current.get('lifecycle_revision') or -1) <= machine.revision:
            _persist_lifecycle_projection_fields(
                current,
                machine,
                lifecycle.projected_status(machine),
            )
        if researcher_agent_id is not None:
            current['researcher_agent_id'] = researcher_agent_id
        if judge_agent_id is not None:
            current['judge_agent_id'] = judge_agent_id
        if extras:
            for key, value in extras.items():
                current[str(key)] = value
        current['updated_at'] = _now_iso()
        return current

    topic = lifecycle.mutate_projection(path, mutate_projection)
    if not topic:
        return {}
    append_operation_event(state_dir, project_root, operation_id, 'topic_runtime_updated', {
        'topic_slug': topic_slug,
        'status': topic.get('status'),
        'researcher_agent_id': topic.get('researcher_agent_id', ''),
        'judge_agent_id': topic.get('judge_agent_id', ''),
        'extras': extras or {},
        'lifecycle_transition': lifecycle_event,
    }, event_id=str(lifecycle_event.get('event_id') or '') or None)
    rebuild_project_index(state_dir, project_root)
    return topic


# ── Sources / claims ────────────────────────────────────────────────


def add_source(
    state_dir: Path,
    project_root: Path,
    *,
    topic_slug: str,
    title: str,
    url: str,
    source_type: str = 'web',
    operation_id: str = '',
    authors: list[str] | None = None,
    published_at: str | None = None,
    credibility: str = 'unknown',
    tags: list[str] | None = None,
    extracted_text: str = '',
) -> dict[str, Any]:
    ensure_research_tree(state_dir, project_root)
    source_id = f'src_{_short_hash(url or title or uuid.uuid4().hex, 12)}'
    snapshot_rel = ''
    if extracted_text.strip():
        snapshot_name = f'{source_id}.md'
        snap_path = research_root(state_dir, project_root) / 'sources' / 'snapshots' / snapshot_name
        snap_path.write_text(extracted_text, encoding='utf-8')
        snapshot_rel = str(snap_path.relative_to(project_dir(state_dir, project_root)))

    row = {
        'source_id': source_id,
        'project_id': resolve_project_id(state_dir, project_root),
        'operation_id': operation_id,
        'topic_slug': topic_slug,
        'url': url.strip(),
        'title': title.strip(),
        'source_type': source_type.strip() or 'web',
        'authors': list(authors or []),
        'published_at': published_at,
        'retrieved_at': _now_iso(),
        'snapshot_path': snapshot_rel,
        'content_hash': _short_hash(extracted_text.strip() or (url + title), 16),
        'credibility': credibility,
        'tags': list(tags or []),
    }
    _append_jsonl(research_root(state_dir, project_root) / 'sources' / 'sources.jsonl', row)
    rebuild_project_index(state_dir, project_root)
    return row


def add_claim(
    state_dir: Path,
    project_root: Path,
    *,
    topic_slug: str,
    source_id: str,
    text: str,
    operation_id: str = '',
    confidence: str = 'medium',
    stance: str = 'supports',
    evidence_grade: str = '',
    entity_refs: list[str] | None = None,
) -> dict[str, Any]:
    ensure_research_tree(state_dir, project_root)
    row = {
        'claim_id': _new_id('clm'),
        'project_id': resolve_project_id(state_dir, project_root),
        'operation_id': operation_id,
        'topic_slug': topic_slug,
        'source_id': source_id,
        'text': text.strip(),
        'confidence': confidence,
        'stance': stance,
        # methodological strength of the underlying evidence, distinct from the
        # agent's confidence: strong / moderate / weak / anecdotal / theoretical
        # / contested. Renders as an epistemic badge on the claim.
        'evidence_grade': (evidence_grade or '').strip().lower(),
        'entity_refs': list(entity_refs or []),
        'created_at': _now_iso(),
    }
    _append_jsonl(research_root(state_dir, project_root) / 'claims.jsonl', row)
    rebuild_project_index(state_dir, project_root)
    return row


def save_evidence(
    state_dir: Path,
    project_root: Path,
    operation_id: str,
    topic_slug: str,
    *,
    markdown: str,
    filename: str | None = None,
) -> dict[str, Any]:
    tdir = topic_dir(state_dir, project_root, operation_id, topic_slug)
    (tdir / 'evidence').mkdir(parents=True, exist_ok=True)
    name = filename or f'{topic_slug}-evidence.md'
    path = tdir / 'evidence' / name
    path.write_text(markdown, encoding='utf-8')
    append_operation_event(state_dir, project_root, operation_id, 'evidence_saved', {
        'topic_slug': topic_slug,
        'path': str(path),
    })
    return {'path': str(path), 'topic_slug': topic_slug}


def save_report_draft(
    state_dir: Path,
    project_root: Path,
    operation_id: str,
    topic_slug: str,
    *,
    markdown: str,
    note: str = '',
) -> dict[str, Any]:
    tdir = topic_dir(state_dir, project_root, operation_id, topic_slug)
    if not tdir.exists():
        raise ValueError(f'Unknown topic: {topic_slug}')
    path = tdir / 'draft-report.md'
    path.write_text(markdown, encoding='utf-8')
    meta = {
        'path': str(path),
        'topic_slug': topic_slug,
        'updated_at': _now_iso(),
        'note': note[:500],
    }
    _write_json(tdir / 'draft-report.json', meta)
    append_operation_event(state_dir, project_root, operation_id, 'draft_report_saved', {
        'topic_slug': topic_slug,
        'path': str(path),
        'note': note[:500],
    })
    return meta


# ── Checkpoints / delivery ──────────────────────────────────────────


def save_checkpoint(
    state_dir: Path,
    project_root: Path,
    operation_id: str,
    topic_slug: str,
    *,
    report_markdown: str,
    critique_markdown: str,
    summary_markdown: str,
    metrics: dict[str, Any] | None = None,
    score: float | int | None = None,
) -> dict[str, Any]:
    tdir = topic_dir(state_dir, project_root, operation_id, topic_slug)
    topic_path = tdir / 'topic.json'
    topic = _read_json(topic_path, {})
    if not topic:
        raise ValueError(f'Unknown topic: {topic_slug}')

    iteration = int(topic.get('checkpoint_count') or 0) + 1
    topic_budget = _normalize_budget(topic.get('budget') or {})
    max_ckp = int(topic_budget.get('max_checkpoints_per_topic') or 0)
    if not max_ckp:
        op = _read_json(operation_dir(state_dir, project_root, operation_id) / 'operation.json', {})
        max_ckp = int(_normalize_budget(op.get('budget') or {}).get('max_checkpoints_per_topic') or 0) if op else 0
    if max_ckp and iteration > max_ckp:
        raise ValueError(f'Topic checkpoint budget exhausted (max_checkpoints_per_topic={max_ckp})')

    ckp_id = f'ckp_{iteration:03d}'
    cdir = tdir / 'checkpoints'
    cdir.mkdir(parents=True, exist_ok=True)

    report_path = cdir / f'{iteration:03d}-report.md'
    critique_path = cdir / f'{iteration:03d}-critique.md'
    summary_path = cdir / f'{iteration:03d}-summary.md'
    report_path.write_text(report_markdown, encoding='utf-8')
    critique_path.write_text(critique_markdown, encoding='utf-8')
    summary_path.write_text(summary_markdown, encoding='utf-8')

    meta = {
        'checkpoint_id': ckp_id,
        'topic_id': topic.get('topic_id'),
        'topic_slug': topic_slug,
        'iteration': iteration,
        'report_path': str(report_path),
        'critique_path': str(critique_path),
        'summary_path': str(summary_path),
        'report_chars': len(report_markdown or ''),
        'score': float(score) if score is not None else None,
        'metrics': metrics or {},
        'selected_by_researcher': False,
        'selected_by_judge': False,
        'created_at': _now_iso(),
    }
    _write_json(cdir / f'{iteration:03d}-meta.json', meta)

    topic['checkpoint_count'] = iteration
    topic['updated_at'] = _now_iso()
    if topic.get('best_checkpoint_id') is None and score is not None:
        topic['best_checkpoint_id'] = ckp_id
    _write_json(topic_path, topic)

    append_operation_event(state_dir, project_root, operation_id, 'checkpoint_saved', {
        'topic_slug': topic_slug,
        'checkpoint_id': ckp_id,
        'iteration': iteration,
        'score': meta['score'],
    })
    rebuild_project_index(state_dir, project_root)
    return meta


def list_checkpoints(state_dir: Path, project_root: Path, operation_id: str, topic_slug: str) -> list[dict[str, Any]]:
    cdir = topic_dir(state_dir, project_root, operation_id, topic_slug) / 'checkpoints'
    items = []
    for meta_path in sorted(cdir.glob('*-meta.json')):
        meta = _read_json(meta_path, {})
        if meta:
            items.append(meta)
    return items


def mark_best_checkpoint(
    state_dir: Path,
    project_root: Path,
    operation_id: str,
    topic_slug: str,
    checkpoint_id: str,
    selector: str = 'judge',
) -> dict[str, Any]:
    cdir = topic_dir(state_dir, project_root, operation_id, topic_slug) / 'checkpoints'
    found = None
    for meta_path in sorted(cdir.glob('*-meta.json')):
        meta = _read_json(meta_path, {})
        if not meta:
            continue
        if meta.get('checkpoint_id') == checkpoint_id:
            if selector == 'judge':
                meta['selected_by_judge'] = True
            elif selector == 'researcher':
                meta['selected_by_researcher'] = True
            else:
                meta[f'selected_by_{selector}'] = True
            _write_json(meta_path, meta)
            found = meta
    if not found:
        return {}

    topic_path = topic_dir(state_dir, project_root, operation_id, topic_slug) / 'topic.json'
    topic = _read_json(topic_path, {})
    if topic:
        topic['best_checkpoint_id'] = checkpoint_id
        topic['updated_at'] = _now_iso()
        _write_json(topic_path, topic)

    append_operation_event(state_dir, project_root, operation_id, 'best_checkpoint_nominated', {
        'topic_slug': topic_slug,
        'checkpoint_id': checkpoint_id,
        'selector': selector,
    })
    rebuild_project_index(state_dir, project_root)
    return found


def finalize_delivery(
    state_dir: Path,
    project_root: Path,
    operation_id: str,
    *,
    topic_slug: str,
    checkpoint_id: str,
    note: str = '',
) -> dict[str, Any]:
    op_path = operation_dir(state_dir, project_root, operation_id) / 'operation.json'
    op = _read_json(op_path, {})
    if not op:
        return {}

    ckp = None
    for meta in list_checkpoints(state_dir, project_root, operation_id, topic_slug):
        if meta.get('checkpoint_id') == checkpoint_id:
            ckp = meta
            break
    if not ckp:
        return {}

    tdir = topic_dir(state_dir, project_root, operation_id, topic_slug)
    final_dir = tdir / 'final'
    final_dir.mkdir(parents=True, exist_ok=True)

    report_src = Path(str(ckp.get('report_path')))
    critique_src = Path(str(ckp.get('critique_path')))
    try:
        report_markdown = report_src.read_text(encoding='utf-8', errors='replace')
    except (OSError, ValueError) as exc:
        _diag(
            'libris_runtime',
            'selected checkpoint report is unreadable; delivery not created',
            error=exc,
            operation_id=operation_id,
            topic_slug=topic_slug,
            checkpoint_id=checkpoint_id,
        )
        return {}
    if not report_markdown.strip():
        _diag(
            'libris_runtime',
            'selected checkpoint report is empty; delivery not created',
            operation_id=operation_id,
            topic_slug=topic_slug,
            checkpoint_id=checkpoint_id,
        )
        return {}

    (final_dir / 'best-report.md').write_text(report_markdown, encoding='utf-8')
    if critique_src.is_file():
        try:
            critique_markdown = critique_src.read_text(encoding='utf-8', errors='replace')
            (final_dir / 'best-critique.md').write_text(critique_markdown, encoding='utf-8')
        except OSError as exc:
            _diag(
                'libris_runtime',
                'selected checkpoint critique is unreadable; delivering report without critique',
                error=exc,
                operation_id=operation_id,
                topic_slug=topic_slug,
                checkpoint_id=checkpoint_id,
            )
    (final_dir / 'delivery-note.md').write_text(note, encoding='utf-8')

    topic = _read_json(tdir / 'topic.json', {})
    topic_id = topic.get('topic_id')
    from charon.libris import libris_lifecycle as lifecycle

    def add_delivered_topic(current: dict[str, Any]) -> dict[str, Any]:
        delivered = list(current.get('delivered_topic_ids') or [])
        if topic_id and topic_id not in delivered:
            delivered.append(topic_id)
        current['delivered_topic_ids'] = delivered
        current['updated_at'] = _now_iso()
        return current

    lifecycle.mutate_projection(op_path, add_delivered_topic)

    append_operation_event(state_dir, project_root, operation_id, 'delivery_selected', {
        'topic_slug': topic_slug,
        'checkpoint_id': checkpoint_id,
    })
    rebuild_project_index(state_dir, project_root)
    return {
        'topic_slug': topic_slug,
        'checkpoint_id': checkpoint_id,
        'report_path': str(final_dir / 'best-report.md'),
        'critique_path': str(final_dir / 'best-critique.md'),
        'delivery_note_path': str(final_dir / 'delivery-note.md'),
    }


def update_operation_budget(
    state_dir: Path,
    project_root: Path,
    operation_id: str,
    budget: dict[str, Any] | None = None,
    model_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from charon.libris import libris_lifecycle as lifecycle

    path = operation_dir(state_dir, project_root, operation_id) / 'operation.json'
    if not path.exists():
        return {}

    def update_budget_projection(current: dict[str, Any]) -> dict[str, Any]:
        if budget is not None:
            merged_budget = dict(_normalize_budget(current.get('budget') or {}))
            merged_budget.update({
                key: value
                for key, value in _normalize_budget(budget).items()
                if value not in (0, 0.0) or key in (budget or {})
            })
            current['budget'] = merged_budget
        if model_policy is not None:
            merged_policy = dict(_normalize_model_policy(current.get('model_policy') or {}))
            merged_policy.update(_normalize_model_policy(model_policy))
            current['model_policy'] = merged_policy
        current['updated_at'] = _now_iso()
        return current

    op = lifecycle.mutate_projection(path, update_budget_projection)
    append_operation_event(state_dir, project_root, operation_id, 'operation_budget_updated', {
        'budget': op.get('budget') or {},
        'model_policy': op.get('model_policy') or {},
    })
    return op


def set_operation_status(
    state_dir: Path,
    project_root: Path,
    operation_id: str,
    status: str,
    note: str = '',
) -> dict[str, Any]:
    from charon.libris import libris_lifecycle as lifecycle

    path = operation_dir(state_dir, project_root, operation_id) / 'operation.json'
    op = _read_json(path, {})
    if not op:
        return {}
    target = str(status).strip() or str(op.get('status') or 'running')
    authoritative = _apply_lifecycle_projection(
        state_dir,
        operation_id,
        op,
        entity_type='operation',
    )
    machine, lifecycle_event = lifecycle.transition_lifecycle(
        state_dir,
        entity_type='operation',
        entity_id=operation_id,
        current_projected_status=str(authoritative.get('status') or op.get('status') or 'running'),
        target_projected_status=target,
        note=note,
        reason=note,
    )

    def mutate_projection(current: dict[str, Any]) -> dict[str, Any]:
        if int(current.get('lifecycle_revision') or -1) <= machine.revision:
            _persist_lifecycle_projection_fields(
                current,
                machine,
                lifecycle.projected_status(machine),
            )
        current['updated_at'] = _now_iso()
        return current

    op = lifecycle.mutate_projection(path, mutate_projection)
    append_operation_event(state_dir, project_root, operation_id, 'operation_status_updated', {
        'status': op['status'],
        'note': note[:500],
        'lifecycle_transition': lifecycle_event,
    }, event_id=str(lifecycle_event.get('event_id') or '') or None)
    rebuild_project_index(state_dir, project_root)
    return op


def update_operation_runtime(
    state_dir: Path,
    project_root: Path,
    operation_id: str,
    *,
    coordinator_agent_id: str | None = None,
    status: str | None = None,
    note: str = '',
) -> dict[str, Any]:
    from charon.libris import libris_lifecycle as lifecycle

    path = operation_dir(state_dir, project_root, operation_id) / 'operation.json'
    op = _read_json(path, {})
    if not op:
        return {}
    machine = None
    lifecycle_event: dict[str, Any] = {}
    if status is not None:
        authoritative = _apply_lifecycle_projection(
            state_dir,
            operation_id,
            op,
            entity_type='operation',
        )
        machine, lifecycle_event = lifecycle.transition_lifecycle(
            state_dir,
            entity_type='operation',
            entity_id=operation_id,
            current_projected_status=str(authoritative.get('status') or op.get('status') or 'running'),
            target_projected_status=str(status),
            note=note,
            reason=note,
        )

    def mutate_projection(current: dict[str, Any]) -> dict[str, Any]:
        if machine is not None and int(current.get('lifecycle_revision') or -1) <= machine.revision:
            _persist_lifecycle_projection_fields(
                current,
                machine,
                lifecycle.projected_status(machine),
            )
        if coordinator_agent_id is not None:
            current['coordinator_agent_id'] = coordinator_agent_id
        current['updated_at'] = _now_iso()
        return current

    op = lifecycle.mutate_projection(path, mutate_projection)
    append_operation_event(state_dir, project_root, operation_id, 'operation_runtime_updated', {
        'coordinator_agent_id': op.get('coordinator_agent_id', ''),
        'status': op.get('status', ''),
        'note': note[:500],
        'lifecycle_transition': lifecycle_event,
    }, event_id=str(lifecycle_event.get('event_id') or '') or None)
    rebuild_project_index(state_dir, project_root)
    return op


def adopt_legacy_incomplete_delivery(
    state_dir: Path,
    project_root: Path,
    operation_id: str,
    *,
    note: str = 'Repairing an incomplete legacy delivery.',
) -> dict[str, Any]:
    """Move a pre-FSM false delivery into authoritative active recovery."""
    from charon.libris import libris_lifecycle as lifecycle

    path = operation_dir(state_dir, project_root, operation_id) / 'operation.json'
    op = _read_json(path, {})
    if not op:
        return {}
    if str(op.get('status') or '') != 'delivered':
        raise ValueError('legacy delivery adoption requires a delivered projection')
    manifest = get_delivery_manifest(state_dir, project_root, operation_id, op=op)
    if manifest.get('ready'):
        return get_operation_state(state_dir, project_root, operation_id)
    machine = lifecycle.adopt_legacy_incomplete_delivery(
        state_dir,
        operation_id=operation_id,
        note=note,
    )

    def project_recovery(current: dict[str, Any]) -> dict[str, Any]:
        _persist_lifecycle_projection_fields(
            current,
            machine,
            lifecycle.projected_status(machine),
        )
        current['updated_at'] = _now_iso()
        return current

    op = lifecycle.mutate_projection(path, project_recovery)
    append_operation_event(state_dir, project_root, operation_id, 'legacy_delivery_reopened', {
        'previous_status': 'delivered',
        'status': op.get('status'),
        'note': note[:500],
    })
    rebuild_project_index(state_dir, project_root)
    return op


def record_usage(
    state_dir: Path,
    project_root: Path,
    operation_id: str,
    *,
    role: str = '',
    model: str = '',
    topic_slug: str = '',
    input_tokens: int = 0,
    output_tokens: int = 0,
    estimated_cost_usd: float | None = None,
    checkpoint_id: str = '',
    note: str = '',
) -> dict[str, Any]:
    path = operation_dir(state_dir, project_root, operation_id) / 'operation.json'
    op = _read_json(path, {})
    if not op:
        return {}

    inp = max(0, _coerce_int(input_tokens))
    out = max(0, _coerce_int(output_tokens))
    total = inp + out
    mdl = (model or '').strip() or 'unknown'
    rle = (role or '').strip() or 'unknown'
    cost = _coerce_float(estimated_cost_usd, _estimate_cost_usd(mdl, inp, out))

    from charon.libris import libris_lifecycle as lifecycle

    def add_operation_usage(current: dict[str, Any]) -> dict[str, Any]:
        usage = dict(_default_usage())
        usage.update(current.get('usage') or {})
        usage['input_tokens'] = _coerce_int(usage.get('input_tokens')) + inp
        usage['output_tokens'] = _coerce_int(usage.get('output_tokens')) + out
        usage['total_tokens'] = _coerce_int(usage.get('total_tokens')) + total
        usage['estimated_cost_usd'] = round(_coerce_float(usage.get('estimated_cost_usd')) + cost, 6)
        if not usage.get('cost_basis'):
            try:
                from charon.providers.model_registry import resolve_billing_mode
                usage['cost_basis'] = resolve_billing_mode(state_dir)
            except Exception as exc:
                _diag('libris_runtime', 'billing-mode resolution failed; cost_basis left unknown', error=exc)
        usage['updated_at'] = _now_iso()

        by_model = dict(usage.get('by_model') or {})
        model_usage = dict(by_model.get(mdl) or {
            'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0, 'estimated_cost_usd': 0.0,
        })
        model_usage['input_tokens'] += inp
        model_usage['output_tokens'] += out
        model_usage['total_tokens'] += total
        model_usage['estimated_cost_usd'] = round(_coerce_float(model_usage.get('estimated_cost_usd')) + cost, 6)
        by_model[mdl] = model_usage
        usage['by_model'] = by_model

        by_role = dict(usage.get('by_role') or {})
        role_usage = dict(by_role.get(rle) or {
            'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0, 'estimated_cost_usd': 0.0,
        })
        role_usage['input_tokens'] += inp
        role_usage['output_tokens'] += out
        role_usage['total_tokens'] += total
        role_usage['estimated_cost_usd'] = round(_coerce_float(role_usage.get('estimated_cost_usd')) + cost, 6)
        by_role[rle] = role_usage
        usage['by_role'] = by_role
        current['usage'] = usage
        current['updated_at'] = _now_iso()
        return current

    op = lifecycle.mutate_projection(path, add_operation_usage)

    event_payload = {
        'role': rle,
        'model': mdl,
        'topic_slug': topic_slug,
        'input_tokens': inp,
        'output_tokens': out,
        'total_tokens': total,
        'estimated_cost_usd': cost,
        'checkpoint_id': checkpoint_id,
        'note': note[:500],
    }
    append_operation_event(state_dir, project_root, operation_id, 'usage_recorded', event_payload)

    if topic_slug:
        tpath = topic_dir(state_dir, project_root, operation_id, topic_slug) / 'topic.json'
        if tpath.exists():
            def add_topic_usage(topic: dict[str, Any]) -> dict[str, Any]:
                tusage = dict(_default_usage())
                tusage.update(topic.get('usage') or {})
                tusage['input_tokens'] = _coerce_int(tusage.get('input_tokens')) + inp
                tusage['output_tokens'] = _coerce_int(tusage.get('output_tokens')) + out
                tusage['total_tokens'] = _coerce_int(tusage.get('total_tokens')) + total
                tusage['estimated_cost_usd'] = round(_coerce_float(tusage.get('estimated_cost_usd')) + cost, 6)
                tusage['updated_at'] = _now_iso()
                topic['usage'] = tusage
                topic['updated_at'] = _now_iso()
                return topic

            lifecycle.mutate_projection(tpath, add_topic_usage)

    budget_status = _evaluate_budget(op)
    if (not budget_status['continue_running']) and op.get('status') == 'running':
        op = set_operation_status(
            state_dir,
            project_root,
            operation_id,
            'budget_exhausted',
            ', '.join(budget_status['reasons']),
        )
        append_operation_event(state_dir, project_root, operation_id, 'operation_budget_exhausted', {
            'reasons': budget_status['reasons'],
            'usage': op.get('usage') or {},
        })

    rebuild_project_index(state_dir, project_root)
    state = get_operation_state(state_dir, project_root, operation_id)
    return {
        'operation_id': operation_id,
        'usage': state.get('usage') or {},
        'budget_status': state.get('budget_status') or {},
    }


def get_budget_status(state_dir: Path, project_root: Path, operation_id: str) -> dict[str, Any]:
    op = get_operation_state(state_dir, project_root, operation_id)
    if not op:
        return {}
    return op.get('budget_status') or _evaluate_budget(op)


# ── Search helpers ──────────────────────────────────────────────────


def _search_jsonl(rows: list[dict[str, Any]], fields: list[str], query: str, limit: int) -> list[dict[str, Any]]:
    q = query.strip().lower()
    if not q:
        return []
    scored: list[tuple[float, dict[str, Any]]] = []
    terms = [t for t in q.split() if t][:8]
    for row in rows:
        hay = ' '.join(str(row.get(f, '')) for f in fields).lower()
        if not hay:
            continue
        score = 0.0
        for term in terms:
            if term in hay:
                score += 1.0
        if 'lead_score' in row:
            score += float(row.get('lead_score') or 0)
        if score:
            scored.append((score, row))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [row for _, row in scored[:limit]]


def _score_text_signal(text: str, terms: list[str]) -> float:
    lower = (text or '').lower()
    if not lower:
        return 0.0
    score = 0.0
    for term in terms:
        if term and term in lower:
            score += 1.0
    return min(1.0, score / max(1, len(terms)))



def _normalize_url_for_dedupe(url: str) -> str:
    u = str(url or '').strip().lower()
    if not u:
        return ''
    u = re.sub(r'^https?://', '', u)
    u = re.sub(r'^www\.', '', u)
    u = u.split('#', 1)[0]
    u = re.sub(r'\?.*$', '', u)
    u = u.rstrip('/')
    return u



def _normalize_title_for_dedupe(title: str) -> str:
    t = re.sub(r'\s+', ' ', str(title or '').strip().lower())
    t = re.sub(r'[^a-z0-9 ]+', '', t)
    return t[:180]



def _source_duplicate_key(source: dict[str, Any]) -> str:
    url_key = _normalize_url_for_dedupe(str(source.get('url') or ''))
    if url_key:
        return f'url:{url_key}'
    title_key = _normalize_title_for_dedupe(str(source.get('title') or ''))
    authors = source.get('authors') or []
    auth_key = ','.join(str(a).strip().lower() for a in authors[:2] if str(a).strip())
    if title_key:
        return f'title:{title_key}|authors:{auth_key}'
    return ''



def _backend_quality_weight(backend: str) -> float:
    b = str(backend or '').lower()
    return {
        'openalex': 0.95,
        'semanticscholar': 0.93,
        'arxiv': 0.9,
        'github': 0.82,
        'web-search': 0.62,
    }.get(b, 0.58)



def _citation_signal(source: dict[str, Any]) -> float:
    raw = source.get('citation_count')
    if raw in (None, ''):
        raw = source.get('cited_by_count')
    try:
        n = int(raw or 0)
    except Exception:
        n = 0
    if n <= 0:
        return 0.2
    if n >= 500:
        return 1.0
    if n >= 100:
        return 0.85
    if n >= 25:
        return 0.65
    if n >= 5:
        return 0.45
    return 0.3



def score_source_lead(source: dict[str, Any], query: str = '') -> dict[str, Any]:
    terms = [t for t in (query or '').lower().split() if t][:8]
    title = str(source.get('title') or '')
    snippet = str(source.get('snippet') or source.get('abstract') or '')
    backend = str(source.get('backend') or '').lower()
    source_type = str(source.get('source_type') or '').lower()
    url = str(source.get('url') or '').lower()

    recency = 0.7
    published = str(source.get('published_at') or '')
    if published:
        if len(published) >= 4 and published[:4].isdigit():
            year = int(published[:4])
            recency = 1.0 if year >= 2025 else 0.9 if year >= 2024 else 0.72 if year >= 2023 else 0.55 if year >= 2022 else 0.35

    credibility = max(0.4, _backend_quality_weight(backend))
    if source_type in ('official', 'paper', 'repo'):
        credibility = max(credibility, 0.78)
    if 'github.com' in url:
        credibility = max(credibility, 0.7)
    if 'arxiv.org' in url:
        credibility = max(credibility, 0.82)

    query_fit = 0.3 + 0.7 * _score_text_signal(title + ' ' + snippet, terms)
    novelty = min(1.0, 0.45 + 0.35 * _score_text_signal(title + ' ' + snippet, terms) + 0.20 * recency)

    implementation_signal = 0.2
    if source_type == 'repo' or 'github.com' in url:
        implementation_signal = 0.98
    elif 'paperswithcode' in url:
        implementation_signal = 0.82
    elif source_type == 'official':
        implementation_signal = 0.68
    elif source_type == 'paper':
        implementation_signal = 0.42

    citation_signal = 0.25
    if backend in ('semanticscholar', 'openalex') or source_type == 'paper':
        citation_signal = _citation_signal(source)

    lead_score = round(
        0.20 * recency +
        0.22 * credibility +
        0.16 * novelty +
        0.16 * implementation_signal +
        0.18 * query_fit +
        0.08 * citation_signal,
        4,
    )

    action = 'deep_read' if lead_score >= 0.74 else 'monitor' if lead_score >= 0.54 else 'ignore'
    return {
        'lead_score': lead_score,
        'subscores': {
            'recency': round(recency, 4),
            'credibility': round(credibility, 4),
            'novelty': round(novelty, 4),
            'implementation_signal': round(implementation_signal, 4),
            'query_fit': round(query_fit, 4),
            'citation_signal': round(citation_signal, 4),
            'backend_quality': round(_backend_quality_weight(backend), 4),
        },
        'recommended_action': action,
    }


def search_sources(state_dir: Path, project_root: Path, query: str, limit: int = 10) -> list[dict[str, Any]]:
    rows = _iter_jsonl(research_root(state_dir, project_root) / 'sources' / 'sources.jsonl')
    return _search_jsonl(rows, ['title', 'url', 'source_type', 'topic_slug'], query, limit)


def search_claims(state_dir: Path, project_root: Path, query: str, limit: int = 10) -> list[dict[str, Any]]:
    rows = _iter_jsonl(research_root(state_dir, project_root) / 'claims.jsonl')
    return _search_jsonl(rows, ['text', 'topic_slug', 'source_id'], query, limit)


def index_promising_source(
    state_dir: Path,
    project_root: Path,
    *,
    operation_id: str,
    topic_slug: str,
    source: dict[str, Any],
    query: str = '',
) -> dict[str, Any]:
    ensure_research_tree(state_dir, project_root)
    scored = dict(source)
    scored.update(score_source_lead(source, query=query))
    duplicate_key = _source_duplicate_key(scored)
    scored.update({
        'lead_id': _new_id('lead'),
        'operation_id': operation_id,
        'topic_slug': topic_slug,
        'indexed_at': _now_iso(),
        'duplicate_key': duplicate_key,
        'normalized_url': _normalize_url_for_dedupe(str(scored.get('url') or '')),
        'normalized_title': _normalize_title_for_dedupe(str(scored.get('title') or '')),
        'query': query,
    })
    _append_jsonl(research_root(state_dir, project_root) / 'promising_sources.jsonl', scored)
    return scored


def _fuse_promising_source_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = str(row.get('duplicate_key') or _source_duplicate_key(row) or row.get('lead_id') or _new_id('lead'))
        existing = grouped.get(key)
        if not existing:
            merged = dict(row)
            merged['support_count'] = 1
            merged['backends'] = sorted({str(row.get('backend') or '').strip()} - {''})
            grouped[key] = merged
            continue
        existing['support_count'] = int(existing.get('support_count') or 1) + 1
        backends = set(existing.get('backends') or [])
        if row.get('backend'):
            backends.add(str(row.get('backend') or '').strip())
        existing['backends'] = sorted(b for b in backends if b)
        existing['lead_score'] = round(max(float(existing.get('lead_score') or 0.0), float(row.get('lead_score') or 0.0)) + min(0.12, 0.04 * (int(existing.get('support_count') or 1) - 1)), 4)
        # Prefer richer metadata when present.
        for field in ('snippet', 'abstract', 'published_at', 'url', 'title', 'source_type'):
            if (not existing.get(field)) and row.get(field):
                existing[field] = row.get(field)
        old_sub = existing.get('subscores') or {}
        new_sub = row.get('subscores') or {}
        merged_sub = dict(old_sub)
        for k, v in new_sub.items():
            try:
                merged_sub[k] = round(max(float(merged_sub.get(k) or 0.0), float(v or 0.0)), 4)
            except Exception:
                merged_sub[k] = v
        existing['subscores'] = merged_sub
        if float(row.get('lead_score') or 0.0) > float(existing.get('best_single_score') or existing.get('lead_score') or 0.0):
            existing['best_single_score'] = float(row.get('lead_score') or 0.0)
        existing['recommended_action'] = 'deep_read' if float(existing.get('lead_score') or 0.0) >= 0.74 else 'monitor' if float(existing.get('lead_score') or 0.0) >= 0.54 else 'ignore'
    out = list(grouped.values())
    out.sort(key=lambda r: (float(r.get('lead_score') or 0.0), int(r.get('support_count') or 1)), reverse=True)
    return out



def list_promising_sources(
    state_dir: Path,
    project_root: Path,
    *,
    operation_id: str = '',
    topic_slug: str = '',
    limit: int = 20,
) -> list[dict[str, Any]]:
    rows = _iter_jsonl(research_root(state_dir, project_root) / 'promising_sources.jsonl')
    out = []
    for row in rows:
        if operation_id and str(row.get('operation_id') or '') != operation_id:
            continue
        if topic_slug and str(row.get('topic_slug') or '') != topic_slug:
            continue
        out.append(row)
    return _fuse_promising_source_rows(out)[:limit]


def search_promising_sources(state_dir: Path, project_root: Path, query: str, limit: int = 10) -> list[dict[str, Any]]:
    rows = _fuse_promising_source_rows(_iter_jsonl(research_root(state_dir, project_root) / 'promising_sources.jsonl'))
    return _search_jsonl(rows, ['title', 'url', 'snippet', 'abstract', 'topic_slug', 'backend', 'source_type'], query, limit)


def list_topic_contracts(
    state_dir: Path,
    project_root: Path,
    *,
    operation_id: str = '',
    topic_slug: str = '',
    contract_type: str = '',
) -> list[dict[str, Any]]:
    # Prefer JSON contract storage because Libris uses metadata/contract_type fields
    # that may not yet be fully mirrored through the SQLite path.
    contracts = _read_json(state_dir / 'shade_contracts.json', [])
    if not isinstance(contracts, list):
        contracts = []

    out = []
    project_str = str(project_root)
    for ctr in contracts:
        if str(ctr.get('project') or '') != project_str:
            continue
        meta = ctr.get('metadata') or {}
        if operation_id and str(meta.get('operation_id') or '') != operation_id:
            continue
        if topic_slug and str(meta.get('topic_slug') or '') != topic_slug:
            continue
        if contract_type and str(ctr.get('contract_type') or '') != contract_type:
            continue
        out.append(ctr)
    return out


def summarize_contract_for_swarm(contract: dict[str, Any], state_dir: Path) -> dict[str, Any]:
    ctr_id = str(contract.get('id') or '')
    events = _iter_jsonl(state_dir / 'shade_phase_events.jsonl')
    ctr_events = [e for e in events if str(e.get('contract_id') or '') == ctr_id]
    latest = ctr_events[-1] if ctr_events else {}
    current_phase = {}
    current_phase_id = str(contract.get('current_phase_id') or '')
    for p in (contract.get('phases') or []):
        if str(p.get('phase_id') or '') == current_phase_id:
            current_phase = p
            break
    completed = sum(1 for p in (contract.get('phases') or []) if p.get('status') == 'completed')
    return {
        'contract_id': ctr_id,
        'contract_type': contract.get('contract_type') or '',
        'status': contract.get('status') or '',
        'current_phase_id': current_phase_id,
        'current_phase_name': current_phase.get('name') or '',
        'current_phase_objective': current_phase.get('objective') or '',
        'phase_count': int(contract.get('phase_count') or len(contract.get('phases') or [])),
        'completed_phases': completed,
        'shade_agent_id': contract.get('shade_agent_id') or '',
        'parent_agent_id': contract.get('parent_agent_id') or '',
        'metadata': contract.get('metadata') or {},
        'expected_outputs': contract.get('expected_outputs') or [],
        'last_event_type': latest.get('event_type') or '',
        'last_event_ts': latest.get('ts') or '',
        'last_event_payload': latest.get('payload') or {},
    }


def infer_candidate_topics(prompt: str, limit: int = 4) -> list[dict[str, Any]]:
    text = (prompt or '').strip()
    lower = text.lower()
    candidates: list[dict[str, Any]] = []

    def add(title: str, summary: str, why: str, novelty: str = 'medium', strength: str = 'medium') -> None:
        candidates.append({
            'topic_id': _new_id('top'),
            'title': title,
            'slug': _slug(title, 'topic'),
            'summary': summary,
            'why_interesting': why,
            'relevance_to_user': 'Potentially relevant to the stated research goal.',
            'evidence_strength': strength,
            'novelty': novelty,
            'recommended_action': 'deep_research',
        })

    if 'computer vision' in lower or 'vision' in lower:
        add('Vision-language model improvements', 'Recent VLM advances with stronger multimodal reasoning and grounding.', 'High practical relevance across modern CV systems.', 'high', 'medium')
        add('Efficient video generation and understanding', 'New techniques for video diffusion, transformers, and video reasoning.', 'Rapid movement in recent months and likely broad interest.', 'high', 'medium')
        add('Test-time adaptation and robustness in vision', 'Methods that improve robustness, adaptation, and deployment reliability.', 'Often high-value for real-world vision systems.', 'medium', 'medium')
        add('3D and world-model style vision representations', 'Emerging methods connecting vision, geometry, and action-oriented representations.', 'Potentially strategic if the user values forward-looking techniques.', 'high', 'low')
    elif 'reinforcement learning' in lower or 'rl' in lower:
        add('World-model based reinforcement learning', 'Recent work on latent planning and learned environment models.', 'Likely relevant to modern RL directions.', 'high', 'medium')
        add('Offline and dataset-driven RL', 'Recent techniques for learning from static or partially static data.', 'Often high practical leverage.', 'medium', 'medium')
        add('Test-time adaptation in RL', 'Methods that adapt policies online or near deployment.', 'Relevant when robustness and generalization matter.', 'medium', 'low')
        add('Preference and reward modeling alternatives', 'Methods adjacent to RLHF and preference optimization.', 'Broadly relevant to applied RL and alignment-adjacent work.', 'medium', 'medium')
    else:
        add('Emerging methods and trends', 'A broad candidate topic derived from the user request.', 'Useful fallback when the prompt is broad.', 'medium', 'low')
        add('Practical techniques with near-term applicability', 'Methods most likely to matter in implementation.', 'Useful for action-oriented users.', 'medium', 'low')

    return candidates[:max(1, limit)]


def _ckpt_norm_score(item: dict[str, Any]) -> float:
    """Coerce a checkpoint score to 0-1 (judges emit 0-10; some paths store 0-1)."""
    try:
        v = float(item.get('score') or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return v / 10.0 if v > 1.0 else v


def _ckpt_report_len(item: dict[str, Any]) -> int:
    """Length of a checkpoint's report. Prefers the stored report_chars (written
    at save time); falls back to reading the report file for older checkpoints."""
    n = item.get('report_chars')
    if isinstance(n, int) and n > 0:
        return n
    return len(_safe_read_text(str(item.get('report_path') or '')))


# A later checkpoint must beat the running best by more than this (0-1) for its
# score gain to justify a large loss of content; within it, a shorter report is
# treated as a hidden regression rather than an improvement.
_DEPTH_SCORE_MARGIN = 0.15
# A revision that keeps less than this fraction of the incumbent's length has
# "collapsed" — added breadth at the cost of depth the coarse score can't see.
_DEPTH_COLLAPSE_RATIO = 0.75


def select_best_checkpoint(state_dir: Path, project_root: Path, operation_id: str, topic_slug: str,
                           *, collapse_ratio: float = _DEPTH_COLLAPSE_RATIO,
                           score_margin: float = _DEPTH_SCORE_MARGIN) -> dict[str, Any]:
    """Pick the checkpoint to keep, hardened against silent regressions:

    1. Incumbent-preferring tie-break — highest score wins, but on a *tie* the
       EARLIER checkpoint keeps the crown. A new round must strictly beat the
       running best to take over, so an equal-scoring rewrite never displaces a
       proven report (this alone fixes the gut-microbiome regression, where a
       shorter round tied the score and was wrongly promoted by recency).
    2. Depth-collapse veto — a later checkpoint that shed a large fraction of its
       content (<collapse_ratio of the incumbent's length) for only a marginal
       score gain (<=score_margin over the incumbent) is a hidden regression the
       judge's coarse score misses; keep the fuller incumbent instead. A genuinely
       large score jump overrides the veto (we trust a decisive judge).
    3. Pairwise-rejected checkpoints (demoted by the in-loop pairwise gate,
       libris_pairwise) are excluded unless every checkpoint is rejected.
    """
    items = list_checkpoints(state_dir, project_root, operation_id, topic_slug)
    if not items:
        return {}

    live = [it for it in items if not it.get('pairwise_rejected')] or items

    # (1) highest score, earliest iteration on a tie
    ranked = sorted(live, key=lambda it: (_ckpt_norm_score(it), -int(it.get('iteration') or 0)),
                    reverse=True)
    leader = ranked[0]

    # (2) depth-collapse veto against the best strictly-earlier checkpoint
    leader_iter = int(leader.get('iteration') or 0)
    earlier = [it for it in live if int(it.get('iteration') or 0) < leader_iter]
    if earlier:
        incumbent = sorted(earlier, key=lambda it: (_ckpt_norm_score(it), -int(it.get('iteration') or 0)),
                           reverse=True)[0]
        inc_len = _ckpt_report_len(incumbent)
        if (inc_len > 0
                and _ckpt_report_len(leader) < inc_len * collapse_ratio
                and _ckpt_norm_score(leader) <= _ckpt_norm_score(incumbent) + score_margin):
            return incumbent
    return leader


def mark_checkpoint_pairwise_rejected(state_dir: Path, project_root: Path, operation_id: str,
                                      topic_slug: str, checkpoint_id: str, *,
                                      winner: str = '', reason: str = '',
                                      detail: dict[str, Any] | None = None) -> bool:
    """Demote a checkpoint the in-loop pairwise gate judged worse than the running
    best. select_best_checkpoint excludes pairwise-rejected checkpoints, so this
    keeps a higher-scoring-but-actually-worse round from being delivered."""
    cdir = topic_dir(state_dir, project_root, operation_id, topic_slug) / 'checkpoints'
    for meta_path in sorted(cdir.glob('*-meta.json')):
        meta = _read_json(meta_path, {})
        if meta.get('checkpoint_id') == checkpoint_id:
            meta['pairwise_rejected'] = True
            meta['pairwise_verdict'] = {'winner': winner, 'reason': reason, **(detail or {})}
            _write_json(meta_path, meta)
            append_operation_event(state_dir, project_root, operation_id, 'checkpoint_pairwise_rejected', {
                'topic_slug': topic_slug,
                'checkpoint_id': checkpoint_id,
                'winner': winner,
                'reason': reason,
            })
            return True
    return False


def revert_topic_draft_to_best(state_dir: Path, project_root: Path, operation_id: str,
                               topic_slug: str) -> bool:
    """Keep-if-better: make the working draft equal the best-scoring checkpoint's
    report, so a regressed revision round never leaves a worse draft in place and
    the next revision starts from the high-water mark. Returns True if the draft
    was changed. No-op if the latest checkpoint already IS the best."""
    items = list_checkpoints(state_dir, project_root, operation_id, topic_slug)
    if len(items) < 2:
        return False
    best = select_best_checkpoint(state_dir, project_root, operation_id, topic_slug)
    latest = items[-1]
    if not best or best.get('checkpoint_id') == latest.get('checkpoint_id'):
        return False  # latest is already the best — nothing to revert
    best_report = _safe_read_text(str(best.get('report_path') or ''))
    if not best_report.strip():
        return False
    tdir = topic_dir(state_dir, project_root, operation_id, topic_slug)
    draft_path = tdir / 'draft-report.md'
    if draft_path.exists() and draft_path.read_text(encoding='utf-8', errors='replace') == best_report:
        return False
    draft_path.write_text(best_report, encoding='utf-8')
    _write_json(tdir / 'draft-report.json', {
        'path': str(draft_path), 'topic_slug': topic_slug,
        'updated_at': _now_iso(),
        'note': f"reverted to best checkpoint {best.get('checkpoint_id')} (keep-if-better)",
    })
    append_operation_event(state_dir, project_root, operation_id, 'draft_reverted_to_best', {
        'topic_slug': topic_slug,
        'best_checkpoint_id': best.get('checkpoint_id'),
        'best_score': best.get('score'),
        'discarded_checkpoint_id': latest.get('checkpoint_id'),
        'discarded_score': latest.get('score'),
    })
    return True


def _safe_read_text(path_str: str) -> str:
    try:
        p = Path(str(path_str or ''))
        if p.exists():
            return p.read_text(encoding='utf-8', errors='replace')
    except Exception as e:
        _diag('libris_runtime', 'report text unreadable; treating as empty', error=e, path=path_str)
    return ''



def _extract_top_bullets(text: str, limit: int = 4) -> list[str]:
    bullets: list[str] = []
    for raw in (text or '').splitlines():
        s = raw.strip()
        if not s:
            continue
        if re.match(r'^[-*•]\s+', s):
            s = re.sub(r'^[-*•]\s+', '', s).strip()
            if len(s) >= 20:
                bullets.append(s[:240])
        elif re.match(r'^\d+[.)]\s+', s):
            s = re.sub(r'^\d+[.)]\s+', '', s).strip()
            if len(s) >= 20:
                bullets.append(s[:240])
        if len(bullets) >= limit:
            break
    if bullets:
        return bullets[:limit]
    parts = re.split(r'(?<=[.!?])\s+', (text or '').strip())
    return [p.strip()[:240] for p in parts if len(p.strip()) >= 30][:limit]



def build_operation_delivery_bundle(state_dir: Path, project_root: Path, operation_id: str, selections: list[dict[str, Any]]) -> dict[str, Any]:
    op = get_operation_state(state_dir, project_root, operation_id)
    if not op:
        return {}

    op_dir = operation_dir(state_dir, project_root, operation_id)
    bundle_dir = op_dir / 'delivery'
    bundle_dir.mkdir(parents=True, exist_ok=True)

    topic_lookup = {str(t.get('slug') or ''): t for t in (op.get('topics') or [])}
    ranked = sorted(selections, key=lambda s: (float(s.get('score') or 0.0), str(s.get('topic_slug') or '')), reverse=True)

    executive_lines = [
        '# Libris Executive Summary',
        '',
        f'- Operation ID: {operation_id}',
        f'- Status: {op.get("status") or "unknown"}',
        f'- Prompt: {str(op.get("prompt") or "").strip()}',
        f'- Delivered topics: {len(ranked)}',
        '',
        '## Ranked topics',
        '',
    ]

    bundle_topics = []
    for idx, sel in enumerate(ranked, start=1):
        slug = str(sel.get('topic_slug') or '')
        topic = topic_lookup.get(slug, {})
        checkpoint_id = str(sel.get('checkpoint_id') or '')
        score = sel.get('score')
        delivery = sel.get('delivery') or {}
        report_md = _safe_read_text(str(delivery.get('report_path') or ''))
        critique_md = _safe_read_text(str(delivery.get('critique_path') or ''))
        findings = _extract_top_bullets(report_md, limit=4)
        critiques = _extract_top_bullets(critique_md, limit=3)
        why = str(topic.get('why_interesting') or '').strip()

        executive_lines.append(f'### {idx}. {topic.get("title") or slug}')
        executive_lines.append(f'- Topic slug: {slug}')
        executive_lines.append(f'- Best checkpoint: {checkpoint_id}')
        executive_lines.append(f'- Score: {score}')
        if why:
            executive_lines.append(f'- Why selected: {why[:300]}')
        if findings:
            executive_lines.append('- Strongest findings:')
            executive_lines.extend([f'  - {f}' for f in findings[:3]])
        if critiques:
            executive_lines.append('- Remaining caveats:')
            executive_lines.extend([f'  - {c}' for c in critiques[:2]])
        executive_lines.append('')

        bundle_topics.append({
            'rank': idx,
            'topic_id': topic.get('topic_id'),
            'topic_slug': slug,
            'title': topic.get('title') or slug,
            'checkpoint_id': checkpoint_id,
            'score': score,
            'why_selected': why,
            'strongest_findings': findings,
            'remaining_caveats': critiques,
            'delivery': delivery,
        })

    overview = {
        'operation_id': operation_id,
        'prompt': op.get('prompt') or '',
        'status': op.get('status') or '',
        'topic_count': len(bundle_topics),
        'topics': bundle_topics,
        'generated_at': _now_iso(),
    }
    _write_json(bundle_dir / 'delivery-bundle.json', overview)
    (bundle_dir / 'executive-summary.md').write_text('\n'.join(executive_lines).strip() + '\n', encoding='utf-8')
    return {
        'bundle_dir': str(bundle_dir),
        'bundle_json_path': str(bundle_dir / 'delivery-bundle.json'),
        'executive_summary_path': str(bundle_dir / 'executive-summary.md'),
        'overview': overview,
    }


DELIVERABLE_TOPIC_STATUSES = {
    'checkpointed',
    'ready_high_confidence',
    'plateaued',
}

TERMINAL_TOPIC_STATUSES = {
    *DELIVERABLE_TOPIC_STATUSES,
    'judge_failed',
    'no_report',
    'excluded',
}


def _nonempty_file(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _file_attestation(path: Path, *, kind: str) -> dict[str, Any]:
    """Return stable evidence for one file included in a completion certificate."""
    resolved = path.resolve()
    data = resolved.read_bytes()
    if not data:
        raise ValueError(f'Cannot attest empty {kind}: {resolved}')
    return {
        'kind': kind,
        'path': str(resolved),
        'size_bytes': len(data),
        'sha256': hashlib.sha256(data).hexdigest(),
    }


def _certificate_digest(certificate: dict[str, Any]) -> str:
    body = {
        str(key): value
        for key, value in certificate.items()
        if key not in {'certificate_sha256', 'valid', 'path', 'reason'}
    }
    encoded = json.dumps(
        body,
        sort_keys=True,
        separators=(',', ':'),
        ensure_ascii=False,
    ).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def _delivery_completion_evidence(
    state_dir: Path,
    project_root: Path,
    operation_id: str,
    *,
    op: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Inspect delivery state without trusting the operation's projected status."""
    op = op or get_operation_state(state_dir, project_root, operation_id)
    if not op:
        return {'ready': False, 'reason': 'Operation state is unavailable.', 'checks': {}}

    op_dir = operation_dir(state_dir, project_root, operation_id)
    operation_root = op_dir.resolve()
    delivery_dir = op_dir / 'delivery'
    bundle_path = delivery_dir / 'delivery-bundle.json'
    summary_path = delivery_dir / 'executive-summary.md'
    html_path = delivery_dir / 'report.html'
    selection_path = op_dir / 'coordinator' / 'final-selection.md'
    bundle = _read_json(bundle_path, {})
    if not isinstance(bundle, dict):
        bundle = {}
    bundle_topics = bundle.get('topics') if isinstance(bundle.get('topics'), list) else []
    topics = [topic for topic in (op.get('topics') or []) if isinstance(topic, dict)]
    selected_ids = {str(value) for value in (op.get('selected_topic_ids') or []) if str(value)}
    delivered_ids = {str(value) for value in (op.get('delivered_topic_ids') or []) if str(value)}
    topic_ids = {str(topic.get('topic_id') or '') for topic in topics if str(topic.get('topic_id') or '')}
    deliverable = [
        topic for topic in topics
        if str(topic.get('status') or '') in DELIVERABLE_TOPIC_STATUSES
        and int(topic.get('checkpoint_count') or 0) > 0
    ]
    deliverable_ids = {
        str(topic.get('topic_id') or '') for topic in deliverable if str(topic.get('topic_id') or '')
    }
    deliverable_slugs = {
        str(topic.get('slug') or '') for topic in deliverable if str(topic.get('slug') or '')
    }
    bundle_slugs = {
        str(topic.get('topic_slug') or '')
        for topic in bundle_topics
        if isinstance(topic, dict) and str(topic.get('topic_slug') or '')
    }

    topic_reports_valid = True
    topic_attestations: list[dict[str, Any]] = []
    for item in bundle_topics:
        if not isinstance(item, dict):
            topic_reports_valid = False
            continue
        slug = str(item.get('topic_slug') or '')
        checkpoint_id = str(item.get('checkpoint_id') or '')
        delivery = item.get('delivery') if isinstance(item.get('delivery'), dict) else {}
        report_path = Path(str(delivery.get('report_path') or ''))
        topic = next((candidate for candidate in deliverable if str(candidate.get('slug') or '') == slug), {})
        if (
            not slug
            or not checkpoint_id
            or not topic
            or str(topic.get('best_checkpoint_id') or '') != checkpoint_id
            or not report_path.is_absolute()
            or not _path_is_within(report_path, operation_root)
            or not _nonempty_file(report_path)
        ):
            topic_reports_valid = False
            continue
        try:
            attestation = _file_attestation(report_path, kind='topic_report')
        except (OSError, ValueError):
            topic_reports_valid = False
            continue
        attestation.update({
            'topic_id': str(topic.get('topic_id') or ''),
            'topic_slug': slug,
            'checkpoint_id': checkpoint_id,
        })
        topic_attestations.append(attestation)

    checks = {
        'has_topics': bool(topics),
        'all_selected_topics_present': bool(selected_ids) and selected_ids == topic_ids,
        'all_topics_terminal': bool(topics) and all(
            str(topic.get('status') or '') in TERMINAL_TOPIC_STATUSES for topic in topics
        ),
        'has_deliverable_topics': bool(deliverable),
        'delivered_topics_match_deliverable_topics': bool(deliverable_ids) and delivered_ids == deliverable_ids,
        'bundle_topic_count_matches': (
            bool(bundle_topics)
            and int(bundle.get('topic_count') or 0) == len(bundle_topics) == len(deliverable)
        ),
        'bundle_topics_match_deliverable_topics': bool(deliverable_slugs) and bundle_slugs == deliverable_slugs,
        'topic_reports_nonempty': (
            topic_reports_valid and len(topic_attestations) == len(bundle_topics) == len(deliverable)
        ),
        'bundle_nonempty': _nonempty_file(bundle_path),
        'summary_nonempty': _nonempty_file(summary_path),
        'html_nonempty': _nonempty_file(html_path),
        'selection_nonempty': _nonempty_file(selection_path),
    }
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        return {
            'ready': False,
            'reason': f'Completion checks failed: {", ".join(failed)}.',
            'checks': checks,
            'topics': [],
            'artifacts': [],
        }

    try:
        artifacts = [
            _file_attestation(bundle_path, kind='bundle'),
            _file_attestation(summary_path, kind='summary'),
            _file_attestation(html_path, kind='report'),
            _file_attestation(selection_path, kind='selection'),
            *topic_attestations,
        ]
    except (OSError, ValueError) as exc:
        return {
            'ready': False,
            'reason': f'Could not attest delivery artifacts: {exc}',
            'checks': checks,
            'topics': [],
            'artifacts': [],
        }

    certified_topics = [
        {
            'topic_id': str(topic.get('topic_id') or ''),
            'topic_slug': str(topic.get('slug') or ''),
            'status': str(topic.get('status') or ''),
            'checkpoint_id': str(topic.get('best_checkpoint_id') or ''),
        }
        for topic in sorted(deliverable, key=lambda value: str(value.get('slug') or ''))
    ]
    return {
        'ready': True,
        'reason': '',
        'checks': checks,
        'selected_topic_ids': sorted(selected_ids),
        'delivered_topic_ids': sorted(delivered_ids),
        'topics': certified_topics,
        'artifacts': artifacts,
        'operation_updated_at': str(op.get('updated_at') or op.get('created_at') or ''),
    }


def issue_completion_certificate(
    state_dir: Path,
    project_root: Path,
    operation_id: str,
) -> dict[str, Any]:
    """Write a content-addressed certificate only after every delivery check passes."""
    evidence = _delivery_completion_evidence(state_dir, project_root, operation_id)
    if not evidence.get('ready'):
        return {
            'valid': False,
            'operation_id': operation_id,
            'reason': str(evidence.get('reason') or 'Delivery is not certifiable.'),
            'checks': evidence.get('checks') or {},
        }
    certificate = {
        'schema_version': 1,
        'operation_id': operation_id,
        'issued_at': str(evidence.get('operation_updated_at') or _now_iso()),
        'selected_topic_ids': evidence.get('selected_topic_ids') or [],
        'delivered_topic_ids': evidence.get('delivered_topic_ids') or [],
        'topic_count': len(evidence.get('topics') or []),
        'topics': evidence.get('topics') or [],
        'checks': evidence.get('checks') or {},
        'artifacts': evidence.get('artifacts') or [],
    }
    certificate['certificate_sha256'] = _certificate_digest(certificate)
    path = operation_dir(state_dir, project_root, operation_id) / 'delivery' / 'completion-certificate.json'
    _write_json_atomic(path, certificate)
    return {**certificate, 'valid': True, 'path': str(path.resolve()), 'reason': ''}


def validate_completion_certificate(
    state_dir: Path,
    project_root: Path,
    operation_id: str,
) -> dict[str, Any]:
    """Validate the certificate digest and every file attestation against disk."""
    path = operation_dir(state_dir, project_root, operation_id) / 'delivery' / 'completion-certificate.json'
    certificate = _read_json(path, {})
    if not isinstance(certificate, dict) or not certificate:
        return {'valid': False, 'path': str(path), 'reason': 'Completion certificate is missing.'}
    if str(certificate.get('operation_id') or '') != operation_id:
        return {'valid': False, 'path': str(path), 'reason': 'Completion certificate operation id does not match.'}
    expected_digest = str(certificate.get('certificate_sha256') or '')
    if not expected_digest or expected_digest != _certificate_digest(certificate):
        return {'valid': False, 'path': str(path), 'reason': 'Completion certificate digest does not match.'}

    for attestation in certificate.get('artifacts') or []:
        if not isinstance(attestation, dict):
            return {'valid': False, 'path': str(path), 'reason': 'Completion certificate has a malformed attestation.'}
        artifact_path = Path(str(attestation.get('path') or ''))
        if not artifact_path.is_absolute() or not _nonempty_file(artifact_path):
            return {'valid': False, 'path': str(path), 'reason': f'Certified artifact is missing: {artifact_path}'}
        try:
            current = _file_attestation(artifact_path, kind=str(attestation.get('kind') or 'artifact'))
        except (OSError, ValueError) as exc:
            return {'valid': False, 'path': str(path), 'reason': f'Could not validate certified artifact: {exc}'}
        if (
            int(attestation.get('size_bytes') or -1) != current['size_bytes']
            or str(attestation.get('sha256') or '') != current['sha256']
        ):
            return {'valid': False, 'path': str(path), 'reason': f'Certified artifact changed: {artifact_path}'}

    current = _delivery_completion_evidence(state_dir, project_root, operation_id)
    if not current.get('ready'):
        return {'valid': False, 'path': str(path), 'reason': str(current.get('reason') or 'Completion checks failed.')}
    if sorted(current.get('selected_topic_ids') or []) != sorted(certificate.get('selected_topic_ids') or []):
        return {'valid': False, 'path': str(path), 'reason': 'Selected topics changed after certification.'}
    if sorted(current.get('delivered_topic_ids') or []) != sorted(certificate.get('delivered_topic_ids') or []):
        return {'valid': False, 'path': str(path), 'reason': 'Delivered topics changed after certification.'}
    if current.get('topics') != certificate.get('topics'):
        return {'valid': False, 'path': str(path), 'reason': 'Certified topic state changed.'}
    if current.get('checks') != certificate.get('checks'):
        return {'valid': False, 'path': str(path), 'reason': 'Certified completion checks changed.'}
    if current.get('artifacts') != certificate.get('artifacts'):
        return {'valid': False, 'path': str(path), 'reason': 'Certified artifact evidence changed.'}
    return {**certificate, 'valid': True, 'path': str(path.resolve()), 'reason': ''}


def get_delivery_manifest(
    state_dir: Path,
    project_root: Path,
    operation_id: str,
    *,
    op: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a validated, UI-friendly description of an operation's outputs.

    Operation status alone is deliberately not enough to claim success.  Older
    runs could be marked ``delivered`` before any checkpoint was selected, so a
    ready manifest requires a non-empty bundle and the primary files on disk.
    """
    state_dir = Path(state_dir).resolve()
    project_root = Path(project_root).resolve()
    op = op or get_operation_state(state_dir, project_root, operation_id)
    if not op:
        return {
            'status': 'missing',
            'ready': False,
            'topic_count': 0,
            'artifact_count': 0,
            'primary_artifact': {},
            'artifacts': [],
            'reason': 'Operation state is unavailable.',
        }

    op_dir = operation_dir(state_dir, project_root, operation_id)
    delivery_dir = op_dir / 'delivery'
    bundle_path = delivery_dir / 'delivery-bundle.json'
    summary_path = delivery_dir / 'executive-summary.md'
    html_path = delivery_dir / 'report.html'
    selection_path = op_dir / 'coordinator' / 'final-selection.md'
    raw_bundle = _read_json(bundle_path, {}) if bundle_path.exists() else {}
    bundle = raw_bundle if isinstance(raw_bundle, dict) else {}
    raw_topics = bundle.get('topics')
    bundle_topics = raw_topics if isinstance(raw_topics, list) else []
    try:
        topic_count = int(bundle.get('topic_count') or 0)
    except (TypeError, ValueError):
        topic_count = 0

    artifacts: list[dict[str, Any]] = []

    def add_artifact(kind: str, label: str, path: Path | str, media_type: str) -> None:
        artifact_path = Path(str(path))
        if not artifact_path.exists() or not artifact_path.is_file():
            return
        artifacts.append({
            'kind': kind,
            'label': label,
            'path': str(artifact_path),
            'media_type': media_type,
            'exists': True,
        })

    add_artifact('report', 'Shareable HTML report', html_path, 'text/html')
    add_artifact('summary', 'Executive summary', summary_path, 'text/markdown')
    add_artifact('bundle', 'Delivery bundle', bundle_path, 'application/json')
    add_artifact('selection', 'Final selection', selection_path, 'text/markdown')

    valid_topic_report_count = 0
    for topic in bundle_topics:
        if not isinstance(topic, dict):
            continue
        delivery = topic.get('delivery') or {}
        if not isinstance(delivery, dict):
            continue
        slug = str(topic.get('topic_slug') or '')
        title = str(topic.get('title') or slug or 'Topic')
        for kind, label, key in (
            ('topic_report', f'{title} — report', 'report_path'),
            ('topic_critique', f'{title} — critique', 'critique_path'),
            ('topic_note', f'{title} — delivery note', 'delivery_note_path'),
        ):
            path = str(delivery.get(key) or '')
            if not path:
                continue
            before = len(artifacts)
            add_artifact(kind, label, path, 'text/markdown')
            if len(artifacts) > before:
                artifacts[-1]['topic_slug'] = slug
                artifacts[-1]['score'] = topic.get('score')
                if kind == 'topic_report':
                    try:
                        if Path(path).stat().st_size > 0:
                            valid_topic_report_count += 1
                    except OSError:
                        pass

    primary = next(
        (item for item in artifacts if item.get('kind') == 'report'),
        next((item for item in artifacts if item.get('kind') == 'summary'), {}),
    )
    status_claims_delivery = str(op.get('status') or '') == 'delivered'
    bundle_is_complete = bool(
        topic_count > 0
        and len(bundle_topics) == topic_count
        and all(isinstance(topic, dict) for topic in bundle_topics)
        and valid_topic_report_count == topic_count
    )
    base_artifacts_ready = bool(
        bundle_is_complete
        and bundle_path.is_file()
        and _nonempty_file(summary_path)
        and _nonempty_file(html_path)
        and primary
    )

    from charon.libris import libris_lifecycle as lifecycle

    lifecycle_instance = None
    lifecycle_error = ''
    try:
        lifecycle_instance = lifecycle.load_lifecycle(
            state_dir,
            operation_id,
            entity_type='operation',
        )
    except Exception as exc:
        lifecycle_error = str(exc)
        _diag(
            'libris_runtime',
            'delivery manifest could not load authoritative lifecycle',
            error=exc,
            operation_id=operation_id,
        )
    lifecycle_projection_present = any(
        key in op
        for key in (
            'lifecycle_state',
            'lifecycle_revision',
            'lifecycle_event_seq',
            'lifecycle_updated_at',
        )
    )
    if (
        not lifecycle_error
        and lifecycle_instance is None
        and lifecycle_projection_present
    ):
        lifecycle_error = 'Authoritative lifecycle snapshot is missing.'
    certificate = validate_completion_certificate(state_dir, project_root, operation_id)
    # A genuinely complete pre-FSM delivery is migrated lazily. Reconciliation
    # adds the certificate and canonical lifecycle, then refreshes compatibility
    # projections without changing the timestamps used by recent-operation
    # selection.
    if (
        not lifecycle_error
        and lifecycle_instance is None
        and status_claims_delivery
        and base_artifacts_ready
    ):
        if not certificate.get('valid'):
            certificate = issue_completion_certificate(state_dir, project_root, operation_id)
        if certificate.get('valid'):
            lifecycle_instance = lifecycle.adopt_legacy_completed_delivery(
                state_dir,
                operation_id=operation_id,
                operation_dir=op_dir,
                certificate=certificate,
            )
    certificate_required = bool(
        lifecycle_error or status_claims_delivery or lifecycle_instance is not None
    )
    certificate_valid = bool(certificate.get('valid'))
    lifecycle_certificate = (
        (lifecycle_instance.data or {}).get('completion_certificate')
        if lifecycle_instance is not None and lifecycle_instance.state == 'completed'
        else {}
    )
    certificate_bound_to_lifecycle = bool(
        lifecycle_instance is not None
        and lifecycle_instance.state == 'completed'
        and isinstance(lifecycle_certificate, dict)
        and str(lifecycle_certificate.get('path') or '') == str(certificate.get('path') or '')
        and str(lifecycle_certificate.get('sha256') or '') == str(certificate.get('certificate_sha256') or '')
        and int(lifecycle_certificate.get('topic_count') or 0) == int(certificate.get('topic_count') or 0)
    )
    if certificate_valid:
        add_artifact(
            'certificate',
            'Completion certificate',
            str(certificate.get('path') or ''),
            'application/json',
        )
    ready = bool(
        status_claims_delivery
        and base_artifacts_ready
        and certificate_valid
        and certificate_bound_to_lifecycle
    )
    active_topics = [
        str(topic.get('slug') or topic.get('title') or 'topic')
        for topic in (op.get('topics') or [])
        if str(topic.get('status') or '') not in TERMINAL_TOPIC_STATUSES
    ]

    if lifecycle_error:
        status = 'incomplete'
        reason = f'Authoritative lifecycle is unavailable: {lifecycle_error}'
    elif ready:
        status = 'ready'
        reason = ''
    elif status_claims_delivery:
        status = 'incomplete'
        reason = 'The operation claimed delivery, but no validated non-empty delivery is available.'
    elif str(op.get('status') or '') in ('reports_ready', 'assembling_delivery', 'verifying_delivery'):
        status = 'assembling'
        reason = 'Final reports are being assembled.'
    elif str(op.get('status') or '') in ('failed', 'delivery_failed', 'budget_exhausted', 'stopped'):
        status = 'incomplete'
        reason = 'The operation stopped before a validated delivery was produced.'
    else:
        status = 'working'
        reason = 'Research and review are still in progress.'

    manifest = {
        'operation_id': operation_id,
        'status': status,
        'ready': ready,
        'topic_count': topic_count,
        'artifact_count': len(artifacts),
        'primary_artifact': primary,
        'artifacts': artifacts,
        'bundle_path': str(bundle_path) if bundle_path.exists() else '',
        'active_topics': active_topics,
        'reason': reason,
        'completion_certificate': certificate,
        'integrity': {
            'lifecycle_valid': not bool(lifecycle_error),
            'lifecycle_error': lifecycle_error,
            'certificate_required': certificate_required,
            'certificate_valid': certificate_valid,
            'certificate_bound_to_lifecycle': certificate_bound_to_lifecycle,
            'certificate_sha256': str(certificate.get('certificate_sha256') or ''),
            'reason': lifecycle_error or str(certificate.get('reason') or ''),
        },
    }
    if ready:
        return _materialize_validated_delivery_outputs(
            state_dir,
            project_root,
            operation_id,
            manifest,
        )
    return manifest


def _materialize_validated_delivery_outputs(
    state_dir: Path,
    project_root: Path,
    operation_id: str,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    """Atomically refresh compatibility outputs for a validated completion."""
    from charon.libris import libris_lifecycle as lifecycle

    if not manifest.get('ready'):
        return dict(manifest)
    machine = lifecycle.load_lifecycle(
        state_dir,
        operation_id,
        entity_type='operation',
    )
    certificate = manifest.get('completion_certificate') or {}
    bound = (machine.data or {}).get('completion_certificate') if machine else {}
    if not (
        machine is not None
        and machine.state == 'completed'
        and isinstance(bound, dict)
        and str(bound.get('path') or '') == str(certificate.get('path') or '')
        and str(bound.get('sha256') or '')
        == str(certificate.get('certificate_sha256') or '')
        and int(bound.get('topic_count') or 0)
        == int(certificate.get('topic_count') or 0)
    ):
        return dict(manifest)

    op_dir = operation_dir(state_dir, project_root, operation_id)
    op_path = op_dir / 'operation.json'

    def project_completed(current: dict[str, Any]) -> dict[str, Any]:
        if int(current.get('lifecycle_revision') or -1) <= machine.revision:
            _persist_lifecycle_projection_fields(
                current,
                machine,
                lifecycle.projected_status(machine),
            )
        current['completion_certificate_path'] = str(
            certificate.get('path') or ''
        )
        current['completion_certificate_sha256'] = str(
            certificate.get('certificate_sha256') or ''
        )
        if not (machine.data or {}).get('adopted_legacy_projection'):
            current['updated_at'] = machine.updated_at
        return current

    lifecycle.mutate_projection(op_path, project_completed)
    materialized = dict(manifest)
    manifest_path = op_dir / 'delivery' / 'manifest.json'
    materialized['manifest_path'] = str(manifest_path)
    if _read_json(manifest_path, None) != materialized:
        _write_json_atomic(manifest_path, materialized)
    return materialized


def _reconcile_completed_delivery_outputs(
    state_dir: Path,
    project_root: Path,
    operation_id: str,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    """Materialize idempotent compatibility outputs from canonical completion.

    The lifecycle snapshot commits before ``operation.json``, the legacy
    timeline, and ``manifest.json``. A restart at any point after that commit
    can call this helper to recreate those projections without dispatching a
    second terminal transition.
    """
    from charon.libris import libris_lifecycle as lifecycle

    machine = lifecycle.load_lifecycle(
        state_dir,
        operation_id,
        entity_type='operation',
    )
    if machine is None or machine.state != 'completed':
        return dict(manifest)
    transition_id = str(
        (machine.data or {}).get('last_transition_event_id') or ''
    )
    processed = (machine.processed_events or {}).get(transition_id) or {}
    transition = processed.get('transition') or {}
    if not transition_id or not isinstance(transition, dict):
        raise RuntimeError(
            'completed Libris lifecycle has no recoverable terminal transition'
        )
    op_dir = operation_dir(state_dir, project_root, operation_id)
    materialized = _materialize_validated_delivery_outputs(
        state_dir,
        project_root,
        operation_id,
        manifest,
    )
    certificate = materialized.get('completion_certificate') or {}
    note = str((transition.get('payload') or {}).get('note') or '')
    append_operation_event(
        state_dir,
        project_root,
        operation_id,
        'operation_status_updated',
        {
            'status': 'delivered',
            'note': note,
            'lifecycle_transition': transition,
        },
        event_id=transition_id,
    )

    manifest_path = op_dir / 'delivery' / 'manifest.json'
    primary_path = str(
        (materialized.get('primary_artifact') or {}).get('path') or ''
    )
    append_operation_event(
        state_dir,
        project_root,
        operation_id,
        'final_deliveries_selected',
        {
            'count': int(materialized.get('topic_count') or 0),
            'executive_summary_path': str(
                op_dir / 'delivery' / 'executive-summary.md'
            ),
            'bundle_json_path': str(materialized.get('bundle_path') or ''),
            'primary_artifact_path': primary_path,
            'artifact_count': int(materialized.get('artifact_count') or 0),
            'manifest_path': str(manifest_path),
            'completion_certificate_path': str(certificate.get('path') or ''),
            'completion_certificate_sha256': str(
                certificate.get('certificate_sha256') or ''
            ),
        },
        event_id=f'{transition_id}:manifest',
    )
    return materialized



def finalize_operation_selection(state_dir: Path, project_root: Path, operation_id: str) -> dict[str, Any]:
    op = get_operation_state(state_dir, project_root, operation_id)
    if not op:
        return {}
    existing_manifest = get_delivery_manifest(
        state_dir,
        project_root,
        operation_id,
        op=op,
    )
    if existing_manifest.get('ready'):
        existing_manifest = _reconcile_completed_delivery_outputs(
            state_dir,
            project_root,
            operation_id,
            existing_manifest,
        )
        return {
            'operation_id': operation_id,
            'ready': True,
            'status': 'ready',
            'selections': [],
            'bundle': _read_json(
                operation_dir(state_dir, project_root, operation_id)
                / 'delivery'
                / 'delivery-bundle.json',
                {},
            ),
            'delivery_manifest': existing_manifest,
            'completion_certificate': existing_manifest.get('completion_certificate') or {},
            'idempotent': True,
        }
    if str(op.get('status') or '') == 'delivered':
        return {
            'operation_id': operation_id,
            'ready': False,
            'status': 'incomplete',
            'reason': str(existing_manifest.get('reason') or 'A completed delivery failed integrity validation.'),
            'active_topics': existing_manifest.get('active_topics') or [],
            'selections': [],
            'bundle': {},
            'delivery_manifest': existing_manifest,
        }

    topics = list(op.get('topics') or [])
    active_topics = [
        str(topic.get('slug') or topic.get('title') or 'topic')
        for topic in topics
        if str(topic.get('status') or '') not in TERMINAL_TOPIC_STATUSES
    ]
    if active_topics:
        return {
            'operation_id': operation_id,
            'ready': False,
            'status': 'waiting',
            'reason': 'Topic work is still active.',
            'active_topics': active_topics,
            'selections': [],
            'bundle': {},
        }

    selectable_topics = [
        topic
        for topic in topics
        if str(topic.get('status') or '') in DELIVERABLE_TOPIC_STATUSES
        and int(topic.get('checkpoint_count') or 0) > 0
    ]
    if not selectable_topics:
        return {
            'operation_id': operation_id,
            'ready': False,
            'status': 'incomplete',
            'reason': 'No reviewed topic checkpoint is available for delivery.',
            'active_topics': [],
            'selections': [],
            'bundle': {},
        }

    selections = []
    for topic in selectable_topics:
        slug = str(topic.get('slug') or '')
        if not slug:
            continue
        best = select_best_checkpoint(state_dir, project_root, operation_id, slug)
        if not best:
            continue
        delivery = finalize_delivery(
            state_dir,
            project_root,
            operation_id,
            topic_slug=slug,
            checkpoint_id=str(best.get('checkpoint_id') or ''),
            note=f'Coordinator selected checkpoint {best.get("checkpoint_id")} as best available version.',
        )
        if delivery:
            selections.append({
                'topic_slug': slug,
                'checkpoint_id': best.get('checkpoint_id'),
                'score': best.get('score'),
                'delivery': delivery,
            })
            mark_best_checkpoint(state_dir, project_root, operation_id, slug, str(best.get('checkpoint_id') or ''), selector='coordinator')

    if not selections:
        return {
            'operation_id': operation_id,
            'ready': False,
            'status': 'incomplete',
            'reason': 'Reviewed checkpoints exist, but none could be copied into the delivery.',
            'active_topics': [],
            'selections': [],
            'bundle': {},
        }
    if len(selections) != len(selectable_topics):
        return {
            'operation_id': operation_id,
            'ready': False,
            'status': 'incomplete',
            'reason': 'Not every deliverable topic could be copied into the delivery.',
            'active_topics': [],
            'selections': selections,
            'bundle': {},
        }

    op_dir = operation_dir(state_dir, project_root, operation_id)
    if str(op.get('status') or '') != 'verifying_delivery':
        set_operation_status(
            state_dir,
            project_root,
            operation_id,
            'assembling_delivery',
            'All terminal topic reports are being assembled.',
        )
    bundle = build_operation_delivery_bundle(state_dir, project_root, operation_id, selections)
    summary_lines = ['# Libris Final Selection', '']
    for idx, sel in enumerate(sorted(selections, key=lambda s: float(s.get('score') or 0.0), reverse=True), start=1):
        summary_lines.append(f'- {idx}. {sel["topic_slug"]}: {sel["checkpoint_id"]} (score={sel.get("score")})')
    if bundle.get('executive_summary_path'):
        summary_lines.extend(['', f'Executive summary: {bundle.get("executive_summary_path")}'])
    if bundle.get('bundle_json_path'):
        summary_lines.append(f'Delivery bundle JSON: {bundle.get("bundle_json_path")}')
    selection_path = op_dir / 'coordinator' / 'final-selection.md'
    selection_path.write_text('\n'.join(summary_lines), encoding='utf-8')

    try:
        from charon.libris.libris_report import render_operation
        report_html = render_operation(
            op_dir,
            title='Libris Research Report',
            subtitle=str(op.get('prompt') or '')[:300],
        )
        report_path = op_dir / 'delivery' / 'report.html'
        report_path.write_text(report_html, encoding='utf-8')
    except Exception as exc:
        _diag(
            'libris_runtime',
            'delivery HTML rendering failed; operation remains incomplete',
            error=exc,
            operation_id=operation_id,
        )
        return {
            'operation_id': operation_id,
            'ready': False,
            'status': 'incomplete',
            'reason': f'Could not render the shareable report: {exc}',
            'active_topics': [],
            'selections': selections,
            'bundle': bundle,
        }

    set_operation_status(
        state_dir,
        project_root,
        operation_id,
        'verifying_delivery',
        'Delivery artifacts are complete and undergoing final verification.',
    )
    certificate = issue_completion_certificate(
        state_dir,
        project_root,
        operation_id,
    )
    if not certificate.get('valid'):
        reason = str(certificate.get('reason') or 'Completion certificate could not be issued.')
        set_operation_status(
            state_dir,
            project_root,
            operation_id,
            'delivery_failed',
            reason,
        )
        return {
            'operation_id': operation_id,
            'ready': False,
            'status': 'incomplete',
            'reason': reason,
            'active_topics': [],
            'selections': selections,
            'bundle': bundle,
            'completion_certificate': certificate,
        }

    from charon.libris import libris_lifecycle as lifecycle

    current = get_operation_state(state_dir, project_root, operation_id)
    machine, lifecycle_event = lifecycle.complete_operation(
        state_dir,
        operation_id=operation_id,
        operation_dir=op_dir,
        current_projected_status=str(current.get('status') or 'verifying_delivery'),
        certificate=certificate,
        note='Coordinator selected and certified the final delivery.',
    )
    delivered_op = get_operation_state(state_dir, project_root, operation_id)
    manifest = get_delivery_manifest(
        state_dir,
        project_root,
        operation_id,
        op=delivered_op,
    )
    manifest = _reconcile_completed_delivery_outputs(
        state_dir,
        project_root,
        operation_id,
        manifest,
    )
    return {
        'operation_id': operation_id,
        'ready': bool(manifest.get('ready')),
        'status': str(manifest.get('status') or 'incomplete'),
        'selections': selections,
        'bundle': bundle,
        'delivery_manifest': manifest,
        'completion_certificate': certificate,
    }


def get_libris_swarm_state(state_dir: Path, project_root: Path, operation_id: str) -> dict[str, Any]:
    state_dir = Path(state_dir).resolve()
    project_root = Path(project_root).resolve()
    op = get_operation_state(state_dir, project_root, operation_id)
    if not op:
        return {}

    try:
        from charon.agents.agent_lifecycle import load_agents
        agents = load_agents(state_dir)
    except Exception:
        agents = []

    try:
        from charon.shade.shade_orchestrator import load_contracts
        contracts = load_contracts(state_dir)
    except Exception:
        contracts = []
    shade_phase_events = _iter_jsonl(state_dir / 'shade_phase_events.jsonl')
    contract_by_shade = {}
    for ctr in contracts:
        sid = str(ctr.get('shade_agent_id') or '')
        if sid:
            contract_by_shade[sid] = ctr

    agent_map = {str(a.get('id') or ''): a for a in agents}
    events = _iter_jsonl(operation_dir(state_dir, project_root, operation_id) / 'events.jsonl')

    phase_map: dict[str, dict[str, Any]] = {}
    live_line_map: dict[str, str] = {}

    def _one_line(text: Any) -> str:
        s = str(text or '').replace('\r', '\n')
        parts = [p.strip() for p in s.split('\n') if p.strip()]
        if not parts:
            return ''
        return parts[-1][:240]

    for evt in events:
        payload = evt.get('payload') or {}
        if evt.get('type') == 'agent_phase_changed':
            aid = str(payload.get('agent_id') or '')
            if aid:
                phase_map[aid] = payload
                live = _one_line(payload.get('summary') or payload.get('phase') or payload.get('status') or '')
                if live:
                    live_line_map[aid] = live
        elif evt.get('type') == 'agent_communication':
            src = str(payload.get('from_agent_id') or '')
            dst = str(payload.get('to_agent_id') or '')
            summary = _one_line(payload.get('summary') or payload.get('message_kind') or 'message')
            if src and summary:
                live_line_map[src] = f'→ {summary}'[:240]
            if dst and summary:
                live_line_map[dst] = f'← {summary}'[:240]
        elif evt.get('type') == 'checkpoint_saved':
            aid = str(payload.get('agent_id') or payload.get('judge_agent_id') or '')
            if aid:
                live_line_map[aid] = _one_line(payload.get('checkpoint_id') or 'checkpoint saved')
        elif evt.get('type') == 'draft_report_saved':
            aid = str(payload.get('agent_id') or payload.get('researcher_agent_id') or '')
            if aid:
                live_line_map[aid] = _one_line(payload.get('path') or 'draft report saved')
        elif evt.get('type') == 'best_checkpoint_nominated':
            aid = str(payload.get('agent_id') or payload.get('judge_agent_id') or '')
            if aid:
                live_line_map[aid] = _one_line(payload.get('checkpoint_id') or 'best checkpoint nominated')

    for evt in shade_phase_events:
        payload = evt.get('payload') or {}
        shade_id = str(evt.get('shade_agent_id') or payload.get('shade_agent_id') or '')
        summary = _one_line(payload.get('summary') or evt.get('summary') or evt.get('phase_name') or evt.get('event') or '')
        if shade_id and summary:
            live_line_map[shade_id] = summary

    def _agent_card(agent_id: str, role: str, topic_slug: str = '') -> dict[str, Any]:
        a = agent_map.get(agent_id or '', {})
        phase_info = phase_map.get(agent_id or '', {})
        contract = contract_by_shade.get(agent_id or '', {}) if role == 'shade' else {}
        current_phase_id = str(contract.get('current_phase_id') or '')
        contract_phase = {}
        for p in (contract.get('phases') or []):
            if str(p.get('phase_id') or '') == current_phase_id:
                contract_phase = p
                break
        return {
            'agent_id': agent_id,
            'name': a.get('name') or agent_id,
            'role': role,
            'specialization': f'libris-{role}' if role else '',
            'status': phase_info.get('status') or a.get('status') or ('running' if agent_id else 'idle'),
            'phase': phase_info.get('phase') or contract_phase.get('name') or '',
            'goal': a.get('goal') or '',
            'project': a.get('project') or str(project_root),
            'topic_slug': topic_slug,
            'source': a.get('source') or 'virtual',
            'hasTmux': bool(a.get('tmux_session')),
            'phase_summary': phase_info.get('summary') or contract_phase.get('objective') or '',
            'live_line': live_line_map.get(agent_id or '', ''),
            'parent_agent_id': a.get('parent_agent_id') or '',
            'contract_id': contract.get('id') or '',
            'contract_type': contract.get('contract_type') or '',
            'contract_status': contract.get('status') or '',
            'contract_current_phase_id': current_phase_id,
            'contract_expected_outputs': contract.get('expected_outputs') or [],
            'contract_metadata': contract.get('metadata') or {},
        }

    coordinator = _agent_card(str(op.get('coordinator_agent_id') or ''), 'coordinator') if op.get('coordinator_agent_id') else {}
    topic_cards = []
    members = []
    if coordinator:
        members.append(coordinator)

    for topic in op.get('topics') or []:
        slug = str(topic.get('slug') or '')
        researcher = _agent_card(str(topic.get('researcher_agent_id') or ''), 'researcher', slug) if topic.get('researcher_agent_id') else {}
        judge = _agent_card(str(topic.get('judge_agent_id') or ''), 'judge', slug) if topic.get('judge_agent_id') else {}
        shades = []
        for a in agents:
            if str(a.get('role') or '') != 'shade':
                continue
            goal = str(a.get('goal') or '').lower()
            card = _agent_card(str(a.get('id') or ''), 'shade', slug)
            meta = card.get('contract_metadata') or {}
            contract_topic = str(meta.get('topic_slug') or '')
            if slug and (slug.lower() in goal or contract_topic == slug):
                shades.append(card)
        contract_summaries = [
            summarize_contract_for_swarm(c, state_dir)
            for c in list_topic_contracts(state_dir, project_root, operation_id=operation_id, topic_slug=slug)
        ]
        card = {
            'topic_slug': slug,
            'title': topic.get('title') or slug,
            'status': topic.get('status') or 'pending',
            'phase': 'judging' if topic.get('checkpoint_count') else ('drafting' if topic.get('draft_report_path') else 'researching'),
            'checkpoint_count': int(topic.get('checkpoint_count') or 0),
            'best_checkpoint_id': topic.get('best_checkpoint_id'),
            'draft_report_path': topic.get('draft_report_path') or '',
            'researcher': researcher,
            'judge': judge,
            'shades': shades,
            'contracts': contract_summaries,
        }
        topic_cards.append(card)
        if researcher:
            members.append(researcher)
        if judge:
            members.append(judge)
        members.extend(shades)

    non_shade_members = [m for m in members if str(m.get('role') or '') != 'shade']
    team_grid = []
    if coordinator:
        team_grid.append(coordinator)
    for tc in topic_cards:
        researcher = tc.get('researcher') or {}
        judge = tc.get('judge') or {}
        if researcher:
            team_grid.append(researcher)
        if judge:
            team_grid.append(judge)

    final_selection_path = operation_dir(state_dir, project_root, operation_id) / 'coordinator' / 'final-selection.md'
    delivery_dir = operation_dir(state_dir, project_root, operation_id) / 'delivery'
    executive_summary_path = delivery_dir / 'executive-summary.md'
    delivery_bundle_path = delivery_dir / 'delivery-bundle.json'
    try:
        final_selection = final_selection_path.read_text(encoding='utf-8') if final_selection_path.exists() else ''
    except Exception as e:
        _diag('libris_runtime', 'final-selection.md exists but unreadable; omitted from swarm state', error=e)
        final_selection = ''
    try:
        executive_summary = executive_summary_path.read_text(encoding='utf-8') if executive_summary_path.exists() else ''
    except Exception as e:
        _diag('libris_runtime', 'executive-summary.md exists but unreadable; omitted from swarm state', error=e)
        executive_summary = ''
    delivery_bundle = _read_json(delivery_bundle_path, {}) if delivery_bundle_path.exists() else {}
    delivery_manifest = get_delivery_manifest(
        state_dir,
        project_root,
        operation_id,
        op=op,
    )
    edges = []
    edge_map: dict[tuple[str, str, str], dict[str, Any]] = {}
    for evt in events:
        if evt.get('type') != 'agent_communication':
            continue
        payload = evt.get('payload') or {}
        src = str(payload.get('from_agent_id') or '')
        dst = str(payload.get('to_agent_id') or '')
        topic_slug = str(payload.get('topic_slug') or '')
        if not src or not dst:
            continue
        key = (src, dst, topic_slug)
        edge_map[key] = {
            'from_agent_id': src,
            'to_agent_id': dst,
            'from_role': payload.get('from_role') or '',
            'to_role': payload.get('to_role') or '',
            'topic_slug': topic_slug,
            'message_kind': payload.get('message_kind') or 'handoff',
            'summary': payload.get('summary') or '',
            'last_active_at': evt.get('timestamp') or '',
        }
    now_dt = datetime.now(timezone.utc)
    # Add implicit return edges from completed/active shade contracts to their parent agent.
    latest_contract_evt: dict[str, dict[str, Any]] = {}
    for evt in shade_phase_events:
        cid = str(evt.get('contract_id') or '')
        if cid:
            latest_contract_evt[cid] = evt
    for ctr in contracts:
        meta = ctr.get('metadata') or {}
        topic_slug = str(meta.get('topic_slug') or '')
        if str(ctr.get('project') or '') != str(project_root):
            continue
        if ctr.get('shade_agent_id') and ctr.get('parent_agent_id'):
            key = (str(ctr.get('shade_agent_id')), str(ctr.get('parent_agent_id')), topic_slug)
            if key not in edge_map:
                evt = latest_contract_evt.get(str(ctr.get('id') or ''), {})
                edge_map[key] = {
                    'from_agent_id': str(ctr.get('shade_agent_id') or ''),
                    'to_agent_id': str(ctr.get('parent_agent_id') or ''),
                    'from_role': 'shade',
                    'to_role': 'researcher',
                    'topic_slug': topic_slug,
                    'message_kind': 'contract_progress' if ctr.get('status') == 'running' else 'contract_return',
                    'summary': str((evt.get('payload') or {}).get('summary') or ctr.get('goal') or '')[:500],
                    'last_active_at': evt.get('ts') or ctr.get('updated_at') or ctr.get('created_at') or '',
                }

    for edge in edge_map.values():
        ts = edge.get('last_active_at') or ''
        strength = 0.15
        active_now = False
        try:
            dt = datetime.fromisoformat(ts)
            age = max(0.0, (now_dt - dt).total_seconds())
            if age <= 2:
                strength = 1.0
                active_now = True
            elif age <= 10:
                strength = 0.75
            elif age <= 30:
                strength = 0.45
            else:
                strength = 0.18
        except Exception:
            pass
        edge['activity_strength'] = strength
        edge['active_now'] = active_now
        edges.append(edge)

    from charon.libris import libris_lifecycle as lifecycle
    from charon.orchestration.fsm import project_machine

    manifest_integrity = dict(delivery_manifest.get('integrity') or {})
    lifecycle_error = str(manifest_integrity.get('lifecycle_error') or '')
    lifecycle_instance = None
    lifecycle_events = []
    if not lifecycle_error:
        try:
            lifecycle_instance = lifecycle.load_lifecycle(
                state_dir,
                operation_id,
                entity_type='operation',
            )
            if lifecycle_instance is not None:
                from charon.orchestration.fsm_store import DurableMachineStore

                lifecycle_events = DurableMachineStore(
                    state_dir,
                    lifecycle.OPERATION_MACHINE,
                ).events(operation_id)
        except Exception as exc:
            lifecycle_error = str(exc)
            _diag(
                'libris_runtime',
                'swarm state could not load authoritative lifecycle',
                error=exc,
                operation_id=operation_id,
            )
    if lifecycle_error:
        manifest_integrity['lifecycle_valid'] = False
        manifest_integrity['lifecycle_error'] = lifecycle_error
        manifest_integrity['reason'] = lifecycle_error
    lifecycle_graph = project_machine(lifecycle.OPERATION_MACHINE, lifecycle_instance)
    lifecycle_view = {
        'state': (
            lifecycle_instance.state
            if lifecycle_instance is not None
            else ('lifecycle_error' if lifecycle_error else '')
        ),
        'projected_status': op.get('status') or 'unknown',
        'revision': lifecycle_instance.revision if lifecycle_instance is not None else None,
        'event_seq': lifecycle_instance.event_seq if lifecycle_instance is not None else None,
        'last_transition': lifecycle_events[-1] if lifecycle_events else {},
        'integrity': manifest_integrity,
        'completion_certificate': delivery_manifest.get('completion_certificate') or {},
    }

    return {
        'operation_id': operation_id,
        'prompt': op.get('prompt') or '',
        'status': op.get('status') or 'unknown',
        'budget_status': op.get('budget_status') or _evaluate_budget(op),
        'candidate_topics_count': len(op.get('candidate_topics') or []),
        'coordinator': coordinator,
        'topics': topic_cards,
        'members': members,
        'nodes': members,
        'non_shade_members': non_shade_members,
        'team_grid_nodes': team_grid,
        'views': {
            'grid': {
                'kind': 'non_shade_team_grid',
                'description': 'Coordinator + researcher/judge cells for quick session switching.',
                'nodes': team_grid,
            },
            'graph': {
                'kind': 'topic_cluster_graph',
                'description': 'Coordinator / topic / shade topology with communication edges.',
                'nodes': members,
                'edges': edges,
            },
        },
        'counts': {
            'topics': len(topic_cards),
            'members': len(members),
            'non_shade_members': len(non_shade_members),
            'shades': sum(1 for m in members if str(m.get('role') or '') == 'shade'),
            'edges': len(edges),
        },
        'edges': edges,
        'events_tail': events[-50:],
        'promising_sources': list_promising_sources(state_dir, project_root, operation_id=operation_id, limit=50),
        'final_selection_markdown': final_selection if isinstance(final_selection, str) else '',
        'executive_summary_markdown': executive_summary if isinstance(executive_summary, str) else '',
        'delivery_bundle': delivery_bundle if isinstance(delivery_bundle, dict) else {},
        'delivery_manifest': delivery_manifest,
        'lifecycle': lifecycle_view,
        'lifecycle_graph': lifecycle_graph,
        'workflow_graph': lifecycle_graph,
    }
