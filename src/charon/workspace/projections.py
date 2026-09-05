"""Presentation-neutral projection plus the generated overseer documents.

``build_projection`` returns the same shape as the JS kernel's ``projection()`` so Acheron's
panel, the TUI and the hub can read either implementation's ``projection.json``.
``render_status_md`` writes ``PROJECT_STATUS.md`` in the layout from
``docs/plans/overseer-agent-design.md`` (Summary / Staffing / Work Division / Goals /
Velocity / Risks); ``render_plan_md`` writes ``PLAN.md``.  Both are regenerated from records
and must never be edited by hand.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .records import RECORD_TYPES, canonical_json, clone, criteria_progress, parse_iso_ms

if TYPE_CHECKING:  # pragma: no cover
    from .store import WorkspaceStore


def build_projection(store: 'WorkspaceStore') -> dict[str, Any]:
    sessions = [{'id': s['id'], 'kind': s.get('kind'), 'status': s.get('status'), 'host_ref': s.get('host_ref'),
                 'task_ids': s.get('task_ids', []), 'revision': s.get('revision'), 'started_at': s.get('started_at'),
                 'ended_at': s.get('ended_at'), **(clone(s.get('extensions')) or {})} for s in store.list('session')]
    tasks_all = [{'id': t['id'], 'work_item_id': t.get('work_item_id'), 'run_id': t.get('run_id'), 'session_id': t.get('session_id'),
                  'title': t.get('title'), 'status': t.get('status'), 'attempt': t.get('attempt'), 'revision': t.get('revision'),
                  'started_at': t.get('started_at'), 'completed_at': t.get('completed_at'), 'result_summary': t.get('result_summary'),
                  'lease_ids': t.get('lease_ids', []), 'evidence_artifact_ids': t.get('evidence_artifact_ids', []),
                  'context_manifest_id': t.get('context_manifest_id'), 'scopes': t.get('scopes', []),
                  **(clone(t.get('extensions')) or {})} for t in store.list('task')]
    tasks_by_item: dict[str, list[dict[str, Any]]] = {}
    for t in tasks_all:
        tasks_by_item.setdefault(t['work_item_id'], []).append(t)

    def node(w: dict[str, Any]) -> dict[str, Any]:
        return {
            'id': w['id'], 'kind': w.get('kind'), 'title': w.get('title'), 'description': w.get('description') or '',
            'status': w.get('status'), 'priority': w.get('priority'), 'revision': w.get('revision'),
            'parent_id': w.get('parent_id'), 'owner': w.get('owner'), 'owner_role': (w.get('extensions') or {}).get('owner_role'),
            'scopes': w.get('scopes', []), 'created_at': w.get('created_at'), 'updated_at': w.get('updated_at'),
            'criteria': [{'id': c.get('id'), 'statement': c.get('statement'), 'required': c.get('required'),
                          'verifier_kind': (c.get('verifier') or {}).get('kind'), 'verifier_spec': (c.get('verifier') or {}).get('spec') or {},
                          'status': c.get('status'), 'evidence_count': len(c.get('evidence_artifact_ids') or []),
                          'evidence_artifact_ids': c.get('evidence_artifact_ids') or [], 'waiver_reason': c.get('waiver_reason')}
                         for c in w.get('acceptance_criteria') or []],
            'progress': criteria_progress(w), 'tasks': tasks_by_item.get(w['id'], []), 'children': [],
        }

    nodes = {w['id']: node(w) for w in store.list('work_item')}
    roots: list[dict[str, Any]] = []
    for n in nodes.values():
        parent = nodes.get(n['parent_id']) if n.get('parent_id') else None
        if parent is not None:
            parent['children'].append(n)
        else:
            roots.append(n)

    def sort_tree(items: list[dict[str, Any]]) -> None:
        items.sort(key=lambda x: x.get('created_at') or '')
        for item in items:
            sort_tree(item['children'])

    sort_tree(roots)
    now = store.now()
    leases = []
    for l in store.list('lease'):
        expires = parse_iso_ms(l.get('expires_at'))
        leases.append({'id': l['id'], 'status': l.get('status'), 'mode': l.get('mode'), 'resource': l.get('resource'),
                       'owner_task_id': l.get('owner_task_id'), 'owner_session_id': l.get('owner_session_id'),
                       'fencing_token': l.get('fencing_token'), 'acquired_at': l.get('acquired_at'), 'expires_at': l.get('expires_at'),
                       'live': l.get('status') == 'active' and expires is not None and expires > now})
    gates = sorted((clone(g) for g in store.list('gate')),
                   key=lambda g: (0 if g.get('status') == 'open' else 1, _desc(g.get('created_at'))))
    proposals = sorted((clone(p) for p in store.list('proposal')),
                       key=lambda p: (0 if p.get('status') == 'proposed' else 1, _desc(p.get('created_at'))))
    decisions = [{'id': k['id'], 'title': k.get('title'), 'body': k.get('body'), 'status': k.get('status'),
                  'created_at': k.get('created_at'), 'tags': k.get('tags', []), **(clone(k.get('extensions')) or {})}
                 for k in store.list('knowledge_item', lambda k: k.get('kind') == 'decision')]
    recent_events = [{'id': e['id'], 'seq': e.get('replica_sequence'), 'event_type': e.get('event_type'), 'occurred_at': e.get('occurred_at'),
                      'actor': e.get('actor'), 'subject_refs': e.get('subject_refs'),
                      'payload_head': canonical_json(e.get('payload') or {})[:200]} for e in store.events[-50:]]
    counts = {t: len(store.records[t]) for t in RECORD_TYPES}
    counts['events'] = len(store.events)
    ws = store.workspace
    return {
        'generated_at': store.iso(),
        'workspace': {'id': ws.get('id'), 'slug': ws.get('slug'), 'title': ws.get('title'), 'roots': ws.get('roots'), 'revision': ws.get('revision')},
        'sessions': sessions, 'work_items': roots, 'runs': [clone(r) for r in store.list('run')], 'tasks': tasks_all,
        'leases': leases, 'gates': gates, 'proposals': proposals, 'decisions': decisions, 'recent_events': recent_events, 'counts': counts,
    }


class _desc(str):
    """Reverse-order sort key for ISO timestamps (newest first)."""

    def __new__(cls, value: Any):
        return str.__new__(cls, value or '')

    def __lt__(self, other: str) -> bool:  # type: ignore[override]
        return str.__gt__(self, other)


def flatten_tree(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []

    def walk(items: list[dict[str, Any]], depth: int) -> None:
        for n in items:
            out.append({**n, 'depth': depth})
            if n.get('children'):
                walk(n['children'], depth + 1)

    walk(nodes, 0)
    return out


def summarize_work(projection: dict[str, Any]) -> dict[str, int]:
    items = flatten_tree(projection.get('work_items') or [])
    return {'total': len(items), 'done': sum(1 for i in items if i.get('status') == 'done'),
            'active': sum(1 for i in items if i.get('status') == 'active'),
            'blocked': sum(1 for i in items if i.get('status') == 'blocked'),
            'tasks_active': sum(1 for t in projection.get('tasks') or [] if t.get('status') in ('claimed', 'running', 'waiting'))}


def crit_mark(c: dict[str, Any]) -> str:
    return {'passed': '✓', 'waived': '~', 'failed': '✗'}.get(c.get('status') or '', '·')


def render_goals(nodes: list[dict[str, Any]], depth: int = 0) -> str:
    lines = []
    for n in nodes:
        box = '[x]' if n.get('status') == 'done' else '[-]' if n.get('status') == 'cancelled' else '[ ]'
        crit = f" ({''.join(crit_mark(c) for c in n.get('criteria') or [])})" if n.get('criteria') else ''
        role = f" @{n['owner_role']}" if n.get('owner_role') else ''
        lines.append(f"{'  ' * depth}- {box} {n.get('title')}{role} — {n.get('status')}{crit}")
        if n.get('children'):
            lines.append(render_goals(n['children'], depth + 1))
    return '\n'.join(lines)


def render_plan_tree(nodes: list[dict[str, Any]], depth: int = 0) -> str:
    parts = []
    for n in nodes:
        h = '## ' if depth == 0 else '### ' if depth == 1 else '#### '
        crit = '\n'.join(f"- {crit_mark(c)} {c.get('statement')} _({c.get('verifier_kind')}{', optional' if c.get('required') is False else ''}; "
                         f"{c.get('evidence_count') or 0} evidence)_" for c in n.get('criteria') or [])
        role = f", @{n['owner_role']}" if n.get('owner_role') else ''
        text = f"{h}{n.get('title')} — {n.get('status')} ({n.get('kind')}, {n.get('priority')}{role})\n"
        if n.get('description'):
            text += f"{n['description']}\n"
        if crit:
            text += f"{crit}\n"
        if n.get('children'):
            text += render_plan_tree(n['children'], depth + 1)
        parts.append(text)
    return '\n'.join(parts)


def _session_label(s: dict[str, Any]) -> str:
    return s.get('callsign') or s.get('title') or s.get('id') or '?'


def render_status_md(store: 'WorkspaceStore', *, projection: dict[str, Any] | None = None) -> str:
    p = projection or store.projection()
    ov = store.get_extension('overseer') or {}
    report = ov.get('report') or {}
    title = ov.get('name') or store.workspace.get('title') or store.workspace.get('slug')
    sessions = p.get('sessions') or []
    tasks_by_session: dict[str, list[dict[str, Any]]] = {}
    for t in p.get('tasks') or []:
        tasks_by_session.setdefault(t.get('session_id') or '', []).append(t)
    items = flatten_tree(p.get('work_items') or [])
    now = store.now()
    day, week = now - 86_400_000, now - 7 * 86_400_000
    checkpoints_24h = sum(1 for t in p.get('tasks') or [] if t.get('status') in ('succeeded', 'failed')
                          and (parse_iso_ms(t.get('completed_at') or t.get('updated_at')) or 0) > day)
    done_7d = sum(1 for i in items if i.get('status') == 'done' and (parse_iso_ms(i.get('updated_at')) or 0) > week)
    open_gates = [g for g in p.get('gates') or [] if g.get('status') == 'open']
    active_leases = [l for l in p.get('leases') or [] if l.get('status') == 'active']
    staffing = '\n'.join(
        f"- **{_session_label(s)}** ({s.get('agent') or s.get('kind')}{', overseer' if s.get('role') == 'overseer' else ''}) — "
        f"{s.get('acheron_status') or s.get('status') or '?'}{f' · task {s['active_task_id']}' if s.get('active_task_id') else ''}"
        f"{' · retired' if s.get('retired') else ''}" for s in sessions)
    division_lines = []
    for s in sessions:
        if s.get('role') == 'overseer':
            continue
        ts = tasks_by_session.get(s['id'], [])
        ok = sum(1 for t in ts if t.get('status') == 'succeeded')
        bad = sum(1 for t in ts if t.get('status') == 'failed')
        cur = next((t for t in ts if t.get('status') in ('claimed', 'running', 'waiting')), None)
        item_ids = list(dict.fromkeys(t.get('work_item_id') for t in ts if t.get('work_item_id')))
        now_part = f" · now: {cur.get('title')}" if cur else ''
        items_part = f" · items: {', '.join(item_ids)}" if item_ids else ''
        division_lines.append(f"- **{_session_label(s)}**: {len(ts)} task{'' if len(ts) == 1 else 's'} ({ok} succeeded, {bad} failed)"
                              f"{now_part}{items_part}")
    division = '\n'.join(division_lines) or '- (no tasks dispatched yet)'
    goals = render_goals(p.get('work_items') or [])
    risks = [f'- {r}' for r in report.get('risks') or []]
    risks += [f"- expired lease on {(l.get('resource') or {}).get('selector')} ({l.get('owner_session_id')})"
              for l in active_leases if (parse_iso_ms(l.get('expires_at')) or 0) < now]
    risks += [f"- {_session_label(s)} is in error state" for s in sessions if (s.get('acheron_status') or s.get('status')) in ('error', 'failed')]
    risks += [f"- open gate ({g.get('kind')}): {g.get('question')}" for g in open_gates]
    summary = report.get('summary') or '_No report yet._'
    next_line = f"\n**Next:** {report['next']}" if report.get('next') else ''
    return (
        f"# Project Status: {title}\n\n"
        f"_Generated {store.iso()} · cycle {ov.get('cycle') or 0} · health **{report.get('health') or 'unknown'}**"
        f"{' · PAUSED' if ov.get('paused') else ''}_\n\n"
        f"## Summary\n{summary}\n{next_line}\n\n"
        f"## Staffing\n{staffing or '- (no sessions)'}\n{chr(10) + report['staffing'] if report.get('staffing') else ''}\n\n"
        f"## Work Division\n{division}\n{chr(10) + report['work_division'] if report.get('work_division') else ''}\n\n"
        f"## Goals\n{goals or '- (no work items yet)'}\n\n"
        f"## Velocity\n- Checkpoints in the last 24h: {checkpoints_24h}\n- Evidence-backed completions in the last 7 days: {done_7d}\n"
        f"- Active leases: {len(active_leases)} · open gates: {len(open_gates)}\n\n"
        f"## Risks\n{chr(10).join(risks) or '- none recorded'}\n"
    )


def render_plan_md(store: 'WorkspaceStore', *, projection: dict[str, Any] | None = None) -> str:
    p = projection or store.projection()
    ov = store.get_extension('overseer') or {}
    title = ov.get('name') or store.workspace.get('title') or store.workspace.get('slug')
    body = render_plan_tree(p.get('work_items') or []) or '_No work items yet._'
    return f"# Plan: {title}\n\n_Generated {store.iso()} from work items (edit through the overseer's tools, not here)._\n\n{body}\n"
