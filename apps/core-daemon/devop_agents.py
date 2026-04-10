from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from typing import Any


def _role_prompt(role: str, operation_id: str, workstream_slug: str = '', user_goal: str = '') -> str:
    base = [
        'You are part of Charon\'s autonomous software development system.',
        'Work through explicit checkpoints, preserve evidence, and make your progress inspectable.',
        'Use concise, durable outputs that can be reviewed by a judge or verifier.',
    ]
    if role == 'implementer':
        base.extend([
            'ROLE: Implementer',
            'You own one workstream and should produce checkpointed implementation progress.',
            'Prefer bounded progress with evidence over sprawling unchecked changes.',
            'When appropriate, use shades for narrow subtasks and report clear summaries.',
        ])
    elif role == 'judge':
        base.extend([
            'ROLE: Judge',
            'You evaluate workstream checkpoints against user goals, quality, evidence, and readiness.',
            'Your critiques should be actionable, bounded, and suitable for another revision pass.',
        ])
    elif role == 'verifier':
        base.extend([
            'ROLE: Integration Verifier',
            'You validate cross-workstream integration, end-to-end readiness, and release confidence.',
            'You should look for integration breakage, missing checks, and risky assumptions.',
        ])
    else:
        base.extend([
            'ROLE: Development Coordinator',
            'You decompose broad app-building prompts into workstreams, assign workers, and choose best checkpoints.',
            'You should think in terms of dependencies, priorities, and judged progress.',
        ])
    if operation_id:
        base.append(f'Operation ID: {operation_id}')
    if workstream_slug:
        base.append(f'Workstream slug: {workstream_slug}')
    if user_goal:
        base.append(f'User software goal: {user_goal}')
    return '\n'.join(base)


def _role_instruction(role: str, operation_id: str, workstream_slug: str = '', user_goal: str = '') -> str:
    if role == 'implementer':
        return (
            f'Work workstream `{workstream_slug}` in operation `{operation_id}`.\n\n'
            f'Inspect current workstream state first. Then make one disciplined implementation pass and produce a concise checkpoint summary, '
            f'including changed files, tests/build evidence, and remaining risks. Prefer concrete progress over speculative planning.'
        )
    if role == 'judge':
        return (
            f'Judge workstream `{workstream_slug}` in operation `{operation_id}`.\n\n'
            f'Read the latest checkpoint for the workstream. Evaluate requirement fit, test adequacy, code quality, integration readiness, and user fit. '
            f'Produce a concise verdict and actionable critique.'
        )
    if role == 'verifier':
        return (
            f'Verify integrated outputs for workstream `{workstream_slug}` in operation `{operation_id}`.\n\n'
            f'Focus on build/test/integration confidence and highlight concrete blockers or acceptance evidence.'
        )
    return (
        f'Coordinate software operation `{operation_id}` for the goal: {user_goal}.\n\n'
        f'Produce a small set of promising workstreams, considering dependencies, user value, and implementation order. '
        f'Prefer an actionable breakdown over broad discussion.'
    )


def infer_candidate_workstreams(prompt: str, limit: int = 5) -> list[dict[str, Any]]:
    text = (prompt or '').strip()
    lower = text.lower()
    items: list[dict[str, Any]] = []

    def add(title: str, summary: str, priority: float = 0.7, deps: list[str] | None = None) -> None:
        items.append({
            'workstream_id': '',
            'title': title,
            'slug': title.lower().replace(' ', '-'),
            'summary': summary,
            'priority': priority,
            'recommended_action': 'execute_now',
            'dependency_ids': list(deps or []),
        })

    if 'web app' in lower or 'app' in lower or 'frontend' in lower or 'backend' in lower:
        add('Frontend UI', 'User-facing interface, flows, forms, and visual shell.', 0.88)
        add('Backend API', 'Server-side endpoints, business logic, and service boundaries.', 0.90)
        add('Data/Auth Layer', 'Persistence, schema, auth/session model, and access control.', 0.83)
        add('Testing & Integration', 'Integration tests, end-to-end checks, and quality gates.', 0.86, deps=['frontend-ui', 'backend-api'])
        add('Deployment & Environment', 'Runtime config, deployment path, and operational setup.', 0.62, deps=['backend-api'])
    else:
        add('Core Implementation', 'Primary implementation lane derived from the request.', 0.80)
        add('Verification & Testing', 'Validation and regression checks for delivered work.', 0.78, deps=['core-implementation'])
        add('Integration & Packaging', 'Assembly, polish, and delivery readiness.', 0.68, deps=['core-implementation'])

    out = []
    seen = set()
    for item in items:
        slug = item['slug']
        if slug in seen:
            continue
        seen.add(slug)
        out.append(item)
    return out[:max(1, limit)]


