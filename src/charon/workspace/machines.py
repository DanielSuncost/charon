"""Lifecycle machines for workspace records, built on ``charon.orchestration.fsm``.

One ``MachineSpec`` per record type with a lifecycle (work_item, task, lease,
knowledge_item, proposal, gate).  The transition tables are the Workspace contract's
status enums (``records.FSM``); guards and reducers carry the domain rules that the
JS kernel implements inline (evidence-gated ``pass``, terminal timestamps, decision
attribution).  ``WorkspaceStore.transition`` drives these specs through ``fsm.dispatch``
so illegal transitions, terminal absorption and revision conflicts come from the
shared kernel, not from a second engine.

Reducers read a payload of the shape ``{'reason', 'actor', 'ts'}`` supplied by the
store and return the record patch for the target state.
"""
from __future__ import annotations

from typing import Any

from charon.orchestration.fsm import MachineSpec, TransitionContext, TransitionSpec

from .records import FSM, unmet_required_criteria

GUARD_PREFIX = 'GUARD:'


def _payload(ctx: TransitionContext) -> dict[str, Any]:
    return ctx.payload if isinstance(ctx.payload, dict) else {}


def _guard_work_item_pass(ctx: TransitionContext) -> bool | str | None:
    unmet = unmet_required_criteria(ctx.data)
    if unmet:
        return f"{GUARD_PREFIX}work item {ctx.instance_id} cannot pass: {len(unmet)} required criteria unmet — {'; '.join(unmet)}"
    return None


def _reduce_work_item(ctx: TransitionContext) -> dict[str, Any] | None:
    p = _payload(ctx)
    if ctx.target == 'cancelled' and p.get('reason'):
        return {'terminal_reason': p['reason']}
    return None


def _reduce_task(ctx: TransitionContext) -> dict[str, Any] | None:
    p = _payload(ctx)
    if ctx.target in ('succeeded', 'failed', 'cancelled'):
        patch: dict[str, Any] = {'completed_at': p.get('ts')}
        if p.get('reason') and not ctx.data.get('result_summary'):
            patch['result_summary'] = p['reason']
        return patch
    return None


def _reduce_lease(ctx: TransitionContext) -> dict[str, Any] | None:
    p = _payload(ctx)
    if ctx.target in ('released', 'expired', 'revoked'):
        patch: dict[str, Any] = {'released_at': p.get('ts')}
        if p.get('reason'):
            patch['release_reason'] = p['reason']
        return patch
    return None


def _reduce_proposal(ctx: TransitionContext) -> dict[str, Any] | None:
    p = _payload(ctx)
    if ctx.target in ('accepted', 'rejected', 'withdrawn'):
        patch: dict[str, Any] = {'decided_by': p.get('actor'), 'decided_at': p.get('ts')}
        if p.get('reason'):
            patch['decision_reason'] = p['reason']
        return patch
    return None


def _reduce_gate(ctx: TransitionContext) -> dict[str, Any] | None:
    p = _payload(ctx)
    if ctx.target == 'answered':
        return {'decided_by': p.get('actor'), 'decided_at': p.get('ts')}
    return None


_GUARDS = {('work_item', 'pass'): _guard_work_item_pass}
_REDUCERS = {'work_item': _reduce_work_item, 'task': _reduce_task, 'lease': _reduce_lease,
             'proposal': _reduce_proposal, 'gate': _reduce_gate}


def build_spec(record_type: str) -> MachineSpec:
    table = FSM[record_type]
    states = list(table.keys())
    terminal = [state for state, row in table.items() if not row]
    transitions = []
    for source, row in table.items():
        for event, target in row.items():
            transitions.append(TransitionSpec(
                event=event, sources=source, target=target,
                guard=_GUARDS.get((record_type, event)),
                reducer=_REDUCERS.get(record_type),
                metadata={'record_type': record_type},
            ))
    return MachineSpec(
        name=f'workspace.{record_type}', states=states, initial_state=states[0],
        transitions=transitions, terminal_states=terminal, version=1,
        metadata={'record_type': record_type, 'contract': 'workspace.schema.json'},
    )


SPECS: dict[str, MachineSpec] = {t: build_spec(t) for t in FSM}


def spec_for(record_type: str) -> MachineSpec | None:
    return SPECS.get(record_type)


def is_terminal(record_type: str, state: str) -> bool:
    spec = SPECS.get(record_type)
    return bool(spec) and state in spec.terminal_states
