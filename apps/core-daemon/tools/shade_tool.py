"""SpawnShade tool — lets Charon delegate tasks to ephemeral shade agents.

A shade is a lightweight worker agent that:
1. Gets a specific goal and constraints
2. Runs independently (in a background thread with its own ConversationEngine)
3. Reports results back via the shade contract system
4. Self-terminates when done

Usage by Charon:
    SpawnShade(goal="Write unit tests for store.py", scope=["tests/", "libs/store.py"])
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from typing import Any

from tools import ToolResult, ToolContext

SHADE_TOOL_DEF = {
    'name': 'SpawnShade',
    'description': (
        'Spawn an ephemeral shade agent to work on a specific task independently. '
        'The shade runs in the background and reports results when done. '
        'Use this to delegate well-defined subtasks like: writing tests, '
        'fixing a specific bug, researching a codebase, or generating documentation. '
        'You will receive a contract_id to track progress.'
    ),
    'input_schema': {
        'type': 'object',
        'properties': {
            'goal': {
                'type': 'string',
                'description': 'Clear description of what the shade should accomplish',
            },
            'scope': {
                'type': 'array',
                'items': {'type': 'string'},
                'description': 'File paths or directories the shade should focus on (optional)',
            },
            'constraints': {
                'type': 'array',
                'items': {'type': 'string'},
                'description': 'Rules or limitations for the shade (optional)',
            },
        },
        'required': ['goal'],
    },
}


def execute_spawn_shade(params: dict, ctx: ToolContext) -> ToolResult:
    """Spawn a shade agent to work on a task."""
    goal = params.get('goal', '').strip()
    if not goal:
        return ToolResult(content='Error: goal is required', is_error=True)

    scope = params.get('scope', [])
    constraints = params.get('constraints', [])
    state_dir = ctx.state_dir or Path('.charon_state')

    # 1. Create shade agent
    try:
        sys.path.insert(0, str(Path(__file__).parent.parent))
        from agent_lifecycle import create_agent
        shade_agent = create_agent(
            name=None,
            mode='temp',
            goal=goal,
            project=str(ctx.project_root),
            role='shade',
            visibility='background',
            require_tmux=False,
        )
    except Exception as e:
        return ToolResult(content=f'Error creating shade agent: {e}', is_error=True)

    shade_id = shade_agent['id']

    # 2. Create contract
    try:
        from shade_orchestrator import create_contract
        contract = create_contract(
            state_dir,
            parent_task_id='',
            parent_agent_id=ctx.agent_id,
            shade_agent_id=shade_id,
            conversation_id=f'shade-conv-{shade_id}',
            project=str(ctx.project_root),
            goal=goal,
            constraints=constraints,
            scope=scope,
        )
        contract_id = contract['id']
    except Exception as e:
        return ToolResult(content=f'Shade agent created ({shade_id}) but contract failed: {e}', is_error=True)

    # 3. Launch shade in background thread
    thread = threading.Thread(
        target=_run_shade,
        args=(state_dir, shade_id, contract_id, goal, scope, constraints, ctx),
        daemon=True,
    )
    thread.start()

    return ToolResult(
        content=(
            f'Shade spawned successfully.\n'
            f'  Agent: {shade_id} ({shade_agent["name"]})\n'
            f'  Contract: {contract_id}\n'
            f'  Goal: {goal}\n'
            f'  Status: running in background\n\n'
            f'Use Bash to check progress: '
            f'python3 -c "from shade_orchestrator import get_contract; '
            f'import json; print(json.dumps(get_contract(Path(\'.charon_state\'), \'{contract_id}\'), indent=2))"'
        ),
        details={
            'shade_id': shade_id,
            'contract_id': contract_id,
            'status': 'running',
        },
    )


def _run_shade(
    state_dir: Path,
    shade_id: str,
    contract_id: str,
    goal: str,
    scope: list[str],
    constraints: list[str],
    parent_ctx: ToolContext,
):
    """Run a shade agent in a background thread."""
    import asyncio

    try:
        from conversation_engine import ConversationEngine
        from model_registry import get_shade_provider_and_model
        from shade_orchestrator import (
            get_contract, mark_phase_completed, mark_phase_failed,
            next_pending_phase, build_phase_instruction,
        )

        # Create provider using shade-specific config (falls back to main if not set)
        provider, model, _ = get_shade_provider_and_model(state_dir)

        # Build shade system prompt
        scope_str = ', '.join(scope) if scope else 'entire project'
        constraint_str = '\n'.join(f'- {c}' for c in constraints) if constraints else 'None'
        system_prompt = (
            f'You are a Shade — an ephemeral worker agent spawned by Charon to complete a specific task.\n\n'
            f'YOUR GOAL: {goal}\n\n'
            f'SCOPE: {scope_str}\n'
            f'CONSTRAINTS:\n{constraint_str}\n\n'
            f'RULES:\n'
            f'- Focus exclusively on the goal. Do not deviate.\n'
            f'- Be efficient. Use tools to accomplish the task.\n'
            f'- When done, output a clear summary of what you accomplished.\n'
            f'- If you encounter an error you cannot resolve, explain what went wrong.\n'
        )

        engine = ConversationEngine(
            provider=provider,
            model=model,
            project_root=parent_ctx.project_root,
            agent_name=f'shade-{shade_id}',
            system_prompt=system_prompt,
            state_dir=state_dir,
            max_tokens=16384,
        )

        # Process each phase
        contract = get_contract(state_dir, contract_id)
        if not contract:
            return

        while True:
            phase = next_pending_phase(contract)
            if not phase:
                break

            instruction = build_phase_instruction(contract, phase)
            phase_id = phase['phase_id']

            try:
                response, events = asyncio.run(
                    engine.submit_and_collect(instruction)
                )
                mark_phase_completed(
                    state_dir, contract_id, phase_id,
                    task_id=shade_id,
                    summary=response[:500] if response else 'Completed (no output)',
                )
            except Exception as e:
                mark_phase_failed(
                    state_dir, contract_id, phase_id,
                    task_id=shade_id,
                    error=str(e),
                )
                break

            # Reload contract for next phase
            contract = get_contract(state_dir, contract_id)
            if not contract or contract.get('status') != 'running':
                break

        # Mark agent as stopped
        try:
            from agent_lifecycle import stop_agent
            stop_agent(shade_id)
        except Exception:
            pass

    except Exception as e:
        # Log error
        try:
            err_path = state_dir / 'agents' / shade_id / 'error.log'
            err_path.parent.mkdir(parents=True, exist_ok=True)
            err_path.write_text(f'{time.strftime("%Y-%m-%d %H:%M:%S")} Shade error: {e}\n')
        except Exception:
            pass
