from __future__ import annotations

from pathlib import Path
from typing import Any

from charon.devop.devop_runtime import get_operation_state


def _role_label(agent_id: str, operation_role: str = '', specialization: str = '') -> str:
    parts = []
    if operation_role:
        parts.append(operation_role)
    if specialization:
        parts.append(specialization)
    if not parts and agent_id:
        parts.append(agent_id)
    return ' · '.join(parts) if parts else 'agent'


def project_graph(state_dir: Path, operation_id: str) -> dict[str, Any]:
    op = get_operation_state(state_dir, operation_id)
    if not op:
        return {'operation_id': operation_id, 'nodes': [], 'edges': []}

    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    seen_nodes: set[str] = set()

    def add_node(node: dict[str, Any]) -> None:
        nid = str(node.get('id') or '')
        if not nid or nid in seen_nodes:
            return
        seen_nodes.add(nid)
        nodes.append(node)

    add_node({
        'node_type': 'operation',
        'id': op['operation_id'],
        'label': op.get('title') or op.get('prompt') or op['operation_id'],
        'domain': 'software_dev',
        'status': op.get('status') or '',
    })

    coord_id = str(op.get('coordinator_agent_id') or '')
    if coord_id:
        add_node({
            'node_type': 'agent',
            'id': coord_id,
            'runtime_role': 'persistent_agent',
            'operation_role': 'coordinator',
            'specialization': 'development-orchestration',
            'label': _role_label(coord_id, 'coordinator', 'development-orchestration'),
        })
        edges.append({'edge_type': 'owns', 'from': coord_id, 'to': op['operation_id']})

    for ws in op.get('workstreams') or []:
        ws_id = str(ws.get('workstream_id') or '')
        slug = str(ws.get('slug') or '')
        title = str(ws.get('title') or slug or ws_id)
        add_node({
            'node_type': 'work_unit',
            'work_unit_type': 'workstream',
            'id': ws_id,
            'label': title,
            'domain': 'software_dev',
            'status': ws.get('status') or '',
            'slug': slug,
        })
        edges.append({'edge_type': 'owns', 'from': op['operation_id'], 'to': ws_id})

        owner_id = str(ws.get('owner_agent_id') or '')
        if owner_id:
            add_node({
                'node_type': 'agent',
                'id': owner_id,
                'runtime_role': 'persistent_agent',
                'operation_role': 'implementer',
                'specialization': slug,
                'label': _role_label(owner_id, 'implementer', slug),
            })
            edges.append({'edge_type': 'assigns', 'from': coord_id or op['operation_id'], 'to': owner_id, 'work_unit_id': ws_id})

        judge_id = str(ws.get('paired_judge_agent_id') or '')
        if judge_id:
            add_node({
                'node_type': 'agent',
                'id': judge_id,
                'runtime_role': 'judge_actor',
                'operation_role': 'judge',
                'specialization': slug,
                'label': _role_label(judge_id, 'judge', slug),
            })

        for dep in ws.get('dependency_ids') or []:
            edges.append({'edge_type': 'depends_on', 'from': ws_id, 'to': dep})

        for cp in ws.get('checkpoints') or []:
            cp_id = str(cp.get('checkpoint_id') or '')
            if not cp_id:
                continue
            add_node({
                'node_type': 'checkpoint',
                'id': cp_id,
                'label': cp.get('summary') or cp_id,
                'status': cp.get('status') or '',
            })
            if owner_id:
                edges.append({'edge_type': 'submits', 'from': owner_id, 'to': cp_id})
            if cp.get('evidence_bundle_id'):
                ev_id = str(cp.get('evidence_bundle_id'))
                add_node({'node_type': 'evidence_bundle', 'id': ev_id, 'label': ev_id})
                edges.append({'edge_type': 'produces', 'from': cp_id, 'to': ev_id})

        for rv in ws.get('reviews') or []:
            rv_id = str(rv.get('review_id') or '')
            cp_id = str(rv.get('checkpoint_id') or '')
            if not rv_id:
                continue
            add_node({
                'node_type': 'review',
                'id': rv_id,
                'label': rv.get('summary') or rv_id,
                'decision': rv.get('decision') or '',
            })
            if judge_id:
                edges.append({'edge_type': 'reviews', 'from': judge_id, 'to': cp_id or rv_id, 'review_id': rv_id})
            if cp_id:
                edges.append({'edge_type': 'criticizes', 'from': rv_id, 'to': cp_id, 'decision': rv.get('decision') or ''})

    for dec in op.get('decisions_tail') or []:
        dec_id = str(dec.get('decision_id') or '')
        subject_id = str(dec.get('subject_id') or '')
        actor_id = str(dec.get('actor_agent_id') or '')
        if dec_id:
            add_node({'node_type': 'decision', 'id': dec_id, 'label': dec.get('summary') or dec_id})
            if actor_id and subject_id:
                edges.append({'edge_type': 'selects', 'from': actor_id, 'to': subject_id, 'decision_id': dec_id})

    return {'operation_id': operation_id, 'nodes': nodes, 'edges': edges}


