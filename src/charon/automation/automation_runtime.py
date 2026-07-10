from __future__ import annotations

import json
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    from charon.infra.diagnostics import record as _diag
except Exception:  # diagnostics is best-effort and must never block import
    def _diag(*args, **kwargs):
        return None


CRON_FIELD_RANGES = {
    0: (0, 59),
    1: (0, 23),
    2: (1, 31),
    3: (1, 12),
    4: (0, 6),
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_ts() -> float:
    return time.time()


def _new_id(prefix: str) -> str:
    return f'{prefix}-{uuid.uuid4().hex[:10]}'


def _slug(text: str, fallback: str = 'automation') -> str:
    value = re.sub(r'[^a-z0-9]+', '-', str(text or '').strip().lower()).strip('-')
    return value or fallback


def _read_json(path: Path, default: Any):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception as e:
        _diag('automation_runtime', 'JSON read failed; using default', error=e, file=str(path))
        return default


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding='utf-8')


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as f:
        f.write(json.dumps(row, ensure_ascii=False) + '\n')


def _iter_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
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
    return rows


def automations_root(state_dir: Path) -> Path:
    return state_dir / 'automations'


def automation_dir(state_dir: Path, automation_id: str) -> Path:
    return automations_root(state_dir) / 'definitions' / automation_id


def list_automations(state_dir: Path) -> list[dict[str, Any]]:
    root = automations_root(state_dir) / 'definitions'
    if not root.exists():
        return []
    items = []
    for path in sorted(root.glob('*')):
        if not path.is_dir():
            continue
        doc = _read_json(path / 'automation.json', {})
        if doc:
            items.append(doc)
    return items


def get_automation_state(state_dir: Path, automation_id: str) -> dict[str, Any]:
    base = automation_dir(state_dir, automation_id)
    doc = _read_json(base / 'automation.json', {})
    if not doc:
        return {}
    doc['events_tail'] = _iter_jsonl(base / 'events.jsonl')[-40:]
    doc['runs_tail'] = _iter_jsonl(base / 'runs.jsonl')[-20:]
    return doc


