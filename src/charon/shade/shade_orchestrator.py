#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

# SQLite store adapter (optional)
try:
    from charon.infra.store_adapter import (
        get_db as _get_db,
        contract_insert as _db_contract_insert,
        contract_get as _db_contract_get,
        contract_list as _db_contract_list,
        contract_update as _db_contract_update,
        shade_event_append as _db_shade_event_append,
    )
    _HAS_STORE = True
except ImportError:
    _HAS_STORE = False


def _use_store() -> bool:
    return _HAS_STORE and os.environ.get('CHARON_NO_SQLITE', '0') != '1'


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _contracts_path(state_dir: Path) -> Path:
    return state_dir / 'shade_contracts.json'


def _events_path(state_dir: Path) -> Path:
    return state_dir / 'shade_phase_events.jsonl'


def _triage_path(state_dir: Path) -> Path:
    return state_dir / 'shade_triage.json'


def _contract_id() -> str:
    return f"ctr-{uuid.uuid4().hex[:10]}"


def _read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        data = json.loads(path.read_text())
        return data
    except Exception:
        return default


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2))


def _append_jsonl(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as f:
        f.write(json.dumps(payload) + '\n')


def load_contracts(state_dir: Path) -> list[dict]:
    if _use_store():
        try:
            return _db_contract_list(_get_db(state_dir))
        except Exception:
            pass
    data = _read_json(_contracts_path(state_dir), [])
    return data if isinstance(data, list) else []


def save_contracts(state_dir: Path, contracts: list[dict]) -> None:
    # Always write JSON as backup
    _write_json(_contracts_path(state_dir), contracts)


def append_phase_event(
    state_dir: Path,
    *,
    contract_id: str,
    phase_id: str,
    event_type: str,
    payload: dict | None = None,
) -> None:
    _append_jsonl(
        _events_path(state_dir),
        {
            'ts': _now_iso(),
            'contract_id': contract_id,
            'phase_id': phase_id,
            'event_type': event_type,
            'payload': payload or {},
        },
    )
    if _use_store():
        try:
            _db_shade_event_append(_get_db(state_dir), contract_id=contract_id,
                                   phase_id=phase_id, event_type=event_type,
                                   payload=payload)
        except Exception:
            pass


def default_phase_plan(goal: str) -> list[dict]:
    goal = str(goal or '').strip()
    return [
        {'name': 'analysis', 'objective': f'Analyze and plan approach for: {goal}'},
        {'name': 'implementation', 'objective': f'Implement the requested changes for: {goal}'},
        {'name': 'verification', 'objective': 'Verify correctness with focused checks/tests and capture evidence.'},
        {'name': 'report', 'objective': 'Produce concise report with what changed, evidence, and risks.'},
    ]


def _normalize_phase_specs(phase_specs: list[dict] | None, goal: str) -> list[dict]:
    raw = phase_specs or default_phase_plan(goal)
    out: list[dict] = []
    for i, rec in enumerate(raw, start=1):
        name = str((rec or {}).get('name') or f'phase-{i}').strip() or f'phase-{i}'
        objective = str((rec or {}).get('objective') or '').strip() or f'Execute {name}'
        phase_id = f"P{i:02d}"
        out.append(
            {
                'phase_id': phase_id,
                'lookup_key': '',
                'seq': i,
                'name': name,
                'objective': objective,
                'status': 'pending',
                'branch_id': 'main',
                'queued_task_id': None,
                'last_task_id': None,
                'result_summary': None,
                'error': None,
                'created_at': _now_iso(),
                'updated_at': _now_iso(),
            }
        )
    return out


def create_contract(
    state_dir: Path,
    *,
    parent_task_id: str,
    parent_agent_id: str,
    shade_agent_id: str,
    conversation_id: str,
    project: str,
    goal: str,
    constraints: list[str] | None = None,
    expected_outputs: list[str] | None = None,
    scope: list[str] | None = None,
    phase_specs: list[dict] | None = None,
    contract_type: str = '',
    metadata: dict | None = None,
) -> dict:
    contracts = load_contracts(state_dir)
    cid = _contract_id()
    phases = _normalize_phase_specs(phase_specs, goal)
    for p in phases:
        p['lookup_key'] = f"{cid}:{p['phase_id']}"

    rec = {
        'id': cid,
        'status': 'running',
        'active_branch_id': 'main',
        'parent_task_id': parent_task_id,
        'parent_agent_id': parent_agent_id,
        'shade_agent_id': shade_agent_id,
        'conversation_id': conversation_id,
        'project': project,
        'goal': str(goal or ''),
        'constraints': [str(x).strip() for x in (constraints or []) if str(x).strip()],
        'expected_outputs': [str(x).strip() for x in (expected_outputs or []) if str(x).strip()],
        'scope': [str(x).strip() for x in (scope or []) if str(x).strip()],
        'contract_type': str(contract_type or '').strip(),
        'metadata': metadata or {},
        'phase_count': len(phases),
        'phases': phases,
        'current_phase_id': phases[0]['phase_id'] if phases else None,
        'branch_history': [],
        'created_at': _now_iso(),
        'updated_at': _now_iso(),
        'completed_at': None,
        'last_error': None,
    }
    contracts.append(rec)
    save_contracts(state_dir, contracts)
    if _use_store():
        try:
            db = _get_db(state_dir)
            if not _db_contract_get(db, cid):
                _db_contract_insert(db, dict(rec))
        except Exception:
            pass
    append_phase_event(state_dir, contract_id=cid, phase_id=rec.get('current_phase_id') or '-', event_type='contract_created', payload={'phase_count': len(phases)})
    return rec


def get_contract(state_dir: Path, contract_id: str) -> dict | None:
    if _use_store():
        try:
            rec = _db_contract_get(_get_db(state_dir), contract_id)
            if rec:
                return rec
        except Exception:
            pass
    for rec in load_contracts(state_dir):
        if rec.get('id') == contract_id:
            return rec
    return None


def _update_contract(state_dir: Path, contract: dict) -> dict:
    contracts = load_contracts(state_dir)
    for i, rec in enumerate(contracts):
        if rec.get('id') == contract.get('id'):
            contract['updated_at'] = _now_iso()
            contracts[i] = contract
            save_contracts(state_dir, contracts)
            if _use_store():
                try:
                    _db_contract_update(_get_db(state_dir), dict(contract))
                except Exception:
                    pass
            return contract
    contracts.append(contract)
    save_contracts(state_dir, contracts)
    if _use_store():
        try:
            db = _get_db(state_dir)
            if not _db_contract_get(db, contract.get('id', '')):
                _db_contract_insert(db, dict(contract))
        except Exception:
            pass
    return contract


def next_pending_phase(contract: dict) -> dict | None:
    for phase in (contract.get('phases') or []):
        if phase.get('status') == 'pending':
            return phase
    return None


def mark_phase_queued(state_dir: Path, contract_id: str, phase_id: str, queue_task_id: str) -> dict | None:
    contract = get_contract(state_dir, contract_id)
    if not contract:
        return None
    for phase in (contract.get('phases') or []):
        if phase.get('phase_id') != phase_id:
            continue
        phase['status'] = 'queued'
        phase['queued_task_id'] = queue_task_id
        phase['updated_at'] = _now_iso()
        contract['current_phase_id'] = phase_id
        _update_contract(state_dir, contract)
        append_phase_event(state_dir, contract_id=contract_id, phase_id=phase_id, event_type='phase_queued', payload={'queue_task_id': queue_task_id})
        return contract
    return None


def _summary_quality(summary: str) -> dict:
    text = str(summary or '')
    stripped = text.strip()
    non_ws = len(stripped)
    line_count = len([ln for ln in text.splitlines() if ln.strip()])
    weak = non_ws < 24 or line_count == 0
    return {
        'non_ws_chars': non_ws,
        'nonempty_lines': line_count,
        'looks_weak': weak,
    }


def mark_phase_completed(state_dir: Path, contract_id: str, phase_id: str, *, task_id: str, summary: str) -> dict | None:
    contract = get_contract(state_dir, contract_id)
    if not contract:
        return None
    quality = _summary_quality(summary)
    for phase in (contract.get('phases') or []):
        if phase.get('phase_id') != phase_id:
            continue
        phase['status'] = 'completed'
        phase['last_task_id'] = task_id
        phase['result_summary'] = summary
        phase['error'] = None
        phase['updated_at'] = _now_iso()
        append_phase_event(
            state_dir,
            contract_id=contract_id,
            phase_id=phase_id,
            event_type='phase_completed',
            payload={'task_id': task_id, 'summary': summary[:240], 'quality': quality},
        )
        if quality.get('looks_weak'):
            append_phase_event(
                state_dir,
                contract_id=contract_id,
                phase_id=phase_id,
                event_type='phase_output_suspect',
                payload={
                    'task_id': task_id,
                    'reason': 'weak_summary',
                    'summary_preview': summary[:240],
                    'quality': quality,
                },
            )
        break
    pending = next_pending_phase(contract)
    if pending:
        contract['current_phase_id'] = pending.get('phase_id')
    else:
        contract['status'] = 'completed'
        contract['completed_at'] = _now_iso()
        contract['current_phase_id'] = None
        append_phase_event(state_dir, contract_id=contract_id, phase_id='-', event_type='contract_completed', payload={})
    return _update_contract(state_dir, contract)


def mark_phase_failed(state_dir: Path, contract_id: str, phase_id: str, *, task_id: str, error: str) -> dict | None:
    contract = get_contract(state_dir, contract_id)
    if not contract:
        return None
    for phase in (contract.get('phases') or []):
        if phase.get('phase_id') != phase_id:
            continue
        phase['status'] = 'failed'
        phase['last_task_id'] = task_id
        phase['error'] = error
        phase['updated_at'] = _now_iso()
        append_phase_event(state_dir, contract_id=contract_id, phase_id=phase_id, event_type='phase_failed', payload={'task_id': task_id, 'error': error[:240]})
        break
    contract['status'] = 'failed'
    contract['last_error'] = error
    contract['completed_at'] = _now_iso()
    append_phase_event(state_dir, contract_id=contract_id, phase_id='-', event_type='contract_failed', payload={'error': error[:240]})
    return _update_contract(state_dir, contract)


def build_phase_instruction(contract: dict, phase: dict) -> str:
    lines = [
        f"[SHADE_CONTRACT {contract.get('id')}]",
        f"Phase {phase.get('phase_id')} ({phase.get('name')})",
        f"Goal: {contract.get('goal')}",
        f"Phase objective: {phase.get('objective')}",
    ]
    constraints = contract.get('constraints') or []
    if constraints:
        lines.append('Constraints:')
        lines.extend([f"- {c}" for c in constraints])
    expected = contract.get('expected_outputs') or []
    if expected:
        lines.append('Expected outputs:')
        lines.extend([f"- {o}" for o in expected])
    lines.append('Return a concise summary of concrete outcomes and evidence for this phase.')
    return '\n'.join(lines)


def summarize_contract(contract: dict) -> str:
    phases = contract.get('phases') or []
    complete = [p for p in phases if p.get('status') == 'completed']
    failed = [p for p in phases if p.get('status') == 'failed']
    if failed:
        bad = failed[-1]
        return f"Contract {contract.get('id')} failed at {bad.get('phase_id')} ({bad.get('name')}): {bad.get('error') or 'unknown error'}"
    if contract.get('status') == 'completed':
        return f"Contract {contract.get('id')} completed ({len(complete)}/{len(phases)} phases)."
    cur = contract.get('current_phase_id') or '-'
    return f"Contract {contract.get('id')} running ({len(complete)}/{len(phases)} complete), current={cur}."


def branch_from_phase(state_dir: Path, *, contract_id: str, from_phase_id: str, reason: str) -> dict | None:
    contract = get_contract(state_dir, contract_id)
    if not contract:
        return None

    phases = contract.get('phases') or []
    idx = None
    for i, p in enumerate(phases):
        if p.get('phase_id') == from_phase_id:
            idx = i
            break
    if idx is None:
        return None

    branch_id = f"b{len(contract.get('branch_history') or []) + 1:02d}"
    contract.setdefault('branch_history', []).append(
        {
            'branch_id': branch_id,
            'from_phase_id': from_phase_id,
            'reason': reason,
            'created_at': _now_iso(),
        }
    )
    contract['active_branch_id'] = branch_id
    contract['status'] = 'running'
    contract['completed_at'] = None
    contract['last_error'] = None

    for i, phase in enumerate(phases):
        if i < idx:
            continue
        phase['status'] = 'pending'
        phase['queued_task_id'] = None
        phase['last_task_id'] = None
        phase['result_summary'] = None
        phase['error'] = None
        phase['branch_id'] = branch_id
        phase['updated_at'] = _now_iso()

    contract['current_phase_id'] = from_phase_id
    append_phase_event(
        state_dir,
        contract_id=contract_id,
        phase_id=from_phase_id,
        event_type='contract_branched',
        payload={'branch_id': branch_id, 'reason': reason},
    )
    return _update_contract(state_dir, contract)


def load_phase_events(state_dir: Path, contract_id: str | None = None) -> list[dict]:
    p = _events_path(state_dir)
    if not p.exists():
        return []
    rows = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if contract_id and rec.get('contract_id') != contract_id:
            continue
        rows.append(rec)
    return rows


def assess_contract_outcome(contract: dict | None) -> dict:
    contract = contract or {}
    phases = list(contract.get('phases') or [])
    completed = [p for p in phases if p.get('status') == 'completed']
    failed = [p for p in phases if p.get('status') == 'failed']
    current = str(contract.get('current_phase_id') or '')
    quality_flags = []
    for phase in completed:
        quality = _summary_quality(str(phase.get('result_summary') or ''))
        if quality.get('looks_weak'):
            quality_flags.append({
                'phase_id': phase.get('phase_id'),
                'phase_name': phase.get('name'),
                'reason': 'weak_summary',
                'quality': quality,
            })

    if failed:
        outcome = 'failed_runtime'
    elif contract.get('status') == 'completed' and not quality_flags:
        outcome = 'success'
    elif contract.get('status') == 'completed' and quality_flags:
        outcome = 'failed_quality'
    elif contract.get('status') == 'running' and completed and current:
        outcome = 'partial'
    elif contract.get('status') == 'running':
        outcome = 'stalled'
    else:
        outcome = 'unknown'

    return {
        'contract_id': contract.get('id') or '',
        'status': contract.get('status') or 'unknown',
        'outcome': outcome,
        'completed_phases': len(completed),
        'total_phases': len(phases),
        'current_phase_id': current,
        'failed_phase_ids': [p.get('phase_id') for p in failed],
        'quality_flags': quality_flags,
        'expected_outputs': list(contract.get('expected_outputs') or []),
        'resolved_provider': str(contract.get('resolved_provider') or ''),
        'resolved_model': str(contract.get('resolved_model') or ''),
        'resolved_provider_meta': contract.get('resolved_provider_meta') or {},
    }


def _load_triage(state_dir: Path) -> dict:
    data = _read_json(_triage_path(state_dir), {'items': []})
    if isinstance(data, dict) and isinstance(data.get('items'), list):
        return data
    return {'items': []}


def _save_triage(state_dir: Path, data: dict) -> None:
    _write_json(_triage_path(state_dir), data)


def save_triage_record(state_dir: Path, contract: dict | None, assessment: dict | None = None, reviewer_task_id: str = '') -> dict:
    contract = contract or {}
    assessment = assessment or assess_contract_outcome(contract)
    outcome = str(assessment.get('outcome') or 'unknown')
    if outcome == 'failed_runtime':
        recommendation = 'retry_with_debugging_or_escalate'
    elif outcome == 'failed_quality':
        recommendation = 'retry_with_stronger_model_or_judge'
    elif outcome == 'partial':
        recommendation = 'inspect_stall_and_resume_or_escalate'
    elif outcome == 'stalled':
        recommendation = 'inspect_scheduler_or_retry'
    else:
        recommendation = 'none'

    record = {
        'triage_id': f"tri_{uuid.uuid4().hex[:10]}",
        'contract_id': str(contract.get('id') or ''),
        'shade_agent_id': str(contract.get('shade_agent_id') or ''),
        'parent_agent_id': str(contract.get('parent_agent_id') or ''),
        'goal': str(contract.get('goal') or ''),
        'status': str(contract.get('status') or ''),
        'assessment': assessment,
        'recommendation': recommendation,
        'reviewer_task_id': str(reviewer_task_id or ''),
        'created_at': _now_iso(),
    }
    data = _load_triage(state_dir)
    items = list(data.get('items') or [])
    items.append(record)
    data['items'] = items
    _save_triage(state_dir, data)
    append_phase_event(
        state_dir,
        contract_id=str(contract.get('id') or ''),
        phase_id='-',
        event_type='worker_triage_requested',
        payload={
            'triage_id': record['triage_id'],
            'outcome': outcome,
            'recommendation': recommendation,
            'reviewer_task_id': record['reviewer_task_id'],
        },
    )
    return record


__all__ = [
    'load_contracts',
    'save_contracts',
    'append_phase_event',
    'create_contract',
    'get_contract',
    'next_pending_phase',
    'mark_phase_queued',
    'mark_phase_completed',
    'mark_phase_failed',
    'build_phase_instruction',
    'summarize_contract',
    'branch_from_phase',
    'load_phase_events',
    'assess_contract_outcome',
    'save_triage_record',
]


def suggest_branch_phase(contract: dict | None, phase_id: str | None = None) -> dict:
    phases = list((contract or {}).get('phases') or [])
    if not phases:
        return {'recommended_phase_id': phase_id or None, 'reason': 'no phases found'}

    by_id = {p.get('phase_id'): p for p in phases}
    if phase_id and phase_id in by_id:
        chosen = by_id[phase_id]
        if chosen.get('status') in ('failed', 'pending', 'queued'):
            return {'recommended_phase_id': phase_id, 'reason': f"requested phase {phase_id} is {chosen.get('status')}"}
        passed = False
        for p in phases:
            if p.get('phase_id') == phase_id:
                passed = True
                continue
            if passed and p.get('status') in ('failed', 'pending', 'queued'):
                return {'recommended_phase_id': p.get('phase_id'), 'reason': f"requested phase completed; next active phase is {p.get('phase_id')}"}
        return {'recommended_phase_id': phase_id, 'reason': f'requested phase {phase_id} already completed'}

    for p in phases:
        if p.get('status') == 'failed':
            return {'recommended_phase_id': p.get('phase_id'), 'reason': f"phase {p.get('phase_id')} is failed"}

    cur = (contract or {}).get('current_phase_id')
    if cur and cur in by_id:
        return {'recommended_phase_id': cur, 'reason': f'current phase is {cur}'}

    for p in phases:
        if p.get('status') in ('pending', 'queued'):
            return {'recommended_phase_id': p.get('phase_id'), 'reason': f"first active phase is {p.get('phase_id')}"}

    last = phases[-1].get('phase_id')
    return {'recommended_phase_id': last, 'reason': 'all phases completed; selecting last phase as fallback'}
