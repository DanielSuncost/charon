#!/usr/bin/env python3
if __package__ in (None, ''):  # launched by file path: make `charon.*` importable
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import argparse
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from charon.agents import intervention_graph as INTERVENTION_GRAPH
from charon.conversation import conversation_index as CONVERSATION_INDEX
from charon.agents import agent_runtime as AGENT_RUNTIME
from charon.agents import agent_lifecycle as AGENT_LIFECYCLE
from charon.providers import llm_adapter as LLM_ADAPTER
from charon.agents import boundary_runtime as BOUNDARY_RUNTIME
from charon.shade import shade_orchestrator as SHADE_ORCH
from charon.agents import agent_policy as AGENT_POLICY
from charon.agents import goal_runtime as GOAL_RUNTIME
from charon.infra import config

try:
    from charon.infra.diagnostics import record as _diag
except Exception:  # diagnostics is best-effort and must never block import
    def _diag(*args, **kwargs):
        return None

# SQLite store adapter (optional)
try:
    from charon.infra.store_adapter import (
        get_db as _get_db,
        task_insert as _db_task_insert,
        task_get as _db_task_get,
        task_update as _db_task_update,
        task_all as _db_task_all,  # noqa: F401 — availability probe: full adapter API must import
        task_pending as _db_task_pending,  # noqa: F401 — availability probe
        run_log_append as _db_run_log_append,
        event_append as _db_event_append,  # noqa: F401 — availability probe
    )
    _HAS_STORE = True
except ImportError:
    _HAS_STORE = False


def _use_store() -> bool:
    return _HAS_STORE and not config.no_sqlite()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


STDOUT_EVENTS = config.stdout_events()
DEBUG_TRACE_ENABLED = config.debug_trace()


def log_event(log_file: Path, event: str, **data):
    log_file.parent.mkdir(parents=True, exist_ok=True)
    rec = {'ts': utc_now_iso(), 'event': event, **data}
    line = json.dumps(rec)
    with log_file.open('a') as f:
        f.write(line + '\n')
    if STDOUT_EVENTS:
        print(line, flush=True)
    if _use_store():
        try:
            state_dir = log_file.parent
            _db_run_log_append(_get_db(state_dir), event, **data)
        except Exception as e:
            _diag('charon_loop', 'run-log mirror to SQLite failed; store misses this event', error=e, event=event)


def trace_event(trace_file: Path | None, event: str, **data):
    if not trace_file:
        return
    trace_file.parent.mkdir(parents=True, exist_ok=True)
    rec = {'ts': utc_now_iso(), 'event': event, **data}
    with trace_file.open('a') as f:
        f.write(json.dumps(rec) + '\n')


def load_queue(queue_file: Path):
    if not queue_file.exists():
        return []
    try:
        return json.loads(queue_file.read_text())
    except Exception as e:
        _diag('charon_loop', 'queue.json unreadable; treating queue as empty this cycle', error=e)
        return []


def save_queue(queue_file: Path, queue):
    queue_file.parent.mkdir(parents=True, exist_ok=True)
    queue_file.write_text(json.dumps(queue, indent=2))


def _sync_task_to_db(state_dir: Path, task: dict) -> None:
    """Best-effort sync of a task dict to SQLite."""
    if not _use_store():
        return
    try:
        db = _get_db(state_dir)
        task_id = task.get('id', '')
        if not task_id:
            return
        existing = _db_task_get(db, task_id)
        if existing:
            # Build update kwargs from the task's known columns
            _known = {
                'title', 'instruction', 'status', 'task_type',
                'owner_agent_id', 'actor_agent_id', 'conversation_id',
                'project', 'priority', 'attempt_count', 'max_attempts',
                'result_summary', 'correlation_id', 'wait_state',
                'created_at', 'updated_at', 'started_at', 'completed_at',
            }
            updates = {}
            for k in _known:
                if k in task:
                    updates[k] = task[k]
            # Also sync extra fields
            for k, v in task.items():
                if k not in _known and k != 'id':
                    updates[k] = v
            if updates:
                _db_task_update(db, task_id, **updates)
        else:
            _db_task_insert(db, dict(task))
    except Exception as e:
        _diag('charon_loop', 'task sync to SQLite failed; store diverges from queue.json', error=e)


def _is_task_stale(task: dict, *, threshold_sec: int) -> bool:
    started = task.get('started_at')
    if not started:
        return True
    try:
        dt = datetime.fromisoformat(started)
    except Exception as e:
        _diag('charon_loop', 'unparseable started_at on in_progress task; treating task as stale', error=e)
        return True
    age = (datetime.now(timezone.utc) - dt).total_seconds()
    return age >= threshold_sec


def recover_stuck_tasks(queue: list[dict], *, stale_after_sec: int) -> int:
    recovered = 0
    for task in queue:
        if task.get('status') != 'in_progress':
            continue
        if task.get('wait_state') == 'waiting_for_shade':
            continue
        if not _is_task_stale(task, threshold_sec=stale_after_sec):
            continue
        task['status'] = 'pending'
        task['updated_at'] = utc_now_iso()
        task['recovered_from_in_progress'] = int(task.get('recovered_from_in_progress') or 0) + 1
        recovered += 1
    return recovered


def _lookup_agent(agent_id: str, state_dir: Path | None = None) -> dict | None:
    if state_dir is not None:
        p = state_dir / 'agents.json'
        if p.exists():
            try:
                rows = json.loads(p.read_text())
            except Exception as e:
                _diag('charon_loop', 'agents.json unreadable; agent lookup degrades to lifecycle registry', error=e)
                rows = []
            for agent in rows:
                if isinstance(agent, dict) and agent.get('id') == agent_id:
                    return agent
    for agent in AGENT_LIFECYCLE.list_agents():
        if agent.get('id') == agent_id:
            return agent
    return None


def _normalize_scope(task: dict) -> set[str]:
    scope = task.get('scope') or []
    if isinstance(scope, str):
        scope = [scope]
    out = set()
    for entry in scope:
        s = str(entry or '').strip().strip('/')
        if s:
            out.add(s)
    return out


