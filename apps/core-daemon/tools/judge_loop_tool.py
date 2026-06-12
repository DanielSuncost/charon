"""SpawnJudgeLoop tool — iterative optimization with pluggable scoring.

Lets agents spawn judge loops that iterate toward a quality target:
    implement → judge(score + feedback) → keep/rollback → repeat → converge

Judge types:
    - quantitative: run a command, parse a number
    - correctness: run tests, compute pass rate
    - aesthetic: LLM scores against a rubric
    - composite: weighted mix of the above
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tools import ToolContext, ToolResult


JUDGE_LOOP_TOOL_DEF = {
    'name': 'SpawnJudgeLoop',
    'description': (
        'Spawn an iterative optimization loop with a Judge that scores each iteration. '
        'The loop implements a change, scores it, keeps improvements and rolls back regressions, '
        'then repeats until the target score is met or the budget is exhausted. '
        'Judge types: quantitative (run command, parse number), correctness (test pass rate), '
        'aesthetic (LLM scores against rubric), composite (weighted mix). '
        'Use this for any task with a measurable quality signal: performance optimization, '
        'test coverage improvement, code quality refinement, prose editing.'
    ),
    'input_schema': {
        'type': 'object',
        'properties': {
            'goal': {
                'type': 'string',
                'description': 'What to optimize — clear description of the objective.',
            },
            'judge_type': {
                'type': 'string',
                'enum': ['quantitative', 'correctness', 'aesthetic', 'composite'],
                'description': (
                    'Type of judge. quantitative: run command + parse number. '
                    'correctness: test pass rate. aesthetic: LLM scores against rubric. '
                    'composite: weighted mix.'
                ),
            },
            'direction': {
                'type': 'string',
                'enum': ['maximize', 'minimize'],
                'description': 'Whether to maximize or minimize the score.',
            },
            'target_score': {
                'type': 'number',
                'description': 'Stop when this score is reached (optional).',
            },
            'eval_command': {
                'type': 'string',
                'description': (
                    'Command to evaluate the current state. '
                    'For quantitative: must output a number. '
                    'For correctness: run the test suite (e.g. "pytest tests/ -x").'
                ),
            },
            'metric_name': {
                'type': 'string',
                'description': 'Name of the metric being optimized (e.g. "p99_latency_ms").',
            },
            'run_command': {
                'type': 'string',
                'description': 'Optional command to run before eval (e.g. training, build step).',
            },
            'rubric': {
                'type': 'string',
                'description': 'For aesthetic judge: freeform scoring rubric (e.g. "Rate clarity, conciseness, accuracy 1-10").',
            },
            'constraint_commands': {
                'type': 'array',
                'items': {'type': 'string'},
                'description': 'Commands that must pass before scoring (e.g. ["pytest tests/ -x", "mypy src/"]).',
            },
            'scope': {
                'type': 'array',
                'items': {'type': 'string'},
                'description': 'Files/directories the implementer can modify.',
            },
            'frozen': {
                'type': 'array',
                'items': {'type': 'string'},
                'description': 'Files/directories that must NOT be modified.',
            },
            'max_iterations': {
                'type': 'number',
                'description': 'Maximum iterations before stopping (default: 20).',
            },
            'max_wall_minutes': {
                'type': 'number',
                'description': 'Maximum wall-clock time in minutes (default: 0 = no limit).',
            },
            'program': {
                'type': 'string',
                'description': (
                    'Instructions for the implementer — what approaches to try, '
                    'constraints, ideas to explore. Like a program.md for the optimization.'
                ),
            },
            'parse_mode': {
                'type': 'string',
                'enum': ['last_float', 'json_field', 'pass_rate', 'custom_regex'],
                'description': 'How to extract the score from eval output (default: last_float).',
            },
            'parse_field': {
                'type': 'string',
                'description': 'For json_field: the field name. For custom_regex: the pattern with one capture group.',
            },
            'sub_judges': {
                'type': 'array',
                'items': {
                    'type': 'object',
                    'properties': {
                        'judge_type': {'type': 'string'},
                        'weight': {'type': 'number'},
                        'eval_command': {'type': 'string'},
                        'rubric': {'type': 'string'},
                    },
                },
                'description': 'For composite judge: list of sub-judges with weights.',
            },
            'action': {
                'type': 'string',
                'enum': ['create', 'status', 'pause', 'resume', 'stop', 'list'],
                'description': 'Action to take. Default: create.',
            },
            'loop_id': {
                'type': 'string',
                'description': 'For status/pause/resume/stop: the judge loop ID.',
            },
        },
        'required': ['goal'],
    },
}


def execute_judge_loop(params: dict, ctx: ToolContext) -> ToolResult:
    """Execute a judge loop action."""
    action = str(params.get('action', 'create')).strip()

    if action == 'list':
        return _handle_list(ctx)
    elif action == 'status':
        return _handle_status(params, ctx)
    elif action in ('pause', 'resume', 'stop'):
        return _handle_lifecycle(action, params, ctx)
    elif action == 'create':
        return _handle_create(params, ctx)
    else:
        return ToolResult(content=f'Unknown action: {action}', is_error=True)


def _handle_create(params: dict, ctx: ToolContext) -> ToolResult:
    """Create a new judge loop."""
    goal = str(params.get('goal', '')).strip()
    if not goal:
        return ToolResult(content='Error: goal is required', is_error=True)

    if not ctx.state_dir:
        return ToolResult(content='Error: state_dir not available', is_error=True)

    try:
        from judge_engine import create_loop, format_status

        config = create_loop(
            ctx.state_dir,
            goal=goal,
            project=str(ctx.project_root),
            agent_id=ctx.agent_id,
            judge_type=str(params.get('judge_type', 'quantitative')),
            direction=str(params.get('direction', 'maximize')),
            target_score=params.get('target_score'),
            eval_command=str(params.get('eval_command', '')),
            metric_name=str(params.get('metric_name', 'score')),
            parse_mode=str(params.get('parse_mode', 'last_float')),
            parse_field=str(params.get('parse_field', '')),
            run_command=str(params.get('run_command', '')),
            run_timeout=int(params.get('run_timeout', 600)),
            rubric=str(params.get('rubric', '')),
            sub_judges=params.get('sub_judges') or [],
            constraint_commands=params.get('constraint_commands') or [],
            scope=params.get('scope') or [],
            frozen=params.get('frozen') or [],
            max_iterations=int(params.get('max_iterations', 20)),
            max_wall_minutes=int(params.get('max_wall_minutes', 0)),
            program=str(params.get('program', '')),
        )

        summary = (
            f'Judge loop created: `{config.id}`\n'
            f'Goal: {config.goal}\n'
            f'Judge: {config.judge_type} ({config.direction})\n'
            f'Budget: {config.max_iterations} iterations'
        )
        if config.target_score is not None:
            summary += f'\nTarget: {config.target_score}'
        if config.scope:
            summary += f'\nScope: {", ".join(config.scope)}'
        if config.constraint_commands:
            summary += f'\nConstraints: {len(config.constraint_commands)} check(s)'

        summary += (
            f'\n\nThe Charon daemon advances this loop on its heartbeat — one step '
            f'at a time so the daemon stays responsive: it measures a baseline, then '
            f'each iteration spawns a scoped implementer to make one change, scores it '
            f'with the judge, and keeps it if improved (rolling back via shadow git if '
            f'not) until the target is met or the budget is exhausted. The implement '
            f'step needs a provider configured; pause/resume/stop control the loop. '
            f'Check progress with:\n'
            f'  SpawnJudgeLoop(action="status", loop_id="{config.id}")'
        )

        return ToolResult(
            content=summary,
            details={'loop_id': config.id, 'status': config.status},
        )

    except Exception as e:
        return ToolResult(content=f'Error creating judge loop: {e}', is_error=True)


def _handle_status(params: dict, ctx: ToolContext) -> ToolResult:
    """Get status of a judge loop."""
    loop_id = str(params.get('loop_id', '')).strip()
    if not loop_id:
        return ToolResult(content='Error: loop_id is required for status', is_error=True)

    if not ctx.state_dir:
        return ToolResult(content='Error: state_dir not available', is_error=True)

    try:
        from judge_engine import load_loop, format_status
        config = load_loop(ctx.state_dir, loop_id)
        if not config:
            return ToolResult(content=f'Judge loop not found: {loop_id}', is_error=True)

        return ToolResult(content=format_status(config))
    except Exception as e:
        return ToolResult(content=f'Error getting status: {e}', is_error=True)


def _handle_lifecycle(action: str, params: dict, ctx: ToolContext) -> ToolResult:
    """Handle pause/resume/stop actions."""
    loop_id = str(params.get('loop_id', '')).strip()
    if not loop_id:
        return ToolResult(content=f'Error: loop_id is required for {action}', is_error=True)

    if not ctx.state_dir:
        return ToolResult(content='Error: state_dir not available', is_error=True)

    try:
        from judge_engine import load_loop, save_loop

        config = load_loop(ctx.state_dir, loop_id)
        if not config:
            return ToolResult(content=f'Judge loop not found: {loop_id}', is_error=True)

        if action == 'pause':
            config.status = 'paused'
            save_loop(ctx.state_dir, config)
            return ToolResult(content=f'Judge loop `{loop_id}` paused at iteration {config.current_iteration}.')

        elif action == 'resume':
            config.status = 'running'
            save_loop(ctx.state_dir, config)
            return ToolResult(content=f'Judge loop `{loop_id}` resumed.')

        elif action == 'stop':
            config.status = 'completed'
            config.convergence.converged = False
            config.convergence.reason = 'user_stopped'
            config.convergence.final_score = config.best_score
            config.convergence.best_score = config.best_score
            config.convergence.iterations_used = config.current_iteration
            config.completed_at = config.updated_at
            save_loop(ctx.state_dir, config)
            return ToolResult(
                content=(
                    f'Judge loop `{loop_id}` stopped.\n'
                    f'Best score: {config.best_score} (iteration {config.best_iteration})\n'
                    f'Iterations completed: {config.current_iteration}'
                ),
            )

    except Exception as e:
        return ToolResult(content=f'Error: {e}', is_error=True)

    return ToolResult(content=f'Unknown lifecycle action: {action}', is_error=True)


def _handle_list(ctx: ToolContext) -> ToolResult:
    """List all judge loops."""
    if not ctx.state_dir:
        return ToolResult(content='Error: state_dir not available', is_error=True)

    try:
        from judge_engine import list_loops

        loops = list_loops(ctx.state_dir)
        if not loops:
            return ToolResult(content='No judge loops found.')

        lines = [f'Found {len(loops)} judge loop(s):\n']
        for loop in loops:
            lid = loop.get('id', '?')
            goal = loop.get('goal', '?')[:80]
            status = loop.get('status', '?')
            judge_type = loop.get('judge_type', '?')
            current = loop.get('current_iteration', 0)
            max_iter = loop.get('max_iterations', '?')
            best = loop.get('best_score', '—')
            lines.append(f'- `{lid}` ({status}) — {goal}')
            lines.append(f'  Judge: {judge_type}, Iterations: {current}/{max_iter}, Best: {best}')

        return ToolResult(content='\n'.join(lines))
    except Exception as e:
        return ToolResult(content=f'Error listing loops: {e}', is_error=True)
