from __future__ import annotations

import re
from pathlib import Path
from typing import Any


def _claim_candidates(text: str, limit: int = 4) -> list[str]:
    lines = []
    for raw in (text or '').splitlines():
        s = raw.strip()
        if not s:
            continue
        s = re.sub(r'^[-*•]\s*', '', s)
        if len(s) < 30:
            continue
        lines.append(s)
    if not lines:
        parts = re.split(r'(?<=[.!?])\s+', (text or '').strip())
        lines = [p.strip() for p in parts if len(p.strip()) >= 30]
    return lines[:limit]


def ingest_procurement_contracts(
    state_dir: Path,
    project_root: Path,
    operation_id: str,
    topic_slug: str,
) -> dict[str, Any]:
    """Convert completed procurement contracts into canonical sources/claims/evidence.

    This is a pragmatic v1 ingestion path:
    - source comes from contract metadata lead info
    - claims come from latest contract summary payload / phase summaries
    - evidence markdown is appended as a structured procurement evidence note
    """
    from charon.libris.libris_runtime import (
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
        contract_type='libris_source_procurement',
    )

    ingested = []
    evidence_blocks: list[str] = []
    for ctr in contracts:
        rec = summarize_contract_for_swarm(ctr, state_dir)
        meta = rec.get('metadata') or {}
        lead_title = str(meta.get('lead_title') or '').strip()
        lead_url = str(meta.get('lead_url') or '').strip()
        if not lead_title:
            continue

        payload = rec.get('last_event_payload') or {}
        latest_summary = str(payload.get('summary') or '').strip()
        phase_summaries = []
        for phase in (ctr.get('phases') or []):
            if phase.get('result_summary'):
                phase_summaries.append(str(phase.get('result_summary') or '').strip())
        merged_summary = '\n'.join([s for s in [latest_summary, *phase_summaries] if s]).strip()

        source = add_source(
            state_dir,
            project_root,
            topic_slug=topic_slug,
            title=lead_title,
            url=lead_url or f'contract:{ctr.get("id")}',
            source_type='procured_source',
            operation_id=operation_id,
            credibility='medium',
            tags=['libris', 'procurement', 'shade'],
            extracted_text=merged_summary[:12000],
        )

        claims = []
        for claim_text in _claim_candidates(merged_summary):
            claim = add_claim(
                state_dir,
                project_root,
                topic_slug=topic_slug,
                source_id=str(source.get('source_id') or ''),
                text=claim_text,
                operation_id=operation_id,
                confidence='medium',
                stance='supports',
            )
            claims.append(claim)

        evidence_blocks.append(
            '\n'.join([
                f'## Procured source: {lead_title}',
                f'- URL: {lead_url or "(no url)"}',
                f'- Contract: {ctr.get("id")}',
                f'- Claims extracted: {len(claims)}',
                '',
                merged_summary[:2000] if merged_summary else '(no summary)',
                '',
            ])
        )
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
            markdown='# Procurement Ingestion\n\n' + '\n'.join(evidence_blocks),
            filename=f'{topic_slug}-procurement-ingested.md',
        )
        evidence_path = res.get('path')

    append_operation_event(state_dir, project_root, operation_id, 'procurement_contracts_ingested', {
        'topic_slug': topic_slug,
        'count': len(ingested),
        'evidence_path': evidence_path or '',
    })
    return {
        'topic_slug': topic_slug,
        'ingested': ingested,
        'evidence_path': evidence_path,
    }