def _scope_overlaps(a: set[str], b: set[str]) -> bool:
    if not a or not b:
        return False
    for left in a:
        for right in b:
            if left == right:
                return True
            if left.startswith(right + '/') or right.startswith(left + '/'):
                return True
    return False


def _record_overlap_coordination(task: dict, queue: list[dict], state_dir: Path, log_file: Path) -> None:
    if task.get('task_type') != 'agent_task':
        return
    if task.get('boundary_checked'):
        return

    owner = task.get('owner_agent_id') or task.get('actor_agent_id')
    project = str(task.get('project') or '')
    task_scope = _normalize_scope(task)
    overlaps = []
    for other in queue:
        if other is task:
            continue
        if other.get('task_type') != 'agent_task':
            continue
        if other.get('status') not in ('pending', 'in_progress'):
            continue
        other_owner = other.get('owner_agent_id') or other.get('actor_agent_id')
        if not other_owner or other_owner == owner:
            continue
        if str(other.get('project') or '') != project:
            continue
        other_scope = _normalize_scope(other)
        if _scope_overlaps(task_scope, other_scope):
            overlaps.append({
                'task_id': other.get('id'),
                'owner_agent_id': other_owner,
                'scope': sorted(other_scope),
            })

    task['boundary_checked'] = True
    boundary = task.setdefault('boundary', {})
    if overlaps:
        boundary['status'] = 'proposed'
        boundary['overlap_with'] = [x['task_id'] for x in overlaps]
        boundary['lease_owner'] = owner
        if not boundary.get('lease_expires_at'):
            boundary['lease_expires_at'] = utc_now_iso()

        reason = f"Scope overlap detected with {[x['task_id'] for x in overlaps]}"
        task.setdefault('coordination', []).append({
            'ts': utc_now_iso(),
            'event': 'boundary_proposed',
            'reason': reason,
            'overlap_with': overlaps,
        })
        log_event(
            log_file,
            'task_overlap_detected',
            task_id=task.get('id'),
            owner_agent_id=owner,
            overlap_with=[x['task_id'] for x in overlaps],
            scope=sorted(task_scope),
        )

        if task.get('conversation_id') and owner:
            interventions_file = state_dir / 'interventions.jsonl'
            index_file = state_dir / 'conversation_index.json'
            msg = INTERVENTION_GRAPH.append_message(
                interventions_file,
                conversation_id=task.get('conversation_id'),
                actor_agent_id=owner,
                content=(
                    f"Coordination: proposed boundary for task {task.get('id')} due to overlap "
                    f"with {', '.join(x['task_id'] for x in overlaps)}."
                ),
                branch_label='coordination',
                correlation_id=task.get('correlation_id') or task.get('id'),
            )
            task['coordination_message_id'] = msg.get('message_id')
            CONVERSATION_INDEX.rebuild_index(interventions_file, index_file)
    else:
        boundary.setdefault('status', 'clear')
        boundary.setdefault('overlap_with', [])


def _append_graph_event_for_task(task: dict, state_dir: Path) -> dict | None:
    task_type = task.get('task_type')
    if task_type not in ('agent_message', 'agent_intervention'):
        return None

    interventions_file = state_dir / 'interventions.jsonl'
    index_file = state_dir / 'conversation_index.json'

    conversation_id = task.get('conversation_id')
    actor_agent_id = task.get('actor_agent_id')
    if not conversation_id or not actor_agent_id:
        return None

    message_text = task.get('message') or task.get('instruction') or task.get('title') or ''

    if task_type == 'agent_intervention' or task.get('intervention_of_message_id'):
        target = task.get('intervention_of_message_id')
        if not target:
            return None
        graph_event = INTERVENTION_GRAPH.append_intervention(
            interventions_file,
            conversation_id=conversation_id,
            actor_agent_id=actor_agent_id,
            content=message_text,
            intervention_of_message_id=target,
            parent_message_id=task.get('parent_message_id'),
            correlation_id=task.get('correlation_id'),
        )
    else:
        graph_event = INTERVENTION_GRAPH.append_message(
            interventions_file,
            conversation_id=conversation_id,
            actor_agent_id=actor_agent_id,
            content=message_text,
            parent_message_id=task.get('parent_message_id'),
            branch_label=task.get('branch_label'),
            correlation_id=task.get('correlation_id'),
        )

    task['message_id'] = graph_event.get('message_id')
    CONVERSATION_INDEX.rebuild_index(interventions_file, index_file)
    return graph_event


def _record_agent_task_result_message(task: dict, state_dir: Path, summary: str) -> str | None:
    conversation_id = task.get('conversation_id')
    actor_agent_id = task.get('owner_agent_id') or task.get('actor_agent_id')
    if not conversation_id or not actor_agent_id:
        return None

    interventions_file = state_dir / 'interventions.jsonl'
    index_file = state_dir / 'conversation_index.json'
    graph_event = INTERVENTION_GRAPH.append_message(
        interventions_file,
        conversation_id=conversation_id,
        actor_agent_id=actor_agent_id,
        content=summary,
        parent_message_id=task.get('parent_message_id'),
        branch_label=task.get('branch_label') or 'task-result',
        correlation_id=task.get('correlation_id') or task.get('id'),
    )
    CONVERSATION_INDEX.rebuild_index(interventions_file, index_file)
    return graph_event.get('message_id')


def _append_coordination_message(state_dir: Path, *, conversation_id: str, actor_agent_id: str, content: str, correlation_id: str | None = None) -> str | None:
    if not conversation_id or not actor_agent_id:
        return None
    interventions_file = state_dir / 'interventions.jsonl'
    index_file = state_dir / 'conversation_index.json'
    event = INTERVENTION_GRAPH.append_message(
        interventions_file,
        conversation_id=conversation_id,
        actor_agent_id=actor_agent_id,
        content=content,
        branch_label='coordination',
        correlation_id=correlation_id or conversation_id,
    )
    CONVERSATION_INDEX.rebuild_index(interventions_file, index_file)
    return event.get('message_id')


