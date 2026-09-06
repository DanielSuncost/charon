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
import threading
import time
from pathlib import Path

from charon.tools import ToolResult, ToolContext

try:
    from charon.infra.diagnostics import record as _diag
except Exception:  # diagnostics is best-effort and must never block import
    def _diag(*args, **kwargs):
        return None

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
            'expected_outputs': {
                'type': 'array',
                'items': {'type': 'string'},
                'description': 'Expected outputs for the shade contract (optional)',
            },
            'phase_specs': {
                'type': 'array',
                'items': {'type': 'object'},
                'description': 'Optional explicit phase plan: list of {name, objective} records.',
            },
            'contract_type': {
                'type': 'string',
                'description': 'Optional contract type label, e.g. libris_source_procurement.',
            },
            'metadata': {
                'type': 'object',
                'description': 'Optional contract metadata for specialized workflows.',
            },
            'topology_preset': {
                'type': 'string',
                'enum': ['narrow', 'standard', 'wide'],
                'description': (
                    'Governs how deep and wide the delegation tree rooted at this call may grow '
                    '(default: standard). Only takes effect when this call starts a new tree — a '
                    'shade spawned by another shade inherits its tree\'s existing budget instead.'
                ),
            },
            'retain': {
                'type': 'boolean',
                'description': (
                    'If true, the shade goes idle instead of stopping once its contract finishes, '
                    'and stays addressable — call it again via charon.rlm(child_agent_id=...) in '
                    'PyKernel with a follow-up instruction, and it resumes with its prior context. '
                    'Default: false (self-terminates as usual).'
                ),
            },
            'task_complexity': {
                'type': 'string',
                'enum': ['simple', 'normal', 'complex'],
                'description': (
                    'Determines which model tier the shade gets — complex=strong model, '
                    'else=fast/cheap model (default: normal). Downgraded automatically, '
                    'regardless of what is requested here, once this tree\'s token_budget '
                    'is mostly spent — see topology_budget.token_budget_utilization.'
                ),
            },
            'token_budget': {
                'type': 'number',
                'description': (
                    'Only takes effect when this call starts a new tree (see topology_preset). '
                    'Total tokens this whole delegation tree may use across every call, including '
                    'reactivations. Default: unlimited.'
                ),
            },
            'cost_budget_usd': {
                'type': 'number',
                'description': (
                    'Only takes effect when this call starts a new tree. Total real dollar cost '
                    '(estimated from actual token usage and per-model pricing) this whole '
                    'delegation tree may spend across every call. Default: unlimited.'
                ),
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
    expected_outputs = params.get('expected_outputs', [])
    phase_specs = params.get('phase_specs', [])
    contract_type = str(params.get('contract_type', '')).strip()
    metadata = params.get('metadata') or {}
    retain = bool(params.get('retain', False))
    task_complexity = str(params.get('task_complexity') or 'normal').strip().lower()
    token_budget_override = params.get('token_budget')
    cost_budget_override = params.get('cost_budget_usd')
    state_dir = ctx.state_dir or Path('.charon_state')

    try:
        from charon.providers.worker_provider import ensure_worker_provider_or_request_clarification
        provider_status = ensure_worker_provider_or_request_clarification(state_dir, ctx=ctx, purpose='shades')
        if not provider_status.get('ok'):
            choices = provider_status.get('available_providers') or []
            clarification = provider_status.get('clarification') or {}
            cid = clarification.get('clarification_id') or ''
            question = provider_status.get('question') or 'No usable provider is configured for shades.'
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
        _diag('shade_tool', 'worker-provider preflight check failed; shade spawn proceeds without provider validation', error=exc)

    from charon.agents.topology_budget import effective_budget, try_reserve
    topology_preset = str(params.get('topology_preset') or 'standard').strip().lower()
    budget = effective_budget(
        ctx, preset=topology_preset,
        token_budget=int(token_budget_override) if token_budget_override is not None else None,
        cost_budget_usd=float(cost_budget_override) if cost_budget_override is not None else None,
    )
    depth = int(getattr(ctx, 'topology_depth', 0) or 0) + 1
    ok, reason = try_reserve(state_dir, budget, parent_agent_id=ctx.agent_id or 'root', depth=depth)
    if not ok:
        return ToolResult(
            content=f'Error: cannot spawn shade — {reason}. Complete more of this work directly instead of delegating further.',
            is_error=True,
        )

    # 1. Create shade agent
    try:
        from charon.agents.agent_lifecycle import create_agent
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

    if retain:
        try:
            from charon.agents.shade_lifecycle import initialize as init_shade_lifecycle
            init_shade_lifecycle(state_dir, shade_id)
        except Exception as e:
            return ToolResult(content=f'Shade agent created ({shade_id}) but retained lifecycle init failed: {e}', is_error=True)

    # 2. Create contract
    try:
        from charon.shade.shade_orchestrator import create_contract
        contract = create_contract(
            state_dir,
            parent_task_id='',
            parent_agent_id=ctx.agent_id,
            shade_agent_id=shade_id,
            conversation_id=f'shade-conv-{shade_id}',
            project=str(ctx.project_root),
            goal=goal,
            constraints=constraints,
            expected_outputs=expected_outputs,
            scope=scope,
            phase_specs=phase_specs,
            contract_type=contract_type,
            metadata={**metadata, 'topology_depth': depth, 'topology_budget': budget},
        )
        contract_id = contract['id']
    except Exception as e:
        return ToolResult(content=f'Shade agent created ({shade_id}) but contract failed: {e}', is_error=True)

    # 3. Launch shade in background thread
    thread = threading.Thread(
        target=_run_shade,
        args=(state_dir, shade_id, contract_id, goal, scope, constraints, ctx, depth, budget, retain, False, task_complexity),
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
            'contract_type': contract_type,
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
    depth: int = 0,
    budget: dict | None = None,
    retain: bool = False,
    resume: bool = False,
    task_complexity: str = 'normal',
):
    """Run a shade agent in a background thread."""
    import asyncio

    try:
        from charon.conversation.conversation_engine import ConversationEngine
        from charon.providers.model_registry import get_shade_provider_and_model
        from charon.shade.shade_orchestrator import (
            get_contract, mark_phase_completed, mark_phase_failed,
            next_pending_phase, build_phase_instruction,
            assess_contract_outcome, save_triage_record,
        )

        # Budget-aware, multi-objective tier choice: rank fast vs strong via
        # the real router (routing/policy.py) using real per-model pricing,
        # weighted toward cost once this tree's budget is running low —
        # replaces a plain "force normal once utilization is high" downgrade
        # with an actual quality/cost tradeoff. Falls back to the requested
        # task_complexity unchanged whenever fewer than 2 real tiers are
        # configured, billing isn't metered (so cost isn't real), or routing
        # fails for any reason — see route_shade_model's own docstring.
        try:
            from charon.providers.model_registry import route_shade_model
            effective_complexity = route_shade_model(state_dir, task_complexity=task_complexity, budget=budget)
        except Exception as exc:
            effective_complexity = task_complexity
            _diag('shade_tool', 'budget-aware model routing failed; using requested tier', error=exc, contract_id=contract_id)

        # Create provider using shade-specific config (falls back to main if not set)
        provider, model, provider_meta = get_shade_provider_and_model(state_dir, task_complexity=effective_complexity)

        # Build shade system prompt
        scope_str = ', '.join(scope) if scope else 'entire project'
        constraint_str = '\n'.join(f'- {c}' for c in constraints) if constraints else 'None'
        identity = (
            'a retained worker agent spawned by Charon — you stay addressable after this '
            'task and may be called again later with a follow-up instruction'
            if retain else
            'an ephemeral worker agent spawned by Charon to complete a specific task'
        )
        refine_rule = (
            "- If you hit real friction with an external system (an API's undocumented "
            "behavior, a site's layout, an auth flow) that a future call to you is likely "
            "to hit again, consider proposing a Refine on the relevant skill with that "
            "concrete evidence — you're retained, so unlike a one-shot shade you're the one "
            "who benefits from having fixed it.\n"
            if retain else ''
        )
        system_prompt = (
            f'You are a Shade — {identity}.\n\n'
            f'YOUR GOAL: {goal}\n\n'
            f'SCOPE: {scope_str}\n'
            f'CONSTRAINTS:\n{constraint_str}\n\n'
            f'RULES:\n'
            f'- Focus exclusively on the goal. Do not deviate.\n'
            f'- Be efficient. Use tools to accomplish the task.\n'
            f'- When done, output a clear summary of what you accomplished.\n'
            f'- If you encounter an error you cannot resolve, explain what went wrong.\n'
            f'{refine_rule}'
        )

        # A retained shade's agent_id activates the lossless-context store
        # (ConversationEngine gates it on state_dir + agent_id both being
        # set) — needed so a later reactivation has a conversation to load.
        # A plain, non-retained shade keeps agent_id='' exactly as before:
        # zero behavior change to the existing fire-and-forget path.
        engine = ConversationEngine(
            provider=provider,
            model=model,
            project_root=parent_ctx.project_root,
            agent_id=shade_id if retain else '',
            agent_name=f'shade-{shade_id}',
            system_prompt=system_prompt,
            state_dir=state_dir,
            max_tokens=16384,
        )
        if resume:
            # Reactivating a retained, idle shade: reload its prior
            # conversation (the live ConversationEngine/thread from its
            # first run are long gone — this is a brand-new object) and
            # repair any tool call left unpaired by going idle mid-phase.
            engine.load_from_store()
            engine.messages = ConversationEngine._repair_orphaned_tool_calls(engine.messages)
        # Enforce the contract's file scope on Read/Write/Edit (not just advise
        # it via the prompt). Empty scope means "entire project" (no restriction).
        engine.scope = list(scope) if scope else None
        # Inherit this tree's topology budget so a shade that spawns further
        # shades of its own stays governed by the same depth/breadth/total caps.
        engine.topology_depth = depth
        engine.topology_budget = budget

        # Process each phase
        contract = get_contract(state_dir, contract_id)
        if not contract:
            return
        try:
            from charon.shade.shade_orchestrator import append_phase_event
            append_phase_event(
                state_dir,
                contract_id=contract_id,
                phase_id='-',
                event_type='shade_model_resolved',
                payload={
                    'provider': str(provider),
                    'model': str(model),
                    'provider_meta': provider_meta if isinstance(provider_meta, dict) else {},
                },
            )
        except Exception as exc:
            _diag('shade_tool', 'shade_model_resolved phase event not recorded; contract timeline missing model info', error=exc, contract_id=contract_id)

        contract['resolved_provider'] = str(provider)
        contract['resolved_model'] = str(model)
        contract['resolved_provider_meta'] = provider_meta if isinstance(provider_meta, dict) else {}
        try:
            from charon.shade.shade_orchestrator import save_contracts, load_contracts
            contracts = load_contracts(state_dir)
            for idx, rec in enumerate(contracts):
                if rec.get('id') == contract_id:
                    contracts[idx] = contract
                    save_contracts(state_dir, contracts)
                    break
        except Exception as exc:
            _diag('shade_tool', 'contract enrichment save failed; resolved provider/model not persisted on contract', error=exc, contract_id=contract_id)

        libris_meta = dict(contract.get('metadata') or {}) if isinstance(contract.get('metadata'), dict) else {}
        libris_op = str(libris_meta.get('operation_id') or '').strip()
        libris_topic = str(libris_meta.get('topic_slug') or '').strip()
        is_libris = bool(libris_op) or str(contract.get('contract_type') or '').startswith('libris_')

        def _emit_libris_phase(phase_name: str, status: str, summary: str = '') -> None:
            if not is_libris or not libris_op:
                return
            try:
                from charon.libris.libris_runtime import emit_agent_phase
                emit_agent_phase(
                    state_dir, parent_ctx.project_root, libris_op,
                    agent_id=shade_id, role='shade', phase=phase_name, status=status,
                    topic_slug=libris_topic, summary=summary,
                )
            except Exception as e:
                _diag('shade_tool', 'libris phase event emit failed; operation timeline missing shade phase update', error=e, contract_id=contract_id)

        def _emit_libris_comm(kind: str, summary: str = '') -> None:
            if not is_libris or not libris_op:
                return
            try:
                from charon.libris.libris_runtime import emit_agent_comm
                emit_agent_comm(
                    state_dir, parent_ctx.project_root, libris_op,
                    from_agent_id=shade_id,
                    to_agent_id=str(contract.get('parent_agent_id') or ''),
                    from_role='shade', to_role='researcher',
                    topic_slug=libris_topic,
                    message_kind=kind,
                    summary=summary,
                )
            except Exception as e:
                _diag('shade_tool', 'libris comm event emit failed; operation timeline missing shade message', error=e, contract_id=contract_id)

        _emit_libris_phase('starting', 'running', 'Shade contract started.')

        while True:
            phase = next_pending_phase(contract)
            if not phase:
                break

            instruction = build_phase_instruction(contract, phase)
            phase_id = phase['phase_id']
            phase_name = str(phase.get('name') or phase_id)
            _emit_libris_phase(phase_name, 'running', str(phase.get('objective') or '')[:200])

            try:
                response, events = asyncio.run(
                    engine.submit_and_collect(instruction)
                )
                if budget:
                    turn_tokens = 0
                    turn_input_tokens = 0
                    turn_output_tokens = 0
                    for event in events:
                        if event.type == 'message_end':
                            usage = event.data.get('usage') or {}
                            turn_tokens += int(usage.get('total_tokens', 0) or 0)
                            turn_input_tokens += int(usage.get('input_tokens', 0) or 0)
                            turn_output_tokens += int(usage.get('output_tokens', 0) or 0)
                    if turn_tokens:
                        from charon.agents.topology_budget import record_usage, record_cost
                        record_usage(state_dir, budget, turn_tokens)
                        try:
                            from charon.infra.orchestration_trace import estimate_cost_usd
                            record_cost(state_dir, budget, estimate_cost_usd(model.model_id, turn_input_tokens, turn_output_tokens))
                        except Exception as exc:
                            _diag('shade_tool', 'cost estimation/recording failed; cost accounting may undercount', error=exc, contract_id=contract_id)
                summary = response[:500] if response else 'Completed (no output)'
                mark_phase_completed(
                    state_dir, contract_id, phase_id,
                    task_id=shade_id,
                    summary=summary,
                )
                _emit_libris_phase(phase_name, 'running', f'Completed phase: {phase_name}')
                if phase_name in ('summary', 'report', 'extraction'):
                    _emit_libris_comm('shade_result_returned', summary)
            except Exception as e:
                err = str(e)
                mark_phase_failed(
                    state_dir, contract_id, phase_id,
                    task_id=shade_id,
                    error=err,
                )
                _emit_libris_phase('failed', 'failed', err)
                _emit_libris_comm('shade_failed', err)
                break

            # Reload contract for next phase
            contract = get_contract(state_dir, contract_id)
            if not contract or contract.get('status') != 'running':
                break

        contract = get_contract(state_dir, contract_id)
        if contract:
            assessment = assess_contract_outcome(contract)
            if assessment.get('outcome') in ('failed_runtime', 'failed_quality', 'partial', 'stalled'):
                reviewer_task_id = ''
                try:
                    from charon.agents.agent_runtime import append_inbox_event
                    review_prompt = (
                        f"Review failed/suspect worker contract {contract_id}.\n\n"
                        f"Goal: {contract.get('goal') or ''}\n"
                        f"Outcome: {assessment.get('outcome') or ''}\n"
                        f"Status: {assessment.get('status') or ''}\n"
                        f"Completed phases: {assessment.get('completed_phases')}/{assessment.get('total_phases')}\n"
                        f"Current phase: {assessment.get('current_phase_id') or '-'}\n"
                        f"Failed phases: {', '.join(assessment.get('failed_phase_ids') or []) or '(none)'}\n"
                        f"Provider/model: {assessment.get('resolved_provider') or '(unknown)'} / {assessment.get('resolved_model') or '(unknown)'}\n"
                        f"Expected outputs: {', '.join(assessment.get('expected_outputs') or []) or '(none)'}\n"
                        f"Quality flags: {json.dumps(assessment.get('quality_flags') or [], ensure_ascii=False)}\n\n"
                        f"Produce a concise triage verdict covering: root cause, whether the prompt was adequate, whether the model/provider seems too weak, whether completion evidence is sufficient, and the best next action."
                    )
                    reviewer_task_id = f"triage-review-{contract_id}"
                    append_inbox_event(
                        state_dir,
                        str(contract.get('parent_agent_id') or ''),
                        'worker_triage_requested',
                        {
                            'task_id': reviewer_task_id,
                            'contract_id': contract_id,
                            'instruction': review_prompt,
                            'assessment': assessment,
                        },
                    )
                except Exception as exc:
                    _diag('shade_tool', 'worker-triage inbox event failed; parent agent never asked to review failed contract', error=exc, contract_id=contract_id)
                    reviewer_task_id = ''
                triage = save_triage_record(state_dir, contract, assessment, reviewer_task_id=reviewer_task_id)
                try:
                    from charon.shade.shade_orchestrator import append_phase_event
                    append_phase_event(
                        state_dir,
                        contract_id=contract_id,
                        phase_id='-',
                        event_type='worker_triage_summary',
                        payload={
                            'triage_id': triage.get('triage_id', ''),
                            'outcome': assessment.get('outcome'),
                            'recommendation': triage.get('recommendation', ''),
                            'reviewer_task_id': reviewer_task_id,
                        },
                    )
                except Exception as exc:
                    _diag('shade_tool', 'worker_triage_summary phase event not recorded; contract timeline missing triage outcome', error=exc, contract_id=contract_id)

        if contract and contract.get('status') == 'completed':
            _emit_libris_phase('done', 'idle', 'Shade contract completed.')
            _emit_libris_comm('shade_contract_completed', 'Shade finished all contract phases.')

        # Retained shades go idle and stay addressable (charon.rlm(
        # child_agent_id=...) can reactivate them); everyone else stops.
        if retain:
            try:
                from charon.agents.shade_lifecycle import mark_idle
                mark_idle(state_dir, shade_id)
            except Exception as exc:
                _diag('shade_tool', 'retained shade failed to go idle; it may appear running forever', error=exc, contract_id=contract_id)
        else:
            try:
                from charon.agents.agent_lifecycle import set_status
                set_status(shade_id, 'stopped')
            except Exception as exc:
                _diag('shade_tool', 'shade agent status not set to stopped; agent may appear running forever', error=exc, contract_id=contract_id)

    except Exception as e:
        # Log error
        try:
            err_path = state_dir / 'agents' / shade_id / 'error.log'
            err_path.parent.mkdir(parents=True, exist_ok=True)
            err_path.write_text(f'{time.strftime("%Y-%m-%d %H:%M:%S")} Shade error: {e}\n')
        except Exception as exc:
            _diag('shade_tool', 'shade error.log write failed; shade crash has no persisted record', error=exc, contract_id=contract_id)


def execute_reactivate_shade(
    state_dir: Path, shade_id: str, objective: str, ctx: ToolContext, *, depth: int, budget: dict,
) -> dict:
    """Reactivate a retained, idle shade with a new instruction.

    This is the engine behind charon.rlm(child_agent_id=...) — never called
    as a standalone tool. Mirrors execute_spawn_shade's shape (create a
    contract, launch a background thread, return a handle) but resumes an
    existing agent_id's conversation instead of starting fresh.

    Never silently falls back to spawning a new shade: raises KeyError if
    shade_id has no retained lifecycle at all, TransitionRejected
    (charon.orchestration.fsm) if it exists but isn't currently idle, and
    RuntimeError if topology governance refuses the reactivation.
    """
    from charon.agents.topology_budget import try_reserve_reactivation
    ok, reason = try_reserve_reactivation(state_dir, budget, shade_id=shade_id)
    if not ok:
        raise RuntimeError(f'cannot reactivate {shade_id} — {reason}')

    # The sole gate, per shade_lifecycle's own contract: dispatch's success
    # (or the KeyError/TransitionRejected it raises), never a prior read.
    from charon.agents.shade_lifecycle import try_reactivate
    try_reactivate(state_dir, shade_id)

    from charon.shade.shade_orchestrator import create_contract
    contract = create_contract(
        state_dir,
        parent_task_id='',
        parent_agent_id=ctx.agent_id,
        shade_agent_id=shade_id,
        conversation_id=f'shade-conv-{shade_id}',
        project=str(ctx.project_root),
        goal=objective,
        phase_specs=[{'name': 'resume', 'objective': objective}],
        metadata={'topology_depth': depth, 'topology_budget': budget, 'reactivation': True},
    )
    contract_id = contract['id']

    thread = threading.Thread(
        target=_run_shade,
        args=(state_dir, shade_id, contract_id, objective, [], [], ctx, depth, budget, True, True),
        daemon=True,
    )
    thread.start()

    return {'shade_id': shade_id, 'contract_id': contract_id, 'status': 'running'}


LIST_RETAINED_SHADES_TOOL_DEF = {
    'name': 'ListRetainedShades',
    'description': (
        'List your currently retained shades — ones spawned with retain=True that have '
        'finished their last instruction and are sitting idle, addressable again via '
        'charon.rlm(objective, child_agent_id=...) in PyKernel. Shows how long each has '
        'been idle; a shade idle longer than CHARON_RETAINED_SHADE_MAX_IDLE_SECONDS '
        '(default 24h) will be stopped automatically by the daemon heartbeat.'
    ),
    'input_schema': {'type': 'object', 'properties': {}, 'required': []},
}


def execute_list_retained_shades(params: dict, ctx: ToolContext) -> ToolResult:
    """List every currently-idle retained shade with how long it's been idle."""
    if not ctx.state_dir:
        return ToolResult(content='Error: state_dir not available', is_error=True)

    from datetime import datetime, timezone
    from charon.agents import shade_lifecycle
    from charon.agents.agent_lifecycle import list_agents

    shade_ids = shade_lifecycle.list_idle_shade_ids(ctx.state_dir)
    if not shade_ids:
        return ToolResult(content='No retained shades are currently idle.', details={'shades': []})

    agents_by_id = {a.get('id'): a for a in list_agents()}
    now = datetime.now(timezone.utc).timestamp()
    rows = []
    lines = [f'Retained shades ({len(shade_ids)}):']
    for shade_id in shade_ids:
        instance = shade_lifecycle.get_instance(ctx.state_dir, shade_id)
        idle_seconds = None
        if instance is not None:
            try:
                idle_since = datetime.fromisoformat(instance.updated_at.replace('Z', '+00:00')).timestamp()
                idle_seconds = max(0, int(now - idle_since))
            except Exception:
                idle_seconds = None
        goal = str((agents_by_id.get(shade_id) or {}).get('goal') or '')
        idle_display = f'{idle_seconds}s' if idle_seconds is not None else 'unknown'
        lines.append(f'- {shade_id}: idle {idle_display} — {goal[:120]}')
        rows.append({'shade_id': shade_id, 'goal': goal, 'idle_seconds': idle_seconds})

    return ToolResult(content='\n'.join(lines), details={'shades': rows})
