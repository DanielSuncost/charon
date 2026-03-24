#!/usr/bin/env python3
from __future__ import annotations


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


def should_delegate_to_shade(task: dict, agent: dict) -> bool:
    if (agent.get('role') or 'charon') != 'charon':
        return False
    if task.get('shade_phase'):
        return False

    pref = task.get('shade_orchestration') or {}
    if pref.get('enabled') is True:
        return True
    if pref.get('enabled') is False:
        return False

    scope_count = len(_normalize_scope(task))
    instruction = str(task.get('instruction') or '')
    constraints = task.get('constraints') or []
    expected_outputs = task.get('expected_outputs') or []
    return scope_count >= 2 or len(instruction) >= 220 or len(constraints) >= 2 or len(expected_outputs) >= 2


def plan_user_intent(intent_text: str, *, project: str, conversation_id: str, goal_id: str) -> dict:
    text = str(intent_text or '').strip()
    orchestration_enabled = len(text) >= 220 or text.count('\n') >= 3
    return {
        'instruction': text,
        'title': f'user_intent:{conversation_id}',
        'priority': 'normal',
        'scope': [],
        'constraints': [],
        'expected_outputs': ['Concise result summary'],
        'phase_plan': [],
        'shade_orchestration': {'enabled': orchestration_enabled},
        'goal_id': goal_id,
        'project': project,
    }


def recovery_decision(task: dict, error: str) -> dict:
    attempts = int(task.get('attempt_count') or 0)
    max_attempts = int(task.get('max_attempts') or 3)
    if attempts < max_attempts:
        return {'action': 'retry', 'reason': 'attempts_remaining'}
    if task.get('shade_orchestration'):
        return {'action': 'branch_or_escalate', 'reason': str(error or '')[:200]}
    return {'action': 'escalate', 'reason': str(error or '')[:200]}


__all__ = [
    'should_delegate_to_shade',
    'plan_user_intent',
    'recovery_decision',
]
