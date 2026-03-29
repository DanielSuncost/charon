"""Research tool — Libris project/operation persistence and indexing.

Provides the storage/runtime backbone for Libris:
- ensure research-enabled project metadata exists
- create research operations and topics
- persist sources, claims, evidence, and checkpoints
- mark best checkpoints and finalize delivery
- search project-local research artifacts
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from tools import ToolContext, ToolResult


RESEARCH_TOOL_DEF = {
    'name': 'Research',
    'description': (
        'Libris research persistence and runtime management. '
        'Use this to initialize research operations, topics, sources, claims, evidence, '
        'and checkpointed report iterations inside the current project. '
        'Also supports project-local search over research sources and claims.'
    ),
    'input_schema': {
        'type': 'object',
        'properties': {
            'action': {
                'type': 'string',
                'enum': [
                    'ensure_project', 'init_operation', 'get_operation_state', 'request_stop',
                    'update_operation_budget', 'record_usage', 'get_budget_status',
                    'start_autonomous_project', 'get_swarm_state',
                    'spawn_coordinator', 'spawn_researcher', 'spawn_judge',
                    'save_candidate_topics', 'init_topic', 'get_topic_state',
                    'index_promising_source', 'list_promising_sources', 'search_promising_sources',
                    'add_source', 'add_claim', 'save_evidence', 'save_report_draft',
                    'save_checkpoint', 'list_checkpoints', 'mark_best_checkpoint', 'finalize_delivery', 'finalize_operation_selection',
                    'search_sources', 'search_claims', 'rebuild_index',
                ],
                'description': 'Research tool action.',
            },
            'kind': {
                'type': 'string',
                'enum': ['software', 'research', 'hybrid'],
                'description': 'Project kind for ensure_project/init_operation.',
            },
            'research_mode': {
                'type': 'string',
                'description': 'Research mode hint, e.g. exploratory, literature, audit, product.',
            },
            'summary': {
                'type': 'string',
                'description': 'Optional project or delivery summary.',
            },
            'tags': {
                'type': 'array',
                'items': {'type': 'string'},
                'description': 'Optional project tags.',
            },
            'prompt': {
                'type': 'string',
                'description': 'Broad research prompt for init_operation.',
            },
            'mode': {
                'type': 'string',
                'description': 'Operation mode. Default: autonomous_research_operation.',
            },
            'budget': {
                'type': 'object',
                'description': 'Operation or topic budget object: max_wall_hours, max_total_tokens, max_total_cost_usd, max_topics, etc.',
            },
            'model_policy': {
                'type': 'object',
                'description': 'Role-to-model policy, e.g. coordinator=strong, shade=cheap_local.',
            },
            'topic_budget': {
                'type': 'object',
                'description': 'Optional budget override for one topic.',
            },
            'model_policy_override': {
                'type': 'object',
                'description': 'Optional per-topic model policy override.',
            },
            'operation_id': {
                'type': 'string',
                'description': 'Research operation ID.',
            },
            'topic_slug': {
                'type': 'string',
                'description': 'Topic slug.',
            },
            'title': {
                'type': 'string',
                'description': 'Topic or source title.',
            },
            'why_interesting': {
                'type': 'string',
                'description': 'Why a topic is worth deeper research.',
            },
            'focus_questions': {
                'type': 'array',
                'items': {'type': 'string'},
                'description': 'Optional focus questions for a topic.',
            },
            'topics': {
                'type': 'array',
                'items': {'type': 'object'},
                'description': 'Candidate topic objects for save_candidate_topics.',
            },
            'plan_markdown': {
                'type': 'string',
                'description': 'Optional coordinator plan markdown.',
            },
            'url': {
                'type': 'string',
                'description': 'Source URL.',
            },
            'source_type': {
                'type': 'string',
                'description': 'Source type: web, paper, repo, doc, etc.',
            },
            'authors': {
                'type': 'array',
                'items': {'type': 'string'},
                'description': 'Optional source authors.',
            },
            'published_at': {
                'type': 'string',
                'description': 'Optional published date.',
            },
            'credibility': {
                'type': 'string',
                'description': 'Credibility hint: high, medium, low, unknown.',
            },
            'extracted_text': {
                'type': 'string',
                'description': 'Optional extracted source text to save as a snapshot.',
            },
            'source_id': {
                'type': 'string',
                'description': 'Source ID for add_claim.',
            },
            'text': {
                'type': 'string',
                'description': 'Claim text or stop reason depending on action.',
            },
            'confidence': {
                'type': 'string',
                'description': 'Claim confidence.',
            },
            'stance': {
                'type': 'string',
                'description': 'Claim stance: supports, contradicts, unclear.',
            },
            'entity_refs': {
                'type': 'array',
                'items': {'type': 'string'},
                'description': 'Optional entity references for a claim.',
            },
            'markdown': {
                'type': 'string',
                'description': 'Markdown content for save_evidence or save_report_draft.',
            },
            'filename': {
                'type': 'string',
                'description': 'Optional evidence filename.',
            },
            'report_markdown': {
                'type': 'string',
                'description': 'Checkpoint report markdown.',
            },
            'critique_markdown': {
                'type': 'string',
                'description': 'Checkpoint critique markdown.',
            },
            'summary_markdown': {
                'type': 'string',
                'description': 'Checkpoint summary markdown.',
            },
            'metrics': {
                'type': 'object',
                'description': 'Optional score breakdown for a checkpoint.',
            },
            'score': {
                'type': 'number',
                'description': 'Optional overall checkpoint score.',
            },
            'checkpoint_id': {
                'type': 'string',
                'description': 'Checkpoint ID for mark/finalize/usage attribution.',
            },
            'selector': {
                'type': 'string',
                'description': 'Who is selecting a best checkpoint, e.g. judge or researcher.',
            },
            'role': {
                'type': 'string',
                'description': 'Agent role for usage tracking: coordinator, judge, researcher, shade.',
            },
            'model': {
                'type': 'string',
                'description': 'Model or model tier used for usage tracking.',
            },
            'input_tokens': {
                'type': 'number',
                'description': 'Input token count for usage tracking.',
            },
            'output_tokens': {
                'type': 'number',
                'description': 'Output token count for usage tracking.',
            },
            'estimated_cost_usd': {
                'type': 'number',
                'description': 'Optional explicit cost estimate for usage tracking.',
            },
            'query': {
                'type': 'string',
                'description': 'Search query for search_sources/search_claims.',
            },
            'limit': {
                'type': 'number',
                'description': 'Search result limit. Default: 10.',
            },
        },
        'required': ['action'],
    },
}


def _state_dir(ctx: ToolContext) -> Path | None:
    return ctx.state_dir or (ctx.project_root / '.charon_state')


def execute_research(params: dict, ctx: ToolContext) -> ToolResult:
    action = str(params.get('action', '')).strip().lower()
    state_dir = _state_dir(ctx)
    if not state_dir:
        return ToolResult(content='Error: state_dir not available.', is_error=True)

    try:
        from libris_runtime import (
            ensure_project_metadata,
            ensure_research_tree,
            init_operation,
            get_operation_state,
            request_stop,
            update_operation_budget,
            record_usage,
            get_budget_status,
            get_libris_swarm_state,
            save_candidate_topics,
            init_topic,
            get_topic_state,
            add_source,
            add_claim,
            index_promising_source,
            list_promising_sources,
            search_promising_sources,
            save_evidence,
            save_report_draft,
            save_checkpoint,
            list_checkpoints,
            mark_best_checkpoint,
            finalize_delivery,
            finalize_operation_selection,
            search_sources,
            search_claims,
            rebuild_project_index,
        )
    except Exception as e:
        return ToolResult(content=f'Error loading Libris runtime: {e}', is_error=True)

    try:
        if action == 'ensure_project':
            meta = ensure_project_metadata(
                state_dir,
                ctx.project_root,
                kind=params.get('kind') or 'research',
                research_mode=params.get('research_mode'),
                tags=params.get('tags') or [],
                summary=str(params.get('summary') or '').strip(),
            )
            tree = ensure_research_tree(state_dir, ctx.project_root)
            return ToolResult(
                content=(
                    f'Research project ready: {meta.get("name")} ({meta.get("id")})\n'
                    f'Kind: {meta.get("kind")}\n'
                    f'Research mode: {meta.get("research_mode") or "(none)"}\n'
                    f'Research root: {tree.get("research_root")}'
                ),
                details={'project': meta, 'paths': tree},
            )

        if action == 'init_operation':
            prompt = str(params.get('prompt') or '').strip()
            if not prompt:
                return ToolResult(content='Error: prompt is required for init_operation.', is_error=True)
            op = init_operation(
                state_dir,
                ctx.project_root,
                prompt=prompt,
                mode=str(params.get('mode') or 'autonomous_research_operation'),
                coordinator_agent_id=ctx.agent_id,
                kind=str(params.get('kind') or 'research'),
                research_mode=str(params.get('research_mode') or 'exploratory'),
                summary=str(params.get('summary') or '').strip(),
                budget=dict(params.get('budget') or {}),
                model_policy=dict(params.get('model_policy') or {}),
            )
            return ToolResult(
                content=(
                    f'Research operation created: {op.get("operation_id")}\n'
                    f'Mode: {op.get("mode")}\n'
                    f'Status: {op.get("status")}\n'
                    f'Prompt: {str(op.get("prompt") or "")[:200]}'
                ),
                details=op,
            )

        if action == 'get_operation_state':
            op_id = str(params.get('operation_id') or '').strip()
            if not op_id:
                return ToolResult(content='Error: operation_id is required.', is_error=True)
            op = get_operation_state(state_dir, ctx.project_root, op_id)
            if not op:
                return ToolResult(content=f'No operation found: {op_id}', is_error=True)
            topic_count = len(op.get('topics') or [])
            event_count = len(op.get('events_tail') or [])
            return ToolResult(
                content=(
                    f'Operation: {op_id}\n'
                    f'Status: {op.get("status")}\n'
                    f'Stop requested: {op.get("stop_requested")}\n'
                    f'Topics: {topic_count}\n'
                    f'Recent events: {event_count}'
                ),
                details=op,
            )

        if action == 'request_stop':
            op_id = str(params.get('operation_id') or '').strip()
            if not op_id:
                return ToolResult(content='Error: operation_id is required.', is_error=True)
            op = request_stop(state_dir, ctx.project_root, op_id, str(params.get('text') or '').strip())
            if not op:
                return ToolResult(content=f'No operation found: {op_id}', is_error=True)
            return ToolResult(content=f'Stop requested for operation: {op_id}', details=op)

        if action == 'update_operation_budget':
            op_id = str(params.get('operation_id') or '').strip()
            if not op_id:
                return ToolResult(content='Error: operation_id is required.', is_error=True)
            op = update_operation_budget(
                state_dir,
                ctx.project_root,
                op_id,
                budget=dict(params.get('budget') or {}),
                model_policy=dict(params.get('model_policy') or {}),
            )
            if not op:
                return ToolResult(content=f'No operation found: {op_id}', is_error=True)
            return ToolResult(content=f'Updated budget/model policy for {op_id}.', details=op)

        if action == 'record_usage':
            op_id = str(params.get('operation_id') or '').strip()
            if not op_id:
                return ToolResult(content='Error: operation_id is required.', is_error=True)
            usage = record_usage(
                state_dir,
                ctx.project_root,
                op_id,
                role=str(params.get('role') or ''),
                model=str(params.get('model') or ''),
                topic_slug=str(params.get('topic_slug') or ''),
                input_tokens=int(params.get('input_tokens') or 0),
                output_tokens=int(params.get('output_tokens') or 0),
                estimated_cost_usd=params.get('estimated_cost_usd'),
                checkpoint_id=str(params.get('checkpoint_id') or ''),
                note=str(params.get('text') or '').strip(),
            )
            if not usage:
                return ToolResult(content=f'No operation found: {op_id}', is_error=True)
            bs = usage.get('budget_status') or {}
            return ToolResult(
                content=(
                    f'Usage recorded for {op_id}.\n'
                    f'Total tokens: {(usage.get("usage") or {}).get("total_tokens", 0)}\n'
                    f'Estimated cost: ${(usage.get("usage") or {}).get("estimated_cost_usd", 0):.4f}\n'
                    f'Continue running: {bs.get("continue_running")}'
                ),
                details=usage,
            )

        if action == 'get_budget_status':
            op_id = str(params.get('operation_id') or '').strip()
            if not op_id:
                return ToolResult(content='Error: operation_id is required.', is_error=True)
            status = get_budget_status(state_dir, ctx.project_root, op_id)
            if not status:
                return ToolResult(content=f'No operation found: {op_id}', is_error=True)
            return ToolResult(
                content=(
                    f'Budget status for {op_id}:\n'
                    f'Continue running: {status.get("continue_running")}\n'
                    f'Reasons: {", ".join(status.get("reasons") or []) or "(none)"}\n'
                    f'Wall hours elapsed: {status.get("wall_hours_elapsed")}\n'
                    f'Total tokens: {(status.get("usage") or {}).get("total_tokens", 0)}\n'
                    f'Estimated cost: ${(status.get("usage") or {}).get("estimated_cost_usd", 0):.4f}'
                ),
                details=status,
            )

        if action == 'start_autonomous_project':
            prompt = str(params.get('prompt') or '').strip()
            if not prompt:
                return ToolResult(content='Error: prompt is required.', is_error=True)
            from libris_agents import start_autonomous_libris_research
            res = start_autonomous_libris_research(
                state_dir,
                ctx.project_root,
                prompt=prompt,
                parent_agent_id=ctx.agent_id,
                budget=dict(params.get('budget') or {}),
                model_policy=dict(params.get('model_policy') or {}),
            )
            op = res.get('operation') or {}
            coord = res.get('coordinator') or {}
            return ToolResult(
                content=(
                    f'Libris autonomous research started.\n'
                    f'Operation: {op.get("operation_id")}\n'
                    f'Status: {op.get("status")}\n'
                    f'Coordinator: {coord.get("id")} ({coord.get("name")})'
                ),
                details=res,
            )

        if action == 'get_swarm_state':
            op_id = str(params.get('operation_id') or '').strip()
            if not op_id:
                return ToolResult(content='Error: operation_id is required.', is_error=True)
            swarm = get_libris_swarm_state(state_dir, ctx.project_root, op_id)
            if not swarm:
                return ToolResult(content=f'No operation found: {op_id}', is_error=True)
            lines = [
                f'Operation: {swarm.get("operation_id")}',
                f'Status: {swarm.get("status")}',
                f'Topics: {len(swarm.get("topics") or [])}',
            ]
            coord = swarm.get('coordinator') or {}
            if coord:
                lines.append(f'Coordinator: {coord.get("name")} [{coord.get("status")}]')
            for topic in swarm.get('topics') or []:
                lines.append(f'- {topic.get("title")} ({topic.get("topic_slug")}) [{topic.get("status")}/{topic.get("phase")}]')
                researcher = topic.get('researcher') or {}
                judge = topic.get('judge') or {}
                if researcher:
                    lines.append(f'  researcher: {researcher.get("name")} [{researcher.get("status")}/{researcher.get("phase") or ""}]')
                if judge:
                    lines.append(f'  judge: {judge.get("name")} [{judge.get("status")}/{judge.get("phase") or ""}]')
                shades = topic.get('shades') or []
                if shades:
                    lines.append(f'  shades: {len(shades)}')
                    for sh in shades[:3]:
                        lines.append(f'    - {sh.get("name")} [{sh.get("status")}/{sh.get("phase") or ""}] {sh.get("contract_type") or ""}')
            if swarm.get('edges'):
                lines.append(f'Active edges: {sum(1 for e in swarm.get("edges") or [] if e.get("active_now"))}')
            if swarm.get('final_selection_markdown'):
                lines.append('')
                lines.append('Final selection available.')
            return ToolResult(content='\n'.join(lines), details=swarm)

        if action == 'spawn_coordinator':
            op_id = str(params.get('operation_id') or '').strip()
            goal = str(params.get('prompt') or params.get('summary') or '').strip()
            if not op_id:
                return ToolResult(content='Error: operation_id is required.', is_error=True)
            from libris_agents import spawn_libris_role
            agent = spawn_libris_role(
                state_dir,
                ctx.project_root,
                role='coordinator',
                operation_id=op_id,
                user_goal=goal,
                parent_agent_id=ctx.agent_id,
            )
            return ToolResult(
                content=f'Libris coordinator spawned: {agent.get("id")} ({agent.get("name")})',
                details=agent,
            )

        if action == 'spawn_researcher':
            op_id = str(params.get('operation_id') or '').strip()
            topic_slug = str(params.get('topic_slug') or '').strip()
            goal = str(params.get('summary') or params.get('prompt') or '').strip()
            if not op_id or not topic_slug:
                return ToolResult(content='Error: operation_id and topic_slug are required.', is_error=True)
            from libris_agents import spawn_libris_role
            agent = spawn_libris_role(
                state_dir,
                ctx.project_root,
                role='researcher',
                operation_id=op_id,
                topic_slug=topic_slug,
                user_goal=goal,
                parent_agent_id=ctx.agent_id,
            )
            return ToolResult(
                content=f'Libris researcher spawned: {agent.get("id")} ({agent.get("name")}) for {topic_slug}',
                details=agent,
            )

        if action == 'spawn_judge':
            op_id = str(params.get('operation_id') or '').strip()
            topic_slug = str(params.get('topic_slug') or '').strip()
            goal = str(params.get('summary') or params.get('prompt') or '').strip()
            if not op_id or not topic_slug:
                return ToolResult(content='Error: operation_id and topic_slug are required.', is_error=True)
            from libris_agents import spawn_libris_role
            agent = spawn_libris_role(
                state_dir,
                ctx.project_root,
                role='judge',
                operation_id=op_id,
                topic_slug=topic_slug,
                user_goal=goal,
                parent_agent_id=ctx.agent_id,
            )
            return ToolResult(
                content=f'Libris judge spawned: {agent.get("id")} ({agent.get("name")}) for {topic_slug}',
                details=agent,
            )

        if action == 'save_candidate_topics':
            op_id = str(params.get('operation_id') or '').strip()
            if not op_id:
                return ToolResult(content='Error: operation_id is required.', is_error=True)
            res = save_candidate_topics(
                state_dir,
                ctx.project_root,
                op_id,
                topics=list(params.get('topics') or []),
                plan_markdown=str(params.get('plan_markdown') or ''),
            )
            return ToolResult(
                content=f'Saved {res.get("count", 0)} candidate topics for {op_id}.',
                details=res,
            )

        if action == 'init_topic':
            op_id = str(params.get('operation_id') or '').strip()
            title = str(params.get('title') or '').strip()
            if not op_id or not title:
                return ToolResult(content='Error: operation_id and title are required.', is_error=True)
            topic = init_topic(
                state_dir,
                ctx.project_root,
                op_id,
                title=title,
                why_interesting=str(params.get('why_interesting') or '').strip(),
                researcher_agent_id=ctx.agent_id,
                judge_agent_id='',
                focus_questions=list(params.get('focus_questions') or []),
                topic_budget=dict(params.get('topic_budget') or params.get('budget') or {}),
                model_policy_override=dict(params.get('model_policy_override') or {}),
            )
            return ToolResult(
                content=(
                    f'Topic initialized: {topic.get("title")}\n'
                    f'Topic ID: {topic.get("topic_id")}\n'
                    f'Slug: {topic.get("slug")}'
                ),
                details=topic,
            )

        if action == 'get_topic_state':
            op_id = str(params.get('operation_id') or '').strip()
            topic_slug = str(params.get('topic_slug') or '').strip()
            if not op_id or not topic_slug:
                return ToolResult(content='Error: operation_id and topic_slug are required.', is_error=True)
            topic = get_topic_state(state_dir, ctx.project_root, op_id, topic_slug)
            if not topic:
                return ToolResult(content=f'No topic found: {topic_slug}', is_error=True)
            return ToolResult(
                content=(
                    f'Topic: {topic.get("title")}\n'
                    f'Status: {topic.get("status")}\n'
                    f'Checkpoints: {topic.get("checkpoint_count", 0)}\n'
                    f'Best checkpoint: {topic.get("best_checkpoint_id") or "(none)"}'
                ),
                details=topic,
            )

        if action == 'index_promising_source':
            op_id = str(params.get('operation_id') or '').strip()
            topic_slug = str(params.get('topic_slug') or '').strip()
            title = str(params.get('title') or '').strip()
            url = str(params.get('url') or '').strip()
            if not op_id or not topic_slug or not title:
                return ToolResult(content='Error: operation_id, topic_slug, and title are required.', is_error=True)
            row = index_promising_source(
                state_dir,
                ctx.project_root,
                operation_id=op_id,
                topic_slug=topic_slug,
                query=str(params.get('query') or title),
                source={
                    'title': title,
                    'url': url,
                    'source_type': str(params.get('source_type') or 'web'),
                    'backend': str(params.get('backend') or ''),
                    'snippet': str(params.get('text') or ''),
                    'abstract': str(params.get('extracted_text') or ''),
                    'published_at': str(params.get('published_at') or ''),
                },
            )
            return ToolResult(content=f'Indexed promising source: {row.get("lead_id")} score={row.get("lead_score")}', details=row)

        if action == 'list_promising_sources':
            items = list_promising_sources(
                state_dir,
                ctx.project_root,
                operation_id=str(params.get('operation_id') or ''),
                topic_slug=str(params.get('topic_slug') or ''),
                limit=int(params.get('limit') or 20),
            )
            if not items:
                return ToolResult(content='No promising sources found.')
            lines = ['Promising sources:']
            for item in items:
                lines.append(f'- {item.get("lead_id")} score={item.get("lead_score")} {item.get("title")}')
            return ToolResult(content='\n'.join(lines), details={'results': items})

        if action == 'search_promising_sources':
            query = str(params.get('query') or '').strip()
            if not query:
                return ToolResult(content='Error: query is required.', is_error=True)
            items = search_promising_sources(state_dir, ctx.project_root, query, int(params.get('limit') or 10))
            if not items:
                return ToolResult(content=f'No promising source matches for: {query}')
            lines = [f'Promising source matches for "{query}":']
            for item in items:
                lines.append(f'- {item.get("lead_id")} score={item.get("lead_score")} {item.get("title")}')
            return ToolResult(content='\n'.join(lines), details={'results': items})

        if action == 'add_source':
            title = str(params.get('title') or '').strip()
            url = str(params.get('url') or '').strip()
            topic_slug = str(params.get('topic_slug') or '').strip()
            if not title or not url or not topic_slug:
                return ToolResult(content='Error: title, url, and topic_slug are required.', is_error=True)
            row = add_source(
                state_dir,
                ctx.project_root,
                topic_slug=topic_slug,
                title=title,
                url=url,
                source_type=str(params.get('source_type') or 'web'),
                operation_id=str(params.get('operation_id') or ''),
                authors=list(params.get('authors') or []),
                published_at=params.get('published_at'),
                credibility=str(params.get('credibility') or 'unknown'),
                tags=list(params.get('tags') or []),
                extracted_text=str(params.get('extracted_text') or ''),
            )
            return ToolResult(
                content=f'Added source: {row.get("source_id")} — {row.get("title")}',
                details=row,
            )

        if action == 'add_claim':
            topic_slug = str(params.get('topic_slug') or '').strip()
            source_id = str(params.get('source_id') or '').strip()
            text = str(params.get('text') or '').strip()
            if not topic_slug or not source_id or not text:
                return ToolResult(content='Error: topic_slug, source_id, and text are required.', is_error=True)
            row = add_claim(
                state_dir,
                ctx.project_root,
                topic_slug=topic_slug,
                source_id=source_id,
                text=text,
                operation_id=str(params.get('operation_id') or ''),
                confidence=str(params.get('confidence') or 'medium'),
                stance=str(params.get('stance') or 'supports'),
                entity_refs=list(params.get('entity_refs') or []),
            )
            return ToolResult(
                content=f'Added claim: {row.get("claim_id")} (source {source_id})',
                details=row,
            )

        if action == 'save_evidence':
            op_id = str(params.get('operation_id') or '').strip()
            topic_slug = str(params.get('topic_slug') or '').strip()
            markdown = str(params.get('markdown') or '')
            if not op_id or not topic_slug or not markdown.strip():
                return ToolResult(content='Error: operation_id, topic_slug, and markdown are required.', is_error=True)
            res = save_evidence(
                state_dir,
                ctx.project_root,
                op_id,
                topic_slug,
                markdown=markdown,
                filename=str(params.get('filename') or '').strip() or None,
            )
            return ToolResult(content=f'Evidence saved: {res.get("path")}', details=res)

        if action == 'save_report_draft':
            op_id = str(params.get('operation_id') or '').strip()
            topic_slug = str(params.get('topic_slug') or '').strip()
            markdown = str(params.get('markdown') or '')
            if not op_id or not topic_slug or not markdown.strip():
                return ToolResult(content='Error: operation_id, topic_slug, and markdown are required.', is_error=True)
            res = save_report_draft(
                state_dir,
                ctx.project_root,
                op_id,
                topic_slug,
                markdown=markdown,
                note=str(params.get('summary') or '').strip(),
            )
            return ToolResult(content=f'Draft report saved: {res.get("path")}', details=res)

        if action == 'save_checkpoint':
            op_id = str(params.get('operation_id') or '').strip()
            topic_slug = str(params.get('topic_slug') or '').strip()
            report_md = str(params.get('report_markdown') or '')
            critique_md = str(params.get('critique_markdown') or '')
            summary_md = str(params.get('summary_markdown') or '')
            if not op_id or not topic_slug:
                return ToolResult(content='Error: operation_id and topic_slug are required.', is_error=True)
            if not report_md.strip() or not critique_md.strip() or not summary_md.strip():
                return ToolResult(content='Error: report_markdown, critique_markdown, and summary_markdown are required.', is_error=True)
            meta = save_checkpoint(
                state_dir,
                ctx.project_root,
                op_id,
                topic_slug,
                report_markdown=report_md,
                critique_markdown=critique_md,
                summary_markdown=summary_md,
                metrics=dict(params.get('metrics') or {}),
                score=params.get('score'),
            )
            return ToolResult(
                content=(
                    f'Checkpoint saved: {meta.get("checkpoint_id")}\n'
                    f'Iteration: {meta.get("iteration")}\n'
                    f'Score: {meta.get("score")}'
                ),
                details=meta,
            )

        if action == 'list_checkpoints':
            op_id = str(params.get('operation_id') or '').strip()
            topic_slug = str(params.get('topic_slug') or '').strip()
            if not op_id or not topic_slug:
                return ToolResult(content='Error: operation_id and topic_slug are required.', is_error=True)
            items = list_checkpoints(state_dir, ctx.project_root, op_id, topic_slug)
            if not items:
                return ToolResult(content=f'No checkpoints for topic: {topic_slug}')
            lines = [f'Checkpoints for {topic_slug}:']
            for item in items:
                lines.append(
                    f'- {item.get("checkpoint_id")}  iter={item.get("iteration")}  score={item.get("score")}  created={item.get("created_at")}'
                )
            return ToolResult(content='\n'.join(lines), details={'checkpoints': items})

        if action == 'mark_best_checkpoint':
            op_id = str(params.get('operation_id') or '').strip()
            topic_slug = str(params.get('topic_slug') or '').strip()
            checkpoint_id = str(params.get('checkpoint_id') or '').strip()
            if not op_id or not topic_slug or not checkpoint_id:
                return ToolResult(content='Error: operation_id, topic_slug, and checkpoint_id are required.', is_error=True)
            item = mark_best_checkpoint(
                state_dir,
                ctx.project_root,
                op_id,
                topic_slug,
                checkpoint_id,
                selector=str(params.get('selector') or 'judge'),
            )
            if not item:
                return ToolResult(content=f'Checkpoint not found: {checkpoint_id}', is_error=True)
            return ToolResult(content=f'Marked best checkpoint: {checkpoint_id}', details=item)

        if action == 'finalize_delivery':
            op_id = str(params.get('operation_id') or '').strip()
            topic_slug = str(params.get('topic_slug') or '').strip()
            checkpoint_id = str(params.get('checkpoint_id') or '').strip()
            if not op_id or not topic_slug or not checkpoint_id:
                return ToolResult(content='Error: operation_id, topic_slug, and checkpoint_id are required.', is_error=True)
            res = finalize_delivery(
                state_dir,
                ctx.project_root,
                op_id,
                topic_slug=topic_slug,
                checkpoint_id=checkpoint_id,
                note=str(params.get('summary') or '').strip(),
            )
            if not res:
                return ToolResult(content='Error finalizing delivery.', is_error=True)
            return ToolResult(content=f'Delivery finalized: {res.get("report_path")}', details=res)

        if action == 'finalize_operation_selection':
            op_id = str(params.get('operation_id') or '').strip()
            if not op_id:
                return ToolResult(content='Error: operation_id is required.', is_error=True)
            res = finalize_operation_selection(state_dir, ctx.project_root, op_id)
            if not res:
                return ToolResult(content=f'No operation found: {op_id}', is_error=True)
            lines = [f'Finalized operation selection for {op_id}:']
            for item in res.get('selections') or []:
                lines.append(f'- {item.get("topic_slug")}: {item.get("checkpoint_id")} score={item.get("score")}')
            return ToolResult(content='\n'.join(lines), details=res)

        if action == 'search_sources':
            query = str(params.get('query') or '').strip()
            if not query:
                return ToolResult(content='Error: query is required.', is_error=True)
            items = search_sources(state_dir, ctx.project_root, query, int(params.get('limit') or 10))
            if not items:
                return ToolResult(content=f'No source matches for: {query}')
            lines = [f'Source matches for "{query}":']
            for item in items:
                lines.append(f'- {item.get("source_id")}  [{item.get("source_type")}] {item.get("title")}')
                lines.append(f'  {item.get("url")}')
            return ToolResult(content='\n'.join(lines), details={'results': items})

        if action == 'search_claims':
            query = str(params.get('query') or '').strip()
            if not query:
                return ToolResult(content='Error: query is required.', is_error=True)
            items = search_claims(state_dir, ctx.project_root, query, int(params.get('limit') or 10))
            if not items:
                return ToolResult(content=f'No claim matches for: {query}')
            lines = [f'Claim matches for "{query}":']
            for item in items:
                lines.append(f'- {item.get("claim_id")}  [{item.get("confidence")}] {str(item.get("text") or "")[:160]}')
            return ToolResult(content='\n'.join(lines), details={'results': items})

        if action == 'rebuild_index':
            idx = rebuild_project_index(state_dir, ctx.project_root)
            return ToolResult(
                content=(
                    f'Research index rebuilt.\n'
                    f'Operations: {len(idx.get("operations") or [])}\n'
                    f'Topics: {len(idx.get("topics") or [])}\n'
                    f'Claims: {idx.get("claim_count", 0)}'
                ),
                details=idx,
            )

        return ToolResult(content=f'Unknown action: {action}', is_error=True)

    except Exception as e:
        return ToolResult(content=f'Research tool error: {e}', is_error=True)
