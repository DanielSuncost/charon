"""Parallel batch orchestration — fan-out/fan-in for independent tasks.

Unlike the sequential shade contract (P01 → P02 → P03 → P04), a batch
spawns N independent shade agents that run simultaneously. Each gets its
own ConversationEngine. Results are collected when all complete.

Usage:
    batch = create_batch(state_dir, tasks=[...], max_concurrent=5, ...)
    # Daemon loop or background thread picks up batch tasks
    # When all complete, batch status becomes 'completed'
"""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _batch_id() -> str:
    return f"batch-{uuid.uuid4().hex[:10]}"


def _batch_path(state_dir: Path) -> Path:
    return state_dir / 'shade_batches.json'


def _load_batches(state_dir: Path) -> list[dict]:
    p = _batch_path(state_dir)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text())
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_batches(state_dir: Path, batches: list[dict]) -> None:
    p = _batch_path(state_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(batches, indent=2))


def create_batch(
    state_dir: Path,
    *,
    parent_agent_id: str,
    project: str,
    goal: str,
    tasks: list[dict],
    max_concurrent: int = 5,
    constraints: list[str] | None = None,
    phase_name: str = 'generation',
) -> dict:
    """Create a parallel batch of independent shade tasks.

    Each task dict should have at minimum:
        {'instruction': str, 'title': str}

    Optional per-task fields:
        scope, constraints, expected_outputs

    Returns the batch record.
    """
    bid = _batch_id()
    now = _now()

    batch_tasks = []
    for i, t in enumerate(tasks):
        task_id = f"task-{bid}-{i:03d}"
        batch_tasks.append({
            'id': task_id,
            'index': i,
            'instruction': t.get('instruction', ''),
            'title': t.get('title', f'Batch item {i+1}'),
            'complexity': t.get('complexity', 'normal'),
            'status': 'pending',
            'shade_agent_id': None,
            'model_used': None,
            'result_summary': None,
            'error': None,
            'started_at': None,
            'completed_at': None,
            'scope': t.get('scope') or [],
            'constraints': t.get('constraints') or list(constraints or []),
            'expected_outputs': t.get('expected_outputs') or [],
        })

    batch = {
        'id': bid,
        'status': 'pending',
        'parent_agent_id': parent_agent_id,
        'project': project,
        'goal': goal,
        'phase_name': phase_name,
        'max_concurrent': max_concurrent,
        'total': len(batch_tasks),
        'completed_count': 0,
        'failed_count': 0,
        'tasks': batch_tasks,
        'created_at': now,
        'updated_at': now,
        'completed_at': None,
    }

    batches = _load_batches(state_dir)
    batches.append(batch)
    _save_batches(state_dir, batches)
    return batch


def get_batch(state_dir: Path, batch_id: str) -> dict | None:
    for b in _load_batches(state_dir):
        if b.get('id') == batch_id:
            return b
    return None


def list_batches(state_dir: Path, status: str | None = None) -> list[dict]:
    batches = _load_batches(state_dir)
    if status:
        batches = [b for b in batches if b.get('status') == status]
    return batches


def _update_batch(state_dir: Path, batch: dict) -> None:
    batches = _load_batches(state_dir)
    for i, b in enumerate(batches):
        if b.get('id') == batch.get('id'):
            batch['updated_at'] = _now()
            batches[i] = batch
            _save_batches(state_dir, batches)
            return
    batches.append(batch)
    _save_batches(state_dir, batches)


def get_next_batch_tasks(state_dir: Path, batch_id: str, count: int = 1) -> list[dict]:
    """Get the next N pending tasks from a batch, respecting max_concurrent."""
    batch = get_batch(state_dir, batch_id)
    if not batch or batch.get('status') not in ('pending', 'running'):
        return []

    in_progress = sum(1 for t in batch['tasks'] if t.get('status') == 'in_progress')
    available = batch.get('max_concurrent', 5) - in_progress
    if available <= 0:
        return []

    pending = [t for t in batch['tasks'] if t.get('status') == 'pending']
    return pending[:min(count, available)]


def mark_batch_task_started(state_dir: Path, batch_id: str, task_id: str, shade_agent_id: str) -> None:
    batch = get_batch(state_dir, batch_id)
    if not batch:
        return
    for t in batch['tasks']:
        if t.get('id') == task_id:
            t['status'] = 'in_progress'
            t['shade_agent_id'] = shade_agent_id
            t['started_at'] = _now()
            break
    batch['status'] = 'running'
    _update_batch(state_dir, batch)


def _record_model_used(state_dir: Path, batch_id: str, task_id: str, model) -> None:
    """Record which model a shade is using."""
    batch = get_batch(state_dir, batch_id)
    if not batch:
        return
    model_id = getattr(model, 'model_id', str(model)) if model else 'unknown'
    for t in batch['tasks']:
        if t.get('id') == task_id:
            t['model_used'] = model_id
            break
    _update_batch(state_dir, batch)


