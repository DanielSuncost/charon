from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ── Helpers ─────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
    except Exception:
        return default


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding='utf-8')


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as f:
        f.write(json.dumps(row, ensure_ascii=False) + '\n')


def _iter_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
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
    except Exception:
        return []
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

from project_registry_loader import load_ensure_project

_ensure_project_registry = load_ensure_project(__file__, 'libris_runtime')


def derive_project_id(project_root: Path) -> str:
    state_dir = project_root.parent / '.charon_state'
    try:
        proj = _ensure_project_registry(state_dir, project_root, provisional=True)
        return str(proj.get('id') or '') or _slug(project_root.name or 'project', 'project', 48)
    except Exception:
        base = _slug(project_root.name or 'project', 'project', 48)
        return f'{base}-{_short_hash(str(project_root.resolve()))}'


def project_dir(state_dir: Path, project_root: Path) -> Path:
    return state_dir / 'projects' / derive_project_id(project_root)


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
            'project_id': derive_project_id(project_root),
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
) -> dict[str, Any]:
    row = {
        'event_id': _new_id('evt'),
        'operation_id': operation_id,
        'type': event_type,
        'timestamp': _now_iso(),
        'payload': payload or {},
    }
    _append_jsonl(operation_dir(state_dir, project_root, operation_id) / 'events.jsonl', row)
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
        'project_id': derive_project_id(project_root),
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

    op_id = f'rop_{datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")}_{uuid.uuid4().hex[:4]}'
    op_dir = operation_dir(state_dir, project_root, op_id)
    (op_dir / 'coordinator').mkdir(parents=True, exist_ok=True)
    (op_dir / 'topics').mkdir(parents=True, exist_ok=True)

    doc = {
        'operation_id': op_id,
        'project_id': derive_project_id(project_root),
        'prompt': str(prompt).strip(),
        'mode': mode,
        'status': 'running',
        'coordinator_agent_id': coordinator_agent_id,
        'created_at': _now_iso(),
        'updated_at': _now_iso(),
        'stop_requested': False,
        'selected_topic_ids': [],
        'delivered_topic_ids': [],
        'budget': _normalize_budget(budget),
        'model_policy': _normalize_model_policy(model_policy),
        'usage': _default_usage(),
    }
    _write_json(op_dir / 'operation.json', doc)
    append_operation_event(state_dir, project_root, op_id, 'operation_started', {
        'prompt': str(prompt).strip()[:500],
        'mode': mode,
        'coordinator_agent_id': coordinator_agent_id,
        'budget': doc['budget'],
        'model_policy': doc['model_policy'],
    })
    rebuild_project_index(state_dir, project_root)
    return doc


