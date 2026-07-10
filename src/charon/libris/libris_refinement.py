from __future__ import annotations

import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    from charon.infra.diagnostics import record as _diag
except Exception:  # diagnostics is best-effort and must never block import
    def _diag(*args, **kwargs):
        return None


def _read_text(path_str: str) -> str:
    try:
        p = Path(str(path_str or ''))
        if p.exists():
            return p.read_text(encoding='utf-8', errors='replace')
    except Exception as e:
        _diag('libris_refinement', 'artifact text unreadable; treating as empty', error=e, path=path_str)
    return ''


def extract_followup_tasks_from_critique(critique_markdown: str, *, limit: int = 4) -> list[str]:
    text = str(critique_markdown or '').strip()
    if not text:
        return []

    tasks: list[str] = []
    priority_patterns = [
        r'\bmissing\b',
        r'\bweak\b',
        r'\binsufficient\b',
        r'\bneeds?\b',
        r'\bshould\b',
        r'\brevise\b',
        r'\badd\b',
        r'\bclarify\b',
        r'\bcompare\b',
        r'\bevidence\b',
        r'\bcitation\b',
        r'\bbenchmark\b',
        r'\buncertaint(?:y|ies)\b',
    ]

    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        line = re.sub(r'^[-*•]\s*', '', line)
        line = re.sub(r'^\d+[.)]\s*', '', line)
        norm = line.lower()
        if len(line) < 24:
            continue
        if any(re.search(p, norm) for p in priority_patterns):
            tasks.append(line[:300])

    if not tasks:
        sentences = re.split(r'(?<=[.!?])\s+', text)
        for sent in sentences:
            s = sent.strip()
            if len(s) < 24:
                continue
            norm = s.lower()
            if any(re.search(p, norm) for p in priority_patterns):
                tasks.append(s[:300])

    deduped: list[str] = []
    seen = set()
    for task in tasks:
        key = re.sub(r'\W+', ' ', task.lower()).strip()
        if not key or key in seen:
            continue
        seen.add(key)
        deduped.append(task)
        if len(deduped) >= max(1, limit):
            break
    return deduped


def _claim_candidates(text: str, limit: int = 4) -> list[str]:
    out = []
    for raw in (text or '').splitlines():
        s = raw.strip()
        if not s:
            continue
        s = re.sub(r'^[-*•]\s*', '', s)
        if len(s) < 30:
            continue
        out.append(s[:400])
    if not out:
        for part in re.split(r'(?<=[.!?])\s+', (text or '').strip()):
            s = part.strip()
            if len(s) >= 30:
                out.append(s[:400])
    return out[:limit]



def wait_for_gap_fill_contracts(
    state_dir: Path,
    project_root: Path,
    operation_id: str,
    topic_slug: str,
    *,
    min_completed: int = 1,
    timeout_seconds: int = 20,
) -> list[dict[str, Any]]:
    from charon.libris.libris_runtime import list_topic_contracts

    deadline = time.time() + max(1, timeout_seconds)
    latest: list[dict[str, Any]] = []
    while time.time() < deadline:
        latest = list_topic_contracts(
            state_dir,
            project_root,
            operation_id=operation_id,
            topic_slug=topic_slug,
            contract_type='libris_gap_fill',
        )
        done = [c for c in latest if str(c.get('status') or '') == 'completed']
        if len(done) >= min_completed:
            return latest
        time.sleep(2)
    return latest



def ingest_gap_fill_contracts(
    state_dir: Path,
    project_root: Path,
    operation_id: str,
    topic_slug: str,
) -> dict[str, Any]:
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
        contract_type='libris_gap_fill',
    )

    ingested = []
    evidence_blocks: list[str] = []
    for ctr in contracts:
        if str(ctr.get('status') or '') != 'completed':
            continue
        rec = summarize_contract_for_swarm(ctr, state_dir)
        meta = rec.get('metadata') or {}
        task = str(meta.get('follow_up_task') or '').strip()
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
            title=f'Gap-fill finding: {task[:80] or rec.get("contract_id")}',
            url=f'contract:{ctr.get("id")}',
            source_type='gap_fill_summary',
            operation_id=operation_id,
            credibility='medium',
            tags=['libris', 'gap-fill', 'shade'],
            extracted_text=merged_summary[:12000],
        )
        claims = []
        for claim_text in _claim_candidates(merged_summary):
            claims.append(add_claim(
                state_dir,
                project_root,
                topic_slug=topic_slug,
                source_id=str(source.get('source_id') or ''),
                text=claim_text,
                operation_id=operation_id,
                confidence='medium',
                stance='supports',
            ))

        evidence_blocks.append('\n'.join([
            f'## Gap-fill task: {task or "(unspecified)"}',
            f'- Contract: {ctr.get("id")}',
            f'- Claims extracted: {len(claims)}',
            '',
            merged_summary[:2500],
            '',
        ]))
        ingested.append({
            'contract_id': ctr.get('id'),
            'task': task,
            'source_id': source.get('source_id'),
            'claim_count': len(claims),
        })

    evidence_path = None
    if evidence_blocks:
        res = save_evidence(
            state_dir,
            project_root,
            operation_id,
            topic_slug,
            markdown='# Gap-Fill Ingestion\n\n' + '\n'.join(evidence_blocks),
            filename=f'{topic_slug}-gap-fill-ingested.md',
        )
        evidence_path = res.get('path')

    append_operation_event(state_dir, project_root, operation_id, 'gap_fill_contracts_ingested', {
        'topic_slug': topic_slug,
        'count': len(ingested),
        'evidence_path': evidence_path or '',
    })
    return {
        'topic_slug': topic_slug,
        'ingested': ingested,
        'evidence_path': evidence_path,
    }