def mark_batch_task_completed(state_dir: Path, batch_id: str, task_id: str, summary: str) -> dict | None:
    batch = get_batch(state_dir, batch_id)
    if not batch:
        return None
    for t in batch['tasks']:
        if t.get('id') == task_id:
            t['status'] = 'completed'
            t['result_summary'] = summary
            t['completed_at'] = _now()
            break

    batch['completed_count'] = sum(1 for t in batch['tasks'] if t.get('status') == 'completed')
    batch['failed_count'] = sum(1 for t in batch['tasks'] if t.get('status') == 'failed')

    if batch['completed_count'] + batch['failed_count'] >= batch['total']:
        batch['status'] = 'completed' if batch['failed_count'] == 0 else 'partial'
        batch['completed_at'] = _now()

    _update_batch(state_dir, batch)
    return batch


def mark_batch_task_failed(state_dir: Path, batch_id: str, task_id: str, error: str) -> dict | None:
    batch = get_batch(state_dir, batch_id)
    if not batch:
        return None
    for t in batch['tasks']:
        if t.get('id') == task_id:
            t['status'] = 'failed'
            t['error'] = error
            t['completed_at'] = _now()
            break

    batch['completed_count'] = sum(1 for t in batch['tasks'] if t.get('status') == 'completed')
    batch['failed_count'] = sum(1 for t in batch['tasks'] if t.get('status') == 'failed')

    if batch['completed_count'] + batch['failed_count'] >= batch['total']:
        batch['status'] = 'completed' if batch['failed_count'] == 0 else 'partial'
        batch['completed_at'] = _now()

    _update_batch(state_dir, batch)
    return batch


def summarize_batch(batch: dict) -> str:
    """Human-readable batch summary."""
    total = batch.get('total', 0)
    done = batch.get('completed_count', 0)
    failed = batch.get('failed_count', 0)
    in_progress = sum(1 for t in batch.get('tasks', []) if t.get('status') == 'in_progress')
    pending = sum(1 for t in batch.get('tasks', []) if t.get('status') == 'pending')

    # Collect unique models used
    models = set()
    for t in batch.get('tasks', []):
        m = t.get('model_used')
        if m:
            models.add(m)
    models_str = f"  models: {', '.join(sorted(models))}" if models else ''

    return (
        f"Batch {batch.get('id', '?')}: {batch.get('status', '?')} "
        f"({done}/{total} done, {failed} failed, {in_progress} running, {pending} pending)"
        f"{models_str}"
    )


