from __future__ import annotations

import time
from pathlib import Path
from typing import Any


def _tool_ctx(project_root: Path, state_dir: Path):
    from tools import ToolContext
    return ToolContext(project_root=project_root, state_dir=state_dir)


def gather_source_leads_for_topic(state_dir: Path, project_root: Path, operation_id: str, topic: dict[str, Any], *, query: str = '') -> list[dict[str, Any]]:
    """Use Paper + SourceDiscovery to gather and score promising source leads."""
    from libris_runtime import index_promising_source, append_operation_event, emit_agent_phase
    from tools.paper_tool import execute_paper
    from tools.source_discovery_tool import execute_source_discovery

    q = query or str(topic.get('title') or topic.get('slug') or '').strip()
    if not q:
        return []
    ctx = _tool_ctx(project_root, state_dir)
    leads: list[dict[str, Any]] = []
    if topic.get('researcher_agent_id'):
        emit_agent_phase(
            state_dir, project_root, operation_id,
            agent_id=str(topic.get('researcher_agent_id') or ''), role='researcher',
            phase='reviewing_leads', status='running', topic_slug=str(topic.get('slug') or ''),
            summary='Gathering and scoring promising source leads.'
        )

    paper_res = execute_paper({'action': 'search', 'query': q, 'backend': 'auto', 'limit': 5}, ctx)
    for row in ((paper_res.details or {}).get('results') or [])[:5]:
        source = dict(row)
        source['source_type'] = source.get('source_type') or 'paper'
        leads.append(index_promising_source(
            state_dir, project_root,
            operation_id=operation_id,
            topic_slug=str(topic.get('slug') or ''),
            source=source,
            query=q,
        ))

    disc_res = execute_source_discovery({'action': 'discover', 'query': q, 'limit': 5}, ctx)
    for row in ((disc_res.details or {}).get('results') or [])[:5]:
        leads.append(index_promising_source(
            state_dir, project_root,
            operation_id=operation_id,
            topic_slug=str(topic.get('slug') or ''),
            source=dict(row),
            query=q,
        ))

    append_operation_event(state_dir, project_root, operation_id, 'source_leads_gathered', {
        'topic_slug': str(topic.get('slug') or ''),
        'count': len(leads),
        'query': q,
    })
    leads.sort(key=lambda r: float(r.get('lead_score') or 0), reverse=True)
    return leads


def wait_for_procurement_contracts(
    state_dir: Path,
    project_root: Path,
    operation_id: str,
    topic_slug: str,
    *,
    min_completed: int = 1,
    timeout_seconds: int = 20,
) -> list[dict[str, Any]]:
    from libris_runtime import list_topic_contracts

    deadline = time.time() + max(1, timeout_seconds)
    latest: list[dict[str, Any]] = []
    while time.time() < deadline:
        latest = list_topic_contracts(
            state_dir,
            project_root,
            operation_id=operation_id,
            topic_slug=topic_slug,
            contract_type='libris_source_procurement',
        )
        done = [c for c in latest if str(c.get('status') or '') == 'completed']
        if len(done) >= min_completed:
            return latest
        time.sleep(2)
    return latest



def build_procurement_summary_markdown(
    state_dir: Path,
    project_root: Path,
    operation_id: str,
    topic_slug: str,
) -> str:
    from libris_runtime import list_topic_contracts, summarize_contract_for_swarm

    contracts = list_topic_contracts(
        state_dir,
        project_root,
        operation_id=operation_id,
        topic_slug=topic_slug,
        contract_type='libris_source_procurement',
    )
    if not contracts:
        return ''
    lines = [f'# Procurement Summary: {topic_slug}', '']
    for ctr in contracts:
        rec = summarize_contract_for_swarm(ctr, state_dir)
        meta = rec.get('metadata') or {}
        lines.append(f'## {meta.get("lead_title") or rec.get("contract_id")}')
        if meta.get('lead_url'):
            lines.append(str(meta.get('lead_url')))
        lines.append(f'- Contract: {rec.get("contract_id")}')
        lines.append(f'- Status: {rec.get("status")}')
        lines.append(f'- Completed phases: {rec.get("completed_phases")}/{rec.get("phase_count")}')
        payload = rec.get('last_event_payload') or {}
        if payload.get('summary'):
            lines.append(f'- Latest summary: {payload.get("summary")}')
        lines.append('')
    return '\n'.join(lines).strip()



def spawn_topic_procurement_shades(state_dir: Path, project_root: Path, operation_id: str, topic: dict[str, Any], *, max_leads: int = 2) -> list[str]:
    """Spawn a couple of shades to procure/summarize top leads for a topic."""
    from libris_runtime import list_promising_sources, append_operation_event, emit_agent_comm, emit_agent_phase
    from tools.shade_tool import execute_spawn_shade

    ctx = _tool_ctx(project_root, state_dir)
    slug = str(topic.get('slug') or '')
    title = str(topic.get('title') or slug)
    leads = list_promising_sources(state_dir, project_root, operation_id=operation_id, topic_slug=slug, limit=max_leads)
    contracts: list[str] = []
    if topic.get('researcher_agent_id'):
        emit_agent_phase(
            state_dir, project_root, operation_id,
            agent_id=str(topic.get('researcher_agent_id') or ''), role='researcher',
            phase='spawning_shades', status='running', topic_slug=slug,
            summary='Spawning procurement shades for top promising sources.'
        )
    for lead in leads[:max_leads]:
        goal = (
            f'Libris source procurement for topic `{slug}` ({title}).\n'
            f'Investigate source lead: {lead.get("title")}\n'
            f'URL: {lead.get("url", "")}\n'
            f'Objective: read the source if possible, extract the strongest claims, summarize relevance, '\
            f'and persist useful findings via the Research tool or normal file outputs if needed.'
        )
        res = execute_spawn_shade({
            'goal': goal,
            'constraints': [
                f'Operation ID: {operation_id}',
                f'Topic slug: {slug}',
                'Focus on source procurement and concise summary only.',
            ],
            'expected_outputs': [
                'A concise source summary',
                'Strongest claims with provenance',
                'Relevance assessment for the topic',
            ],
            'contract_type': 'libris_source_procurement',
            'metadata': {
                'operation_id': operation_id,
                'topic_slug': slug,
                'lead_id': lead.get('lead_id', ''),
                'lead_title': lead.get('title', ''),
                'lead_url': lead.get('url', ''),
            },
            'phase_specs': [
                {'name': 'procurement', 'objective': f'Fetch or inspect the source lead `{lead.get("title", "")}` and determine what material is available.'},
                {'name': 'extraction', 'objective': 'Extract the strongest concrete claims, evidence, and uncertainties from the source.'},
                {'name': 'summary', 'objective': 'Summarize why this source matters for the parent topic and what follow-up work is justified.'},
            ],
        }, ctx)
        details = res.details or {}
        if details.get('contract_id'):
            contracts.append(str(details['contract_id']))
        if details.get('shade_id') and topic.get('researcher_agent_id'):
            emit_agent_comm(
                state_dir, project_root, operation_id,
                from_agent_id=str(topic.get('researcher_agent_id') or ''), to_agent_id=str(details.get('shade_id') or ''),
                from_role='researcher', to_role='shade', topic_slug=slug,
                message_kind='source_procurement', summary=f'Assigned promising source lead: {lead.get("title")}'
            )
    append_operation_event(state_dir, project_root, operation_id, 'topic_procurement_shades_spawned', {
        'topic_slug': slug,
        'count': len(contracts),
    })
    return contracts
