"""Cron tool — lightweight scheduled task management for Charon.

Implements a generic scheduler API over Charon's existing agent task queue.
Schedules are persisted in .charon_state/cron_jobs.json and runs are enqueued
as conversation_runtime agent_tasks.
"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from tools import ToolContext, ToolResult


CRON_TOOL_DEF = {
    'name': 'Cron',
    'description': (
        'Manage scheduled jobs. Actions: create, list, update, pause, resume, remove, run. '
        'Schedules are mapped onto Charon agent tasks with optional recurrence.'
    ),
    'input_schema': {
        'type': 'object',
        'properties': {
            'action': {
                'type': 'string',
                'enum': ['create', 'list', 'update', 'pause', 'resume', 'remove', 'run'],
            },
            'job_id': {'type': 'string'},
            'prompt': {'type': 'string'},
            'schedule': {'type': 'string'},
            'name': {'type': 'string'},
            'repeat': {'type': 'number'},
            'deliver': {'type': 'string'},
            'model': {'type': 'string'},
            'provider': {'type': 'string'},
            'base_url': {'type': 'string'},
            'owner_agent_id': {'type': 'string'},
            'project': {'type': 'string'},
            'include_disabled': {'type': 'boolean'},
            'reason': {'type': 'string'},
        },
        'required': ['action'],
    },
}


def _state_dir(ctx: ToolContext) -> Path:
    return ctx.state_dir or (ctx.project_root / '.charon_state')


def _jobs_path(state_dir: Path) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / 'cron_jobs.json'


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(ts: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(ts.strip())
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _parse_interval_minutes(schedule: str) -> int | None:
    s = schedule.strip().lower()
    m = re.fullmatch(r'(\d+)\s*([mhd])', s)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        return n if unit == 'm' else (n * 60 if unit == 'h' else n * 1440)

    m = re.fullmatch(r'every\s+(\d+)\s*(min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)', s)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        if unit.startswith('m'):
            return n
        if unit.startswith('h'):
            return n * 60
        return n * 1440

    return None


def _parse_daily_cron(schedule: str) -> datetime | None:
    parts = schedule.strip().split()
    if len(parts) != 5:
        return None
    minute, hour, dom, month, dow = parts
    if dom != '*' or month != '*' or dow != '*':
        return None
    if not minute.isdigit() or not hour.isdigit():
        return None
    mm = int(minute)
    hh = int(hour)
    if mm < 0 or mm > 59 or hh < 0 or hh > 23:
        return None

    now = datetime.now(timezone.utc)
    cand = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if cand <= now:
        cand = cand + timedelta(days=1)
    return cand


def _compute_schedule(schedule: str) -> tuple[str | None, int | None, str | None]:
    if not schedule or not schedule.strip():
        return None, None, 'schedule is required.'

    interval = _parse_interval_minutes(schedule)
    if interval and interval > 0:
        run_at = datetime.now(timezone.utc) + timedelta(minutes=interval)
        return run_at.isoformat(), interval, None

    iso = _parse_iso(schedule)
    if iso:
        return iso.isoformat(), None, None

    daily = _parse_daily_cron(schedule)
    if daily:
        return daily.isoformat(), 1440, None

    return None, None, f'unsupported schedule format: {schedule}'


def _load_jobs(state_dir: Path) -> dict[str, Any]:
    path = _jobs_path(state_dir)
    if not path.exists():
        return {'jobs': {}}
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if isinstance(data, dict) and isinstance(data.get('jobs'), dict):
            return data
    except Exception:
        pass
    return {'jobs': {}}


def _save_jobs(state_dir: Path, data: dict[str, Any]) -> None:
    _jobs_path(state_dir).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding='utf-8')


def _enqueue_job_run(state_dir: Path, job: dict[str, Any], *, immediate: bool = False) -> dict[str, Any]:
    from conversation_runtime import enqueue_agent_task

    owner_agent_id = str(job.get('owner_agent_id') or '').strip()
    if not owner_agent_id:
        raise ValueError('owner_agent_id missing for cron job')

    not_before = None if immediate else job.get('next_run')
    task = enqueue_agent_task(
        state_dir,
        owner_agent_id=owner_agent_id,
        instruction=str(job.get('prompt') or ''),
        title=str(job.get('name') or f"cron:{job.get('job_id')}")[:120],
        project=job.get('project'),
        priority='normal',
        correlation_id=f"cron:{job.get('job_id')}",
        interval_minutes=float(job['interval_minutes']) if job.get('interval_minutes') else None,
        not_before=not_before,
    )
    return task


def execute_cron(params: dict, ctx: ToolContext) -> ToolResult:
    action = str(params.get('action') or '').strip().lower()
    state_dir = _state_dir(ctx)
    jobs_doc = _load_jobs(state_dir)
    jobs = jobs_doc.setdefault('jobs', {})

    try:
        if action == 'list':
            include_disabled = bool(params.get('include_disabled', False))
            rows = []
            for job in jobs.values():
                if not include_disabled and job.get('status') not in ('active', 'running', 'paused'):
                    continue
                rows.append(job)
            rows.sort(key=lambda r: str(r.get('created_at') or ''))
            if not rows:
                return ToolResult(content='No cron jobs found.', details={'jobs': []})
            lines = [f'Cron jobs ({len(rows)}):']
            for j in rows:
                lines.append(
                    f"- {j.get('job_id')}  [{j.get('status')}]  name={j.get('name') or '(unnamed)'}  schedule={j.get('schedule')}  next={j.get('next_run') or '(none)'}"
                )
            return ToolResult(content='\n'.join(lines), details={'jobs': rows})

        if action == 'create':
            prompt = str(params.get('prompt') or '').strip()
            schedule = str(params.get('schedule') or '').strip()
            if not prompt:
                return ToolResult(content='Error: prompt is required for create.', is_error=True)
            if not schedule:
                return ToolResult(content='Error: schedule is required for create.', is_error=True)

            next_run, interval_minutes, err = _compute_schedule(schedule)
            if err:
                return ToolResult(content=f'Error: {err}', is_error=True)

            owner = str(params.get('owner_agent_id') or ctx.agent_id or '').strip()
            if not owner:
                return ToolResult(content='Error: owner_agent_id required (or active agent context).', is_error=True)

            job_id = f"job_{uuid.uuid4().hex[:10]}"
            job = {
                'job_id': job_id,
                'name': str(params.get('name') or '').strip(),
                'prompt': prompt,
                'schedule': schedule,
                'status': 'active',
                'created_at': _now_iso(),
                'updated_at': _now_iso(),
                'next_run': next_run,
                'interval_minutes': interval_minutes,
                'owner_agent_id': owner,
                'project': str(params.get('project') or ctx.project_root),
                'repeat': int(params.get('repeat') or 0),
                'deliver': str(params.get('deliver') or 'origin'),
                'model': str(params.get('model') or ''),
                'provider': str(params.get('provider') or ''),
                'base_url': str(params.get('base_url') or ''),
                'last_task_id': None,
                'pause_reason': '',
            }
            task = _enqueue_job_run(state_dir, job, immediate=False)
            job['last_task_id'] = task.get('id')
            jobs[job_id] = job
            _save_jobs(state_dir, jobs_doc)
            return ToolResult(
                content=(
                    f"Cron job created: {job_id}\n"
                    f"Name: {job.get('name') or '(unnamed)'}\n"
                    f"Schedule: {schedule}\n"
                    f"Next run: {job.get('next_run')}\n"
                    f"Initial task: {task.get('id')}"
                ),
                details={'job': job, 'task': task},
            )

        job_id = str(params.get('job_id') or '').strip()
        if action in ('update', 'pause', 'resume', 'remove', 'run') and not job_id:
            return ToolResult(content='Error: job_id is required.', is_error=True)
        job = jobs.get(job_id)
        if action in ('update', 'pause', 'resume', 'remove', 'run') and not job:
            return ToolResult(content=f'Error: job not found: {job_id}', is_error=True)

        if action == 'update':
            if 'prompt' in params and str(params.get('prompt') or '').strip():
                job['prompt'] = str(params.get('prompt')).strip()
            if 'name' in params:
                job['name'] = str(params.get('name') or '').strip()
            if 'schedule' in params and str(params.get('schedule') or '').strip():
                next_run, interval_minutes, err = _compute_schedule(str(params.get('schedule')))
                if err:
                    return ToolResult(content=f'Error: {err}', is_error=True)
                job['schedule'] = str(params.get('schedule'))
                job['next_run'] = next_run
                job['interval_minutes'] = interval_minutes
            for key in ('deliver', 'model', 'provider', 'base_url'):
                if key in params:
                    job[key] = str(params.get(key) or '')
            job['updated_at'] = _now_iso()
            _save_jobs(state_dir, jobs_doc)
            return ToolResult(content=f'Cron job updated: {job_id}', details={'job': job})

        if action == 'pause':
            job['status'] = 'paused'
            job['pause_reason'] = str(params.get('reason') or '').strip()
            job['updated_at'] = _now_iso()
            _save_jobs(state_dir, jobs_doc)
            return ToolResult(content=f'Cron job paused: {job_id}', details={'job': job})

        if action == 'resume':
            job['status'] = 'active'
            if not job.get('next_run'):
                next_run, _, _ = _compute_schedule(str(job.get('schedule') or ''))
                job['next_run'] = next_run
            job['updated_at'] = _now_iso()
            task = _enqueue_job_run(state_dir, job, immediate=False)
            job['last_task_id'] = task.get('id')
            _save_jobs(state_dir, jobs_doc)
            return ToolResult(content=f'Cron job resumed: {job_id}', details={'job': job, 'task': task})

        if action == 'run':
            if job.get('status') == 'removed':
                return ToolResult(content=f'Error: job removed: {job_id}', is_error=True)
            task = _enqueue_job_run(state_dir, job, immediate=True)
            job['last_task_id'] = task.get('id')
            job['updated_at'] = _now_iso()
            _save_jobs(state_dir, jobs_doc)
            return ToolResult(content=f'Cron job run enqueued: {job_id} -> {task.get("id")}', details={'job': job, 'task': task})

        if action == 'remove':
            job['status'] = 'removed'
            job['updated_at'] = _now_iso()
            _save_jobs(state_dir, jobs_doc)
            return ToolResult(content=f'Cron job removed: {job_id}', details={'job': job})

        return ToolResult(content=f'Error: unknown action "{action}".', is_error=True)

    except Exception as e:
        return ToolResult(content=f'Cron tool error: {e}', is_error=True)