def append_automation_event(
    state_dir: Path,
    automation_id: str,
    kind: str,
    *,
    summary: str = '',
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    row = {
        'event_id': _new_id('aevt'),
        'automation_id': automation_id,
        'ts': _now_iso(),
        'kind': str(kind).strip(),
        'summary': str(summary or kind)[:500],
        'payload': payload or {},
    }
    _append_jsonl(automation_dir(state_dir, automation_id) / 'events.jsonl', row)
    return row


def _default_alert_policy() -> dict[str, Any]:
    return {
        'alert_on_failure': True,
        'alert_on_recovery': True,
        'alert_on_success': False,
        'dedupe_state_changes': True,
        'webhook_url': '',
    }


def _default_execution_policy() -> dict[str, Any]:
    return {
        'max_concurrent_runs': 1,
        'skip_if_running': True,
        'max_runtime_seconds': 300,
        'retry_count': 0,
        'poll_seconds': 60,
        'backoff_on_failure_seconds': 0,
    }


def _parse_cron_field(field: str, low: int, high: int) -> set[int]:
    values: set[int] = set()
    field = str(field or '*').strip()
    if field == '*':
        return set(range(low, high + 1))
    for part in field.split(','):
        part = part.strip()
        if not part:
            continue
        if '/' in part:
            base, step_str = part.split('/', 1)
            try:
                step = int(step_str)
            except Exception:
                step = 1
            if step <= 0:
                step = 1
            if base == '*':
                start, end = low, high
            elif '-' in base:
                a, b = base.split('-', 1)
                start, end = int(a), int(b)
            else:
                start = int(base)
                end = high
            start = max(low, start)
            end = min(high, end)
            values.update(range(start, end + 1, step))
            continue
        if '-' in part:
            a, b = part.split('-', 1)
            values.update(range(max(low, int(a)), min(high, int(b)) + 1))
            continue
        try:
            n = int(part)
        except Exception:
            continue
        if low <= n <= high:
            values.add(n)
    return values or set(range(low, high + 1))


def cron_matches_dt(cron_expr: str, dt: datetime) -> bool:
    parts = str(cron_expr or '').split()
    if len(parts) != 5:
        return False
    fields = [
        dt.minute,
        dt.hour,
        dt.day,
        dt.month,
        (dt.weekday() + 1) % 7,
    ]
    for idx, part in enumerate(parts):
        low, high = CRON_FIELD_RANGES[idx]
        if fields[idx] not in _parse_cron_field(part, low, high):
            return False
    return True


def compute_next_run(now_ts: float, mode: str, schedule: dict[str, Any]) -> tuple[float | None, str | None]:
    schedule = dict(schedule or {})
    schedule_type = str(schedule.get('type') or '').strip().lower()

    if mode == 'once':
        if schedule.get('run_at_ts'):
            ts = float(schedule.get('run_at_ts') or 0)
            ts = ts if ts > 0 else now_ts
            return ts, datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        return now_ts, datetime.fromtimestamp(now_ts, tz=timezone.utc).isoformat()

    if mode == 'continuous':
        poll_seconds = int(schedule.get('poll_seconds') or 0) or int(schedule.get('interval_seconds') or 0) or 60
        ts = now_ts + max(1, poll_seconds)
        return ts, datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()

    if schedule_type == 'cron' or schedule.get('cron'):
        cron_expr = str(schedule.get('cron') or '').strip()
        if cron_expr:
            cursor = datetime.fromtimestamp(now_ts, tz=timezone.utc).replace(second=0, microsecond=0) + timedelta(minutes=1)
            for _ in range(60 * 24 * 370):
                if cron_matches_dt(cron_expr, cursor):
                    return cursor.timestamp(), cursor.isoformat()
                cursor += timedelta(minutes=1)

    interval = int(schedule.get('interval_seconds') or 0)
    if interval <= 0:
        interval = 3600 if mode == 'scheduled' else 60
    next_ts = now_ts + interval
    return next_ts, datetime.fromtimestamp(next_ts, tz=timezone.utc).isoformat()


def create_automation(
    state_dir: Path,
    project_root: Path,
    *,
    title: str,
    goal: str,
    kind: str,
    mode: str = 'scheduled',
    schedule: dict[str, Any] | None = None,
    action: dict[str, Any] | None = None,
    created_by_agent_id: str = '',
    runtime_role: str = 'automation',
    operation_role: str = 'monitor',
    alert_policy: dict[str, Any] | None = None,
    execution_policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    schedule_doc = dict(schedule or {})
    now_ts = _now_ts()
    first_ts, first_iso = compute_next_run(now_ts, mode, schedule_doc)
    automation_id = f'auto-{_slug(title)[:32]}-{uuid.uuid4().hex[:4]}'
    doc = {
        'automation_id': automation_id,
        'title': str(title or goal).strip()[:240],
        'goal': str(goal).strip(),
        'kind': str(kind).strip() or 'generic_task',
        'mode': str(mode).strip() or 'scheduled',
        'status': 'active',
        'health': 'unknown',
        'project_root': str(project_root.resolve()),
        'state_dir': str(state_dir.resolve()),
        'created_by_agent_id': created_by_agent_id,
        'runtime_role': runtime_role,
        'operation_role': operation_role,
        'schedule': schedule_doc,
        'action': action or {},
        'alert_policy': {**_default_alert_policy(), **(alert_policy or {})},
        'execution_policy': {**_default_execution_policy(), **(execution_policy or {})},
        'active_run_count': 0,
        'last_run_at': '',
        'last_success_at': '',
        'last_failure_at': '',
        'last_result_summary': '',
        'last_error': '',
        'last_alert_state': '',
        'next_run_at': first_iso or '',
        'next_run_ts': first_ts or 0,
        'last_heartbeat_at': '',
        'current_run_started_at': '',
        'consecutive_failures': 0,
        'created_at': _now_iso(),
        'updated_at': _now_iso(),
        'stop_requested': False,
    }
    base = automation_dir(state_dir, automation_id)
    base.mkdir(parents=True, exist_ok=True)
    _write_json(base / 'automation.json', doc)
    append_automation_event(state_dir, automation_id, 'automation_created', summary=doc['title'], payload={'kind': doc['kind'], 'mode': doc['mode'], 'schedule': schedule_doc})
    return doc


def update_automation_doc(state_dir: Path, automation_id: str, mutate) -> dict[str, Any]:
    path = automation_dir(state_dir, automation_id) / 'automation.json'
    doc = _read_json(path, {})
    if not doc:
        return {}
    mutate(doc)
    doc['updated_at'] = _now_iso()
    _write_json(path, doc)
    return doc


def set_automation_webhook(state_dir: Path, automation_id: str, webhook_url: str) -> dict[str, Any]:
    url = str(webhook_url or '').strip()
    doc = update_automation_doc(
        state_dir,
        automation_id,
        lambda d: d.__setitem__('alert_policy', {**(d.get('alert_policy') or {}), 'webhook_url': url}),
    )
    if doc:
        append_automation_event(state_dir, automation_id, 'automation_webhook_updated', summary='Updated automation webhook URL.', payload={'webhook_url': url})
    return doc


def reconcile_stale_automation_runs(state_dir: Path, *, stale_after_seconds: int = 300) -> list[str]:
    recovered: list[str] = []
    now = _now_ts()
    for item in list_automations(state_dir):
        automation_id = str(item.get('automation_id') or '')
        if not automation_id:
            continue
        active_runs = int(item.get('active_run_count') or 0)
        started_at = str(item.get('current_run_started_at') or '')
        if active_runs <= 0 or not started_at:
            continue
        try:
            started_ts = datetime.fromisoformat(started_at.replace('Z', '+00:00')).timestamp()
        except Exception:
            started_ts = 0.0
        if started_ts <= 0 or (now - started_ts) < stale_after_seconds:
            continue

        def _mut(doc: dict[str, Any]) -> None:
            doc['active_run_count'] = 0
            doc['current_run_started_at'] = ''
            if str(doc.get('status') or '') == 'stopping' and doc.get('stop_requested'):
                doc['status'] = 'stopped'
                doc['next_run_ts'] = 0
                doc['next_run_at'] = ''
            elif str(doc.get('mode') or '') == 'continuous':
                doc['status'] = 'active'
                doc['next_run_ts'] = _now_ts()
                doc['next_run_at'] = _now_iso()
            else:
                next_ts, next_iso = compute_next_run(_now_ts(), str(doc.get('mode') or 'scheduled'), doc.get('schedule') or {})
                doc['status'] = 'active'
                doc['next_run_ts'] = next_ts or 0
                doc['next_run_at'] = next_iso or ''
        update_automation_doc(state_dir, automation_id, _mut)
        append_automation_event(state_dir, automation_id, 'stale_run_recovered', summary='Recovered stale automation run after restart.', payload={'stale_after_seconds': stale_after_seconds})
        recovered.append(automation_id)
    return recovered


def heartbeat_automation(state_dir: Path, automation_id: str, summary: str = '') -> dict[str, Any]:
    doc = update_automation_doc(state_dir, automation_id, lambda d: d.__setitem__('last_heartbeat_at', _now_iso()))
    if summary:
        append_automation_event(state_dir, automation_id, 'automation_heartbeat', summary=summary)
    return doc


def set_automation_status(state_dir: Path, automation_id: str, status: str, summary: str = '') -> dict[str, Any]:
    doc = update_automation_doc(state_dir, automation_id, lambda d: d.__setitem__('status', str(status).strip() or d.get('status') or 'active'))
    if doc:
        append_automation_event(state_dir, automation_id, 'automation_status_updated', summary=summary or doc['status'], payload={'status': doc['status']})
    return doc


def pause_automation(state_dir: Path, automation_id: str) -> dict[str, Any]:
    return set_automation_status(state_dir, automation_id, 'paused', 'Automation paused.')


def resume_automation(state_dir: Path, automation_id: str) -> dict[str, Any]:
    def _mut(doc: dict[str, Any]) -> None:
        doc['status'] = 'active'
        doc['stop_requested'] = False
        next_ts, next_iso = compute_next_run(_now_ts(), str(doc.get('mode') or 'scheduled'), doc.get('schedule') or {})
        if doc.get('mode') == 'continuous':
            doc['next_run_ts'] = _now_ts()
            doc['next_run_at'] = _now_iso()
        else:
            doc['next_run_ts'] = next_ts or 0
            doc['next_run_at'] = next_iso or ''
    doc = update_automation_doc(state_dir, automation_id, _mut)
    if doc:
        append_automation_event(state_dir, automation_id, 'automation_resumed', summary='Automation resumed.')
    return doc


def request_stop_automation(state_dir: Path, automation_id: str) -> dict[str, Any]:
    def _mut(doc: dict[str, Any]) -> None:
        doc['stop_requested'] = True
        doc['status'] = 'stopping'
    doc = update_automation_doc(state_dir, automation_id, _mut)
    if doc:
        append_automation_event(state_dir, automation_id, 'stop_requested', summary='Stop requested by user.')
    return doc


def mark_run_started(state_dir: Path, automation_id: str) -> dict[str, Any]:
    def _mut(doc: dict[str, Any]) -> None:
        doc['active_run_count'] = int(doc.get('active_run_count') or 0) + 1
        doc['last_run_at'] = _now_iso()
        doc['current_run_started_at'] = _now_iso()
        doc['last_heartbeat_at'] = _now_iso()
    return update_automation_doc(state_dir, automation_id, _mut)


def _deliver_webhook(webhook_url: str, payload: dict[str, Any]) -> tuple[bool, str]:
    url = str(webhook_url or '').strip()
    if not url:
        return False, 'missing_webhook_url'
    try:
        import httpx
        resp = httpx.post(url, json=payload, timeout=10)
        if 200 <= resp.status_code < 300:
            return True, ''
        return False, f'http_{resp.status_code}'
    except Exception as e:
        return False, str(e)


def finalize_run(
    state_dir: Path,
    automation_id: str,
    *,
    ok: bool,
    summary: str,
    details: dict[str, Any] | None = None,
    error: str = '',
) -> dict[str, Any]:
    path = automation_dir(state_dir, automation_id) / 'runs.jsonl'
    doc = get_automation_state(state_dir, automation_id)
    if not doc:
        return {}
    run_id = _new_id('run')
    now_ts = _now_ts()
    next_ts, next_iso = compute_next_run(now_ts, str(doc.get('mode') or 'scheduled'), doc.get('schedule') or {})
    row = {
        'run_id': run_id,
        'automation_id': automation_id,
        'ts': _now_iso(),
        'ok': bool(ok),
        'summary': str(summary)[:1000],
        'error': str(error)[:1000],
        'details': details or {},
    }
    _append_jsonl(path, row)

    def _mut(d: dict[str, Any]) -> None:
        d['active_run_count'] = max(0, int(d.get('active_run_count') or 0) - 1)
        d['health'] = 'healthy' if ok else 'degraded'
        d['last_result_summary'] = row['summary']
        d['last_error'] = row['error']
        d['current_run_started_at'] = ''
        d['consecutive_failures'] = 0 if ok else int(d.get('consecutive_failures') or 0) + 1
        if ok:
            d['last_success_at'] = row['ts']
        else:
            d['last_failure_at'] = row['ts']
        if d.get('mode') == 'once':
            d['status'] = 'completed' if ok else 'failed'
            d['next_run_ts'] = 0
            d['next_run_at'] = ''
        elif d.get('stop_requested'):
            d['status'] = 'stopped'
            d['next_run_ts'] = 0
            d['next_run_at'] = ''
        else:
            d['status'] = 'active'
            d['next_run_ts'] = next_ts or 0
            d['next_run_at'] = next_iso or ''
    doc = update_automation_doc(state_dir, automation_id, _mut)

    kind = 'automation_run_succeeded' if ok else 'automation_run_failed'
    append_automation_event(state_dir, automation_id, kind, summary=summary, payload={'run_id': run_id, 'ok': ok, 'error': error, 'details': details or {}})

    last_state = str(doc.get('last_alert_state') or '')
    current_state = 'failure' if not ok else 'recovery'
    alert_policy = doc.get('alert_policy') or {}
    should_alert = False
    if not ok and alert_policy.get('alert_on_failure', True):
        should_alert = True
    elif ok and alert_policy.get('alert_on_recovery', True) and last_state == 'failure':
        should_alert = True
    elif ok and alert_policy.get('alert_on_success', False):
        should_alert = True
    if alert_policy.get('dedupe_state_changes', True) and current_state == last_state and not ok:
        should_alert = False
    if should_alert:
        alert_payload = {'run_id': run_id, 'state': current_state, 'error': error}
        append_automation_event(state_dir, automation_id, 'automation_alert', summary=summary, payload=alert_payload)
        webhook_url = str(alert_policy.get('webhook_url') or '').strip()
        if webhook_url:
            webhook_payload = {
                'automation_id': automation_id,
                'run_id': run_id,
                'title': doc.get('title') or '',
                'kind': doc.get('kind') or '',
                'mode': doc.get('mode') or '',
                'state': current_state,
                'ok': ok,
                'summary': summary,
                'error': error,
                'details': details or {},
                'ts': row['ts'],
            }
            delivered, webhook_error = _deliver_webhook(webhook_url, webhook_payload)
            append_automation_event(
                state_dir,
                automation_id,
                'automation_webhook_delivered' if delivered else 'automation_webhook_failed',
                summary='Delivered automation webhook.' if delivered else f'Automation webhook failed: {webhook_error}',
                payload={'webhook_url': webhook_url, 'run_id': run_id, 'error': webhook_error},
            )
    update_automation_doc(state_dir, automation_id, lambda d: d.__setitem__('last_alert_state', current_state))
    return row


def summarize_automation(state_dir: Path, automation_id: str) -> dict[str, Any]:
    doc = get_automation_state(state_dir, automation_id)
    if not doc:
        return {}
    return {
        'automation_id': doc.get('automation_id') or '',
        'title': doc.get('title') or '',
        'kind': doc.get('kind') or '',
        'mode': doc.get('mode') or '',
        'status': doc.get('status') or '',
        'health': doc.get('health') or '',
        'next_run_at': doc.get('next_run_at') or '',
        'last_result_summary': doc.get('last_result_summary') or '',
        'last_error': doc.get('last_error') or '',
        'active_run_count': int(doc.get('active_run_count') or 0),
        'schedule': doc.get('schedule') or {},
    }


__all__ = [
    'automations_root',
    'automation_dir',
    'list_automations',
    'get_automation_state',
    'append_automation_event',
    'cron_matches_dt',
    'compute_next_run',
    'create_automation',
    'heartbeat_automation',
    'set_automation_webhook',
    'reconcile_stale_automation_runs',
    'set_automation_status',
    'pause_automation',
    'resume_automation',
    'request_stop_automation',
    'mark_run_started',
    'finalize_run',
    'summarize_automation',
]
