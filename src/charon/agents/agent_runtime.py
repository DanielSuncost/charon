#!/usr/bin/env python3
from __future__ import annotations

import json
import re
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from charon.infra import config

try:
    from charon.infra.diagnostics import record as _diag
except Exception:  # diagnostics is best-effort and must never block import
    def _diag(*args, **kwargs):
        return None

if TYPE_CHECKING:
    from charon.conversation.conversation_engine import ConversationEngine

# SQLite store adapter (optional — gracefully degrades to JSON)
try:
    from charon.infra.store_adapter import (
        get_db as _get_db,
        agent_profile_upsert as _db_profile_upsert,
        agent_profile_get as _db_profile_get,  # noqa: F401 — availability probe: full adapter API must import
        agent_memory_upsert as _db_memory_upsert,
        agent_memory_get as _db_memory_get,  # noqa: F401 — availability probe
        agent_inbox_append as _db_inbox_append,
        agent_attempt_append as _db_attempt_append,
        onboarding_get as _db_onboarding_get,
    )
    _HAS_STORE = True
except ImportError:
    _HAS_STORE = False


def _use_store() -> bool:
    return _HAS_STORE and not config.no_sqlite()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _agent_dir(state_dir: Path, agent_id: str) -> Path:
    return state_dir / 'agents' / agent_id


def _read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception as e:
        _diag('agent_runtime', 'unreadable JSON state file; returning default (stored state ignored)', error=e, path=str(path))
        return default


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2))