def project_room_messages(state_dir: Path, operation_id: str, *, workstream_slug: str = '') -> list[dict[str, Any]]:
    op = get_operation_state(state_dir, operation_id)
    if not op:
        return []
    out = []
    for evt in op.get('events_tail') or []:
        payload = evt.get('payload') or {}
        evt_ws = str(payload.get('workstream_slug') or '')
        if workstream_slug and evt_ws and evt_ws != workstream_slug:
            continue
        kind = str(evt.get('kind') or '')
        msg_class = 'worker_progress'
        if 'operation' in kind or kind.startswith('final_'):
            msg_class = 'operation_status'
        elif 'checkpoint' in kind:
            msg_class = 'checkpoint_notice'
        elif 'review' in kind:
            msg_class = 'review_notice'
        elif 'decision' in kind:
            msg_class = 'decision_notice'
        elif 'handoff' in kind or 'assignment' in kind:
            msg_class = 'assignment'
        out.append({
            'message_id': evt.get('event_id') or '',
            'message_class': msg_class,
            'operation_id': operation_id,
            'workstream_slug': evt_ws,
            'ts': evt.get('ts') or '',
            'summary': evt.get('summary') or kind,
            'kind': kind,
            'links': {
                'workstream_id': evt.get('workstream_id') or '',
                'checkpoint_id': payload.get('checkpoint_id') or '',
                'review_id': payload.get('review_id') or '',
            },
            'payload': payload,
        })
    return out


def project_f4_stream(state_dir: Path, operation_id: str) -> dict[str, Any]:
    op = get_operation_state(state_dir, operation_id)
    if not op:
        return {'operation_id': operation_id, 'stream': [], 'active_reviews': [], 'workstreams': []}

    stream = []
    for evt in op.get('events_tail') or []:
        kind = str(evt.get('kind') or '')
        item_class = 'operation_event_item'
        payload = evt.get('payload') or {}
        if 'checkpoint' in kind:
            item_class = 'checkpoint_item'
        elif 'review' in kind:
            item_class = 'review_item'
        elif 'decision' in kind or kind.startswith('final_'):
            item_class = 'decision_item'
        elif 'phase' in kind or 'runtime_updated' in kind:
            item_class = 'phase_item'
        stream.append({
            'item_id': evt.get('event_id') or '',
            'item_class': item_class,
            'ts': evt.get('ts') or '',
            'kind': kind,
            'summary': evt.get('summary') or kind,
            'operation_id': operation_id,
            'workstream_id': evt.get('workstream_id') or '',
            'links': {
                'checkpoint_id': payload.get('checkpoint_id') or '',
                'review_id': payload.get('review_id') or '',
                'decision_id': payload.get('decision_id') or '',
            },
            'payload': payload,
        })

    active_reviews = []
    workstreams = []
    for ws in op.get('workstreams') or []:
        latest_review = ws.get('latest_review') or {}
        latest_checkpoint = ws.get('latest_checkpoint') or {}
        workstreams.append({
            'workstream_id': ws.get('workstream_id') or '',
            'slug': ws.get('slug') or '',
            'title': ws.get('title') or '',
            'status': ws.get('status') or '',
            'owner_agent_id': ws.get('owner_agent_id') or '',
            'paired_judge_agent_id': ws.get('paired_judge_agent_id') or '',
            'checkpoint_id': latest_checkpoint.get('checkpoint_id') or '',
            'checkpoint_summary': latest_checkpoint.get('summary') or '',
            'checkpoint_score': ((latest_checkpoint.get('scorecard') or {}).get('overall')),
            'review_id': latest_review.get('review_id') or '',
            'review_decision': latest_review.get('decision') or '',
            'review_summary': latest_review.get('summary') or '',
        })
        if latest_review and latest_review.get('status') != 'completed':
            active_reviews.append(latest_review)

    return {
        'operation_id': operation_id,
        'status': op.get('status') or '',
        'title': op.get('title') or '',
        'stream': stream,
        'active_reviews': active_reviews,
        'workstreams': workstreams,
    }


def summarize_operation(state_dir: Path, operation_id: str) -> dict[str, Any]:
    op = get_operation_state(state_dir, operation_id)
    if not op:
        return {}
    return {
        'operation_id': op.get('operation_id') or '',
        'title': op.get('title') or '',
        'status': op.get('status') or '',
        'workstream_count': len(op.get('workstreams') or []),
        'checkpoint_count': sum(len(ws.get('checkpoints') or []) for ws in (op.get('workstreams') or [])),
        'review_count': sum(len(ws.get('reviews') or []) for ws in (op.get('workstreams') or [])),
        'last_event': (op.get('events_tail') or [{}])[-1],
    }


def summarize_workstream(state_dir: Path, operation_id: str, workstream_slug: str) -> dict[str, Any]:
    op = get_operation_state(state_dir, operation_id)
    if not op:
        return {}
    for ws in op.get('workstreams') or []:
        if str(ws.get('slug') or '') == workstream_slug:
            latest_checkpoint = ws.get('latest_checkpoint') or {}
            latest_review = ws.get('latest_review') or {}
            return {
                'workstream_id': ws.get('workstream_id') or '',
                'slug': workstream_slug,
                'title': ws.get('title') or '',
                'status': ws.get('status') or '',
                'checkpoint_id': latest_checkpoint.get('checkpoint_id') or '',
                'review_id': latest_review.get('review_id') or '',
                'review_decision': latest_review.get('decision') or '',
            }
    return {}


__all__ = [
    'project_graph',
    'project_room_messages',
    'project_f4_stream',
    'summarize_operation',
    'summarize_workstream',
]