def plan_critique_followups(
    state_dir: Path,
    project_root: Path,
    operation_id: str,
    topic_slug: str,
    *,
    max_tasks: int = 3,
    spawn_gap_fill: bool = True,
) -> dict[str, Any]:
    from charon.libris.libris_runtime import (
        get_topic_state,
        update_topic_runtime,
        append_operation_event,
        emit_agent_phase,
        emit_agent_comm,
        save_evidence,
    )
    from charon.tools.shade_tool import execute_spawn_shade
    from charon.libris.libris_orchestrator import _tool_ctx

    topic = get_topic_state(state_dir, project_root, operation_id, topic_slug)
    if not topic:
        return {'topic_slug': topic_slug, 'follow_up_tasks': [], 'contracts': []}

    latest_checkpoint = topic.get('latest_checkpoint') or {}
    critique_path = str(latest_checkpoint.get('critique_path') or '')
    critique_md = _read_text(critique_path)
    tasks = extract_followup_tasks_from_critique(critique_md, limit=max_tasks)

    guidance_lines = [f'# Critique Follow-up Plan: {topic_slug}', '']
    if tasks:
        guidance_lines.append('## Required fixes')
        guidance_lines.extend([f'- {t}' for t in tasks])
        guidance_lines.append('')
        guidance_lines.append('Address these issues directly in the next revision.')
    guidance_md = '\n'.join(guidance_lines).strip()

    contracts: list[str] = []
    if guidance_md:
        try:
            save_evidence(
                state_dir,
                project_root,
                operation_id,
                topic_slug,
                markdown=guidance_md,
                filename=f'{topic_slug}-critique-followup.md',
            )
        except Exception as e:
            _diag('libris_refinement', 'saving critique-followup guidance failed; revision proceeds without written plan', error=e, topic_slug=topic_slug)

    if tasks and spawn_gap_fill:
        ctx = _tool_ctx(project_root, state_dir)
        parent_agent_id = str(topic.get('researcher_agent_id') or '')
        for i, task in enumerate(tasks[:2], start=1):
            res = execute_spawn_shade({
                'goal': (
                    f'Libris gap-fill support for topic `{topic_slug}`.\n'
                    f'Judge-identified deficiency to address: {task}\n\n'
                    f'Objective: gather missing evidence, comparisons, caveats, or implementation-significance '
                    f'that would help a researcher strengthen the next draft revision.'
                ),
                'constraints': [
                    f'Operation ID: {operation_id}',
                    f'Topic slug: {topic_slug}',
                    'Focus only on filling the specific critique gap.',
                    'Return concise, source-aware findings.',
                ],
                'expected_outputs': [
                    'A concise gap-fill summary',
                    'Specific evidence or comparisons that strengthen the draft',
                    'Open uncertainties that remain unresolved',
                ],
                'contract_type': 'libris_gap_fill',
                'metadata': {
                    'operation_id': operation_id,
                    'topic_slug': topic_slug,
                    'follow_up_task': task,
                    'task_index': i,
                },
                'phase_specs': [
                    {'name': 'scoping', 'objective': f'Understand the critique gap: {task}'},
                    {'name': 'gap_fill', 'objective': 'Gather the strongest missing evidence, comparisons, or clarifications.'},
                    {'name': 'return', 'objective': 'Summarize exactly what the researcher should add or revise.'},
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
                    from_role='researcher', to_role='shade', topic_slug=topic_slug,
                    message_kind='gap_fill_assignment',
                    summary=f'Gap-fill task assigned: {task[:140]}',
                )
        if parent_agent_id:
            emit_agent_phase(
                state_dir, project_root, operation_id,
                agent_id=parent_agent_id, role='researcher',
                phase='gap_fill', status='running', topic_slug=topic_slug,
                summary=f'Preparing revision against {len(tasks)} judge-identified issue(s).'
            )

    update_topic_runtime(
        state_dir,
        project_root,
        operation_id,
        topic_slug,
        extras={
            'follow_up_tasks': tasks,
            'follow_up_contract_ids': contracts,
            'follow_up_generated_at': datetime.now(timezone.utc).isoformat(),
        },
    )
    append_operation_event(state_dir, project_root, operation_id, 'critique_followups_planned', {
        'topic_slug': topic_slug,
        'task_count': len(tasks),
        'contract_count': len(contracts),
        'tasks': tasks,
    })
    return {
        'topic_slug': topic_slug,
        'follow_up_tasks': tasks,
        'contracts': contracts,
        'guidance_markdown': guidance_md,
    }
