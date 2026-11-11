from __future__ import annotations

import asyncio
import json
import re
import threading
from pathlib import Path
from typing import Any

try:
    from charon.infra.diagnostics import record as _diag
except Exception:  # diagnostics is best-effort and must never block import
    def _diag(*args, **kwargs):
        return None


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


# Workstream lane sets: (title, summary, priority, dependency slugs).
# Data, not code paths, so a platform is a new entry rather than a branch.
LANE_SETS: dict[str, list[tuple[str, str, float, list[str]]]] = {
    'ios': [
        ('App Logic and Services', 'Models, services and view models, with unit tests. Keep their public API stable for the interface lane.', 0.92, []),
        ('Native Interface', 'SwiftUI views, navigation and assets, built on the logic lane\'s public API.', 0.88, []),
        ('Testing and Integration', 'Build the combined app, run unit and UI tests, check the simulator UI, and record what only a physical device can confirm.', 0.86, ['app-logic-and-services', 'native-interface']),
    ],
    'web': [
        ('Frontend UI', 'User-facing interface, flows, forms, and visual shell.', 0.88, []),
        ('Backend API', 'Server-side endpoints, business logic, and service boundaries.', 0.90, []),
        ('Data/Auth Layer', 'Persistence, schema, auth/session model, and access control.', 0.83, []),
        ('Testing & Integration', 'Integration tests, end-to-end checks, and quality gates.', 0.86, ['frontend-ui', 'backend-api']),
        ('Deployment & Environment', 'Runtime config, deployment path, and operational setup.', 0.62, ['backend-api']),
    ],
    'generic': [
        ('Core Implementation', 'Primary implementation lane derived from the request.', 0.80, []),
        ('Verification & Testing', 'Validation and regression checks for delivered work.', 0.78, ['core-implementation']),
        ('Integration & Packaging', 'Assembly, polish, and delivery readiness.', 0.68, ['core-implementation']),
    ],
}

# Files at the project root (or one level down) that identify its platform.
_IOS_MARKERS = ('*.xcodeproj', '*.xcworkspace')
_OTHER_PLATFORM_MARKERS = (
    'go.mod', 'Cargo.toml', 'pyproject.toml', 'setup.py', 'requirements.txt', 'package.json',
    'pom.xml', 'build.gradle', 'build.gradle.kts', 'Gemfile', 'composer.json', 'mix.exs',
    'CMakeLists.txt', 'Makefile', '*.csproj', '*.sln',
)
# Whole words only: substring tests matched "scenarios"/"kiosk" as iOS and
# "approach"/"apply" as a web app.
_IOS_WORDS = re.compile(r'\b(ios|swiftui|iphone|ipad|xcode)\b', re.I)
# A web lane set includes a frontend lane, so "backend" or "API" alone (a Go
# or Rust service) does not select it.
_WEB_WORDS = re.compile(
    r'\b(web ?apps?|website|frontend|front-end|react|vue|svelte|next\.js)\b', re.I,
)


def _project_platforms(project_root: Path | None) -> tuple[bool, bool]:
    """(has iOS markers, has any other platform's markers) for the project."""
    if project_root is None:
        return False, False
    root = Path(project_root)
    if not root.is_dir():
        return False, False

    def found(patterns: tuple[str, ...]) -> bool:
        for pattern in patterns:
            if any(root.glob(pattern)) or any(root.glob(f'*/{pattern}')):
                return True
        return False

    try:
        return found(_IOS_MARKERS), found(_OTHER_PLATFORM_MARKERS)
    except OSError:
        return False, False


def _lane_set_for(prompt: str, project_root: Path | None) -> str:
    """Choose the lane set from the project first, the prompt second.

    iOS lanes need an iOS project (an .xcodeproj or .xcworkspace), or an
    explicit iOS request in a project with no platform markers at all (a new,
    empty project). A repo of another platform never gets iOS lanes, whatever
    the prompt says.
    """
    has_ios, has_other = _project_platforms(project_root)
    if has_ios:
        return 'ios'
    if not has_other and _IOS_WORDS.search(prompt or ''):
        return 'ios'
    if _WEB_WORDS.search(prompt or ''):
        return 'web'
    return 'generic'


