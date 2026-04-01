from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any


def _claim_candidates(text: str, limit: int = 5) -> list[str]:
    out: list[str] = []
    for raw in (text or '').splitlines():
        s = raw.strip()
        if not s:
            continue
        s = re.sub(r'^[-*•]\s*', '', s)
        if len(s) >= 28:
            out.append(s[:400])
        if len(out) >= limit:
            break
    if out:
        return out[:limit]
    parts = re.split(r'(?<=[.!?])\s+', (text or '').strip())
    return [p.strip()[:400] for p in parts if len(p.strip()) >= 28][:limit]



def _iter_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding='utf-8', errors='replace').splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def wait_for_claim_extraction_contracts(
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
            contract_type='libris_claim_extraction',
        )
        done = [c for c in latest if str(c.get('status') or '') == 'completed']
        if len(done) >= min_completed:
            return latest
        time.sleep(2)
    return latest


def spawn_topic_claim_extraction_shades(
    state_dir: Path,
    project_root: Path,
    operation_id: str,
    topic: dict[str, Any],
    *,
    max_leads: int = 1,
) -> list[str]:
    from libris_runtime import list_promising_sources, append_operation_event, emit_agent_comm, emit_agent_phase
    from libris_orchestrator import _tool_ctx
    from tools.shade_tool import execute_spawn_shade

    slug = str(topic.get('slug') or '')
    title = str(topic.get('title') or slug)
    parent_agent_id = str(topic.get('researcher_agent_id') or '')
    leads = list_promising_sources(state_dir, project_root, operation_id=operation_id, topic_slug=slug, limit=max_leads)
    ctx = _tool_ctx(project_root, state_dir)
    contracts: list[str] = []

    if parent_agent_id:
        emit_agent_phase(
            state_dir, project_root, operation_id,
            agent_id=parent_agent_id, role='researcher',
            phase='extracting_claims', status='running', topic_slug=slug,
            summary='Spawning claim-extraction specialists for strongest leads.'
        )

    for lead in leads[:max_leads]:
        goal = (
            f'Libris claim extraction for topic `{slug}` ({title}).\n'
            f'Investigate source lead: {lead.get("title")}\n'
            f'URL: {lead.get("url", "")}\n'
            'Objective: extract the strongest concrete claims, uncertainties, and implementation-significance signals '
            'from this source, and summarize them in a reusable way for the parent researcher.'
        )
        res = execute_spawn_shade({
            'goal': goal,
            'constraints': [
                f'Operation ID: {operation_id}',
                f'Topic slug: {slug}',
                'Focus on concrete claim extraction and uncertainty/caveat identification.',
            ],
            'expected_outputs': [
                'Strongest concrete claims',
                'Uncertainties or caveats',
                'Implementation significance signals',
            ],
            'contract_type': 'libris_claim_extraction',
            'metadata': {
                'operation_id': operation_id,
                'topic_slug': slug,
                'lead_id': lead.get('lead_id', ''),
                'lead_title': lead.get('title', ''),
                'lead_url': lead.get('url', ''),
            },
            'phase_specs': [
                {'name': 'read', 'objective': 'Inspect the source and identify its main technical or factual content.'},
                {'name': 'claims', 'objective': 'Extract the strongest concrete claims and associated caveats.'},
                {'name': 'signals', 'objective': 'Summarize implementation significance, uncertainty, and follow-up value.'},
            ],
        }, ctx)
        details = res.details or {}
        if details.get('contract_id'):
            contracts.append(str(details.get('contract_id') or ''))
        if details.get('shade_id') and parent_agent_id:
            emit_agent_comm(
                state_dir, project_root, operation_id,
                from_agent_id=parent_agent_id,
                to_agent_id=str(details.get('shade_id') or ''),
                from_role='researcher', to_role='shade', topic_slug=slug,
                message_kind='claim_extraction_assignment',
                summary=f'Claim extraction assigned for lead: {lead.get("title")}',
            )

    append_operation_event(state_dir, project_root, operation_id, 'topic_claim_extraction_spawned', {
        'topic_slug': slug,
        'count': len(contracts),
    })
    return contracts


