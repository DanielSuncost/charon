"""Clarify tool — structured clarification state for user decisions.

Because tools are synchronous and cannot pause the run for user input directly,
this tool stores pending clarification prompts in state and lets the UI / user
respond later with action=answer.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from charon.tools import ToolContext, ToolResult


CLARIFY_TOOL_DEF = {
    'name': 'Clarify',
    'description': 'Create/read/answer clarification prompts. Actions: ask, list, answer, clear.',
    'input_schema': {
        'type': 'object',
        'properties': {
            'action': {'type': 'string', 'enum': ['ask', 'list', 'answer', 'clear']},
            'question': {'type': 'string'},
            'choices': {'type': 'array', 'items': {'type': 'string'}},
            'clarification_id': {'type': 'string'},
            'answer': {'type': 'string'},
        },
        'required': ['action'],
    },
}


def _state_dir(ctx: ToolContext) -> Path:
    return ctx.state_dir or (ctx.project_root / '.charon_state')


def _path(state_dir: Path) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    return state_dir / 'clarifications.json'


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load(state_dir: Path) -> dict:
    p = _path(state_dir)
    if not p.exists():
        return {'items': []}
    try:
        d = json.loads(p.read_text(encoding='utf-8'))
        if isinstance(d, dict) and isinstance(d.get('items'), list):
            return d
    except Exception:
        pass
    return {'items': []}


def _save(state_dir: Path, data: dict) -> None:
    _path(state_dir).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding='utf-8')


def execute_clarify(params: dict, ctx: ToolContext) -> ToolResult:
    action = str(params.get('action') or '').strip().lower()
    state_dir = _state_dir(ctx)
    data = _load(state_dir)
    items = data.get('items', [])

    try:
        if action == 'ask':
            q = str(params.get('question') or '').strip()
            if not q:
                return ToolResult(content='Error: question is required.', is_error=True)
            choices = [str(c).strip() for c in (params.get('choices') or []) if str(c).strip()][:4]
            cid = f'clar_{uuid.uuid4().hex[:10]}'
            row = {
                'clarification_id': cid,
                'question': q,
                'choices': choices,
                'status': 'pending',
                'asked_by_agent_id': ctx.agent_id,
                'answer': '',
                'created_at': _now_iso(),
                'updated_at': _now_iso(),
            }
            items.append(row)
            _save(state_dir, data)
            lines = [f'Clarification requested: {cid}', f'Q: {q}']
            if choices:
                for i,c in enumerate(choices,1):
                    lines.append(f'{i}. {c}')
            lines.append('Respond with Clarify(action="answer", clarification_id="...", answer="...").')
            return ToolResult(content='\n'.join(lines), details=row)

        if action == 'list':
            pending = [r for r in items if r.get('status') == 'pending']
            if not pending:
                return ToolResult(content='No pending clarifications.', details={'items': []})
            lines = [f'Pending clarifications ({len(pending)}):']
            for r in pending:
                lines.append(f"- {r.get('clarification_id')}: {r.get('question')}")
            return ToolResult(content='\n'.join(lines), details={'items': pending})

        if action == 'answer':
            cid = str(params.get('clarification_id') or '').strip()
            ans = str(params.get('answer') or '').strip()
            if not cid or not ans:
                return ToolResult(content='Error: clarification_id and answer are required.', is_error=True)
            for r in items:
                if r.get('clarification_id') == cid:
                    r['answer'] = ans
                    r['status'] = 'answered'
                    r['updated_at'] = _now_iso()
                    _save(state_dir, data)
                    try:
                        from charon.providers.worker_provider import apply_worker_provider_choice
                        q = str(r.get('question') or '').lower()
                        answer_norm = ans.strip().lower()
                        if 'which provider should i use for worker tasks' in q and answer_norm in ('codex', 'lmstudio'):
                            applied = apply_worker_provider_choice(state_dir, answer_norm)
                            r['applied_at'] = _now_iso()
                            r['applied_result'] = applied
                            _save(state_dir, data)
                    except Exception:
                        pass
                    return ToolResult(content=f'Clarification answered: {cid}', details=r)
            return ToolResult(content=f'Clarification not found: {cid}', is_error=True)

        if action == 'clear':
            data['items'] = []
            _save(state_dir, data)
            return ToolResult(content='Cleared all clarifications.')

        return ToolResult(content=f'Unknown action: {action}', is_error=True)

    except Exception as e:
        return ToolResult(content=f'Clarify tool error: {e}', is_error=True)