def _handle_boundary_proposal_task(task: dict, state_dir: Path) -> tuple[bool, dict]:
    proposer = task.get('actor_agent_id')
    target = task.get('target_agent_id')
    if not proposer or not target:
        return False, {'status': 'task_failed', 'error': 'boundary_proposal missing actor/target'}

    proposal = BOUNDARY_RUNTIME.create_proposal(
        state_dir,
        proposer_agent_id=proposer,
        target_agent_id=target,
        project=str(task.get('project') or ''),
        scope=list(task.get('scope') or []),
        reason=str(task.get('reason') or ''),
        source_task_id=task.get('source_task_id'),
        correlation_id=task.get('correlation_id') or task.get('id'),
    )
    AGENT_RUNTIME.append_inbox_event(
        state_dir,
        target,
        'boundary_proposal_received',
        {
            'proposal_id': proposal.get('id'),
            'from_agent_id': proposer,
            'project': proposal.get('project'),
            'scope': proposal.get('scope'),
            'reason': proposal.get('reason'),
        },
    )
    _append_coordination_message(
        state_dir,
        conversation_id=task.get('conversation_id') or f"conv-{proposal.get('id')}",
        actor_agent_id=proposer,
        content=(
            f"Boundary proposal {proposal.get('id')} sent to {target}: "
            f"scope={proposal.get('scope')} reason={proposal.get('reason')}"
        ),
        correlation_id=task.get('correlation_id') or proposal.get('id'),
    )
    return True, {
        'status': 'task_succeeded',
        'summary': f"boundary proposal created: {proposal.get('id')}",
        'proposal_id': proposal.get('id'),
    }


def _handle_boundary_resolution_task(task: dict, state_dir: Path) -> tuple[bool, dict]:
    resolver = task.get('actor_agent_id')
    proposal_id = task.get('proposal_id')
    if not resolver or not proposal_id:
        return False, {'status': 'task_failed', 'error': 'boundary_resolution missing actor/proposal_id'}

    rec = BOUNDARY_RUNTIME.resolve_proposal(
        state_dir,
        proposal_id=proposal_id,
        resolver_agent_id=resolver,
        decision=str(task.get('decision') or ''),
        reason=str(task.get('reason') or ''),
    )
    if not rec:
        return False, {'status': 'task_failed', 'error': f'proposal not found: {proposal_id}'}

    proposer = rec.get('proposer_agent_id')
    if proposer:
        AGENT_RUNTIME.append_inbox_event(
            state_dir,
            proposer,
            'boundary_resolution_received',
            {
                'proposal_id': rec.get('id'),
                'decision': rec.get('status'),
                'resolver_agent_id': resolver,
                'reason': rec.get('resolution_reason'),
            },
        )

    _append_coordination_message(
        state_dir,
        conversation_id=task.get('conversation_id') or f"conv-{proposal_id}",
        actor_agent_id=resolver,
        content=(
            f"Boundary proposal {proposal_id} {rec.get('status')} by {resolver}. "
            f"reason={rec.get('resolution_reason') or '-'}"
        ),
        correlation_id=task.get('correlation_id') or proposal_id,
    )
    return True, {
        'status': 'task_succeeded',
        'summary': f"boundary {proposal_id} {rec.get('status')}",
        'proposal_id': proposal_id,
    }


def _should_delegate_to_shade(task: dict, agent: dict) -> bool:
    return bool(AGENT_POLICY.should_delegate_to_shade(task, agent))



def _new_task_id(prefix: str = 'task') -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _enqueue_shade_phase_task(queue: list[dict], *, contract: dict, phase: dict, parent_task: dict, owner_agent_id: str) -> dict:
    now = utc_now_iso()
    task_id = _new_task_id('task')
    child = {
        'id': task_id,
        'title': f"shade_phase:{contract.get('id')}:{phase.get('phase_id')}",
        'instruction': SHADE_ORCH.build_phase_instruction(contract, phase),
        'status': 'pending',
        'task_type': 'agent_task',
        'owner_agent_id': owner_agent_id,
        'actor_agent_id': owner_agent_id,
        'conversation_id': parent_task.get('conversation_id') or f"conv-{task_id}",
        'project': parent_task.get('project'),
        'priority': parent_task.get('priority') or 'normal',
        'created_at': now,
        'updated_at': now,
        'attempt_count': 0,
        'max_attempts': int((parent_task.get('shade_orchestration') or {}).get('max_phase_attempts') or 2),
        'result_summary': None,
        'scope': list(contract.get('scope') or parent_task.get('scope') or []),
        'deps': [],
        'correlation_id': f"{parent_task.get('correlation_id') or parent_task.get('id')}:{phase.get('phase_id')}",
        'constraints': list(contract.get('constraints') or []),
        'expected_outputs': list(contract.get('expected_outputs') or []),
        'phase_plan': [],
        'shade_phase': {
            'contract_id': contract.get('id'),
            'phase_id': phase.get('phase_id'),
            'phase_lookup_key': phase.get('lookup_key'),
            'parent_task_id': parent_task.get('id'),
            'parent_agent_id': parent_task.get('owner_agent_id') or parent_task.get('actor_agent_id'),
            'shade_agent_id': owner_agent_id,
        },
        'boundary': {
            'status': 'unclaimed',
            'lease_owner': owner_agent_id,
            'lease_expires_at': None,
            'overlap_with': [],
        },
    }
    queue.append(child)
    return child


def _find_tasks_for_phase(queue: list[dict], *, contract_id: str, phase_id: str) -> list[dict]:
    rows = []
    for t in queue:
        meta = t.get('shade_phase') or {}
        if meta.get('contract_id') == contract_id and meta.get('phase_id') == phase_id:
            rows.append(t)
    return rows