def infer_candidate_workstreams(
    prompt: str, limit: int = 5, project_root: Path | None = None,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for title, summary, priority, deps in LANE_SETS[_lane_set_for(prompt, project_root)]:
        items.append({
            'workstream_id': '',
            'title': title,
            'slug': title.lower().replace(' ', '-'),
            'summary': summary,
            'priority': priority,
            'recommended_action': 'execute_now',
            'dependency_ids': list(deps),
        })

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
    from charon.conversation.conversation_engine import ConversationEngine
    from charon.providers.model_registry import get_shade_provider_and_model
    from charon.context.system_prompt_builder import build_system_prompt
    from charon.providers.worker_provider import request_worker_provider_for_background_flow

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
        # A background process with nobody to answer: the interactive flag is
        # the control for that. It withholds Clarify (from the tools and from
        # ToolCatalog), rejects direct Clarify calls, adds the non-interactive
        # guidance and disables browser-visibility prompts.
        interactive=False,
        # Passes are bounded by the operation controller and the idle and
        # no-progress guards, so no per-pass wall-clock cap, as before.
        # CHARON_TASK_TIME_BUDGET_SECONDS still overrides this.
        time_budget_seconds=0,
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
    from charon.agents.agent_lifecycle import create_agent

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

    # Register the actor before starting it: fast failures must not race a later
    # controller write that sets the lane back to active/reviewing.
    from charon.devop.devop_runtime import update_workstream_runtime, operation_dir
    if workstream_slug:
        fields = {'owner_agent_id': agent['id']} if role == 'implementer' else {'paired_judge_agent_id': agent['id']}
        update_workstream_runtime(state_dir, operation_id, workstream_slug,
                                 status='active' if role == 'implementer' else 'reviewing', **fields)
    elif role == 'coordinator':
        path = operation_dir(state_dir, operation_id) / 'operation.json'
        doc = json.loads(path.read_text())
        doc['coordinator_agent_id'] = agent['id']
        path.write_text(json.dumps(doc, indent=2))
    thread = threading.Thread(
        target=_run_devop_role,
        args=(state_dir, project_root, agent, role, operation_id, workstream_slug, user_goal),
        daemon=True,
    )
    thread.start()
    # Internal handle lets the controller sequence review after implementation.
    agent['_thread'] = thread
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
    # All entry points share the managed-process launcher, including /devop.
    from charon.tools.software_team_tool import launch_software_team
    return launch_software_team(state_dir, project_root, prompt=prompt,
                                parent_agent_id=parent_agent_id, budget=budget,
                                policy=policy, max_workstreams=max_workstreams_default)


def _stop_requested(state_dir: Path, operation_id: str) -> bool:
    from charon.devop.devop_runtime import get_operation_state, set_operation_status
    if get_operation_state(state_dir, operation_id).get('stop_requested'):
        set_operation_status(state_dir, operation_id, 'stopped', 'Stopped between worker passes; artifacts retained.')
        return True
    return False


def _run_operation_controller(
    state_dir: Path,
    project_root: Path,
    operation_id: str,
    prompt: str,
    coordinator: dict[str, Any],
    max_workstreams_default: int,
) -> None:
    try:
        from charon.devop.devop_runtime import (
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
        candidates = infer_candidate_workstreams(prompt, limit=max_workstreams_default, project_root=project_root)
        save_candidate_workstreams(state_dir, operation_id, candidates)
        selected = [c for c in candidates if c.get('recommended_action') == 'execute_now'][:max_workstreams_default]
        if not selected:
            set_operation_status(state_dir, operation_id, 'paused', 'No candidate workstreams selected.')
            return

        set_operation_status(state_dir, operation_id, 'running', 'Executing selected workstreams with review gates.')
        lanes = []
        for item in selected:
            ws = init_workstream(
                state_dir,
                operation_id,
                title=str(item.get('title') or ''),
                summary=str(item.get('summary') or ''),
                dependency_ids=list(item.get('dependency_ids') or []),
            )
            update_workstream_runtime(state_dir, operation_id, ws['slug'], status='pending')
            lanes.append(ws)
        for ws in lanes:
            if _stop_requested(state_dir, operation_id):
                return
            impl = spawn_devop_role(
                state_dir,
                project_root,
                role='implementer',
                operation_id=operation_id,
                workstream_slug=ws['slug'],
                user_goal=prompt,
                parent_agent_id=coordinator.get('id', ''),
            )
            impl['_thread'].join()
            if _stop_requested(state_dir, operation_id):
                return
            from charon.devop.devop_runtime import get_workstream_state
            lane_status = get_workstream_state(state_dir, operation_id, ws['slug']).get('status')
            if lane_status == 'failed':
                set_operation_status(state_dir, operation_id, 'failed', f'Implementation failed: {ws["slug"]}')
                return
            if lane_status == 'incomplete':
                set_operation_status(state_dir, operation_id, 'needs_review', f'Implementation stopped before finishing: {ws["slug"]}; artifacts retained.')
                return
            judge = spawn_devop_role(
                state_dir,
                project_root,
                role='judge',
                operation_id=operation_id,
                workstream_slug=ws['slug'],
                user_goal=prompt,
                parent_agent_id=coordinator.get('id', ''),
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
            judge['_thread'].join()
            if _stop_requested(state_dir, operation_id):
                return
            lane_status = get_workstream_state(state_dir, operation_id, ws['slug']).get('status')
            if lane_status == 'failed':
                set_operation_status(state_dir, operation_id, 'failed', f'Review failed: {ws["slug"]}')
                return
            if lane_status == 'incomplete':
                set_operation_status(state_dir, operation_id, 'needs_review', f'Review stopped before a verdict: {ws["slug"]}; artifacts retained.')
                return
            reviewed = get_workstream_state(state_dir, operation_id, ws['slug'])
            if (reviewed.get('latest_review') or {}).get('decision') != 'approve':
                update_workstream_runtime(state_dir, operation_id, ws['slug'], status='needs_revision')
                set_operation_status(state_dir, operation_id, 'needs_review', f'Review requires changes: {ws["slug"]}')
                return
            update_workstream_runtime(state_dir, operation_id, ws['slug'], status='reviewed')
        set_operation_status(state_dir, operation_id, 'needs_review', 'Worker passes finished; acceptance and integration evidence require review.')
    except Exception as e:
        try:
            from charon.devop.devop_runtime import append_operation_event, set_operation_status
            append_operation_event(state_dir, operation_id, 'operation_controller_failed', summary=str(e), payload={'error': str(e)})
            set_operation_status(state_dir, operation_id, 'failed', str(e))
        except Exception as exc:
            _diag('devop_agents', 'failed to record operation-controller failure; operation failed silently', error=exc, operation=operation_id)


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
        from charon.devop.devop_runtime import append_operation_event

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
        if workstream_slug:
            from charon.devop.devop_runtime import get_workstream_state
            instruction += '\n\nCurrent workstream (including checkpoint evidence):\n' + json.dumps(get_workstream_state(state_dir, operation_id, workstream_slug), default=str)
            if role == 'judge':
                from charon.devop.devop_runtime import workstream_dir
                report = workstream_dir(state_dir, operation_id, workstream_slug) / 'implementer-report.md'
                instruction += '\n\nImplementation report:\n' + report.read_text(encoding='utf-8')
                instruction += '\nRead-only review: do not edit implementation. End with exactly VERDICT: approve or VERDICT: revise. Approve only with concrete evidence for this lane, not claims of unrun device validation.'
        else:
            instruction += '\nPlanning only: do not edit source files or spawn workers. The controller owns assignments.'
        response, events = asyncio.run(engine.submit_and_collect(instruction))
        # A guard stop (no-progress, or an explicit time budget) ends this pass
        # incomplete. Record it and keep the artifacts; one wedged pass must not
        # mark the whole operation failed. The controller decides what follows.
        if any(getattr(e, 'type', '') == 'budget_stop' for e in events) or (
                'Stopped:' in (response or '') and 'Task is incomplete;' in (response or '')):
            from charon.devop.devop_runtime import append_operation_event, update_workstream_runtime, workstream_dir
            if workstream_slug:
                (workstream_dir(state_dir, operation_id, workstream_slug) / f'{role}-report.md').write_text(response or '', encoding='utf-8')
                update_workstream_runtime(state_dir, operation_id, workstream_slug, status='incomplete',
                                          extras={'last_error': (response or '').strip()[-500:]})
            append_operation_event(state_dir, operation_id, f'{role}_incomplete', workstream_id=workstream_slug,
                                   from_agent_id=agent.get('id', ''), summary=(response or f'{role} stopped')[-240:])
            return
        failures = [e for e in events if getattr(e, 'type', '') == 'error' or (getattr(e, 'type', '') == 'done' and (getattr(e, 'data', {}) or {}).get('stop_reason') in ('error', 'aborted'))]
        if failures or not (response or '').strip():
            raise RuntimeError(f'{role} interrupted or returned no output: ' + str([getattr(e, 'data', {}) for e in failures]))
        if workstream_slug:
            from charon.devop.devop_runtime import workstream_dir
            (workstream_dir(state_dir, operation_id, workstream_slug) / f'{role}-report.md').write_text(response or '', encoding='utf-8')
            from charon.devop.devop_runtime import save_checkpoint, save_review, get_workstream_state
            if role == 'implementer':
                save_checkpoint(state_dir, operation_id, workstream_slug, producer_agent_id=agent['id'], markdown=response, summary=response[:240])
            elif role == 'judge':
                cp = get_workstream_state(state_dir, operation_id, workstream_slug).get('latest_checkpoint') or {}
                verdict = 'approve' if response.strip().splitlines()[-1].strip() == 'VERDICT: approve' else 'revise'
                save_review(state_dir, operation_id, workstream_slug, checkpoint_id=cp.get('checkpoint_id', ''), reviewer_agent_id=agent['id'], review_type='implementation', decision=verdict, critique_markdown=response, summary=response[:240])
        else:
            from charon.devop.devop_runtime import operation_dir
            (operation_dir(state_dir, operation_id) / f'{role}-report.md').write_text(response, encoding='utf-8')
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
            from charon.devop.devop_runtime import append_operation_event, update_workstream_runtime, set_operation_status
            if workstream_slug:
                update_workstream_runtime(state_dir, operation_id, workstream_slug, status='failed', extras={'last_error': str(e)})
            set_operation_status(state_dir, operation_id, 'failed', str(e))
            append_operation_event(
                state_dir,
                operation_id,
                f'{role}_failed',
                workstream_id=workstream_slug,
                from_agent_id=agent.get('id', ''),
                summary=str(e),
                payload={'error': str(e)},
            )
        except Exception as exc:
            _diag('devop_agents', 'failed to record devop role failure; role failed silently', error=exc, operation=operation_id)


__all__ = [
    'infer_candidate_workstreams',
    'create_devop_engine',
    'spawn_devop_role',
    'start_autonomous_software_operation',
]