def run_batch_worker(
    state_dir: Path,
    batch_id: str,
    *,
    phase_name: str = 'generation',
) -> None:
    """Run pending batch tasks on background threads.

    Each task gets its own ConversationEngine with the shade-configured
    model. Runs up to max_concurrent simultaneously.

    Called from the daemon loop or from the SpawnShade tool.
    """
    import asyncio

    batch = get_batch(state_dir, batch_id)
    if not batch:
        return

    try:
        from worker_provider import get_worker_provider_status
        provider_status = get_worker_provider_status(state_dir)
        if not provider_status.get('ok'):
            for task in batch.get('tasks', []):
                if task.get('status') == 'pending':
                    mark_batch_task_failed(state_dir, batch_id, task['id'], f"Worker provider unavailable: {provider_status.get('reason') or 'no_provider'}")
            return
    except Exception:
        pass

    def _run_single_task(batch_task: dict):
        """Execute one batch task in its own thread."""
        try:
            from model_registry import get_shade_provider_and_model
            from conversation_engine import ConversationEngine
            from agent_lifecycle import create_agent
            from task_summarizer import summarize_fast

            task_complexity = batch_task.get('complexity', 'normal')
            provider, model, ready = get_shade_provider_and_model(
                state_dir,
                phase_name=phase_name,
                task_complexity=task_complexity,
            )
            if not ready:
                mark_batch_task_failed(state_dir, batch_id, batch_task['id'], 'No provider configured')
                return

            # Record which model this shade is using
            _record_model_used(state_dir, batch_id, batch_task['id'], model)

            # Create shade agent
            shade = create_agent(
                name='', mode='temp',
                goal=f"Batch task: {batch_task.get('title', '')[:60]}",
                project=batch.get('project', ''),
                role='shade', visibility='internal',
                parent_agent_id=batch.get('parent_agent_id', ''),
                require_tmux=False,
            )
            shade_id = shade.get('id', '')
            mark_batch_task_started(state_dir, batch_id, batch_task['id'], shade_id)

            # Build system prompt for shade
            from system_prompt_builder import build_system_prompt
            sys_prompt = build_system_prompt(
                state_dir=state_dir,
                agent={'id': shade_id, 'role': 'shade', 'parent_agent_id': batch.get('parent_agent_id')},
                task={'project': batch.get('project', ''), 'shade_phase': {}},
            )

            engine = ConversationEngine(
                provider=provider, model=model,
                project_root=batch.get('project', '.'),
                agent_id=shade_id,
                system_prompt=sys_prompt,
                state_dir=state_dir,
                max_tokens=16384,
            )
            # Set scope restriction from task constraints
            task_scope = batch_task.get('scope') or []
            if task_scope:
                engine.scope = task_scope

            # Build instruction
            instruction = batch_task.get('instruction', '')
            constraints = batch_task.get('constraints') or []
            if constraints:
                instruction += '\n\nConstraints:\n' + '\n'.join(f'- {c}' for c in constraints)

            # Execute
            text_parts = []
            tool_calls = []
            errors = []
            total_input_tokens = 0
            total_output_tokens = 0

            async def _exec():
                nonlocal total_input_tokens, total_output_tokens
                async for event in engine.submit(instruction):
                    if event.type == 'text_delta':
                        text_parts.append(event.data.get('text', ''))
                    elif event.type == 'tool_execution_end':
                        tool_calls.append({
                            'tool': event.data.get('tool_name', ''),
                            'is_error': event.data.get('is_error', False),
                        })
                    elif event.type == 'message_end':
                        usage = event.data.get('usage', {})
                        total_input_tokens += usage.get('input_tokens', 0)
                        total_output_tokens += usage.get('output_tokens', 0)
                    elif event.type == 'error':
                        errors.append(event.data.get('error', ''))

            asyncio.run(_exec())

            # Retry once on transient errors
            if errors and not response:
                transient = any(
                    'chunked read' in e or 'connection' in e.lower() or
                    '502' in e or '503' in e or '429' in e or 'timed out' in e.lower()
                    for e in errors
                )
                if transient:
                    import time as _retry_time
                    _retry_time.sleep(5)
                    errors.clear()
                    text_parts.clear()
                    tool_calls.clear()
                    asyncio.run(_exec())

            # Record shade token usage
            try:
                from shade_stats import record_shade_usage
                record_shade_usage(
                    state_dir,
                    parent_agent_id=batch.get('parent_agent_id', ''),
                    shade_agent_id=shade_id,
                    model_id=getattr(model, 'model_id', 'unknown'),
                    input_tokens=total_input_tokens,
                    output_tokens=total_output_tokens,
                )
            except Exception:
                pass

            response = ''.join(text_parts).strip()
            summary = summarize_fast(
                instruction=batch_task.get('instruction', ''),
                tool_calls=tool_calls,
                response_text=response,
                errors=errors,
                total_turns=1,
            )

            if errors and not response:
                mark_batch_task_failed(state_dir, batch_id, batch_task['id'], '; '.join(errors))
            else:
                mark_batch_task_completed(state_dir, batch_id, batch_task['id'], summary)

        except Exception as e:
            mark_batch_task_failed(state_dir, batch_id, batch_task['id'], str(e))

    # Determine safe concurrency based on provider type
    # OAuth providers (claude-code, codex) have strict rate limits
    # Local and API-key providers can handle more concurrency
    max_conc = batch.get('max_concurrent', 5)
    try:
        from model_registry import load_registry
        reg = load_registry(state_dir)
        shade_mode = reg.get('shade_model_mode', 'auto')
        shade_provider = reg.get('shade_provider', '')
        is_local = shade_provider in ('local', 'lmstudio', 'ollama') or shade_mode == 'same'

        if is_local and shade_mode != 'same':
            # Local model — no rate limits, full concurrency
            stagger_seconds = 0.5
        else:
            # Check if main provider is OAuth-based (claude-code, codex)
            import json as _json
            onboarding = _json.loads((state_dir / 'onboarding.json').read_text()) if (state_dir / 'onboarding.json').exists() else {}
            provider = str(onboarding.get('provider', '')).lower()
            if provider in ('claude-code', 'codex'):
                # OAuth — strict limits. Cap at 2 concurrent, longer stagger
                max_conc = min(max_conc, 2)
                stagger_seconds = 5
            elif provider in ('api',) or 'openrouter' in str(reg.get('shade_base_url', '')).lower():
                # API key or OpenRouter — moderate limits
                max_conc = min(max_conc, 5)
                stagger_seconds = 1
            else:
                # Local provider as main
                stagger_seconds = 0.5
    except Exception:
        stagger_seconds = 2

    tasks_to_run = get_next_batch_tasks(state_dir, batch_id, count=max_conc)
    threads = []
    for i, bt in enumerate(tasks_to_run):
        t = threading.Thread(target=_run_single_task, args=(bt,), daemon=True)
        t.start()
        threads.append(t)
        if i < len(tasks_to_run) - 1 and stagger_seconds > 0:
            import time as _t
            _t.sleep(stagger_seconds)

    # Wait for this wave to complete, then check for more
    for t in threads:
        t.join(timeout=300)  # 5 min timeout per task

    # Check if there are more pending tasks
    updated = get_batch(state_dir, batch_id)
    if updated and any(t.get('status') == 'pending' for t in updated.get('tasks', [])):
        run_batch_worker(state_dir, batch_id, phase_name=phase_name)