def _collect_usage(events: list[Any]) -> tuple[int, int]:
    inp = 0
    out = 0
    for event in events:
        try:
            if getattr(event, 'type', '') == 'message_end':
                usage = getattr(event, 'data', {}) or {}
                usage = usage.get('usage', usage)
                inp += int(usage.get('input_tokens', 0) or 0)
                out += int(usage.get('output_tokens', 0) or 0)
        except Exception:
            continue
    return inp, out


def create_devop_engine(
    state_dir: Path,
    project_root: Path,
    *,
    agent: dict[str, Any],
    role: str,
    operation_id: str,
    workstream_slug: str = '',
    user_goal: str = '',
):
    from conversation_engine import ConversationEngine
    from model_registry import get_shade_provider_and_model
    from system_prompt_builder import build_system_prompt
    from worker_provider import request_worker_provider_for_background_flow

    provider_status = request_worker_provider_for_background_flow(
        state_dir,
        purpose='software engineering worker tasks',
        agent_id=agent.get('id', ''),
        project_root=project_root,
    )
    if not provider_status.get('ok'):
        raise RuntimeError(f"worker provider unavailable: {provider_status.get('reason') or 'no_provider'}")

    complexity = 'complex' if role in ('coordinator', 'judge', 'verifier') else 'normal'
    provider, model, _ = get_shade_provider_and_model(state_dir, phase_name='implementation', task_complexity=complexity)

    agent_doc = {
        'id': agent.get('id', ''),
        'name': agent.get('name', ''),
        'role': role,
        'goal': agent.get('goal', ''),
        'project': str(project_root),
        'parent_agent_id': agent.get('parent_agent_id', ''),
    }
    task_doc = {'project': str(project_root)}
    base_prompt = build_system_prompt(state_dir=state_dir, agent=agent_doc, task=task_doc)
    system_prompt = base_prompt + '\n\n' + _role_prompt(role, operation_id, workstream_slug, user_goal)

    engine = ConversationEngine(
        provider=provider,
        model=model,
        project_root=project_root,
        agent_id=agent.get('id', ''),
        agent_name=agent.get('name', ''),
        system_prompt=system_prompt,
        state_dir=state_dir,
        operation_id=operation_id,
        operation_domain='software_dev',
        work_unit_id=workstream_slug,
        operation_role=role,
        runtime_role='background_agent',
        parent_agent_id=agent.get('parent_agent_id', ''),
        max_tokens=16384,
    )
    return engine, model


def spawn_devop_role(
    state_dir: Path,
    project_root: Path,
    *,
    role: str,
    operation_id: str,
    workstream_slug: str = '',
    user_goal: str = '',
    parent_agent_id: str = '',
) -> dict[str, Any]:
    from agent_lifecycle import create_agent

    goal = user_goal or f'DevOp {role} for {workstream_slug or operation_id}'
    agent = create_agent(
        name=None,
        mode='temp',
        goal=goal,
        project=str(project_root),
        role=role,
        visibility='background',
        parent_agent_id=parent_agent_id or None,
        require_tmux=False,
    )

    thread = threading.Thread(
        target=_run_devop_role,
        args=(state_dir, project_root, agent, role, operation_id, workstream_slug, user_goal),
        daemon=True,
    )
    thread.start()
    return agent


def start_autonomous_software_operation(
    state_dir: Path,
    project_root: Path,
    *,
    prompt: str,
    parent_agent_id: str = '',
    budget: dict[str, Any] | None = None,
    policy: dict[str, Any] | None = None,
    max_workstreams_default: int = 4,
) -> dict[str, Any]:
    from devop_runtime import init_operation, append_operation_event

    op = init_operation(
        state_dir,
        project_root,
        prompt=prompt,
        title=prompt[:120],
        budget=budget,
        policy=policy,
    )
    coordinator = spawn_devop_role(
        state_dir,
        project_root,
        role='coordinator',
        operation_id=op['operation_id'],
        user_goal=prompt,
        parent_agent_id=parent_agent_id,
    )
    try:
        from devop_runtime import operation_dir
        op_path = operation_dir(state_dir, op['operation_id']) / 'operation.json'
        doc = json.loads(op_path.read_text())
        doc['coordinator_agent_id'] = coordinator.get('id', '')
        op_path.write_text(json.dumps(doc, indent=2))
    except Exception:
        pass
    append_operation_event(
        state_dir,
        op['operation_id'],
        'autonomous_software_started',
        from_agent_id=coordinator.get('id', ''),
        summary=prompt[:240],
        payload={'parent_agent_id': parent_agent_id, 'max_workstreams_default': max_workstreams_default},
    )
    thread = threading.Thread(
        target=_run_operation_controller,
        args=(state_dir, project_root, op['operation_id'], prompt, coordinator, max_workstreams_default),
        daemon=True,
    )
    thread.start()
    return {'operation': op, 'coordinator': coordinator}


