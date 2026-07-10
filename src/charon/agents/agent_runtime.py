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
    except Exception:
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
        except Exception:
            pass

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
        except Exception:
            pass


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
        except Exception:
            pass


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
        except Exception:
            pass


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
        except Exception:
            pass

    # Fallback to JSON
    if not onboarding:
        onboarding_path = state_dir / 'onboarding.json'
        if onboarding_path.exists():
            try:
                onboarding = json.loads(onboarding_path.read_text())
            except Exception:
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

# Cache of conversation engines per agent — preserves context across tasks
_agent_engines: dict[str, 'ConversationEngine'] = {}


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
        except Exception:
            pass

    return build_layered_prompt(
        state_dir=state_dir,
        agent=agent,
        task=task,
        contract=contract,
    )


def _get_or_create_engine(state_dir: Path, agent: dict, task: dict):
    """Get or create a ConversationEngine for an agent.

    Engines are cached per agent_id so conversation context persists
    across multiple tasks within the same daemon lifecycle.
    The system prompt is rebuilt per task so memory stays fresh.
    """
    from charon.providers.provider_bridge import create_provider_and_model
    from charon.conversation.conversation_engine import ConversationEngine

    agent_id = agent.get('id', '')
    project = str(task.get('project') or agent.get('project') or '').strip()
    project_root = Path(project) if project else Path.cwd()
    if not project_root.is_dir():
        project_root = Path.cwd()

    # Build fresh system prompt for this task
    system_prompt = _build_task_system_prompt(state_dir, agent, task)

    # Reuse existing engine if same agent and same project
    cached = _agent_engines.get(agent_id)
    if cached is not None:
        cached_root = str(getattr(cached, 'project_root', ''))
        if cached_root == str(project_root.resolve()):
            # Refresh the system prompt with current memory/goals/coordination
            cached.update_system_prompt(system_prompt)
            return cached, True

    provider, model, ready = create_provider_and_model(state_dir)
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

    _agent_engines[agent_id] = engine
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
            input_tokens=0,
            output_tokens=0,
        )
    except Exception:
        pass


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
            elif event.type == 'done':
                total_turns = event.data.get('total_turns', 0)

    try:
        asyncio.run(_execute())
    except Exception as e:
        errors.append(str(e))

    response_text = ''.join(text_parts).strip()

    # Determine success
    has_fatal_error = any(
        'Connection failed' in e or 'API' in e or 'timed out' in e
        for e in errors
    )

    if has_fatal_error and not response_text:
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

    # Try the unified engine path first
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