def _tick_shade_contract(task: dict, state_dir: Path, queue: list[dict], *, parent_agent: dict) -> tuple[bool, dict]:
    orchestration = task.setdefault('shade_orchestration', {})
    contract_id = orchestration.get('contract_id')

    if not contract_id:
        require_tmux = config.shade_require_tmux()
        shade = AGENT_LIFECYCLE.create_agent(
            name='',
            mode='temp',
            goal=f"Shade contract for task {task.get('id')}",
            project=task.get('project') or parent_agent.get('project'),
            role='shade',
            visibility='internal',
            parent_agent_id=parent_agent.get('id'),
            require_tmux=require_tmux,
        )
        contract = SHADE_ORCH.create_contract(
            state_dir,
            parent_task_id=task.get('id'),
            parent_agent_id=parent_agent.get('id'),
            shade_agent_id=shade.get('id'),
            conversation_id=task.get('conversation_id') or f"conv-{task.get('id')}",
            project=task.get('project') or parent_agent.get('project') or '',
            goal=task.get('instruction') or task.get('title') or '',
            constraints=task.get('constraints') or [],
            expected_outputs=task.get('expected_outputs') or [],
            scope=task.get('scope') or [],
            phase_specs=task.get('phase_plan') or None,
        )
        orchestration.update(
            {
                'enabled': True,
                'contract_id': contract.get('id'),
                'shade_agent_id': shade.get('id'),
                'status': 'running',
                'created_at': utc_now_iso(),
            }
        )
        AGENT_RUNTIME.append_inbox_event(
            state_dir,
            parent_agent.get('id'),
            'shade_contract_created',
            {
                'task_id': task.get('id'),
                'contract_id': contract.get('id'),
                'shade_agent_id': shade.get('id'),
            },
        )
        contract_id = contract.get('id')

    contract = SHADE_ORCH.get_contract(state_dir, contract_id)
    if not contract:
        return False, {'status': 'task_failed', 'error': f'shade contract not found: {contract_id}'}

    if contract.get('status') == 'failed':
        orchestration['status'] = 'failed'
        return False, {'status': 'task_failed', 'error': contract.get('last_error') or SHADE_ORCH.summarize_contract(contract)}

    if contract.get('status') == 'completed':
        orchestration['status'] = 'completed'
        return True, {'status': 'task_succeeded', 'summary': SHADE_ORCH.summarize_contract(contract)}

    phase = SHADE_ORCH.next_pending_phase(contract)
    if not phase:
        for cand in (contract.get('phases') or []):
            if cand.get('status') == 'queued':
                phase = cand
                break
    if not phase:
        phases = contract.get('phases') or []
        if phases and all(p.get('status') == 'completed' for p in phases):
            contract['status'] = 'completed'
            contract['completed_at'] = utc_now_iso()
            contracts = SHADE_ORCH.load_contracts(state_dir)
            for idx, rec in enumerate(contracts):
                if rec.get('id') == contract.get('id'):
                    contracts[idx] = contract
                    SHADE_ORCH.save_contracts(state_dir, contracts)
                    break
            orchestration['status'] = 'completed'
            return True, {'status': 'task_succeeded', 'summary': SHADE_ORCH.summarize_contract(contract)}
        orchestration['status'] = contract.get('status') or 'running'
        return False, {'status': 'task_failed', 'error': 'no pending phase but contract not completed'}

    phase_tasks = _find_tasks_for_phase(queue, contract_id=contract.get('id'), phase_id=phase.get('phase_id'))
    failed = [t for t in phase_tasks if t.get('status') == 'failed']
    if failed:
        last = failed[-1]
        err = str(last.get('result_summary') or (last.get('last_error') or {}).get('error') or 'phase failed')
        SHADE_ORCH.mark_phase_failed(state_dir, contract.get('id'), phase.get('phase_id'), task_id=last.get('id'), error=err)
        orchestration['status'] = 'failed'
        return False, {'status': 'task_failed', 'error': f"{phase.get('phase_id')} failed: {err}"}

    completed = [t for t in phase_tasks if t.get('status') == 'completed']
    if completed:
        last = completed[-1]
        summary = str(last.get('result_summary') or 'phase complete')
        SHADE_ORCH.mark_phase_completed(state_dir, contract.get('id'), phase.get('phase_id'), task_id=last.get('id'), summary=summary)
        contract2 = SHADE_ORCH.get_contract(state_dir, contract.get('id')) or contract
        orchestration['status'] = contract2.get('status')
        if contract2.get('status') == 'completed':
            return True, {'status': 'task_succeeded', 'summary': SHADE_ORCH.summarize_contract(contract2)}
        return True, {'status': 'task_deferred', 'summary': SHADE_ORCH.summarize_contract(contract2)}

    active = [t for t in phase_tasks if t.get('status') in ('pending', 'in_progress')]
    if active:
        return True, {'status': 'task_deferred', 'summary': f"waiting for {phase.get('phase_id')}"}

    child = _enqueue_shade_phase_task(
        queue,
        contract=contract,
        phase=phase,
        parent_task=task,
        owner_agent_id=contract.get('shade_agent_id'),
    )
    SHADE_ORCH.mark_phase_queued(state_dir, contract.get('id'), phase.get('phase_id'), child.get('id'))
    AGENT_RUNTIME.append_inbox_event(
        state_dir,
        parent_agent.get('id'),
        'shade_phase_queued',
        {
            'task_id': task.get('id'),
            'contract_id': contract.get('id'),
            'phase_id': phase.get('phase_id'),
            'shade_task_id': child.get('id'),
        },
    )
    return True, {'status': 'task_deferred', 'summary': f"queued shade phase {phase.get('phase_id')}"}


def _wake_parent_task_after_shade_update(queue: list[dict], child_task: dict) -> bool:
    shade_meta = child_task.get('shade_phase') or {}
    parent_task_id = shade_meta.get('parent_task_id')
    if not parent_task_id:
        return False
    for t in queue:
        if t.get('id') != parent_task_id:
            continue
        if t.get('status') == 'in_progress' and t.get('wait_state') == 'waiting_for_shade':
            t['status'] = 'pending'
            t['updated_at'] = utc_now_iso()
            t.pop('started_at', None)
            t.pop('wait_state', None)
            return True
    return False


