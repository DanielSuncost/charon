"""Refine tool — judged, evidence-backed self-refinement of Charon's own skills.

Wraps the existing judge-loop engine (implement -> judge -> keep/rollback) so an
agent can propose an edit to one of its own procedural-memory skills (SKILL.md),
grounded in cited trajectory evidence, and have an independent LLM judge score
the result before it's kept. Regressions or unsupported edits are auto-rolled
back by the underlying judge loop's shadow-git checkpoints (CheckpointManager) —
nothing is kept on the implementer's say-so alone.

Actions: propose, status, list.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from charon.tools import ToolContext, ToolResult
from charon.tools.skills_tool import _skills_root, _validate_name

try:
    from charon.infra.diagnostics import record as _diag
except Exception:  # diagnostics is best-effort and must never block import
    def _diag(*args, **kwargs):
        return None


REFINE_TOOL_DEF = {
    'name': 'Refine',
    'description': (
        'Propose a judged, evidence-backed edit to one of your own skills (procedural '
        'memory). Unlike Skills (raw CRUD), Refine requires citing concrete trajectory '
        'evidence for why the change is needed, then runs it as a judged loop: an '
        'independent LLM judge scores the edit against that evidence, and the change is '
        'auto-rolled-back unless it actually improves on the baseline. Use this for '
        'self-improvement claims you want checked, not just recorded.'
    ),
    'input_schema': {
        'type': 'object',
        'properties': {
            'action': {'type': 'string', 'enum': ['propose', 'status', 'list']},
            'skill': {
                'type': 'string',
                'description': 'Skill name to refine (created if it does not exist yet).',
            },
            'evidence': {
                'type': 'array',
                'items': {'type': 'string'},
                'description': (
                    'Concrete quotes/citations from the trajectory that justify this change. '
                    'Required — refinements are not accepted on unsupported claims.'
                ),
            },
            'proposed_change': {
                'type': 'string',
                'description': 'What should change and why, in your own words.',
            },
            'max_iterations': {'type': 'number', 'description': 'Default 3, max 10.'},
            'target_score': {
                'type': 'number',
                'description': 'Stop once the judge scores at least this (0-10). Default 8.',
            },
            'refinement_id': {'type': 'string', 'description': 'For status: the refinement to check.'},
        },
        'required': ['action'],
    },
}


def _records_path(ctx: ToolContext) -> Path:
    return (ctx.state_dir or (ctx.project_root / '.charon_state')) / 'refinements.json'


def _load_records(ctx: ToolContext) -> list[dict]:
    from charon.infra.fileio import read_json_or_quarantine
    data = read_json_or_quarantine(_records_path(ctx), [], component='refine_tool')
    return data if isinstance(data, list) else []


def _save_records(ctx: ToolContext, records: list[dict]) -> None:
    from charon.infra.fileio import write_json_atomic
    write_json_atomic(_records_path(ctx), records)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def execute_refine(params: dict, ctx: ToolContext) -> ToolResult:
    action = str(params.get('action') or '').strip().lower()
    if action == 'propose':
        return _handle_propose(params, ctx)
    if action == 'status':
        return _handle_status(params, ctx)
    if action == 'list':
        return _handle_list(ctx)
    return ToolResult(content=f'Unknown action: {action}', is_error=True)


def _handle_propose(params: dict, ctx: ToolContext) -> ToolResult:
    if not ctx.state_dir:
        return ToolResult(content='Error: state_dir not available', is_error=True)

    try:
        skill_name = _validate_name(str(params.get('skill') or ''))
    except ValueError as e:
        return ToolResult(content=f'Error: {e}', is_error=True)

    evidence = [str(e).strip() for e in (params.get('evidence') or []) if str(e).strip()]
    if not evidence:
        return ToolResult(
            content=(
                'Error: evidence is required — cite specific trajectory evidence for this '
                'change. Refine rejects unsupported claims.'
            ),
            is_error=True,
        )

    proposed_change = str(params.get('proposed_change') or '').strip()
    if not proposed_change:
        return ToolResult(content='Error: proposed_change is required.', is_error=True)

    max_iterations = int(params.get('max_iterations') or 3)
    max_iterations = max(1, min(max_iterations, 10))
    target_score = params.get('target_score')
    target_score = float(target_score) if target_score is not None else 8.0

    skill_dir = _skills_root(ctx) / skill_name
    skill_file = skill_dir / 'SKILL.md'
    skill_dir.mkdir(parents=True, exist_ok=True)
    is_new = not skill_file.exists()
    if is_new:
        # Judge loops need a real baseline to checkpoint and diff against.
        skill_file.write_text(
            f'# {skill_name}\n\n(no content yet — proposed for refinement)\n', encoding='utf-8',
        )

    evidence_block = '\n'.join(f'- {e}' for e in evidence)
    rubric = (
        "You are reviewing a proposed refinement to one of Charon's own procedural-memory "
        'skills (a SKILL.md file it consults on future tasks). Score how well the CURRENT '
        'content of the file reflects a real, evidence-backed improvement — not a generic '
        'rewrite.\n\n'
        f'Cited evidence for why this skill needs to change:\n{evidence_block}\n\n'
        f'Proposed change (what the implementer was asked to do):\n{proposed_change}\n\n'
        'Score 0-10:\n'
        '- 8-10: the guidance is directly traceable to the cited evidence, stays narrowly '
        'scoped to what the evidence supports, and does not contradict or duplicate existing '
        'guidance already in the file.\n'
        '- 4-7: a real edit was made but it is partially generic, broader than the evidence '
        'supports, or loses precision from the original.\n'
        '- 0-3: unchanged from baseline, vague, contradicts the evidence, or invents guidance '
        'the evidence does not support.\n\n'
        'Be skeptical of confident-sounding language that is not backed by the evidence above '
        '— that is overclaiming, and it must be penalized even if the prose reads well.'
    )
    program = (
        'Update the skill file at SKILL.md in this directory based on the following '
        'evidence-backed need. Make ONE focused edit — do not rewrite the whole file unless '
        'it has no real content yet.\n\n'
        f'Evidence:\n{evidence_block}\n\n'
        f'Requested change:\n{proposed_change}\n\n'
        'Keep the skill narrowly scoped to what the evidence actually supports.'
    )

    try:
        from charon.judge.judge_engine import create_loop
        config = create_loop(
            ctx.state_dir,
            goal=f'Refine skill "{skill_name}": {proposed_change}'[:500],
            project=str(skill_dir),
            agent_id=ctx.agent_id,
            judge_type='aesthetic',
            direction='maximize',
            target_score=target_score,
            rubric=rubric,
            scope=['SKILL.md'],
            max_iterations=max_iterations,
            program=program,
        )
    except Exception as e:
        return ToolResult(content=f'Error creating refinement loop: {e}', is_error=True)

    record = {
        'id': f'refine-{config.id}',
        'loop_id': config.id,
        'skill': skill_name,
        'is_new_skill': is_new,
        'evidence': evidence,
        'proposed_change': proposed_change,
        'proposed_by': ctx.agent_id,
        'created_at': _now(),
    }
    records = _load_records(ctx)
    records.append(record)
    _save_records(ctx, records)

    return ToolResult(
        content=(
            f'Refinement proposed for skill "{skill_name}" ({"new" if is_new else "existing"}).\n'
            f'Refinement ID: {record["id"]}\n'
            f'Judge loop: {config.id} (aesthetic, target {target_score}/10, up to '
            f'{max_iterations} iterations)\n\n'
            'The daemon heartbeat advances this like any judge loop: it measures a baseline, '
            'then each iteration spawns a scoped implementer to edit SKILL.md and an '
            'independent judge scores the result against your cited evidence — improvements '
            'are kept, regressions are rolled back automatically via shadow-git checkpoints.\n\n'
            f'Check progress with: Refine(action="status", refinement_id="{record["id"]}")'
        ),
        details={'refinement_id': record['id'], 'loop_id': config.id, 'skill': skill_name},
    )


def _format_record(record: dict, loop_summary: dict | None) -> list[str]:
    lines = [f'### {record["id"]} — skill "{record["skill"]}"']
    lines.append(f'- Proposed: {record.get("proposed_change", "")[:160]}')
    lines.append('- Evidence: ' + '; '.join(record.get('evidence') or [])[:200])
    if loop_summary:
        lines.append(
            f'- Judge loop: {loop_summary.get("status", "?")}, '
            f'best score {loop_summary.get("best_score", "—")}, '
            f'iterations {loop_summary.get("current_iteration", 0)}/'
            f'{loop_summary.get("max_iterations", "?")}'
        )
        conv = loop_summary.get('convergence') or {}
        if conv.get('converged'):
            lines.append(f'- Converged: {conv.get("reason", "?")}')
    else:
        lines.append('- Judge loop: not found (may have been cleared)')
    return lines


def _handle_status(params: dict, ctx: ToolContext) -> ToolResult:
    refinement_id = str(params.get('refinement_id') or '').strip()
    if not refinement_id:
        return ToolResult(content='Error: refinement_id is required for status', is_error=True)
    if not ctx.state_dir:
        return ToolResult(content='Error: state_dir not available', is_error=True)

    records = _load_records(ctx)
    record = next((r for r in records if r.get('id') == refinement_id), None)
    if not record:
        return ToolResult(content=f'Refinement not found: {refinement_id}', is_error=True)

    try:
        from charon.judge.judge_engine import list_loops
        loops = {lp.get('id'): lp for lp in list_loops(ctx.state_dir)}
    except Exception as e:
        _diag('refine_tool', 'judge loop list failed; refinement status shown without loop detail', error=e)
        loops = {}

    lines = _format_record(record, loops.get(record.get('loop_id')))
    return ToolResult(content='\n'.join(lines))


def _handle_list(ctx: ToolContext) -> ToolResult:
    if not ctx.state_dir:
        return ToolResult(content='Error: state_dir not available', is_error=True)
    records = _load_records(ctx)
    if not records:
        return ToolResult(content='No refinements proposed yet.')

    try:
        from charon.judge.judge_engine import list_loops
        loops = {lp.get('id'): lp for lp in list_loops(ctx.state_dir)}
    except Exception as e:
        _diag('refine_tool', 'judge loop list failed; refinement list shown without loop detail', error=e)
        loops = {}

    lines = [f'Refinements ({len(records)}):']
    for record in records:
        loop_summary = loops.get(record.get('loop_id'))
        status = (loop_summary or {}).get('status', '?')
        best = (loop_summary or {}).get('best_score', '—')
        lines.append(f'- {record["id"]} — skill "{record["skill"]}" ({status}, best {best})')
    return ToolResult(content='\n'.join(lines), details={'refinements': records})
