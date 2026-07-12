"""SpawnBatch tool — lets the agent create parallel shade swarms from conversation.

The agent decomposes a task into independent sub-tasks and spawns them
as a parallel batch. Each sub-task gets its own shade with its own engine.
"""
from __future__ import annotations

import threading

from charon.tools import ToolContext, ToolResult

try:
    from charon.infra.diagnostics import record as _diag
except Exception:  # diagnostics is best-effort and must never block import
    def _diag(*args, **kwargs):
        return None


SPAWN_BATCH_TOOL_DEF = {
    'name': 'SpawnBatch',
    'description': (
        'Spawn a parallel swarm of shade workers for independent sub-tasks. '
        'Each task runs simultaneously on its own shade agent. '
        'Use this when you have multiple independent items to process '
        '(e.g., "generate 10 images", "write tests for 5 modules", '
        '"check 8 API endpoints"). '
        'Each task should be self-contained — shades cannot see each other\'s work.'
    ),
    'input_schema': {
        'type': 'object',
        'properties': {
            'goal': {
                'type': 'string',
                'description': 'Overall goal of the batch (e.g., "Generate product images").',
            },
            'tasks': {
                'type': 'array',
                'description': 'List of independent tasks. Each has title and instruction.',
                'items': {
                    'type': 'object',
                    'properties': {
                        'title': {'type': 'string', 'description': 'Short title for this task.'},
                        'instruction': {'type': 'string', 'description': 'Full instruction for the shade.'},
                        'complexity': {
                            'type': 'string',
                            'enum': ['simple', 'normal', 'complex'],
                            'description': 'Task complexity — determines which model tier the shade gets. simple=fast/cheap model, complex=strong model.',
                        },
                    },
                    'required': ['title', 'instruction'],
                },
            },
            'max_concurrent': {
                'type': 'number',
                'description': 'Max shades running simultaneously (default: 3).',
            },
            'constraints': {
                'type': 'array',
                'items': {'type': 'string'},
                'description': 'Constraints applied to ALL tasks in the batch.',
            },
        },
        'required': ['goal', 'tasks'],
    },
}


def execute_spawn_batch(params: dict, ctx: ToolContext) -> ToolResult:
    """Create and launch a parallel batch of shade workers."""
    goal = str(params.get('goal', '')).strip()
    tasks = params.get('tasks') or []
    max_concurrent = int(params.get('max_concurrent') or 3)
    constraints = params.get('constraints') or []

    if not goal:
        return ToolResult(content='Error: goal is required.', is_error=True)
    if not tasks:
        return ToolResult(content='Error: tasks list is required and must not be empty.', is_error=True)
    if len(tasks) > 50:
        return ToolResult(content='Error: maximum 50 tasks per batch.', is_error=True)

    if not ctx.state_dir:
        return ToolResult(content='Error: state_dir not available.', is_error=True)

    try:
        from charon.providers.worker_provider import ensure_worker_provider_or_request_clarification
        provider_status = ensure_worker_provider_or_request_clarification(ctx.state_dir, ctx=ctx, purpose='batches')
        if not provider_status.get('ok'):
            choices = provider_status.get('available_providers') or []
            clarification = provider_status.get('clarification') or {}
            cid = clarification.get('clarification_id') or ''
            question = provider_status.get('question') or 'No usable provider is configured for batches.'
            return ToolResult(
                content=(
                    f'{question}\n'
                    + (f'Clarification ID: {cid}\n' if cid else '')
                    + ('Choices:\n' + '\n'.join(f'- {c}' for c in choices) if choices else 'No provider options detected.')
                ),
                is_error=True,
                details=provider_status,
            )
    except Exception as exc:
        _diag('batch_tool', 'worker-provider preflight check failed; batch proceeds without provider validation', error=exc)

    # Validate tasks
    clean_tasks = []
    for i, t in enumerate(tasks):
        if not isinstance(t, dict):
            continue
        instruction = str(t.get('instruction', '')).strip()
        title = str(t.get('title', f'Task {i+1}')).strip()
        if not instruction:
            continue
        clean_tasks.append({
            'instruction': instruction,
            'title': title,
            'constraints': list(t.get('constraints') or constraints),
            'expected_outputs': list(t.get('expected_outputs') or []),
        })

    if not clean_tasks:
        return ToolResult(content='Error: no valid tasks in the list.', is_error=True)

    try:
        from charon.automation.batch_orchestrator import create_batch, run_batch_worker, summarize_batch

        batch = create_batch(
            ctx.state_dir,
            parent_agent_id=ctx.agent_id,
            project=str(ctx.project_root),
            goal=goal,
            tasks=clean_tasks,
            max_concurrent=max_concurrent,
            constraints=constraints,
        )

        # Launch batch worker in background thread
        def _run():
            try:
                run_batch_worker(ctx.state_dir, batch['id'])
            except Exception as e:
                _diag('batch_tool', 'background batch worker crashed; batch marked failed', error=e, batch_id=str(batch.get('id') or ''))
                try:
                    from charon.automation.batch_orchestrator import mark_batch_failed
                    mark_batch_failed(ctx.state_dir, batch['id'], f'batch worker crashed: {e}')
                except Exception as e2:
                    _diag('batch_tool', 'failed to mark crashed batch as failed; batch may stay running forever', error=e2, batch_id=str(batch.get('id') or ''))

        t = threading.Thread(target=_run, daemon=True)
        t.start()

        summary = summarize_batch(batch)
        task_list = '\n'.join(f'  {i+1}. {t["title"]}' for i, t in enumerate(clean_tasks))

        return ToolResult(
            content=(
                f'Batch created: {batch["id"]}\n'
                f'Goal: {goal}\n'
                f'Tasks ({len(clean_tasks)}, max {max_concurrent} concurrent):\n'
                f'{task_list}\n\n'
                f'Status: {summary}\n'
                f'Shades are running in the background. '
                f'Check progress with: /batch {batch["id"]}'
            ),
        )

    except Exception as e:
        return ToolResult(content=f'Error creating batch: {e}', is_error=True)