def _spawn_agent_task_from_intent(task: dict, state_dir: Path, queue: list[dict], agent: dict, trace_file: Path | None = None) -> dict:
    project = task.get('project') or agent.get('project') or ''
    session_id = task.get('session_id') or f"session-{agent.get('id')}"
    conversation_id = task.get('conversation_id') or f"conv-{task.get('id')}"

    trace_event(
        trace_file,
        'user_intent_ingest_start',
        task_id=task.get('id'),
        owner_agent_id=agent.get('id'),
        session_id=session_id,
        conversation_id=conversation_id,
    )

    goal_meta = GOAL_RUNTIME.ingest_user_intent(
        state_dir,
        agent_id=agent.get('id'),
        project=project,
        session_id=session_id,
        conversation_id=conversation_id,
        text=task.get('message') or task.get('instruction') or task.get('title') or '',
    )
    plan = AGENT_POLICY.plan_user_intent(
        task.get('message') or task.get('instruction') or '',
        project=project,
        conversation_id=conversation_id,
        goal_id=goal_meta['goal']['goal_id'],
    )

    child = {
        'id': _new_task_id('task'),
        'title': plan.get('title') or f"agent_task:{agent.get('id')}",
        'instruction': plan.get('instruction') or (task.get('message') or ''),
        'status': 'pending',
        'task_type': 'agent_task',
        'owner_agent_id': agent.get('id'),
        'actor_agent_id': agent.get('id'),
        'conversation_id': conversation_id,
        'project': project,
        'priority': plan.get('priority') or 'normal',
        'created_at': utc_now_iso(),
        'updated_at': utc_now_iso(),
        'attempt_count': 0,
        'max_attempts': 3,
        'result_summary': None,
        'scope': list(plan.get('scope') or []),
        'deps': [],
        'correlation_id': task.get('correlation_id') or task.get('id'),
        'constraints': list(plan.get('constraints') or []),
        'expected_outputs': list(plan.get('expected_outputs') or []),
        'phase_plan': list(plan.get('phase_plan') or []),
        'shade_orchestration': dict(plan.get('shade_orchestration') or {}),
        'goal_ref': {
            'goal_id': goal_meta['goal']['goal_id'],
            'project_id': goal_meta['project_id'],
            'session_id': goal_meta['session_id'],
        },
        'boundary': {
            'status': 'unclaimed',
            'lease_owner': agent.get('id'),
            'lease_expires_at': None,
            'overlap_with': [],
        },
    }
    queue.append(child)
    GOAL_RUNTIME.attach_task(
        state_dir,
        project_id=goal_meta['project_id'],
        session_id=goal_meta['session_id'],
        goal_id=goal_meta['goal']['goal_id'],
        task_id=child['id'],
    )
    packet = GOAL_RUNTIME.build_context_packet(
        state_dir,
        agent_id=agent.get('id'),
        project_id=goal_meta['project_id'],
        session_id=goal_meta['session_id'],
    )
    task['spawned_task_id'] = child['id']
    task['goal_ref'] = child['goal_ref']
    task['context_packet_at_spawn'] = packet.get('updated_at')

    trace_event(
        trace_file,
        'user_intent_spawned_agent_task',
        intent_task_id=task.get('id'),
        spawned_task_id=child.get('id'),
        goal_id=goal_meta['goal']['goal_id'],
        project_id=goal_meta['project_id'],
        session_id=goal_meta['session_id'],
    )
    return child


def _update_goal_context_from_task(task: dict, state_dir: Path, *, summary: str, status: str, trace_file: Path | None = None) -> None:
    ref = task.get('goal_ref') or {}
    goal_id = ref.get('goal_id')
    project_id = ref.get('project_id')
    session_id = ref.get('session_id')
    agent_id = task.get('owner_agent_id') or task.get('actor_agent_id')
    if not (goal_id and project_id and session_id and agent_id):
        trace_event(
            trace_file,
            'goal_context_update_skipped',
            task_id=task.get('id'),
            has_goal_ref=bool(ref),
            has_agent_id=bool(agent_id),
            status=status,
        )
        return
    GOAL_RUNTIME.record_result(
        state_dir,
        project_id=project_id,
        session_id=session_id,
        goal_id=goal_id,
        summary=summary,
        status=status,
    )
    GOAL_RUNTIME.build_context_packet(
        state_dir,
        agent_id=agent_id,
        project_id=project_id,
        session_id=session_id,
    )
    trace_event(
        trace_file,
        'goal_context_updated',
        task_id=task.get('id'),
        goal_id=goal_id,
        status=status,
    )