def ingest_claim_extraction_contracts(
    state_dir: Path,
    project_root: Path,
    operation_id: str,
    topic_slug: str,
) -> dict[str, Any]:
    from libris_runtime import (
        list_topic_contracts,
        summarize_contract_for_swarm,
        add_source,
        add_claim,
        save_evidence,
        append_operation_event,
    )

    contracts = list_topic_contracts(
        state_dir,
        project_root,
        operation_id=operation_id,
        topic_slug=topic_slug,
        contract_type='libris_claim_extraction',
    )
    ingested = []
    evidence_blocks: list[str] = []
    for ctr in contracts:
        if str(ctr.get('status') or '') != 'completed':
            continue
        rec = summarize_contract_for_swarm(ctr, state_dir)
        meta = rec.get('metadata') or {}
        lead_title = str(meta.get('lead_title') or '').strip() or str(rec.get('contract_id') or '')
        lead_url = str(meta.get('lead_url') or '').strip()
        payload = rec.get('last_event_payload') or {}
        latest_summary = str(payload.get('summary') or '').strip()
        phase_summaries = [str(p.get('result_summary') or '').strip() for p in (ctr.get('phases') or []) if p.get('result_summary')]
        merged_summary = '\n'.join([s for s in [latest_summary, *phase_summaries] if s]).strip()
        if not merged_summary:
            continue

        source = add_source(
            state_dir,
            project_root,
            topic_slug=topic_slug,
            title=f'Claim extraction: {lead_title}',
            url=lead_url or f'contract:{ctr.get("id")}',
            source_type='claim_extraction_summary',
            operation_id=operation_id,
            credibility='medium',
            tags=['libris', 'claim-extraction', 'shade'],
            extracted_text=merged_summary[:12000],
        )
        claims = []
        for claim_text in _claim_candidates(merged_summary, limit=5):
            stance = 'supports'
            low = claim_text.lower()
            if 'uncertain' in low or 'caveat' in low or 'limitation' in low:
                stance = 'unclear'
            claims.append(add_claim(
                state_dir,
                project_root,
                topic_slug=topic_slug,
                source_id=str(source.get('source_id') or ''),
                text=claim_text,
                operation_id=operation_id,
                confidence='medium',
                stance=stance,
            ))

        evidence_blocks.append('\n'.join([
            f'## Claim extraction: {lead_title}',
            f'- URL: {lead_url or "(no url)"}',
            f'- Contract: {ctr.get("id")}',
            f'- Claims extracted: {len(claims)}',
            '',
            merged_summary[:2500],
            '',
        ]))
        ingested.append({
            'contract_id': ctr.get('id'),
            'source_id': source.get('source_id'),
            'claim_count': len(claims),
            'title': lead_title,
        })

    evidence_path = None
    if evidence_blocks:
        res = save_evidence(
            state_dir,
            project_root,
            operation_id,
            topic_slug,
            markdown='# Claim Extraction Ingestion\n\n' + '\n'.join(evidence_blocks),
            filename=f'{topic_slug}-claim-extraction-ingested.md',
        )
        evidence_path = res.get('path')

    append_operation_event(state_dir, project_root, operation_id, 'claim_extraction_contracts_ingested', {
        'topic_slug': topic_slug,
        'count': len(ingested),
        'evidence_path': evidence_path or '',
    })
    return {
        'topic_slug': topic_slug,
        'ingested': ingested,
        'evidence_path': evidence_path,
    }



def _topic_claim_rows(state_dir: Path, project_root: Path, operation_id: str, topic_slug: str) -> list[dict[str, Any]]:
    from libris_runtime import research_root
    all_rows = _iter_jsonl(research_root(state_dir, project_root) / 'claims.jsonl')
    return [
        r for r in all_rows
        if str(r.get('operation_id') or '') == operation_id and str(r.get('topic_slug') or '') == topic_slug
    ]



def wait_for_contradiction_check_contracts(
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
            contract_type='libris_contradiction_check',
        )
        done = [c for c in latest if str(c.get('status') or '') == 'completed']
        if len(done) >= min_completed:
            return latest
        time.sleep(2)
    return latest



