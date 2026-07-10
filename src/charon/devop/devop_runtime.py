from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slug(text: str, fallback: str = 'item', max_len: int = 80) -> str:
    raw = re.sub(r'[^a-z0-9]+', '-', (text or '').strip().lower())
    raw = re.sub(r'-+', '-', raw).strip('-')
    return (raw or fallback)[:max_len]


def _short_hash(text: str, length: int = 8) -> str:
    return hashlib.sha1(text.encode('utf-8', errors='replace')).hexdigest()[:length]


def _new_id(prefix: str) -> str:
    return f'{prefix}-{uuid.uuid4().hex[:10]}'


def _read_json(path: Path, default: Any):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding='utf-8'))
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
            except Exception:
                continue
            if isinstance(row, dict):
                rows.append(row)
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


def default_budget() -> dict[str, Any]:
    return {
        'max_wall_hours': 0,
        'max_total_tokens': 0,
        'max_total_cost_usd': 0.0,
        'max_workstreams': 0,
        'max_checkpoints_per_workstream': 0,
        'max_revision_rounds': 0,
        'max_concurrent_implementers': 0,
        'max_concurrent_shades': 0,
    }


def default_policy() -> dict[str, Any]:
    return {
        'coordinator': 'strong',
        'judge': 'strong',
        'implementer': 'fast',
        'verifier': 'strong',
        'shade': 'cheap',
    }


def default_usage() -> dict[str, Any]:
    return {
        'input_tokens': 0,
        'output_tokens': 0,
        'total_tokens': 0,
        'estimated_cost_usd': 0.0,
        'by_model': {},
        'by_role': {},
        'updated_at': _now_iso(),
    }


def normalize_budget(budget: dict[str, Any] | None) -> dict[str, Any]:
    out = default_budget()
    data = budget or {}
    for key in out:
        if key == 'max_total_cost_usd':
            out[key] = _coerce_float(data.get(key), out[key])
        else:
            out[key] = _coerce_int(data.get(key), out[key])
    return out


def normalize_policy(policy: dict[str, Any] | None) -> dict[str, Any]:
    out = default_policy()
    for key, value in (policy or {}).items():
        if value not in (None, ''):
            out[str(key)] = str(value)
    return out


def derive_project_id(project_root: Path) -> str:
    base = _slug(project_root.name or 'project', 'project', 48)
    return f'{base}-{_short_hash(str(project_root.resolve()))}'


def software_ops_root(state_dir: Path) -> Path:
    return state_dir / 'software_ops' / 'operations'


def operation_dir(state_dir: Path, operation_id: str) -> Path:
    return software_ops_root(state_dir) / operation_id


def workstream_dir(state_dir: Path, operation_id: str, workstream_slug: str) -> Path:
    return operation_dir(state_dir, operation_id) / 'workstreams' / workstream_slug


def append_operation_event(
    state_dir: Path,
    operation_id: str,
    kind: str,
    payload: dict[str, Any] | None = None,
    *,
    workstream_id: str = '',
    from_agent_id: str = '',
    to_agent_id: str = '',
    summary: str = '',
) -> dict[str, Any]:
    row = {
        'event_id': _new_id('evt'),
        'ts': _now_iso(),
        'operation_id': operation_id,
        'workstream_id': workstream_id,
        'kind': str(kind).strip(),
        'from_agent_id': from_agent_id,
        'to_agent_id': to_agent_id,
        'summary': summary[:500],
        'payload': payload or {},
    }
    _append_jsonl(operation_dir(state_dir, operation_id) / 'events.jsonl', row)
    return row


def append_handoff(
    state_dir: Path,
    operation_id: str,
    *,
    workstream_id: str,
    kind: str,
    from_agent_id: str,
    to_agent_id: str,
    from_role: str = '',
    to_role: str = '',
    subject_id: str = '',
    payload: dict[str, Any] | None = None,
    status: str = 'sent',
    summary: str = '',
) -> dict[str, Any]:
    row = {
        'handoff_id': _new_id('ho'),
        'operation_id': operation_id,
        'workstream_id': workstream_id,
        'kind': kind,
        'from_agent_id': from_agent_id,
        'to_agent_id': to_agent_id,
        'from_role': from_role,
        'to_role': to_role,
        'subject_id': subject_id,
        'payload': payload or {},
        'status': status,
        'summary': summary[:500],
        'created_at': _now_iso(),
    }
    _append_jsonl(operation_dir(state_dir, operation_id) / 'handoffs.jsonl', row)
    append_operation_event(
        state_dir,
        operation_id,
        'handoff_sent',
        workstream_id=workstream_id,
        from_agent_id=from_agent_id,
        to_agent_id=to_agent_id,
        summary=summary or f'{kind} from {from_role or from_agent_id} to {to_role or to_agent_id}',
        payload={'handoff_id': row['handoff_id'], 'kind': kind, 'subject_id': subject_id},
    )
    return row