def process_task(task: dict, state_dir: Path, queue: list[dict], trace_file: Path | None = None):
    task_type = task.get('task_type')
    trace_event(trace_file, 'process_task_start', task_id=task.get('id'), task_type=task_type)

    if task_type == 'user_intent':
        owner_agent_id = task.get('owner_agent_id') or task.get('actor_agent_id')
        if not owner_agent_id:
            trace_event(trace_file, 'process_task_error', task_id=task.get('id'), error='user_intent missing owner_agent_id')
            return False, {'status': 'task_failed', 'error': 'user_intent missing owner_agent_id'}
        agent = _lookup_agent(owner_agent_id, state_dir)
        if not agent:
            trace_event(trace_file, 'process_task_error', task_id=task.get('id'), error=f'agent not found: {owner_agent_id}')
            return False, {'status': 'task_failed', 'error': f"agent not found: {owner_agent_id}"}
        child = _spawn_agent_task_from_intent(task, state_dir, queue, agent, trace_file)
        trace_event(trace_file, 'process_task_user_intent_completed', task_id=task.get('id'), spawned_task_id=child.get('id'))
        return True, {'status': 'task_succeeded', 'summary': f"intent accepted; spawned {child.get('id')}"}

    if task_type == 'agent_task':
        owner_agent_id = task.get('owner_agent_id') or task.get('actor_agent_id')
        if not owner_agent_id:
            trace_event(trace_file, 'process_task_error', task_id=task.get('id'), error='agent_task missing owner_agent_id')
            return False, {'status': 'task_failed', 'error': 'agent_task missing owner_agent_id'}

        agent = _lookup_agent(owner_agent_id, state_dir)
        if not agent:
            trace_event(trace_file, 'process_task_error', task_id=task.get('id'), error=f'agent not found: {owner_agent_id}')
            return False, {'status': 'task_failed', 'error': f'agent not found: {owner_agent_id}'}
        if agent.get('status') == 'stopped':
            trace_event(trace_file, 'process_task_error', task_id=task.get('id'), error=f'agent is stopped: {owner_agent_id}')
            return False, {'status': 'task_failed', 'error': f'agent is stopped: {owner_agent_id}'}

        if _should_delegate_to_shade(task, agent):
            trace_event(trace_file, 'process_task_delegate_to_shade', task_id=task.get('id'), owner_agent_id=owner_agent_id)
            return _tick_shade_contract(task, state_dir, queue, parent_agent=agent)

        trace_event(trace_file, 'process_task_local_execution', task_id=task.get('id'), owner_agent_id=owner_agent_id)
        ok, result = AGENT_RUNTIME.run_task_tick(
            state_dir,
            task,
            agent=agent,
            llm_adapter=LLM_ADAPTER,
        )
        if ok:
            summary = result.get('summary') or 'task completed'
            task['result_summary'] = summary
            message_id = _record_agent_task_result_message(task, state_dir, summary)
            trace_event(trace_file, 'process_task_agent_success', task_id=task.get('id'), message_id=message_id, attempt_id=result.get('attempt_id'))
            return True, {
                'status': 'task_succeeded',
                'summary': summary,
                'message_id': message_id,
                'graph_event_type': 'agent_message' if message_id else None,
                'attempt_id': result.get('attempt_id'),
            }
        trace_event(trace_file, 'process_task_agent_failed', task_id=task.get('id'), error=result.get('error'), attempt_id=result.get('attempt_id'))
        return False, {
            'status': 'task_failed',
            'error': result.get('error') or 'agent task failed',
            'attempt_id': result.get('attempt_id'),
            'tool_result': result.get('tool_result'),
        }

    if task_type == 'boundary_proposal':
        trace_event(trace_file, 'process_task_boundary_proposal', task_id=task.get('id'))
        return _handle_boundary_proposal_task(task, state_dir)

    if task_type == 'boundary_resolution':
        trace_event(trace_file, 'process_task_boundary_resolution', task_id=task.get('id'))
        return _handle_boundary_resolution_task(task, state_dir)

    graph_event = _append_graph_event_for_task(task, state_dir)

    # F00+ baseline fallback for non-agent_task work items.
    task['status'] = 'completed'
    task['completed_at'] = utc_now_iso()
    result = {
        'status': 'simulated_success',
        'message_id': task.get('message_id'),
        'graph_event_type': graph_event.get('event_type') if graph_event else None,
    }
    trace_event(trace_file, 'process_task_fallback_completed', task_id=task.get('id'), graph_event_type=result.get('graph_event_type'))
    return True, result


def ensure_bootstrap_queue(queue_file: Path):
    if queue_file.exists():
        return
    save_queue(queue_file, [
        {'id': 'bootstrap-1', 'title': 'Create foundational event schema', 'status': 'pending'},
        {'id': 'bootstrap-2', 'title': 'Add test harness scaffold', 'status': 'pending'},
    ])