def _run_operation_controller(
    state_dir: Path,
    project_root: Path,
    operation_id: str,
    prompt: str,
    coordinator: dict[str, Any],
    max_workstreams_default: int,
) -> None:
    try:
        from devop_runtime import (
            get_operation_state,
            save_candidate_workstreams,
            init_workstream,
            append_operation_event,
            append_handoff,
            update_workstream_runtime,
            set_operation_status,
        )

        append_operation_event(
            state_dir,
            operation_id,
            'coordinator_phase_changed',
            from_agent_id=coordinator.get('id', ''),
            summary='Coordinator scouting workstreams',
        )
        candidates = infer_candidate_workstreams(prompt, limit=max_workstreams_default)
        save_candidate_workstreams(state_dir, operation_id, candidates)
        selected = [c for c in candidates if c.get('recommended_action') == 'execute_now'][:max_workstreams_default]
        if not selected:
            set_operation_status(state_dir, operation_id, 'paused', 'No candidate workstreams selected.')
            return

        for item in selected:
            ws = init_workstream(
                state_dir,
                operation_id,
                title=str(item.get('title') or ''),
                summary=str(item.get('summary') or ''),
                dependency_ids=list(item.get('dependency_ids') or []),
            )
            impl = spawn_devop_role(
                state_dir,
                project_root,
                role='implementer',
                operation_id=operation_id,
                workstream_slug=ws['slug'],
                user_goal=prompt,
                parent_agent_id=coordinator.get('id', ''),
            )
            judge = spawn_devop_role(
                state_dir,
                project_root,
                role='judge',
                operation_id=operation_id,
                workstream_slug=ws['slug'],
                user_goal=prompt,
                parent_agent_id=coordinator.get('id', ''),
            )
            update_workstream_runtime(
                state_dir,
                operation_id,
                ws['slug'],
                owner_agent_id=impl.get('id', ''),
                paired_judge_agent_id=judge.get('id', ''),
                extras={'status': 'active'},
            )
            append_handoff(
                state_dir,
                operation_id,
                workstream_id=ws['workstream_id'],
                kind='assignment',
                from_agent_id=coordinator.get('id', ''),
                to_agent_id=impl.get('id', ''),
                from_role='coordinator',
                to_role='implementer',
                summary=f'Assigned workstream {ws["title"]}',
            )
        set_operation_status(state_dir, operation_id, 'running', 'Implementers and judges spawned.')
    except Exception as e:
        try:
            from devop_runtime import append_operation_event, set_operation_status
            append_operation_event(state_dir, operation_id, 'operation_controller_failed', summary=str(e), payload={'error': str(e)})
            set_operation_status(state_dir, operation_id, 'failed', str(e))
        except Exception:
            pass


def _run_devop_role(
    state_dir: Path,
    project_root: Path,
    agent: dict[str, Any],
    role: str,
    operation_id: str,
    workstream_slug: str,
    user_goal: str,
) -> None:
    try:
        from devop_runtime import append_operation_event

        engine, model = create_devop_engine(
            state_dir,
            project_root,
            agent=agent,
            role=role,
            operation_id=operation_id,
            workstream_slug=workstream_slug,
            user_goal=user_goal,
        )
        append_operation_event(
            state_dir,
            operation_id,
            f'{role}_spawned',
            workstream_id=workstream_slug,
            from_agent_id=agent.get('id', ''),
            summary=f'{role} spawned',
            payload={'agent_id': agent.get('id', ''), 'model': getattr(model, 'model_id', '')},
        )
        instruction = _role_instruction(role, operation_id, workstream_slug, user_goal)
        response, events = asyncio.run(engine.submit_and_collect(instruction))
        inp, out = _collect_usage(events)
        append_operation_event(
            state_dir,
            operation_id,
            f'{role}_completed',
            workstream_id=workstream_slug,
            from_agent_id=agent.get('id', ''),
            summary=(response or f'{role} completed')[:240],
            payload={'input_tokens': inp, 'output_tokens': out},
        )
    except Exception as e:
        try:
            from devop_runtime import append_operation_event
            append_operation_event(
                state_dir,
                operation_id,
                f'{role}_failed',
                workstream_id=workstream_slug,
                from_agent_id=agent.get('id', ''),
                summary=str(e),
                payload={'error': str(e)},
            )
        except Exception:
            pass


__all__ = [
    'infer_candidate_workstreams',
    'create_devop_engine',
    'spawn_devop_role',
    'start_autonomous_software_operation',
]