def spawn_topic_contradiction_check_shades(
    state_dir: Path,
    project_root: Path,
    operation_id: str,
    topic: dict[str, Any],
    *,
    max_claims: int = 6,
) -> list[str]:
    from libris_runtime import append_operation_event, emit_agent_comm, emit_agent_phase
    from libris_orchestrator import _tool_ctx
    from tools.shade_tool import execute_spawn_shade

    slug = str(topic.get('slug') or '')
    title = str(topic.get('title') or slug)
    parent_agent_id = str(topic.get('researcher_agent_id') or '')
    claims = _topic_claim_rows(state_dir, project_root, operation_id, slug)[:max_claims]
    if len(claims) < 2:
        return []
    claim_lines = [f'- [{c.get("claim_id")}] {str(c.get("text") or "")[:240]}' for c in claims]
    ctx = _tool_ctx(project_root, state_dir)
    if parent_agent_id:
        emit_agent_phase(
            state_dir, project_root, operation_id,
            agent_id=parent_agent_id, role='researcher',
            phase='checking_consistency', status='running', topic_slug=slug,
            summary='Spawning contradiction-check specialist over accumulated claims.'
        )
    res = execute_spawn_shade({
        'goal': (
            f'Libris contradiction check for topic `{slug}` ({title}).\n\n'
            'Analyze the following accumulated claims and identify any contradictions, tensions, uncertainty clusters, '
            'or apparent disagreements in evidence. Return a concise contradiction map and recommended researcher follow-ups.\n\n'
            'Claims:\n' + '\n'.join(claim_lines)
        ),
        'constraints': [
            f'Operation ID: {operation_id}',
            f'Topic slug: {slug}',
            'Focus on contradiction/tension detection only.',
            'Be explicit about whether a contradiction is strong, weak, or only an uncertainty/tension.',
        ],
        'expected_outputs': [
            'Contradiction map across claims',
            'Uncertainty/tension summary',
            'Recommended follow-up checks for the researcher',
        ],
        'contract_type': 'libris_contradiction_check',
        'metadata': {
            'operation_id': operation_id,
            'topic_slug': slug,
            'claim_count': len(claims),
        },
        'phase_specs': [
            {'name': 'scan', 'objective': 'Scan the provided claims and group related assertions.'},
            {'name': 'compare', 'objective': 'Compare claims for contradictions, tensions, or unresolved uncertainty.'},
            {'name': 'return', 'objective': 'Return a contradiction map and concrete researcher follow-up guidance.'},
        ],
    }, ctx)
    details = res.details or {}
    contracts: list[str] = []
    if details.get('contract_id'):
        contracts.append(str(details.get('contract_id') or ''))
    if details.get('shade_id') and parent_agent_id:
        emit_agent_comm(
            state_dir, project_root, operation_id,
            from_agent_id=parent_agent_id,
            to_agent_id=str(details.get('shade_id') or ''),
            from_role='researcher', to_role='shade', topic_slug=slug,
            message_kind='contradiction_check_assignment',
            summary='Contradiction check assigned across topic claims.',
        )
    append_operation_event(state_dir, project_root, operation_id, 'topic_contradiction_check_spawned', {
        'topic_slug': slug,
        'count': len(contracts),
        'claim_count': len(claims),
    })
    return contracts



def ingest_contradiction_check_contracts(
    state_dir: Path,
    project_root: Path,
    operation_id: str,
    topic_slug: str,
) -> dict[str, Any]:
    from libris_runtime import list_topic_contracts, summarize_contract_for_swarm, save_evidence, append_operation_event

    contracts = list_topic_contracts(
        state_dir,
        project_root,
        operation_id=operation_id,
        topic_slug=topic_slug,
        contract_type='libris_contradiction_check',
    )
    ingested = []
    evidence_blocks: list[str] = []
    for ctr in contracts:
        if str(ctr.get('status') or '') != 'completed':
            continue
        rec = summarize_contract_for_swarm(ctr, state_dir)
        payload = rec.get('last_event_payload') or {}
        latest_summary = str(payload.get('summary') or '').strip()
        phase_summaries = [str(p.get('result_summary') or '').strip() for p in (ctr.get('phases') or []) if p.get('result_summary')]
        merged_summary = '\n'.join([s for s in [latest_summary, *phase_summaries] if s]).strip()
        if not merged_summary:
            continue
        evidence_blocks.append('\n'.join([
            f'## Contradiction check: {rec.get("contract_id")}',
            f'- Contract: {ctr.get("id")}',
            '',
            merged_summary[:3000],
            '',
        ]))
        ingested.append({
            'contract_id': ctr.get('id'),
            'summary_preview': merged_summary[:160],
        })

    evidence_path = None
    if evidence_blocks:
        res = save_evidence(
            state_dir,
            project_root,
            operation_id,
            topic_slug,
            markdown='# Contradiction Check Ingestion\n\n' + '\n'.join(evidence_blocks),
            filename=f'{topic_slug}-contradiction-check.md',
        )
        evidence_path = res.get('path')

    append_operation_event(state_dir, project_root, operation_id, 'contradiction_check_contracts_ingested', {
        'topic_slug': topic_slug,
        'count': len(ingested),
        'evidence_path': evidence_path or '',
    })
    return {
        'topic_slug': topic_slug,
        'ingested': ingested,
        'evidence_path': evidence_path,
    }