def run_loop(state_dir: Path, stop_file: Path, max_consecutive_failures: int, sleep_sec: float, max_cycles: int = 0, debug_trace: bool = False):
    queue_file = state_dir / 'queue.json'
    log_file = state_dir / 'run.log'
    trace_file = state_dir / 'debug.log' if debug_trace else None

    state_dir.mkdir(parents=True, exist_ok=True)
    ensure_bootstrap_queue(queue_file)

    # Start fleet sync and memory threads (optional — graceful if fleet not configured)
    try:
        from charon.fleet.fleet_sync import start_fleet_sync
        start_fleet_sync()
    except Exception as e:
        _diag('charon_loop', 'fleet sync failed to start; fleet state will not synchronize', error=e)
    try:
        from charon.fleet.fleet_memory import start_fleet_memory
        start_fleet_memory()
    except Exception as e:
        _diag('charon_loop', 'fleet memory failed to start; cross-fleet memory disabled', error=e)

    stale_in_progress_sec = config.stale_in_progress_sec()

    consec_fail = 0
    cycles = 0
    loop_start_time = time.time()
    last_heartbeat_cycle = 0
    heartbeat_interval = config.heartbeat_interval()  # cycles between heartbeats
    log_event(log_file, 'loop_start', state_dir=str(state_dir), stop_file=str(stop_file), debug_trace=bool(debug_trace))
    trace_event(trace_file, 'loop_start_trace', state_dir=str(state_dir), stop_file=str(stop_file))

    while True:
        trace_event(trace_file, 'loop_cycle_begin', cycle=cycles)

        # Heartbeat: emit timing info every N cycles
        if cycles - last_heartbeat_cycle >= heartbeat_interval:
            uptime = time.time() - loop_start_time
            log_event(log_file, 'heartbeat', cycle=cycles, uptime_seconds=round(uptime, 1))
            trace_event(trace_file, 'heartbeat', cycle=cycles, uptime_seconds=round(uptime, 1))
            last_heartbeat_cycle = cycles

            # User model consolidation check on heartbeat
            try:
                from charon.memory.consolidation import load_config as _load_consol_config, should_run as _consol_should_run, run_consolidation as _run_consolidation
                consol_config = _load_consol_config(state_dir)
                consol_interval = consol_config.get('scan_interval_heartbeats', 50)
                if consol_config.get('enabled', True) and cycles % (consol_interval * heartbeat_interval) < heartbeat_interval:
                    if _consol_should_run(state_dir, consol_config):
                        trace_event(trace_file, 'consolidation_start', cycle=cycles)
                        result = _run_consolidation(state_dir, consol_config)
                        change_count = len(result.get('changes', []))
                        log_event(log_file, 'consolidation_complete',
                                  changes=change_count,
                                  events_processed=result.get('events_processed', 0),
                                  duration_ms=result.get('duration_ms', 0),
                                  error=result.get('error'))
                        trace_event(trace_file, 'consolidation_complete',
                                    cycle=cycles, changes=change_count,
                                    error=result.get('error'))
            except Exception as e:
                _diag('charon_loop', 'user-model consolidation heartbeat failed; consolidation skipped', error=e)  # Consolidation is best-effort

            # Soft specialization refresh on heartbeat
            try:
                from charon.agents.soft_specialization import refresh_all_agents as _refresh_specs
                # Only run every few heartbeats (controlled by CHARON_SPEC_INTERVAL)
                updated = _refresh_specs(state_dir, mode='heuristic')
                if updated:
                    for aid, label in updated.items():
                        log_event(log_file, 'specialization_updated', agent_id=aid, label=label)
                        trace_event(trace_file, 'specialization_updated', cycle=cycles, agent_id=aid, label=label)
            except Exception as e:
                _diag('charon_loop', 'soft specialization refresh failed; agent labels not updated', error=e)  # Specialization is best-effort

            # Judge-loop driver: advance running optimization loops one step
            try:
                from charon.judge.judge_loop_driver import tick_judge_loops
                for ev in tick_judge_loops(state_dir, max_loops=1):
                    if ev.get('action') in ('skipped',):
                        continue
                    log_event(log_file, 'judge_loop_tick', **ev)
                    trace_event(trace_file, 'judge_loop_tick', cycle=cycles, **ev)
            except Exception as e:
                _diag('charon_loop', 'judge-loop tick failed; optimization loops not advanced', error=e)  # Judge loops are best-effort

            # Durable orchestration runtime: advance any running durable operations
            # one step each. This is what makes them crash-resumable — a restarted
            # daemon reloads persisted state and continues. No-op when nothing is
            # registered/runnable, so it is safe to always call.
            try:
                from charon.orchestration.runtime import tick_operations
                for ev in tick_operations(state_dir, max_ops=8):
                    if ev.get('action') in ('skipped', 'deferred'):
                        continue
                    log_event(log_file, 'orchestration_tick', **ev)
                    trace_event(trace_file, 'orchestration_tick', cycle=cycles, **ev)
            except Exception as e:
                _diag('charon_loop', 'orchestration tick failed; durable operations not advanced', error=e)

        if stop_file.exists():
            log_event(log_file, 'loop_stop_file_detected', stop_file=str(stop_file))
            trace_event(trace_file, 'loop_stop_file_detected', stop_file=str(stop_file))
            break

        if max_cycles and cycles >= max_cycles:
            log_event(log_file, 'loop_halt', reason='max_cycles_reached', max_cycles=max_cycles)
            trace_event(trace_file, 'loop_halt_max_cycles', cycle=cycles, max_cycles=max_cycles)
            break

        queue = load_queue(queue_file)
        trace_event(trace_file, 'queue_loaded', cycle=cycles, queue_size=len(queue))
        recovered = recover_stuck_tasks(queue, stale_after_sec=stale_in_progress_sec)
        if recovered:
            save_queue(queue_file, queue)
            log_event(log_file, 'queue_recovered', recovered_count=recovered)
            trace_event(trace_file, 'queue_recovered', cycle=cycles, recovered_count=recovered)

        now_iso = utc_now_iso()
        pending = [t for t in queue if t.get('status') == 'pending'
                   and (not t.get('not_before') or t.get('not_before') <= now_iso)]
        if not pending:
            # Autonomous mode: self-assign from confirmed goals
            auto_task = None
            try:
                from charon.agents.autonomous import load_autonomous_config, self_assign_next_task
                auto_config = load_autonomous_config(state_dir)
                if auto_config.get('enabled'):
                    # Find agent for this loop
                    agents_file = state_dir / 'agents.json'
                    if agents_file.exists():
                        for a in json.loads(agents_file.read_text()):
                            if isinstance(a, dict) and a.get('role') == 'charon' and a.get('status') == 'running':
                                auto_task = self_assign_next_task(
                                    state_dir, agent_id=a.get('id', ''),
                                    project=a.get('project', ''), config=auto_config,
                                )
                                if auto_task:
                                    queue.append(auto_task)
                                    save_queue(queue_file, queue)
                                    log_event(log_file, 'autonomous_task_created',
                                              task_id=auto_task.get('id'),
                                              task_type=auto_task.get('task_type'),
                                              goal_ref=auto_task.get('goal_ref'))
                                    trace_event(trace_file, 'autonomous_task_created',
                                                cycle=cycles, task_id=auto_task.get('id'))
                                break
            except Exception as e:
                _diag('charon_loop', 'autonomous self-assignment failed; no task self-assigned this cycle', error=e)

            if not auto_task:
                log_event(log_file, 'loop_idle', reason='no_pending_tasks')
                trace_event(trace_file, 'loop_idle', cycle=cycles, reason='no_pending_tasks')
                time.sleep(sleep_sec)
                cycles += 1
                continue

            pending = [auto_task]

        task = pending[0]
        trace_event(trace_file, 'task_selected', cycle=cycles, task_id=task.get('id'), task_type=task.get('task_type'))
        _record_overlap_coordination(task, queue, state_dir, log_file)
        task['status'] = 'in_progress'
        task['started_at'] = utc_now_iso()
        task['updated_at'] = utc_now_iso()
        task['attempt_count'] = int(task.get('attempt_count') or 0) + 1
        save_queue(queue_file, queue)
        log_event(log_file, 'task_start', task_id=task.get('id'), title=task.get('title'), attempt_count=task.get('attempt_count'))
        trace_event(trace_file, 'task_started', task_id=task.get('id'), attempt_count=task.get('attempt_count'))

        ok, result = process_task(task, state_dir, queue, trace_file=trace_file)
        trace_event(trace_file, 'task_processed', task_id=task.get('id'), ok=bool(ok), result_status=(result or {}).get('status') if isinstance(result, dict) else None)
        if ok:
            status = (result or {}).get('status') if isinstance(result, dict) else None
            if status == 'task_deferred':
                consec_fail = 0
                task['status'] = 'in_progress'
                task['wait_state'] = 'waiting_for_shade'
                task['updated_at'] = utc_now_iso()
                task['attempt_count'] = max(0, int(task.get('attempt_count') or 1) - 1)
                save_queue(queue_file, queue)
                log_event(log_file, 'task_deferred', task_id=task.get('id'), result=result)
                trace_event(trace_file, 'task_deferred_waiting_for_shade', task_id=task.get('id'))
            else:
                consec_fail = 0
                task['status'] = 'completed'
                task['completed_at'] = utc_now_iso()
                task['updated_at'] = utc_now_iso()
                task.pop('wait_state', None)
                if isinstance(result, dict) and result.get('summary'):
                    task['result_summary'] = result.get('summary')
                    _update_goal_context_from_task(task, state_dir, summary=str(result.get('summary')), status='completed', trace_file=trace_file)
                woke = _wake_parent_task_after_shade_update(queue, task)
                save_queue(queue_file, queue)
                log_event(log_file, 'task_success', task_id=task.get('id'), result=result)
                trace_event(trace_file, 'task_marked_completed', task_id=task.get('id'))

                # Re-enqueue recurring tasks
                interval = task.get('interval_minutes')
                if interval and isinstance(interval, (int, float)) and interval > 0:
                    from datetime import timedelta
                    next_run = datetime.now(timezone.utc) + timedelta(minutes=interval)
                    recurring_copy = {
                        'id': f"{task.get('id').split('-')[0]}-{uuid.uuid4().hex[:8]}",
                        'title': task.get('title', ''),
                        'instruction': task.get('instruction', ''),
                        'status': 'pending',
                        'task_type': task.get('task_type', ''),
                        'owner_agent_id': task.get('owner_agent_id'),
                        'actor_agent_id': task.get('actor_agent_id'),
                        'project': task.get('project'),
                        'priority': task.get('priority', 'normal'),
                        'created_at': utc_now_iso(),
                        'updated_at': utc_now_iso(),
                        'attempt_count': 0,
                        'max_attempts': int(task.get('max_attempts') or 3),
                        'interval_minutes': interval,
                        'not_before': next_run.isoformat(),
                        'correlation_id': task.get('correlation_id'),
                    }
                    queue.append(recurring_copy)
                    save_queue(queue_file, queue)
                    log_event(log_file, 'recurring_task_enqueued',
                              task_id=recurring_copy['id'],
                              next_run=recurring_copy['not_before'],
                              interval_minutes=interval)
                    trace_event(trace_file, 'recurring_task_enqueued',
                                cycle=cycles, task_id=recurring_copy['id'])
                if woke:
                    log_event(log_file, 'shade_parent_woken', parent_task_id=(task.get('shade_phase') or {}).get('parent_task_id'), child_task_id=task.get('id'))
                if isinstance(result, dict) and result.get('message_id'):
                    log_event(
                        log_file,
                        'task_graph_recorded',
                        task_id=task.get('id'),
                        message_id=result.get('message_id'),
                        graph_event_type=result.get('graph_event_type'),
                        conversation_id=task.get('conversation_id'),
                    )
        else:
            consec_fail += 1
            max_attempts = int(task.get('max_attempts') or 3)
            task['last_error'] = result
            task['updated_at'] = utc_now_iso()
            woke = _wake_parent_task_after_shade_update(queue, task)
            if int(task.get('attempt_count') or 0) >= max_attempts:
                task['status'] = 'failed'
                task['completed_at'] = utc_now_iso()
                task['result_summary'] = (result or {}).get('error') if isinstance(result, dict) else str(result)
                _update_goal_context_from_task(task, state_dir, summary=str(task.get('result_summary') or ''), status='failed', trace_file=trace_file)
                save_queue(queue_file, queue)
                log_event(log_file, 'task_failed_terminal', task_id=task.get('id'), error=result, attempt_count=task.get('attempt_count'))
                trace_event(trace_file, 'task_marked_failed', task_id=task.get('id'), attempt_count=task.get('attempt_count'))
            else:
                task['status'] = 'pending'
                save_queue(queue_file, queue)
                log_event(
                    log_file,
                    'task_failure',
                    task_id=task.get('id'),
                    error=result,
                    consec_fail=consec_fail,
                    attempt_count=task.get('attempt_count'),
                    max_attempts=max_attempts,
                )
                trace_event(trace_file, 'task_requeued_after_failure', task_id=task.get('id'), attempt_count=task.get('attempt_count'), max_attempts=max_attempts)
            if woke:
                log_event(log_file, 'shade_parent_woken', parent_task_id=(task.get('shade_phase') or {}).get('parent_task_id'), child_task_id=task.get('id'))

            if consec_fail >= max_consecutive_failures:
                log_event(
                    log_file,
                    'loop_halt',
                    reason='max_consecutive_failures',
                    max_consecutive_failures=max_consecutive_failures,
                )
                trace_event(trace_file, 'loop_halt_max_consecutive_failures', consec_fail=consec_fail, max_consecutive_failures=max_consecutive_failures)
                break

        time.sleep(sleep_sec)
        cycles += 1

    log_event(log_file, 'loop_exit')
    trace_event(trace_file, 'loop_exit', cycles=cycles)


def parse_args():
    p = argparse.ArgumentParser(description='Charon F00 persistent loop runner')
    env_state_dir = config.state_dir()
    p.add_argument('--state-dir', default=str(env_state_dir) if env_state_dir else './.charon_state')
    p.add_argument('--stop-file', default=config.stop_file())
    p.add_argument('--max-consecutive-failures', type=int, default=config.max_consec_fail())
    p.add_argument('--sleep-sec', type=float, default=config.loop_sleep())
    p.add_argument('--max-cycles', type=int, default=config.max_cycles(),
                   help='0 means run indefinitely')
    p.add_argument('--debug-trace', action='store_true', default=DEBUG_TRACE_ENABLED,
                   help='Write high-volume JSONL trace to <state-dir>/debug.log')
    return p.parse_args()


def main():
    args = parse_args()
    run_loop(
        state_dir=Path(args.state_dir),
        stop_file=Path(args.stop_file),
        max_consecutive_failures=args.max_consecutive_failures,
        sleep_sec=args.sleep_sec,
        max_cycles=args.max_cycles,
        debug_trace=args.debug_trace,
    )


if __name__ == '__main__':
    main()
