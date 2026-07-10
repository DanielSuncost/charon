#!/usr/bin/env python3
from __future__ import annotations

import json
import re

try:
    from charon.infra.diagnostics import record as _diag
except Exception:  # diagnostics is best-effort and must never block import
    def _diag(*args, **kwargs):
        return None

# ---------------------------------------------------------------------------
# Intent parsing — structured extraction via local LLM
# ---------------------------------------------------------------------------

_PARSE_PROMPT = """\
Extract structured intent from the following user message.

Return ONLY a JSON object with these fields:
- "title": short clean goal title (max 80 chars, fix typos, remove filler)
- "constraints": list of strings — explicit/implied restrictions (e.g. "don't break tests", "Python only", "no new dependencies"). Empty list if none.
- "acceptance_criteria": list of strings — how we know it's done (e.g. "tests pass", "PR is open", "feature works end-to-end"). Empty list if unclear.
- "priority": one of "high", "normal", "low"
- "intent_type": one of "user_intent", "idea", "question"
- "sub_goal_of_active": true if this is clearly a follow-up/sub-task of what the agent is already doing, false otherwise

User message:
{message}

JSON:"""

_JSON_RE = re.compile(r'\{.*\}', re.DOTALL)


def parse_user_intent(text: str, *, llm_adapter=None) -> dict:
    """Parse raw user message into structured goal fields via local LLM.

    Falls back gracefully to a minimal passthrough if the LLM call fails
    or returns unparseable output.
    """
    fallback = {
        'title': str(text or '').strip()[:80],
        'constraints': [],
        'acceptance_criteria': [],
        'priority': 'normal',
        'intent_type': 'user_intent',
        'sub_goal_of_active': False,
    }

    if not text or not text.strip():
        return fallback

    if llm_adapter is None:
        try:
            from charon.providers import llm_adapter as _la
            llm_adapter = _la
        except ImportError:
            return fallback

    prompt = _PARSE_PROMPT.format(message=str(text).strip()[:1000])
    try:
        ok, raw = llm_adapter.query_local_model(prompt, timeout=30)
    except Exception as e:
        _diag('agent_policy', 'intent-parse LLM call failed; using passthrough fallback intent', error=e)
        return fallback

    if not ok or not raw:
        return fallback

    # Extract JSON from response (model may wrap in markdown)
    m = _JSON_RE.search(raw)
    if not m:
        return fallback

    try:
        parsed = json.loads(m.group())
    except Exception as e:
        _diag('agent_policy', 'intent-parse LLM output not valid JSON; using passthrough fallback intent', error=e)
        return fallback

    def _strlist(val) -> list[str]:
        if not val:
            return []
        if isinstance(val, list):
            return [str(v).strip() for v in val if v and str(v).strip()][:10]
        return []

    title = str(parsed.get('title') or '').strip()[:80] or fallback['title']
    priority = parsed.get('priority') or 'normal'
    if priority not in ('high', 'normal', 'low'):
        priority = 'normal'
    intent_type = parsed.get('intent_type') or 'user_intent'
    if intent_type not in ('user_intent', 'idea', 'question'):
        intent_type = 'user_intent'

    return {
        'title': title,
        'constraints': _strlist(parsed.get('constraints')),
        'acceptance_criteria': _strlist(parsed.get('acceptance_criteria')),
        'priority': priority,
        'intent_type': intent_type,
        'sub_goal_of_active': bool(parsed.get('sub_goal_of_active')),
    }


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


def plan_user_intent(intent_text: str, *, project: str, conversation_id: str, goal_id: str, parsed: dict | None = None) -> dict:
    text = str(intent_text or '').strip()
    p = parsed or {}
    constraints = list(p.get('constraints') or [])
    acceptance_criteria = list(p.get('acceptance_criteria') or [])
    priority = p.get('priority') or 'normal'

    # Shade orchestration: enable if complex by length/shape OR if we have
    # structured constraints/criteria that suggest multi-step work
    orchestration_enabled = (
        len(text) >= 220
        or text.count('\n') >= 3
        or len(constraints) >= 2
        or len(acceptance_criteria) >= 2
    )

    expected_outputs = acceptance_criteria if acceptance_criteria else ['Concise result summary']

    return {
        'instruction': text,
        'title': p.get('title') or f'user_intent:{conversation_id}',
        'priority': priority,
        'scope': [],
        'constraints': constraints,
        'expected_outputs': expected_outputs,
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