def get_operation_state(state_dir: Path, project_root: Path, operation_id: str) -> dict[str, Any]:
    op_dir = operation_dir(state_dir, project_root, operation_id)
    op = _read_json(op_dir / 'operation.json', {})
    if not op:
        return {}
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
    path = operation_dir(state_dir, project_root, operation_id) / 'operation.json'
    op = _read_json(path, {})
    if not op:
        return {}
    op['stop_requested'] = True
    op['updated_at'] = _now_iso()
    _write_json(path, op)
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
        'created_at': _now_iso(),
        'updated_at': _now_iso(),
    }
    _write_json(tdir / 'topic.json', doc)

    selected = list(op.get('selected_topic_ids') or [])
    selected.append(topic_id)
    op['selected_topic_ids'] = selected
    op['updated_at'] = _now_iso()
    _write_json(op_path, op)

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
    path = topic_dir(state_dir, project_root, operation_id, topic_slug) / 'topic.json'
    topic = _read_json(path, {})
    if not topic:
        return {}
    if status is not None:
        topic['status'] = status
    if researcher_agent_id is not None:
        topic['researcher_agent_id'] = researcher_agent_id
    if judge_agent_id is not None:
        topic['judge_agent_id'] = judge_agent_id
    if extras:
        for k, v in extras.items():
            topic[str(k)] = v
    topic['updated_at'] = _now_iso()
    _write_json(path, topic)
    append_operation_event(state_dir, project_root, operation_id, 'topic_runtime_updated', {
        'topic_slug': topic_slug,
        'status': topic.get('status'),
        'researcher_agent_id': topic.get('researcher_agent_id', ''),
        'judge_agent_id': topic.get('judge_agent_id', ''),
        'extras': extras or {},
    })
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
        'project_id': derive_project_id(project_root),
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
    entity_refs: list[str] | None = None,
) -> dict[str, Any]:
    ensure_research_tree(state_dir, project_root)
    row = {
        'claim_id': _new_id('clm'),
        'project_id': derive_project_id(project_root),
        'operation_id': operation_id,
        'topic_slug': topic_slug,
        'source_id': source_id,
        'text': text.strip(),
        'confidence': confidence,
        'stance': stance,
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
    if report_src.exists():
        (final_dir / 'best-report.md').write_text(report_src.read_text(encoding='utf-8', errors='replace'), encoding='utf-8')
    if critique_src.exists():
        (final_dir / 'best-critique.md').write_text(critique_src.read_text(encoding='utf-8', errors='replace'), encoding='utf-8')
    (final_dir / 'delivery-note.md').write_text(note, encoding='utf-8')

    delivered = list(op.get('delivered_topic_ids') or [])
    topic = _read_json(tdir / 'topic.json', {})
    topic_id = topic.get('topic_id')
    if topic_id and topic_id not in delivered:
        delivered.append(topic_id)
    op['delivered_topic_ids'] = delivered
    op['updated_at'] = _now_iso()
    _write_json(op_path, op)

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
    path = operation_dir(state_dir, project_root, operation_id) / 'operation.json'
    op = _read_json(path, {})
    if not op:
        return {}
    if budget is not None:
        merged_budget = dict(_normalize_budget(op.get('budget') or {}))
        merged_budget.update({k: v for k, v in _normalize_budget(budget).items() if v not in (0, 0.0) or k in (budget or {})})
        op['budget'] = merged_budget
    if model_policy is not None:
        merged_policy = dict(_normalize_model_policy(op.get('model_policy') or {}))
        merged_policy.update(_normalize_model_policy(model_policy))
        op['model_policy'] = merged_policy
    op['updated_at'] = _now_iso()
    _write_json(path, op)
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
    path = operation_dir(state_dir, project_root, operation_id) / 'operation.json'
    op = _read_json(path, {})
    if not op:
        return {}
    op['status'] = str(status).strip() or op.get('status') or 'running'
    op['updated_at'] = _now_iso()
    _write_json(path, op)
    append_operation_event(state_dir, project_root, operation_id, 'operation_status_updated', {
        'status': op['status'],
        'note': note[:500],
    })
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
    path = operation_dir(state_dir, project_root, operation_id) / 'operation.json'
    op = _read_json(path, {})
    if not op:
        return {}
    if coordinator_agent_id is not None:
        op['coordinator_agent_id'] = coordinator_agent_id
    if status is not None:
        op['status'] = status
    op['updated_at'] = _now_iso()
    _write_json(path, op)
    append_operation_event(state_dir, project_root, operation_id, 'operation_runtime_updated', {
        'coordinator_agent_id': op.get('coordinator_agent_id', ''),
        'status': op.get('status', ''),
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

    usage = dict(_default_usage())
    usage.update(op.get('usage') or {})
    usage['input_tokens'] = _coerce_int(usage.get('input_tokens')) + inp
    usage['output_tokens'] = _coerce_int(usage.get('output_tokens')) + out
    usage['total_tokens'] = _coerce_int(usage.get('total_tokens')) + total
    usage['estimated_cost_usd'] = round(_coerce_float(usage.get('estimated_cost_usd')) + cost, 6)
    usage['updated_at'] = _now_iso()

    by_model = dict(usage.get('by_model') or {})
    md = dict(by_model.get(mdl) or {'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0, 'estimated_cost_usd': 0.0})
    md['input_tokens'] += inp
    md['output_tokens'] += out
    md['total_tokens'] += total
    md['estimated_cost_usd'] = round(_coerce_float(md.get('estimated_cost_usd')) + cost, 6)
    by_model[mdl] = md
    usage['by_model'] = by_model

    by_role = dict(usage.get('by_role') or {})
    rl = dict(by_role.get(rle) or {'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0, 'estimated_cost_usd': 0.0})
    rl['input_tokens'] += inp
    rl['output_tokens'] += out
    rl['total_tokens'] += total
    rl['estimated_cost_usd'] = round(_coerce_float(rl.get('estimated_cost_usd')) + cost, 6)
    by_role[rle] = rl
    usage['by_role'] = by_role

    op['usage'] = usage
    op['updated_at'] = _now_iso()
    _write_json(path, op)

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
        topic = _read_json(tpath, {})
        if topic:
            tusage = dict(_default_usage())
            tusage.update(topic.get('usage') or {})
            tusage['input_tokens'] = _coerce_int(tusage.get('input_tokens')) + inp
            tusage['output_tokens'] = _coerce_int(tusage.get('output_tokens')) + out
            tusage['total_tokens'] = _coerce_int(tusage.get('total_tokens')) + total
            tusage['estimated_cost_usd'] = round(_coerce_float(tusage.get('estimated_cost_usd')) + cost, 6)
            tusage['updated_at'] = _now_iso()
            topic['usage'] = tusage
            topic['updated_at'] = _now_iso()
            _write_json(tpath, topic)

    budget_status = _evaluate_budget(op)
    if (not budget_status['continue_running']) and op.get('status') == 'running':
        op['status'] = 'budget_exhausted'
        op['updated_at'] = _now_iso()
        _write_json(path, op)
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


def select_best_checkpoint(state_dir: Path, project_root: Path, operation_id: str, topic_slug: str) -> dict[str, Any]:
    items = list_checkpoints(state_dir, project_root, operation_id, topic_slug)
    if not items:
        return {}

    def _score(item: dict[str, Any]) -> tuple[float, int]:
        return (float(item.get('score') or 0.0), int(item.get('iteration') or 0))

    best = sorted(items, key=_score, reverse=True)[0]
    return best


def _safe_read_text(path_str: str) -> str:
    try:
        p = Path(str(path_str or ''))
        if p.exists():
            return p.read_text(encoding='utf-8', errors='replace')
    except Exception:
        pass
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



def finalize_operation_selection(state_dir: Path, project_root: Path, operation_id: str) -> dict[str, Any]:
    op = get_operation_state(state_dir, project_root, operation_id)
    if not op:
        return {}

    selections = []
    for topic in op.get('topics') or []:
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

    op_dir = operation_dir(state_dir, project_root, operation_id)
    bundle = build_operation_delivery_bundle(state_dir, project_root, operation_id, selections)
    summary_lines = ['# Libris Final Selection', '']
    for idx, sel in enumerate(sorted(selections, key=lambda s: float(s.get('score') or 0.0), reverse=True), start=1):
        summary_lines.append(f'- {idx}. {sel["topic_slug"]}: {sel["checkpoint_id"]} (score={sel.get("score")})')
    if bundle.get('executive_summary_path'):
        summary_lines.extend(['', f'Executive summary: {bundle.get("executive_summary_path")}'])
    if bundle.get('bundle_json_path'):
        summary_lines.append(f'Delivery bundle JSON: {bundle.get("bundle_json_path")}')
    (op_dir / 'coordinator' / 'final-selection.md').write_text('\n'.join(summary_lines), encoding='utf-8')
    set_operation_status(state_dir, project_root, operation_id, 'delivered', 'Coordinator selected final deliveries.')
    append_operation_event(state_dir, project_root, operation_id, 'final_deliveries_selected', {
        'count': len(selections),
        'executive_summary_path': bundle.get('executive_summary_path', ''),
        'bundle_json_path': bundle.get('bundle_json_path', ''),
    })
    return {'operation_id': operation_id, 'selections': selections, 'bundle': bundle}


def get_libris_swarm_state(state_dir: Path, project_root: Path, operation_id: str) -> dict[str, Any]:
    op = get_operation_state(state_dir, project_root, operation_id)
    if not op:
        return {}

    try:
        from agent_lifecycle import load_agents
        agents = load_agents(state_dir)
    except Exception:
        agents = []

    try:
        from shade_orchestrator import load_contracts
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
    except Exception:
        final_selection = ''
    try:
        executive_summary = executive_summary_path.read_text(encoding='utf-8') if executive_summary_path.exists() else ''
    except Exception:
        executive_summary = ''
    delivery_bundle = _read_json(delivery_bundle_path, {}) if delivery_bundle_path.exists() else {}
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

    return {
        'operation_id': operation_id,
        'prompt': op.get('prompt') or '',
        'status': op.get('status') or 'unknown',
        'budget_status': op.get('budget_status') or _evaluate_budget(op),
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
    }