def _append_jsonl(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as f:
        f.write(json.dumps(payload) + '\n')


def ensure_agent_runtime_state(state_dir: Path, agent: dict) -> dict:
    agent_id = agent.get('id')
    if not agent_id:
        raise ValueError('agent id missing')
    adir = _agent_dir(state_dir, agent_id)
    profile_path = adir / 'profile.json'
    memory_path = adir / 'working_memory.json'

    profile = _read_json(profile_path, None)
    if not isinstance(profile, dict):
        profile = {
            'agent_id': agent_id,
            'name': agent.get('name') or agent_id,
            'mode': agent.get('mode') or 'persistent',
            'goal': agent.get('goal') or '',
            'project': agent.get('project') or '',
            'created_at': utc_now_iso(),
            'updated_at': utc_now_iso(),
        }
    else:
        profile.update({
            'name': agent.get('name') or profile.get('name') or agent_id,
            'mode': agent.get('mode') or profile.get('mode') or 'persistent',
            'goal': agent.get('goal') or profile.get('goal') or '',
            'project': agent.get('project') or profile.get('project') or '',
            'updated_at': utc_now_iso(),
        })
    _write_json(profile_path, profile)

    memory = _read_json(memory_path, None)
    if not isinstance(memory, dict):
        memory = {
            'agent_id': agent_id,
            'notes': [],
            'last_task_id': None,
            'last_task_summary': None,
            'updated_at': utc_now_iso(),
        }
        _write_json(memory_path, memory)

    # Sync to SQLite
    if _use_store():
        try:
            db = _get_db(state_dir)
            _db_profile_upsert(db, agent_id, profile)
            _db_memory_upsert(db, agent_id, memory)
        except Exception as e:
            _diag('agent_runtime', 'agent profile/memory sync to SQLite failed; store diverges from JSON', error=e)

    return {
        'agent_dir': str(adir),
        'profile_path': str(profile_path),
        'memory_path': str(memory_path),
    }


def append_inbox_event(state_dir: Path, agent_id: str, event_type: str, payload: dict) -> None:
    adir = _agent_dir(state_dir, agent_id)
    rec = {
        'ts': utc_now_iso(),
        'event_type': event_type,
        'payload': payload,
    }
    _append_jsonl(adir / 'inbox.jsonl', rec)
    if _use_store():
        try:
            _db_inbox_append(_get_db(state_dir), agent_id, event_type, payload)
        except Exception as e:
            _diag('agent_runtime', 'inbox event mirror to SQLite failed; store misses this event', error=e, event_type=event_type)


def record_attempt_event(state_dir: Path, agent_id: str, task_id: str, attempt_id: str, stage: str, payload: dict | None = None) -> None:
    adir = _agent_dir(state_dir, agent_id)
    rec = {
        'ts': utc_now_iso(),
        'task_id': task_id,
        'attempt_id': attempt_id,
        'stage': stage,
        'payload': payload or {},
    }
    _append_jsonl(adir / 'attempts.jsonl', rec)
    if _use_store():
        try:
            _db_attempt_append(_get_db(state_dir), agent_id, task_id, attempt_id, stage, payload)
        except Exception as e:
            _diag('agent_runtime', 'attempt event mirror to SQLite failed; store misses this attempt stage', error=e, stage=stage)


def update_working_memory(state_dir: Path, agent_id: str, *, task_id: str, summary: str) -> None:
    adir = _agent_dir(state_dir, agent_id)
    path = adir / 'working_memory.json'
    memory = _read_json(path, {'agent_id': agent_id, 'notes': []})
    notes = list(memory.get('notes') or [])
    notes.append({'ts': utc_now_iso(), 'task_id': task_id, 'summary': summary})
    notes = notes[-20:]
    memory.update({
        'agent_id': agent_id,
        'notes': notes,
        'last_task_id': task_id,
        'last_task_summary': summary,
        'updated_at': utc_now_iso(),
    })
    _write_json(path, memory)
    if _use_store():
        try:
            _db_memory_upsert(_get_db(state_dir), agent_id, memory)
        except Exception as e:
            _diag('agent_runtime', 'working-memory sync to SQLite failed; store diverges from JSON', error=e)


def _extract_json_object(text: str) -> dict | None:
    text = (text or '').strip()
    if not text:
        return None
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass

    match = re.search(r'\{.*\}', text, flags=re.DOTALL)
    if not match:
        return None
    chunk = match.group(0)
    try:
        obj = json.loads(chunk)
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _plan_prompt(agent: dict, task: dict, memory: dict) -> str:
    return (
        'You are planning exactly one next action for a persistent coding agent. '        'Respond ONLY JSON with keys: action, summary, command, path, content. '        'Allowed actions: final, shell, write_file. '        'Use action=final if no tool needed. '        f"Agent: {agent.get('id')} {agent.get('name')} goal={agent.get('goal')}\n"
        f"Project: {task.get('project') or agent.get('project') or ''}\n"
        f"Task id: {task.get('id')}\n"
        f"Instruction: {task.get('instruction') or task.get('message') or ''}\n"
        f"Recent memory: {json.dumps((memory.get('notes') or [])[-3:], ensure_ascii=False)}"
    )


def _resolve_planner_mode(state_dir: Path) -> str:
    env = config.agent_planner()
    if env in ('heuristic', 'llm'):
        return env

    onboarding = None
    # Try SQLite first
    if _use_store():
        try:
            onboarding = _db_onboarding_get(_get_db(state_dir))
        except Exception as e:
            _diag('agent_runtime', 'onboarding lookup via SQLite failed; falling back to onboarding.json', error=e)

    # Fallback to JSON
    if not onboarding:
        onboarding_path = state_dir / 'onboarding.json'
        if onboarding_path.exists():
            try:
                onboarding = json.loads(onboarding_path.read_text())
            except Exception as e:
                _diag('agent_runtime', 'onboarding.json unreadable; planner mode falls back to heuristic', error=e)
                onboarding = {}

    if isinstance(onboarding, dict):
        provider_mode = str(onboarding.get('provider_mode') or '').strip().lower()
        provider = str(onboarding.get('provider') or '').strip().lower()
        complete = bool(onboarding.get('complete'))
        if complete and provider_mode == 'provider' and provider not in ('', 'none', 'no-provider'):
            return 'llm'

    return 'heuristic'


def decide_action(task: dict, agent: dict, memory: dict, *, llm_adapter=None, planner_mode: str = 'heuristic') -> dict:
    instruction = (task.get('instruction') or task.get('message') or '').strip()
    if not instruction:
        return {'action': 'final', 'summary': 'No instruction provided.'}

    if instruction.lower().startswith('run:'):
        return {'action': 'shell', 'command': instruction[4:].strip(), 'summary': 'Executed run: command'}

    if instruction.lower().startswith('write:'):
        payload = instruction[6:].strip()
        if '|' in payload:
            target, content = payload.split('|', 1)
            return {'action': 'write_file', 'path': target.strip(), 'content': content.strip(), 'summary': f'Wrote {target.strip()}'}

    if planner_mode == 'llm' and llm_adapter is not None:
        prompt = _plan_prompt(agent, task, memory)
        ok, response = llm_adapter.query_local_model(prompt)
        if ok:
            parsed = _extract_json_object(response)
            if parsed and isinstance(parsed.get('action'), str):
                return parsed
            # fallback to text completion if not structured
            return {'action': 'final', 'summary': response.strip()[:600]}

    return {'action': 'final', 'summary': f"Completed: {instruction[:200]}"}


def _safe_project_root(task: dict, agent: dict) -> Path:
    project = (task.get('project') or agent.get('project') or '').strip()
    if project:
        return Path(project).resolve()
    return Path.cwd().resolve()


def _execute_shell(command: str, cwd: Path, timeout_sec: int) -> tuple[bool, dict]:
    if not command:
        return False, {'error': 'missing shell command'}
    try:
        proc = subprocess.run(
            command,
            shell=True,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired:
        return False, {'error': f'shell command timed out after {timeout_sec}s'}
    except Exception as e:
        return False, {'error': f'shell command failed to start: {e}'}

    out = (proc.stdout or '').strip()
    err = (proc.stderr or '').strip()
    tail = '\n'.join([line for line in (out + '\n' + err).splitlines() if line.strip()][-20:])
    payload = {
        'command': command,
        'exit_code': proc.returncode,
        'output_tail': tail,
    }
    if proc.returncode != 0:
        return False, payload
    return True, payload


def _execute_write_file(path_text: str, content: str, cwd: Path) -> tuple[bool, dict]:
    if not path_text:
        return False, {'error': 'missing path for write_file'}
    target = (cwd / path_text).resolve() if not Path(path_text).is_absolute() else Path(path_text).resolve()
    try:
        # basic cleanroom guard: keep writes inside project root when relative
        if not str(target).startswith(str(cwd)):
            return False, {'error': 'write_file path escapes project root'}
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content or '', encoding='utf-8')
        return True, {'path': str(target), 'bytes': len((content or '').encode('utf-8'))}
    except Exception as e:
        return False, {'error': f'write_file failed: {e}'}


def execute_action(action: dict, task: dict, agent: dict) -> tuple[bool, dict]:
    action_name = str(action.get('action') or 'final').strip().lower()
    summary = str(action.get('summary') or '').strip()
    cwd = _safe_project_root(task, agent)

    if action_name == 'final':
        return True, {'summary': summary or f"Completed: {(task.get('instruction') or '')[:120]}"}

    if action_name == 'shell':
        timeout_sec = config.agent_shell_timeout()
        ok, payload = _execute_shell(str(action.get('command') or ''), cwd=cwd, timeout_sec=timeout_sec)
        if ok:
            cmd = str(action.get('command') or '')
            text = summary or f'shell ok: {cmd}'
            if payload.get('output_tail'):
                text = f"{text}\n{payload['output_tail']}"
            return True, {'summary': text[:1200], 'tool_result': payload}
        return False, {'error': payload.get('error') or 'shell failed', 'tool_result': payload}

    if action_name == 'write_file':
        ok, payload = _execute_write_file(str(action.get('path') or ''), str(action.get('content') or ''), cwd=cwd)
        if ok:
            text = summary or f"wrote file: {payload.get('path')}"
            return True, {'summary': text, 'tool_result': payload}
        return False, {'error': payload.get('error') or 'write_file failed', 'tool_result': payload}

    return False, {'error': f'unsupported action: {action_name}'}


# ============================================================================
# Unified execution via ConversationEngine
# ============================================================================

# Cache of ordinary engines by agent/project and explicitly routed engines by
# agent/project/endpoint. This preserves ordinary behavior without crossing a
# route boundary.
_EngineCacheKey = tuple[str, str, str, str, str, int, str, str]
_agent_engines: dict[_EngineCacheKey, 'ConversationEngine'] = {}


def _route_was_requested(task: dict) -> bool:
    return 'model_route' in task or 'routing_decision' in task


def _task_route_override(task: dict) -> dict | None:
    """Return the explicit route carried by a task.

    A present-but-invalid route is an execution error, not an instruction to
    use the ordinary configured provider.
    """
    from charon.providers.provider_bridge import ProviderRouteError

    if 'model_route' in task:
        route = task.get('model_route')
        if not isinstance(route, dict):
            raise ProviderRouteError('model_route must be an object')
        return dict(route)

    if 'routing_decision' in task:
        decision = task.get('routing_decision')
        if not isinstance(decision, dict):
            raise ProviderRouteError('routing_decision must be an object')
        route = decision.get('selected_endpoint')
        if not isinstance(route, dict):
            raise ProviderRouteError(
                'routing_decision is missing selected_endpoint'
            )
        return dict(route)

    return None


def _public_endpoint(endpoint: dict | None) -> dict | None:
    """Copy endpoint provenance without credentials or unrelated metadata."""
    if not isinstance(endpoint, dict):
        return None
    public = {
        key: endpoint[key]
        for key in (
            'candidate_id',
            'provider',
            'model_id',
            'context_window',
            'base_url',
            'resolver',
            'phase_name',
            'task_complexity',
        )
        if endpoint.get(key) is not None
    }
    if 'model_id' not in public and endpoint.get('model') is not None:
        public['model_id'] = endpoint['model']
    return public


def _engine_cache_key(
    state_dir: Path,
    agent_id: str,
    project_root: Path,
    endpoint: dict | None,
    credential_fingerprint: str = '',
) -> _EngineCacheKey:
    endpoint = endpoint or {}
    context_window = endpoint.get('context_window')
    try:
        context_identity = int(context_window) if context_window is not None else 0
    except (TypeError, ValueError):
        context_identity = 0
    return (
        str(Path(state_dir).resolve()),
        str(agent_id),
        str(project_root.resolve()),
        str(endpoint.get('provider') or ''),
        str(endpoint.get('model_id') or endpoint.get('model') or ''),
        context_identity,
        str(endpoint.get('base_url') or '').rstrip('/'),
        str(credential_fingerprint),
    )


def _endpoints_match(
    selected: dict | None,
    executed: dict | None,
) -> bool:
    if not isinstance(selected, dict) or not isinstance(executed, dict):
        return False
    for key in ('provider', 'model_id', 'context_window'):
        if selected.get(key) != executed.get(key):
            return False
    selected_base = str(selected.get('base_url') or '').rstrip('/')
    executed_base = str(executed.get('base_url') or '').rstrip('/')
    return not selected_base or selected_base == executed_base


def _record_route_provenance(
    task: dict,
    *,
    selected: dict | None,
    executed: dict | None,
) -> dict:
    selected_public = _public_endpoint(selected)
    executed_public = _public_endpoint(executed)
    honored = _endpoints_match(selected_public, executed_public)
    task['selected_model'] = selected_public
    task['executed_model'] = executed_public
    task['selected_endpoint'] = selected_public
    task['executed_endpoint'] = executed_public
    task['route_honored'] = honored
    if task.get('attempts'):
        task['attempts'][-1].update({
            'selected_endpoint': selected_public,
            'executed_endpoint': executed_public,
            'route_honored': honored,
        })
    return {
        'selected_model': selected_public,
        'executed_model': executed_public,
        'selected_endpoint': selected_public,
        'executed_endpoint': executed_public,
        'route_honored': honored,
    }


def _routed_execution_failure(
    state_dir: Path,
    task: dict,
    agent: dict,
    *,
    error: str,
    selected: dict | None,
) -> tuple[bool, dict]:
    agent_id = str(agent.get('id') or '')
    task_id = task.get('id') or f"task-{uuid.uuid4().hex[:8]}"
    attempt_id = f"att-{uuid.uuid4().hex[:10]}"
    task.setdefault('attempts', [])
    task['attempts'].append({
        'attempt_id': attempt_id,
        'started_at': utc_now_iso(),
        'completed_at': utc_now_iso(),
        'status': 'failed',
        'error': error,
    })
    provenance = _record_route_provenance(
        task,
        selected=selected,
        executed=None,
    )
    task['execution_error'] = error
    record_attempt_event(
        state_dir,
        agent_id,
        task_id,
        attempt_id,
        'attempt_started',
        {
            'task_type': task.get('task_type'),
            'mode': 'engine',
            'selected_endpoint': provenance['selected_endpoint'],
        },
    )
    append_inbox_event(
        state_dir,
        agent_id,
        'task_failed',
        {'task_id': task_id, 'error': error, **provenance},
    )
    record_attempt_event(
        state_dir,
        agent_id,
        task_id,
        attempt_id,
        'attempt_failed',
        {'error': error, 'mode': 'engine', **provenance},
    )
    return False, {
        'status': 'task_failed',
        'error': error,
        'attempt_id': attempt_id,
        **provenance,
    }


def _build_task_system_prompt(state_dir: Path, agent: dict, task: dict) -> str:
    """Build the system prompt for a task using the layered builder.

    Loads shade contract if this is a shade phase task.
    """
    from charon.context.system_prompt_builder import build_system_prompt as build_layered_prompt

    contract = None
    shade_phase = task.get('shade_phase') or {}
    contract_id = shade_phase.get('contract_id')
    if contract_id and agent.get('role') == 'shade':
        try:
            from charon.shade import shade_orchestrator as _shade_orch
            contract = _shade_orch.get_contract(state_dir, contract_id)
        except Exception as e:
            _diag('agent_runtime', 'shade contract load failed; task runs without contract constraints', error=e)

    return build_layered_prompt(
        state_dir=state_dir,
        agent=agent,
        task=task,
        contract=contract,
    )


def _get_or_create_engine(state_dir: Path, agent: dict, task: dict):
    """Get or create a ConversationEngine for an agent.

    Explicit routes include provider/model/endpoint in their cache identity.
    Unrouted tasks retain the prior agent-and-project cache behavior. The
    system prompt is rebuilt per task so memory stays fresh.
    """
    from charon.providers.provider_bridge import (
        ProviderRouteError,
        create_provider_and_model,
        credential_fingerprint_for_provider,
        describe_provider_endpoint,
        resolve_route_override,
    )
    from charon.conversation.conversation_engine import ConversationEngine

    agent_id = agent.get('id', '')
    project = str(task.get('project') or agent.get('project') or '').strip()
    project_root = Path(project) if project else Path.cwd()
    if not project_root.is_dir():
        project_root = Path.cwd()

    # Build fresh system prompt for this task
    system_prompt = _build_task_system_prompt(state_dir, agent, task)

    route_override = _task_route_override(task)
    selected_endpoint = None
    credential_fingerprint = ''
    registry_execution = None
    if route_override is not None:
        resolver = str(route_override.get('resolver') or '').strip()
        if resolver:
            if resolver != 'model_registry':
                raise ProviderRouteError(
                    f'unsupported model route resolver {resolver!r}'
                )
            from charon.providers.model_registry import (
                get_shade_provider_and_model,
            )

            provider, model, ready = get_shade_provider_and_model(
                state_dir,
                phase_name=str(route_override.get('phase_name') or ''),
                task_complexity=str(
                    route_override.get('task_complexity') or 'normal'
                ),
            )
            if not ready:
                raise ProviderRouteError(
                    'model registry route is unavailable'
                )
            executed_endpoint = _public_endpoint(
                describe_provider_endpoint(provider, model)
            )
            requested_endpoint = _public_endpoint(route_override)
            if not _endpoints_match(
                requested_endpoint,
                executed_endpoint,
            ):
                raise ProviderRouteError(
                    'model registry route no longer matches its configured '
                    'execution endpoint'
                )
            selected_endpoint = dict(executed_endpoint or {})
            for key in (
                'candidate_id',
                'resolver',
                'phase_name',
                'task_complexity',
            ):
                if requested_endpoint and requested_endpoint.get(key) is not None:
                    selected_endpoint[key] = requested_endpoint[key]
            credential_fingerprint = credential_fingerprint_for_provider(
                provider
            )
            registry_execution = (
                provider,
                model,
                ready,
                executed_endpoint,
            )
        else:
            route_config = resolve_route_override(state_dir, route_override)
            selected_endpoint = _public_endpoint(
                route_config['selected_endpoint']
            )
            credential_fingerprint = str(
                route_config.get('credential_fingerprint') or ''
            )

    cache_key = _engine_cache_key(
        state_dir,
        agent_id,
        project_root,
        selected_endpoint,
        credential_fingerprint,
    )

    # Reuse only an engine with the same complete execution identity.
    cached = _agent_engines.get(cache_key)
    if cached is not None:
        cached.update_system_prompt(system_prompt)
        if selected_endpoint is not None:
            cached._charon_selected_endpoint = selected_endpoint
        return cached, True

    if registry_execution is not None:
        provider, model, ready, executed_endpoint = registry_execution
    else:
        provider, model, ready = create_provider_and_model(
            state_dir,
            route_override=route_override,
        )
        executed_endpoint = describe_provider_endpoint(provider, model)
    if not ready:
        return None, False

    engine = ConversationEngine(
        provider=provider,
        model=model,
        project_root=project_root,
        agent_id=agent_id,
        agent_name=agent.get('name') or 'Charon',
        system_prompt=system_prompt,
        state_dir=state_dir,
        max_tokens=32768,
    )
    if selected_endpoint is not None:
        engine._charon_selected_endpoint = selected_endpoint
        engine._charon_executed_endpoint = _public_endpoint(executed_endpoint)

    _agent_engines[cache_key] = engine
    return engine, True


def _promote_task_to_episode(
    state_dir: Path,
    agent: dict,
    task: dict,
    *,
    task_id: str,
    instruction: str,
    summary: str,
    tool_calls: list[dict],
    response_text: str,
    total_turns: int,
    provider: str = '',
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> None:
    """Promote a completed task into the episodic memory pipeline: a
    first-class Episode with typed events (and auto-captured decisions),
    attributed to this agent — the WHO that makes cross-agent threads and a
    specialist's long-lived track record work. Best-effort: never raises."""
    try:
        from charon.memory.execution_memory import create_task_episode
        create_task_episode(
            state_dir,
            session_id=task_id,
            agent_id=agent.get('id', ''),
            project_root=task.get('project') or agent.get('project') or '',
            provider=provider,
            objective=instruction,
            summary=summary,
            tool_calls=tool_calls,
            response_text=response_text,
            total_turns=total_turns,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
    except Exception as e:
        _diag('agent_runtime', 'episodic promotion failed; task episode not recorded', error=e, task_id=task_id)


def _run_task_with_engine(
    state_dir: Path,
    task: dict,
    agent: dict,
    engine,
) -> tuple[bool, dict]:
    """Execute a task using the ConversationEngine (real LLM multi-turn loop).

    This is the unified path that replaces the old heuristic decide_action().
    """
    import asyncio

    agent_id = agent.get('id', '')
    task_id = task.get('id') or f"task-{uuid.uuid4().hex[:8]}"
    attempt_id = f"att-{uuid.uuid4().hex[:10]}"
    instruction = (task.get('instruction') or task.get('message') or '').strip()

    if not instruction:
        return True, {
            'status': 'task_succeeded',
            'summary': 'No instruction provided.',
            'attempt_id': attempt_id,
        }

    # Record attempt start
    task.setdefault('attempts', [])
    task['attempts'].append({
        'attempt_id': attempt_id,
        'started_at': utc_now_iso(),
        'status': 'running',
    })
    record_attempt_event(
        state_dir, agent_id, task_id, attempt_id,
        'attempt_started', {'task_type': task.get('task_type'), 'mode': 'engine'},
    )

    # Build context-enriched prompt
    constraints = task.get('constraints') or []
    expected_outputs = task.get('expected_outputs') or []
    prompt_parts = [instruction]
    if constraints:
        prompt_parts.append('\nConstraints:\n' + '\n'.join(f'- {c}' for c in constraints))
    if expected_outputs:
        prompt_parts.append('\nExpected outputs:\n' + '\n'.join(f'- {o}' for o in expected_outputs))
    full_prompt = '\n'.join(prompt_parts)

    # Run the conversation engine
    text_parts = []
    tool_calls_made = []
    errors = []
    total_turns = 0
    # Summed across every turn's usage (a task can be many turns of tool use);
    # this is what actually gets recorded now, rather than hardcoded zeros.
    total_usage = {'input_tokens': 0, 'output_tokens': 0, 'total_tokens': 0}
    _current_tool_args = {}

    async def _execute():
        nonlocal total_turns
        async for event in engine.submit(full_prompt):
            if event.type == 'text_delta':
                text_parts.append(event.data.get('text', ''))
            elif event.type == 'tool_call':
                _current_tool_args[event.data.get('tool_call_id', '')] = {
                    'tool': event.data.get('tool_name', ''),
                    'arguments': event.data.get('arguments', {}),
                }
            elif event.type == 'tool_execution_end':
                tc_id = event.data.get('tool_call_id', '')
                tc_info = _current_tool_args.pop(tc_id, {})
                tool_calls_made.append({
                    'tool': tc_info.get('tool') or event.data.get('tool_name', ''),
                    'arguments': tc_info.get('arguments', {}),
                    'is_error': event.data.get('is_error', False),
                    'result': event.data.get('content', '')[:500],
                })
            elif event.type == 'error':
                errors.append(event.data.get('error', 'unknown error'))
            elif event.type == 'message_end':
                usage = event.data.get('usage') or {}
                for k in total_usage:
                    total_usage[k] += int(usage.get(k, 0) or 0)
            elif event.type == 'done':
                total_turns = event.data.get('total_turns', 0)

    # Unified trace span around the agent's engine run (additive, best-effort).
    try:
        from charon.infra import orchestration_trace as _ot
        _run_span_cm = _ot.span(
            state_dir, name=f'agent task {task_id}', system='agent', kind='agent_run',
            trace_id=f'tr_{task_id}' if task_id else None,
            agent_id=agent_id or None, task_id=task_id or None,
            attributes={'task_type': task.get('task_type')},
        )
    except Exception:
        _run_span_cm = None
    if _run_span_cm is not None:
        with _run_span_cm as _run_span:
            try:
                asyncio.run(_execute())
            except Exception as e:
                errors.append(str(e))
                _run_span.status = 'error'
            _run_span.set(turns=total_turns, tool_calls=len(tool_calls_made),
                          errors=len(errors))
    else:
        try:
            asyncio.run(_execute())
        except Exception as e:
            errors.append(str(e))

    response_text = ''.join(text_parts).strip()

    # Roll this task's real token cost into its goal, if it has one — success
    # or failure both cost tokens. This is the accounting half of goal token
    # budgets; self_assign_next_task is the enforcement half.
    goal_ref = task.get('goal_ref') or {}
    if goal_ref.get('goal_id') and total_usage['total_tokens']:
        try:
            from charon.agents.autonomous import add_goal_tokens_used
            add_goal_tokens_used(
                state_dir,
                project=task.get('project') or agent.get('project') or '',
                goal_id=goal_ref['goal_id'],
                tokens=total_usage['total_tokens'],
            )
        except Exception as e:
            _diag('agent_runtime', 'goal token-usage rollup failed; goal.tokens_used not updated',
                  error=e, task_id=task_id, goal_id=goal_ref.get('goal_id'))

    # Determine success
    has_fatal_error = any(
        'Connection failed' in e or 'API' in e or 'timed out' in e
        for e in errors
    )

    routed_error = _route_was_requested(task) and bool(errors)
    if routed_error or (has_fatal_error and not response_text):
        error_msg = '; '.join(errors)
        append_inbox_event(state_dir, agent_id, 'task_failed', {
            'task_id': task_id, 'error': error_msg,
        })
        record_attempt_event(
            state_dir, agent_id, task_id, attempt_id,
            'attempt_failed', {'error': error_msg, 'mode': 'engine'},
        )
        task['attempts'][-1]['status'] = 'failed'
        task['attempts'][-1]['completed_at'] = utc_now_iso()
        task['attempts'][-1]['error'] = error_msg
        return False, {
            'status': 'task_failed',
            'error': error_msg,
            'attempt_id': attempt_id,
        }

    # Build intelligent summary from execution facts
    from charon.agents.task_summarizer import summarize_fast
    summary = summarize_fast(
        instruction=instruction,
        tool_calls=tool_calls_made,
        response_text=response_text,
        errors=errors,
        total_turns=total_turns,
    )

    # Record success
    update_working_memory(state_dir, agent_id, task_id=task_id, summary=summary)
    append_inbox_event(state_dir, agent_id, 'task_succeeded', {
        'task_id': task_id,
        'summary': summary,
        'turns': total_turns,
        'tool_calls': len(tool_calls_made),
    })

    # Index conversation into semantic memory (background, non-blocking)
    try:
        from charon.memory.memory_indexer import index_conversation, extract_and_index_facts
        # Reconstruct turns from what the engine produced
        index_turns = []
        if instruction:
            index_turns.append({'role': 'user', 'content': instruction})
        if response_text:
            index_turns.append({'role': 'assistant', 'content': response_text})
        for tc in tool_calls_made:
            if tc.get('result') and not tc.get('is_error'):
                index_turns.append({'role': 'tool', 'content': tc['result']})
        # Fast path: verbatim embedding of all turns
        index_conversation(state_dir, index_turns, agent_id=agent_id, conv_id=task_id)
        # Slow path: LLM-based structured fact extraction (skips trivial sessions)
        extract_and_index_facts(state_dir, index_turns, agent_id=agent_id, conv_id=task_id)
    except ImportError:
        pass

    # Episodic promotion: first-class Episode + typed events + auto-captured
    # decisions, attributed to this agent (Phase B pipeline).
    _promote_task_to_episode(
        state_dir, agent, task,
        task_id=task_id, instruction=instruction, summary=summary,
        tool_calls=tool_calls_made, response_text=response_text,
        total_turns=total_turns,
        provider=str(getattr(getattr(engine, 'provider', None), 'name', '') or ''),
        input_tokens=total_usage['input_tokens'],
        output_tokens=total_usage['output_tokens'],
    )
    record_attempt_event(
        state_dir, agent_id, task_id, attempt_id,
        'attempt_succeeded', {
            'summary': summary[:500],
            'turns': total_turns,
            'tool_calls': len(tool_calls_made),
            'mode': 'engine',
        },
    )
    task['attempts'][-1]['status'] = 'succeeded'
    task['attempts'][-1]['completed_at'] = utc_now_iso()

    return True, {
        'status': 'task_succeeded',
        'summary': summary,
        'attempt_id': attempt_id,
        'turns': total_turns,
        'tool_calls': len(tool_calls_made),
    }


def run_task_tick(state_dir: Path, task: dict, *, agent: dict, llm_adapter=None) -> tuple[bool, dict]:
    """Execute a single task tick.

    Dispatches to the ConversationEngine when LLM mode is active (onboarding
    complete with a provider configured), or falls back to the heuristic
    path for no-provider setups.
    """
    ensure_agent_runtime_state(state_dir, agent)
    agent_id = agent.get('id')
    task_id = task.get('id') or f"task-{uuid.uuid4().hex[:8]}"
    attempt_id = f"att-{uuid.uuid4().hex[:10]}"

    # An explicit graph route is an execution constraint. It must not be
    # silently replaced by the heuristic planner or the onboarding provider.
    routed = _route_was_requested(task)
    if routed:
        selected = _public_endpoint(
            task.get('model_route')
            if isinstance(task.get('model_route'), dict)
            else (
                task.get('routing_decision', {}).get('selected_endpoint')
                if isinstance(task.get('routing_decision'), dict)
                else None
            )
        )
        try:
            engine, ready = _get_or_create_engine(state_dir, agent, task)
        except Exception as exc:
            return _routed_execution_failure(
                state_dir,
                task,
                agent,
                error=f'explicit model route unavailable: {exc}',
                selected=selected,
            )
        if engine is None or not ready:
            return _routed_execution_failure(
                state_dir,
                task,
                agent,
                error='explicit model route unavailable',
                selected=selected,
            )

        selected = getattr(engine, '_charon_selected_endpoint', selected)
        executed = getattr(engine, '_charon_executed_endpoint', None)
        if not _endpoints_match(
            _public_endpoint(selected),
            _public_endpoint(executed),
        ):
            return _routed_execution_failure(
                state_dir,
                task,
                agent,
                error='explicit model route did not match the executable endpoint',
                selected=selected,
            )

        ok, result = _run_task_with_engine(state_dir, task, agent, engine)
        provenance = _record_route_provenance(
            task,
            selected=selected,
            executed=executed,
        )
        return ok, {**result, **provenance}

    # Try the unified engine path first for ordinary, unrouted tasks.
    planner_mode = _resolve_planner_mode(state_dir)
    if planner_mode == 'llm':
        engine, ready = _get_or_create_engine(state_dir, agent, task)
        if engine is not None and ready:
            return _run_task_with_engine(state_dir, task, agent, engine)

    # Fallback: heuristic path (original behavior)
    task.setdefault('attempts', [])
    task['attempts'].append({'attempt_id': attempt_id, 'started_at': utc_now_iso(), 'status': 'running'})
    record_attempt_event(state_dir, agent_id, task_id, attempt_id, 'attempt_started', {'task_type': task.get('task_type')})
    append_inbox_event(state_dir, agent_id, 'task_received', {'task_id': task_id, 'instruction': task.get('instruction')})

    memory = _read_json(_agent_dir(state_dir, agent_id) / 'working_memory.json', {'notes': []})
    action = decide_action(task, agent=agent, memory=memory, llm_adapter=llm_adapter, planner_mode=planner_mode)
    record_attempt_event(state_dir, agent_id, task_id, attempt_id, 'action_planned', {'action': action.get('action'), 'planner_mode': planner_mode})

    ok, payload = execute_action(action, task=task, agent=agent)
    if ok:
        summary = str(payload.get('summary') or '').strip()[:1200]
        update_working_memory(state_dir, agent_id, task_id=task_id, summary=summary)
        append_inbox_event(state_dir, agent_id, 'task_succeeded', {'task_id': task_id, 'summary': summary})
        record_attempt_event(state_dir, agent_id, task_id, attempt_id, 'attempt_succeeded', {'summary': summary})
        task['attempts'][-1]['status'] = 'succeeded'
        task['attempts'][-1]['completed_at'] = utc_now_iso()
        return True, {
            'status': 'task_succeeded',
            'summary': summary,
            'attempt_id': attempt_id,
            'tool_result': payload.get('tool_result'),
        }

    error = str(payload.get('error') or 'task execution failed')
    append_inbox_event(state_dir, agent_id, 'task_failed', {'task_id': task_id, 'error': error})
    record_attempt_event(state_dir, agent_id, task_id, attempt_id, 'attempt_failed', {'error': error})
    task['attempts'][-1]['status'] = 'failed'
    task['attempts'][-1]['completed_at'] = utc_now_iso()
    task['attempts'][-1]['error'] = error
    return False, {
        'status': 'task_failed',
        'error': error,
        'attempt_id': attempt_id,
        'tool_result': payload.get('tool_result'),
    }


__all__ = [
    'ensure_agent_runtime_state',
    'append_inbox_event',
    'record_attempt_event',
    'update_working_memory',
    'run_task_tick',
]