def append_decision(
    state_dir: Path,
    operation_id: str,
    *,
    decision_type: str,
    actor_agent_id: str,
    subject_id: str = '',
    workstream_id: str = '',
    summary: str = '',
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    row = {
        'decision_id': _new_id('dec'),
        'operation_id': operation_id,
        'workstream_id': workstream_id,
        'decision_type': decision_type,
        'actor_agent_id': actor_agent_id,
        'subject_id': subject_id,
        'summary': summary[:500],
        'metadata': metadata or {},
        'created_at': _now_iso(),
    }
    _append_jsonl(operation_dir(state_dir, operation_id) / 'decisions.jsonl', row)
    append_operation_event(
        state_dir,
        operation_id,
        'decision_recorded',
        workstream_id=workstream_id,
        from_agent_id=actor_agent_id,
        summary=summary or decision_type,
        payload={'decision_id': row['decision_id'], 'decision_type': decision_type, 'subject_id': subject_id},
    )
    return row


def init_operation(
    state_dir: Path,
    project_root: Path,
    *,
    prompt: str,
    title: str = '',
    coordinator_agent_id: str = '',
    global_judge_agent_id: str = '',
    integration_verifier_agent_id: str = '',
    budget: dict[str, Any] | None = None,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    op_id = f'op-dev-{datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")}-{uuid.uuid4().hex[:4]}'
    op_dir = operation_dir(state_dir, op_id)
    (op_dir / 'workstreams').mkdir(parents=True, exist_ok=True)
    doc = {
        'operation_id': op_id,
        'domain': 'software_dev',
        'title': str(title or prompt).strip()[:240],
        'prompt': str(prompt).strip(),
        'status': 'running',
        'project_root': str(project_root.resolve()),
        'project_id': derive_project_id(project_root),
        'coordinator_agent_id': coordinator_agent_id,
        'global_judge_agent_id': global_judge_agent_id,
        'integration_verifier_agent_id': integration_verifier_agent_id,
        'budget': normalize_budget(budget),
        'usage': default_usage(),
        'policy': normalize_policy(policy),
        'candidate_workstreams': [],
        'selected_workstream_ids': [],
        'delivered_checkpoint_ids': [],
        'created_at': _now_iso(),
        'updated_at': _now_iso(),
        'stop_requested': False,
    }
    _write_json(op_dir / 'operation.json', doc)
    append_operation_event(
        state_dir,
        op_id,
        'operation_started',
        summary=doc['title'],
        payload={
            'prompt': doc['prompt'][:500],
            'coordinator_agent_id': coordinator_agent_id,
            'budget': doc['budget'],
            'policy': doc['policy'],
        },
    )
    return doc


def get_operation_state(state_dir: Path, operation_id: str) -> dict[str, Any]:
    op_dir = operation_dir(state_dir, operation_id)
    op = _read_json(op_dir / 'operation.json', {})
    if not op:
        return {}
    workstreams = []
    for ws_path in sorted((op_dir / 'workstreams').glob('*')):
        rec = get_workstream_state(state_dir, operation_id, ws_path.name)
        if rec:
            workstreams.append(rec)
    op['workstreams'] = workstreams
    op['events_tail'] = _iter_jsonl(op_dir / 'events.jsonl')[-50:]
    op['handoffs_tail'] = _iter_jsonl(op_dir / 'handoffs.jsonl')[-50:]
    op['decisions_tail'] = _iter_jsonl(op_dir / 'decisions.jsonl')[-50:]
    return op


def set_operation_status(state_dir: Path, operation_id: str, status: str, note: str = '') -> dict[str, Any]:
    path = operation_dir(state_dir, operation_id) / 'operation.json'
    op = _read_json(path, {})
    if not op:
        return {}
    op['status'] = str(status).strip() or op.get('status') or 'running'
    op['updated_at'] = _now_iso()
    _write_json(path, op)
    append_operation_event(
        state_dir,
        operation_id,
        'operation_status_updated',
        summary=note or op['status'],
        payload={'status': op['status'], 'note': note[:500]},
    )
    return op


def save_candidate_workstreams(
    state_dir: Path,
    operation_id: str,
    workstreams: list[dict[str, Any]],
) -> dict[str, Any]:
    path = operation_dir(state_dir, operation_id) / 'operation.json'
    op = _read_json(path, {})
    if not op:
        return {}
    clean = []
    for rec in workstreams:
        title = str(rec.get('title') or rec.get('workstream') or '').strip()
        if not title:
            continue
        clean.append({
            'workstream_id': str(rec.get('workstream_id') or _new_id('wu')),
            'title': title,
            'slug': str(rec.get('slug') or _slug(title, 'workstream')),
            'summary': str(rec.get('summary') or '').strip(),
            'priority': _coerce_float(rec.get('priority'), 0.0),
            'recommended_action': str(rec.get('recommended_action') or 'execute_now'),
            'dependency_ids': list(rec.get('dependency_ids') or []),
        })
    op['candidate_workstreams'] = clean
    op['updated_at'] = _now_iso()
    _write_json(path, op)
    append_operation_event(
        state_dir,
        operation_id,
        'candidate_workstreams_saved',
        summary=f'{len(clean)} candidate workstreams saved',
        payload={'count': len(clean)},
    )
    return {'operation_id': operation_id, 'count': len(clean), 'workstreams': clean}


def init_workstream(
    state_dir: Path,
    operation_id: str,
    *,
    title: str,
    summary: str = '',
    constraints: list[str] | None = None,
    acceptance_criteria: list[str] | None = None,
    dependency_ids: list[str] | None = None,
    owner_agent_id: str = '',
    paired_judge_agent_id: str = '',
) -> dict[str, Any]:
    op_path = operation_dir(state_dir, operation_id) / 'operation.json'
    op = _read_json(op_path, {})
    if not op:
        raise ValueError(f'Unknown operation_id: {operation_id}')
    slug = _slug(title, 'workstream')
    ws_dir = workstream_dir(state_dir, operation_id, slug)
    for sub in ('checkpoints', 'reviews', 'evidence', 'artifacts'):
        (ws_dir / sub).mkdir(parents=True, exist_ok=True)
    workstream_id = _new_id('wu')
    doc = {
        'workstream_id': workstream_id,
        'operation_id': operation_id,
        'work_unit_type': 'workstream',
        'slug': slug,
        'title': title.strip(),
        'status': 'active',
        'priority': 0.0,
        'summary': summary.strip(),
        'constraints': list(constraints or []),
        'acceptance_criteria': list(acceptance_criteria or []),
        'dependency_ids': list(dependency_ids or []),
        'owner_agent_id': owner_agent_id,
        'paired_judge_agent_id': paired_judge_agent_id,
        'checkpoint_ids': [],
        'best_checkpoint_id': None,
        'revision_round': 0,
        'created_at': _now_iso(),
        'updated_at': _now_iso(),
    }
    _write_json(ws_dir / 'workstream.json', doc)
    selected = list(op.get('selected_workstream_ids') or [])
    selected.append(workstream_id)
    op['selected_workstream_ids'] = selected
    op['updated_at'] = _now_iso()
    _write_json(op_path, op)
    append_operation_event(
        state_dir,
        operation_id,
        'workstream_selected',
        workstream_id=workstream_id,
        summary=title,
        payload={'workstream_id': workstream_id, 'slug': slug, 'title': title},
    )
    return doc


def get_workstream_state(state_dir: Path, operation_id: str, workstream_slug: str) -> dict[str, Any]:
    ws_dir = workstream_dir(state_dir, operation_id, workstream_slug)
    ws = _read_json(ws_dir / 'workstream.json', {})
    if not ws:
        return {}
    ws['checkpoints'] = list_checkpoints(state_dir, operation_id, workstream_slug)
    ws['reviews'] = list_reviews(state_dir, operation_id, workstream_slug)
    ws['latest_checkpoint'] = ws['checkpoints'][-1] if ws['checkpoints'] else {}
    ws['latest_review'] = ws['reviews'][-1] if ws['reviews'] else {}
    return ws


def update_workstream_runtime(
    state_dir: Path,
    operation_id: str,
    workstream_slug: str,
    *,
    status: str | None = None,
    owner_agent_id: str | None = None,
    paired_judge_agent_id: str | None = None,
    extras: dict[str, Any] | None = None,
) -> dict[str, Any]:
    path = workstream_dir(state_dir, operation_id, workstream_slug) / 'workstream.json'
    ws = _read_json(path, {})
    if not ws:
        return {}
    if status is not None:
        ws['status'] = status
    if owner_agent_id is not None:
        ws['owner_agent_id'] = owner_agent_id
    if paired_judge_agent_id is not None:
        ws['paired_judge_agent_id'] = paired_judge_agent_id
    if extras:
        ws.update(extras)
    ws['updated_at'] = _now_iso()
    _write_json(path, ws)
    append_operation_event(
        state_dir,
        operation_id,
        'workstream_runtime_updated',
        workstream_id=str(ws.get('workstream_id') or ''),
        summary=f'{workstream_slug}: {ws.get("status")}',
        payload={'slug': workstream_slug, 'status': ws.get('status')},
    )
    return ws


def save_evidence_bundle(
    state_dir: Path,
    operation_id: str,
    workstream_slug: str,
    *,
    checkpoint_id: str,
    changed_files: list[str] | None = None,
    commands: list[str] | None = None,
    artifacts: list[dict[str, Any]] | None = None,
    verification: dict[str, Any] | None = None,
    summary: str = '',
) -> dict[str, Any]:
    ws_dir = workstream_dir(state_dir, operation_id, workstream_slug)
    if not (ws_dir / 'workstream.json').exists():
        raise ValueError(f'Unknown workstream: {workstream_slug}')
    ev_id = _new_id('ev')
    row = {
        'evidence_bundle_id': ev_id,
        'operation_id': operation_id,
        'workstream_id': str(_read_json(ws_dir / 'workstream.json', {}).get('workstream_id') or ''),
        'checkpoint_id': checkpoint_id,
        'changed_files': list(changed_files or []),
        'commands': list(commands or []),
        'artifacts': list(artifacts or []),
        'verification': dict(verification or {}),
        'summary': summary[:1000],
        'created_at': _now_iso(),
    }
    _write_json(ws_dir / 'evidence' / f'{ev_id}.json', row)
    append_operation_event(
        state_dir,
        operation_id,
        'evidence_bundle_saved',
        workstream_id=row['workstream_id'],
        summary=summary or ev_id,
        payload={'workstream_slug': workstream_slug, 'checkpoint_id': checkpoint_id, 'evidence_bundle_id': ev_id},
    )
    return row


def save_checkpoint(
    state_dir: Path,
    operation_id: str,
    workstream_slug: str,
    *,
    producer_agent_id: str,
    markdown: str,
    summary: str,
    artifact_refs: list[dict[str, Any]] | None = None,
    evidence_bundle_id: str = '',
    scorecard: dict[str, Any] | None = None,
    best_so_far: bool = False,
) -> dict[str, Any]:
    ws_dir = workstream_dir(state_dir, operation_id, workstream_slug)
    ws_path = ws_dir / 'workstream.json'
    ws = _read_json(ws_path, {})
    if not ws:
        raise ValueError(f'Unknown workstream: {workstream_slug}')
    idx = len(list((ws_dir / 'checkpoints').glob('*-meta.json'))) + 1
    checkpoint_id = f'cp-{_slug(workstream_slug, "workstream", 40)}-{idx:03d}'
    report_rel = f'workstreams/{workstream_slug}/checkpoints/{checkpoint_id}.md'
    meta = {
        'checkpoint_id': checkpoint_id,
        'operation_id': operation_id,
        'workstream_id': ws.get('workstream_id'),
        'producer_agent_id': producer_agent_id,
        'status': 'submitted',
        'summary': summary[:1000],
        'report_path': report_rel,
        'artifact_refs': list(artifact_refs or []),
        'evidence_bundle_id': evidence_bundle_id,
        'review_ids': [],
        'scorecard': dict(scorecard or {}),
        'best_so_far': bool(best_so_far),
        'created_at': _now_iso(),
        'updated_at': _now_iso(),
    }
    (ws_dir / 'checkpoints' / f'{checkpoint_id}.md').write_text(markdown, encoding='utf-8')
    _write_json(ws_dir / 'checkpoints' / f'{checkpoint_id}-meta.json', meta)
    checkpoint_ids = list(ws.get('checkpoint_ids') or [])
    checkpoint_ids.append(checkpoint_id)
    ws['checkpoint_ids'] = checkpoint_ids
    if best_so_far or not ws.get('best_checkpoint_id'):
        ws['best_checkpoint_id'] = checkpoint_id
    ws['updated_at'] = _now_iso()
    _write_json(ws_path, ws)
    append_operation_event(
        state_dir,
        operation_id,
        'checkpoint_submitted',
        workstream_id=str(ws.get('workstream_id') or ''),
        from_agent_id=producer_agent_id,
        to_agent_id=str(ws.get('paired_judge_agent_id') or ''),
        summary=summary[:240] or checkpoint_id,
        payload={'workstream_slug': workstream_slug, 'checkpoint_id': checkpoint_id},
    )
    return meta


def list_checkpoints(state_dir: Path, operation_id: str, workstream_slug: str) -> list[dict[str, Any]]:
    ws_dir = workstream_dir(state_dir, operation_id, workstream_slug)
    items = []
    for meta_path in sorted((ws_dir / 'checkpoints').glob('*-meta.json')):
        meta = _read_json(meta_path, {})
        if meta:
            items.append(meta)
    return items


def save_review(
    state_dir: Path,
    operation_id: str,
    workstream_slug: str,
    *,
    checkpoint_id: str,
    reviewer_agent_id: str,
    review_type: str,
    decision: str,
    critique_markdown: str,
    summary: str,
    scores: dict[str, Any] | None = None,
    requested_changes: list[str] | None = None,
) -> dict[str, Any]:
    ws_dir = workstream_dir(state_dir, operation_id, workstream_slug)
    ws_path = ws_dir / 'workstream.json'
    ws = _read_json(ws_path, {})
    if not ws:
        raise ValueError(f'Unknown workstream: {workstream_slug}')
    review_id = _new_id('rv')
    critique_rel = f'workstreams/{workstream_slug}/reviews/{review_id}-critique.md'
    meta = {
        'review_id': review_id,
        'operation_id': operation_id,
        'workstream_id': ws.get('workstream_id'),
        'checkpoint_id': checkpoint_id,
        'reviewer_agent_id': reviewer_agent_id,
        'review_type': review_type,
        'status': 'completed',
        'decision': decision,
        'summary': summary[:1000],
        'critique_path': critique_rel,
        'scores': dict(scores or {}),
        'requested_changes': list(requested_changes or []),
        'created_at': _now_iso(),
        'updated_at': _now_iso(),
    }
    (ws_dir / 'reviews' / f'{review_id}-critique.md').write_text(critique_markdown, encoding='utf-8')
    _write_json(ws_dir / 'reviews' / f'{review_id}-meta.json', meta)

    # Attach to checkpoint metadata
    for meta_path in (ws_dir / 'checkpoints').glob('*-meta.json'):
        ck = _read_json(meta_path, {})
        if ck.get('checkpoint_id') == checkpoint_id:
            ids = list(ck.get('review_ids') or [])
            ids.append(review_id)
            ck['review_ids'] = ids
            ck['updated_at'] = _now_iso()
            _write_json(meta_path, ck)
            if scores:
                ck['scorecard'] = dict(scores)
                _write_json(meta_path, ck)
            break

    if decision == 'accept':
        ws['status'] = 'active'
    elif decision == 'repair_requested':
        ws['status'] = 'revising'
        ws['revision_round'] = int(ws.get('revision_round') or 0) + 1
    elif decision == 'reject':
        ws['status'] = 'blocked'
    elif decision == 'escalate':
        ws['status'] = 'blocked'
    ws['updated_at'] = _now_iso()
    _write_json(ws_path, ws)

    append_operation_event(
        state_dir,
        operation_id,
        'review_completed',
        workstream_id=str(ws.get('workstream_id') or ''),
        from_agent_id=reviewer_agent_id,
        summary=summary[:240] or decision,
        payload={
            'workstream_slug': workstream_slug,
            'checkpoint_id': checkpoint_id,
            'review_id': review_id,
            'decision': decision,
        },
    )
    return meta


def list_reviews(state_dir: Path, operation_id: str, workstream_slug: str) -> list[dict[str, Any]]:
    ws_dir = workstream_dir(state_dir, operation_id, workstream_slug)
    items = []
    for meta_path in sorted((ws_dir / 'reviews').glob('*-meta.json')):
        meta = _read_json(meta_path, {})
        if meta:
            items.append(meta)
    return items


def select_best_checkpoint(state_dir: Path, operation_id: str, workstream_slug: str) -> dict[str, Any]:
    items = list_checkpoints(state_dir, operation_id, workstream_slug)
    if not items:
        return {}
    def _score(item: dict[str, Any]) -> tuple[float, str]:
        sc = item.get('scorecard') or {}
        overall = _coerce_float(sc.get('overall'), 0.0)
        return (overall, str(item.get('checkpoint_id') or ''))
    best = sorted(items, key=_score, reverse=True)[0]
    path = workstream_dir(state_dir, operation_id, workstream_slug) / 'workstream.json'
    ws = _read_json(path, {})
    if ws:
        ws['best_checkpoint_id'] = best.get('checkpoint_id')
        ws['updated_at'] = _now_iso()
        _write_json(path, ws)
    return best


def finalize_operation_selection(state_dir: Path, operation_id: str, *, actor_agent_id: str = '') -> dict[str, Any]:
    op_path = operation_dir(state_dir, operation_id) / 'operation.json'
    op = _read_json(op_path, {})
    if not op:
        return {}
    selections = []
    for ws_dir in sorted((operation_dir(state_dir, operation_id) / 'workstreams').glob('*')):
        slug = ws_dir.name
        best = select_best_checkpoint(state_dir, operation_id, slug)
        if not best:
            continue
        selections.append({'workstream_slug': slug, 'checkpoint_id': best.get('checkpoint_id'), 'score': (best.get('scorecard') or {}).get('overall')})
        append_decision(
            state_dir,
            operation_id,
            decision_type='select_best_checkpoint',
            actor_agent_id=actor_agent_id,
            workstream_id=str(_read_json(ws_dir / 'workstream.json', {}).get('workstream_id') or ''),
            subject_id=str(best.get('checkpoint_id') or ''),
            summary=f'Selected best checkpoint for {slug}: {best.get("checkpoint_id")}',
        )
    op['delivered_checkpoint_ids'] = [s['checkpoint_id'] for s in selections if s.get('checkpoint_id')]
    op['status'] = 'delivered'
    op['updated_at'] = _now_iso()
    _write_json(op_path, op)
    append_operation_event(
        state_dir,
        operation_id,
        'final_selection_completed',
        from_agent_id=actor_agent_id,
        summary=f'{len(selections)} workstream selections finalized',
        payload={'selections': selections},
    )
    return {'operation_id': operation_id, 'selections': selections}


def get_swarm_state(state_dir: Path, operation_id: str) -> dict[str, Any]:
    op = get_operation_state(state_dir, operation_id)
    if not op:
        return {}
    cards = []
    for ws in op.get('workstreams') or []:
        latest_checkpoint = ws.get('latest_checkpoint') or {}
        latest_review = ws.get('latest_review') or {}
        cards.append({
            'workstream_id': ws.get('workstream_id') or '',
            'workstream_slug': ws.get('slug') or '',
            'title': ws.get('title') or '',
            'status': ws.get('status') or '',
            'owner_agent_id': ws.get('owner_agent_id') or '',
            'paired_judge_agent_id': ws.get('paired_judge_agent_id') or '',
            'checkpoint_id': latest_checkpoint.get('checkpoint_id') or '',
            'checkpoint_score': ((latest_checkpoint.get('scorecard') or {}).get('overall')),
            'review_id': latest_review.get('review_id') or '',
            'review_decision': latest_review.get('decision') or '',
            'summary': latest_checkpoint.get('summary') or latest_review.get('summary') or ws.get('summary') or '',
        })
    return {
        'operation_id': operation_id,
        'status': op.get('status') or '',
        'title': op.get('title') or '',
        'cards': cards,
        'events_tail': op.get('events_tail') or [],
    }


__all__ = [
    'default_budget', 'default_policy', 'default_usage',
    'normalize_budget', 'normalize_policy',
    'derive_project_id', 'software_ops_root', 'operation_dir', 'workstream_dir',
    'append_operation_event', 'append_handoff', 'append_decision',
    'init_operation', 'get_operation_state', 'set_operation_status',
    'save_candidate_workstreams', 'init_workstream', 'get_workstream_state', 'update_workstream_runtime',
    'save_evidence_bundle', 'save_checkpoint', 'list_checkpoints',
    'save_review', 'list_reviews', 'select_best_checkpoint', 'finalize_operation_selection',
    'get_swarm_state',
]
