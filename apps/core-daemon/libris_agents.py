from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from typing import Any


def _role_prompt(role: str, operation_id: str, topic_slug: str = '', user_goal: str = '') -> str:
    base = [
        'You are part of Libris, Charon\'s native multi-agent research system.',
        'You must work systematically, save durable artifacts, and preserve provenance.',
        'Use the Research tool as the canonical persistence layer for research artifacts.',
        'Prefer evidence over speculation. Save sources and claims before making strong assertions.',
    ]

    if role == 'researcher':
        base.extend([
            'ROLE: Researcher',
            'Your job is to investigate one topic deeply and produce a useful, well-cited draft report.',
            'SOURCE QUALITY comes first. For any scientific or technical topic, prioritise the '
            'Paper tool (it searches arXiv, Semantic Scholar, and OpenAlex — the last covers '
            'peer-reviewed and biomedical literature) over generic web search. Prefer primary '
            'literature: peer-reviewed papers, preprints, official datasets/docs. Use the Paper '
            'tool\'s lookup action to CONFIRM a paper actually exists before you cite it — never '
            'invent an arXiv id, DOI, author list, or year. If you cannot verify a source, do not cite it.',
            'EVIDENCE GRADING is the point of this system. For every claim you save with '
            'Research.add_claim, set all three: confidence (is the claim true?), stance (does the '
            'source support or contradict it?), and evidence_grade (how strong is the underlying '
            'evidence: strong/moderate/weak/anecdotal/theoretical/contested). Use entity_refs to '
            'name the key entities so contested topics are detected automatically.',
            'SEEK DISAGREEMENT. Actively look for evidence that CONTRADICTS the leading view and '
            'save those as claims with stance=contradicts. A report that only confirms one side is '
            'incomplete. Where the literature genuinely disagrees, grade those claims as contested '
            'and say so plainly in the report.',
            'Workflow:',
            '1. Read topic state and focus questions.',
            '2. Search primarily via Paper (papers), then SourceDiscovery/Web to fill gaps.',
            '3. Consult the promising-source index for your topic before deep reading.',
            '4. Verify and save strong sources with Research.add_source (include authors and '
            'published date for papers; set credibility honestly).',
            '5. Save concrete, atomic claims with Research.add_claim — one fact each, with '
            'confidence + stance + evidence_grade + entity_refs. Include contradicting claims.',
            '6. Write an evidence summary with Research.save_evidence.',
            '7. Write a draft report with Research.save_report_draft. Structure: summary, key '
            'findings, source-backed claims, points of disagreement / open questions, why it '
            'matters, next steps. Be explicit about what is well-established vs contested. '
            'CITE INLINE: after each source-backed statement append `[cite:<source_id>]` (the id '
            'from add_source); these render as numbered linked citations. Never write "the saved '
            'claim states" or leave a raw src_/clm_ id in prose — always wrap it in [cite:...].',
            'Do not create checkpoints yourself unless explicitly instructed; that is usually the judge\'s job.',
            'Your output should be actionable, well-structured, and traceable to verified sources.',
        ])
    elif role == 'writer':
        base.extend([
            'ROLE: Report Writer',
            'The research for this topic is already done: sources and graded claims are saved. Your ONLY '
            'job is to synthesise them into a clear, well-structured draft report and SAVE it. Do not do '
            'new web/paper research — work from the saved artifacts.',
            'Workflow: (1) Research.get_topic_state to see the topic and any existing draft; '
            '(2) Research.search_claims and Research.list_promising_sources / search_sources to review the '
            'saved evidence; (3) write a structured report and save it with Research.save_report_draft. '
            'Do this within your first several tool calls — saving the draft is the whole point.',
            'The report must include: a summary that states the bottom line, key findings, the source-backed '
            'claims grouped sensibly, an explicit "points of disagreement / what is contested" section that '
            'reflects any contradicting claims, why it matters, and open questions. Be epistemically honest: '
            'distinguish what is well-established from what is weak, theoretical, or contested. Do not invent '
            'sources or facts beyond what is saved.',
            'CITE INLINE, like a real paper. Immediately after each source-backed statement, append a citation '
            'token `[cite:<source_id>]` using the source_id of the source that supports it (from '
            'get_topic_state / search_sources / the claims you reviewed). Cite multiple sources as '
            '`[cite:src_a,src_b]`. These tokens render as numbered, linked citations. NEVER write meta-prose '
            'like "the saved claim states..." or "source src_1234 says...", and never leave a raw src_/clm_ id '
            'visible in the prose — always wrap it in a [cite:...] token. Aim for at least one inline citation '
            'on every factual sentence in the key-findings and claims sections.',
        ])
    elif role == 'judge':
        base.extend([
            'ROLE: Judge',
            'Your job is to critique the latest topic draft report from the perspective of the user\'s goals and preferences.',
            'Workflow:',
            '1. Read topic state and the latest draft report.',
            '2. Call Research.search_sources (and search_claims) to see the FULL bibliography — authors, '
            'titles, years, credibility — behind the inline citations. The report cites sources inline with '
            '`[cite:<source_id>]` tokens that render as numbered, linked citations; treat these as proper '
            'inline citations, NOT as clutter, and resolve each id against the bibliography when judging.',
            '3. Evaluate relevance, evidence quality, actionability, novelty, and user fit.',
            '4. Write a detailed critique and a concise topline summary.',
            '5. Save a checkpoint with Research.save_checkpoint using the current draft report as the report body.',
            'Judge citation_quality on: are the sources real, credible, and primary where it matters; is the '
            'source base broad enough for the claims; does (nearly) every factual statement carry an inline '
            '[cite:...] citation. Do not penalize the [cite:...] token syntax itself.',
            'Your critique should produce bounded, actionable next steps for the researcher.',
        ])
    elif role == 'coordinator':
        base.extend([
            'ROLE: Research Coordinator',
            'Your job is to take a broad research prompt, scout the topic landscape, and select promising topics for deeper investigation.',
            'Workflow:',
            '1. Recall prior related work.',
            '2. Search broadly for promising topics and trends using Paper and SourceDiscovery where appropriate.',
            '3. Produce a shortlist of candidate topics with relevance and novelty notes.',
            '4. Save candidate topics with Research.save_candidate_topics.',
            '5. If appropriate, initialize high-value topics with Research.init_topic.',
            '6. Prefer source-diverse scouting: papers, official sources, repos, and trend surfaces.',
            'Be budget-aware: avoid spawning too many topics when the budget is tight.',
            'IMPORTANT: during scouting no topics exist yet, so do NOT call '
            'Research.index_promising_source (it is per-topic). Carry the best source '
            'URLs inside each candidate topic\'s why_interesting notes; researchers will '
            'index them once topics are initialized.',
            'SCOUTING IS BOUNDED: use at most ~10 search/browse tool calls total. '
            'SAVE EARLY: as soon as you can name 3-6 plausible topics — even after just '
            'a few searches — call Research.save_candidate_topics with that shortlist. '
            'You may call it again later to refine if budget remains. A good-enough '
            'shortlist saved immediately beats a perfect one lost to turn/token caps.',
            'Your run is NOT complete until you have called Research.save_candidate_topics '
            'with your shortlist — save it even if scouting was only partially successful.',
        ])
    if operation_id:
        base.append(f'Operation ID: {operation_id}')
    if topic_slug:
        base.append(f'Topic slug: {topic_slug}')
    if user_goal:
        base.append(f'User research goal: {user_goal}')
    return '\n'.join(base)


def _role_instruction(role: str, operation_id: str, topic_slug: str = '', user_goal: str = '') -> str:
    if role == 'researcher':
        return (
            f'Work topic `{topic_slug}` in operation `{operation_id}`.\n\n'
            f'First call Research.get_topic_state to inspect the topic. If checkpoints already exist, read the latest critique and summary before revising. '
            f'If the topic state includes follow_up_tasks, treat them as required fixes and address them explicitly in the next draft.\n\n'
            f'SAVE AS YOU GO — do not batch all saving for the end. The moment you find a solid source, call '
            f'Research.add_source immediately; the moment you can state a fact, call Research.add_claim (with '
            f'confidence, stance, evidence_grade, entity_refs). You have a limited number of tool calls, so '
            f'interleave saving with reading. Aim to finish your reading within roughly 25-30 searches/reads, '
            f'then STOP researching and spend your remaining turns writing.\n\n'
            f'WRITE THE DRAFT EARLY. Once you have a handful of solid sources and claims (by roughly your 40th '
            f'tool call at the latest), call Research.save_report_draft with a first complete draft — even a brief '
            f'one. Then, if turns remain, keep researching and call save_report_draft AGAIN to improve it. A saved '
            f'early draft that you refine beats an exhaustive reading pass that never gets written down. '
            f'A run that ends without a saved draft is a FAILED run.\n\n'
            f'Before you stop, make sure you have saved: several sources, several graded claims (including any '
            f'contradicting ones), an evidence markdown via Research.save_evidence, and the draft report.\n\n'
            f'The draft report should include: summary, key findings, source-backed claims, points of disagreement / '
            f'open questions, why it matters, and next research steps. Be explicit about what is well-established vs '
            f'contested.\n\n'
            f'CITE INLINE: right after each source-backed statement, append `[cite:<source_id>]` (the source_id from '
            f'the source you saved with add_source), e.g. `...predicts mortality [cite:src_ab12cd].` Cite multiple '
            f'as `[cite:src_a,src_b]`. These render as numbered, linked citations. Do NOT write "the saved claim '
            f'states" or leave raw src_/clm_ ids in the prose — always wrap them in a [cite:...] token. Every '
            f'factual sentence in key findings and claims should carry at least one inline citation.\n\n'
            f'If revising, explicitly address the judge\'s weaknesses and required fixes — especially any citation '
            f'weaknesses: add an inline citation to every unsupported statement, replace weak sources with stronger '
            f'primary literature, and verify each source resolves before citing it.'
        )
    if role == 'writer':
        return (
            f'Write the draft report for topic `{topic_slug}` in operation `{operation_id}`.\n\n'
            f'The research is DONE — sources and graded claims are already saved. Do NOT run new research. '
            f'Review the saved evidence with Research.get_topic_state and Research.search_claims, then '
            f'synthesise a structured draft report and save it with Research.save_report_draft — do this '
            f'promptly, within your first several tool calls.\n\n'
            f'Structure: summary (state the bottom line up front), key findings, source-backed claims, an '
            f'explicit points-of-disagreement / contested section, why it matters, and open questions. Be '
            f'epistemically honest about what is well-established vs. weak or contested. Do not invent sources.\n\n'
            f'CITE INLINE like a real paper: after each source-backed statement append `[cite:<source_id>]` '
            f'(the source_id from the reviewed sources/claims), `[cite:src_a,src_b]` for several. These render '
            f'as numbered linked citations. Never write "the saved claim states" or leave a raw src_/clm_ id '
            f'visible — always wrap it in a [cite:...] token. Every factual sentence should carry one.'
        )
    if role == 'judge':
        return (
            f'Judge topic `{topic_slug}` in operation `{operation_id}`.\n\n'
            f'First call Research.get_topic_state to find the draft report path. Read the draft report. '
            f'Then call Research.search_sources to see the full bibliography behind the inline [cite:<id>] '
            f'citations (authors/titles/years/credibility) and resolve each cited id against it — the tokens '
            f'render as numbered links, so treat them as proper inline citations, not clutter. '
            f'Then produce: (1) a detailed critique markdown, (2) a concise topline summary markdown, '
            f'and (3) save a checkpoint via Research.save_checkpoint using the report markdown you reviewed.\n\n'
            f'Score the report on relevance, citation_quality, actionability, novelty, and user_fit. '
            f'If evidence is weak, say so clearly.'
        )
    return (
        f'Coordinate operation `{operation_id}` for the research goal: {user_goal}.\n\n'
        f'FIRST, within your first few tool calls, draft 3-6 candidate topics from what you '
        f'already know and save them with Research.save_candidate_topics (set recommended_action '
        f'to deep_research for the strongest). THEN, if budget remains, do a bounded scouting '
        f'pass (at most ~10 searches) and save a refined shortlist. '
        f'Only initialize topics that look genuinely promising and relevant.'
    )


def _agent_status(agent_id: str) -> str:
    """Registry status of an agent ('' when unknown). Used to detect that the
    coordinator's scouting run has finished (its finally-block sets 'stopped')."""
    if not agent_id:
        return ''
    try:
        from agent_lifecycle import list_agents
        for a in list_agents():
            if a.get('id') == agent_id:
                return str(a.get('status') or '')
    except Exception:
        pass
    return ''


def _collect_usage(events: list[Any]) -> tuple[int, int]:
    inp = 0
    out = 0
    for event in events:
        try:
            if getattr(event, 'type', '') == 'message_end':
                usage = getattr(event, 'data', {}) or {}
                usage = usage.get('usage', usage)
                inp += int(usage.get('input_tokens', 0) or 0)
                out += int(usage.get('output_tokens', 0) or 0)
        except Exception:
            continue
    return inp, out


def spawn_libris_role(
    state_dir: Path,
    project_root: Path,
    *,
    role: str,
    operation_id: str,
    topic_slug: str = '',
    user_goal: str = '',
    parent_agent_id: str = '',
) -> dict[str, Any]:
    from agent_lifecycle import create_agent

    goal = user_goal or f'Libris {role} for {topic_slug or operation_id}'
    agent = create_agent(
        name=None,
        mode='temp',
        goal=goal,
        project=str(project_root),
        role=role,
        visibility='background',
        parent_agent_id=parent_agent_id or None,
        require_tmux=False,
    )

    thread = threading.Thread(
        target=_run_libris_role,
        args=(state_dir, project_root, agent, role, operation_id, topic_slug, user_goal),
        daemon=True,
    )
    thread.start()
    return agent


def start_autonomous_libris_research(
    state_dir: Path,
    project_root: Path,
    *,
    prompt: str,
    parent_agent_id: str = '',
    budget: dict[str, Any] | None = None,
    model_policy: dict[str, Any] | None = None,
    max_topics_default: int = 3,
) -> dict[str, Any]:
    from libris_runtime import init_operation, update_operation_runtime

    op = init_operation(
        state_dir,
        project_root,
        prompt=prompt,
        coordinator_agent_id='',
        budget=budget,
        model_policy=model_policy,
        summary=f'Libris autonomous research: {prompt[:120]}',
    )
    coordinator = spawn_libris_role(
        state_dir,
        project_root,
        role='coordinator',
        operation_id=op['operation_id'],
        user_goal=prompt,
        parent_agent_id=parent_agent_id,
    )

    try:
        from libris_runtime import update_operation_budget
        update_operation_budget(state_dir, project_root, op['operation_id'], budget=budget or {}, model_policy=model_policy or {})
    except Exception:
        pass

    try:
        from libris_runtime import append_operation_event
        append_operation_event(state_dir, project_root, op['operation_id'], 'autonomous_research_started', {
            'prompt': prompt[:500],
            'parent_agent_id': parent_agent_id,
            'coordinator_agent_id': coordinator.get('id', ''),
        })
    except Exception:
        pass

    try:
        from libris_runtime import get_operation_state
        update_operation_runtime(
            state_dir,
            project_root,
            op['operation_id'],
            coordinator_agent_id=coordinator.get('id', ''),
            status='scouting',
            note='Coordinator spawned for autonomous Libris run.',
        )
        op = get_operation_state(state_dir, project_root, op['operation_id'])
    except Exception:
        pass

    thread = threading.Thread(
        target=_run_operation_controller,
        args=(state_dir, project_root, op['operation_id'], prompt, coordinator, max_topics_default),
        daemon=True,
    )
    thread.start()
    return {
        'operation': op,
        'coordinator': coordinator,
    }


def _run_operation_controller(
    state_dir: Path,
    project_root: Path,
    operation_id: str,
    prompt: str,
    coordinator: dict[str, Any],
    max_topics_default: int,
) -> None:
    try:
        from libris_runtime import (
            get_operation_state,
            save_candidate_topics,
            init_topic,
            update_topic_runtime,
            set_operation_status,
            update_operation_runtime,
            append_operation_event,
            get_budget_status,
            emit_agent_phase,
            emit_agent_comm,
        )

        emit_agent_phase(
            state_dir, project_root, operation_id,
            agent_id=coordinator.get('id', ''), role='coordinator',
            phase='scouting', status='running',
            summary='Coordinator is scouting and waiting for candidate topics.'
        )
        # Wait for the coordinator's scouting pass. It is a real LLM run that
        # takes minutes, so wait until it finishes (its agent record leaves
        # 'running') or candidate topics appear — bounded by the budget checks
        # and a hard wall cap. Falling to the clarification path after a few
        # seconds would permanently park the operation with no resume.
        waited = 0
        max_wait = 30 * 60
        topics = []
        while waited < max_wait:
            op = get_operation_state(state_dir, project_root, operation_id)
            if not op:
                return
            budget = op.get('budget_status') or get_budget_status(state_dir, project_root, operation_id)
            if not budget.get('continue_running', True):
                set_operation_status(state_dir, project_root, operation_id, 'budget_exhausted', ', '.join(budget.get('reasons') or []))
                return
            topics = list(op.get('candidate_topics') or [])
            if topics:
                break
            if op.get('stop_requested'):
                set_operation_status(state_dir, project_root, operation_id, 'stopped', 'User requested stop.')
                return
            if _agent_status(coordinator.get('id', '')) != 'running':
                # Coordinator finished (or is unknown/dead) without topics yet;
                # one grace read in case topics landed between checks.
                op = get_operation_state(state_dir, project_root, operation_id) or {}
                topics = list(op.get('candidate_topics') or [])
                break
            time.sleep(5)
            waited += 5

        if not topics:
            from tools import ToolContext
            from tools.clarify_tool import execute_clarify

            question = (
                'Libris could not confidently derive candidate research topics from your request: '
                f'"{prompt[:220]}". What should it research?'
            )
            choices = [
                f'Focus strictly on the named topic: {prompt[:120]}',
                'Narrow to core definitions, key papers, and major methods only',
                'Narrow to one domain/application area before researching',
                'Rewrite the topic in my own words / give a custom direction',
            ]
            clar_id = ''
            try:
                clar_ctx = ToolContext(project_root=project_root, agent_id=coordinator.get('id', ''), state_dir=state_dir)
                clar = execute_clarify({'action': 'ask', 'question': question, 'choices': choices}, clar_ctx)
                clar_details = clar.details or {}
                clar_id = str(clar_details.get('clarification_id') or '')
            except Exception as e:
                append_operation_event(
                    state_dir,
                    project_root,
                    operation_id,
                    'clarification_request_failed',
                    {
                        'reason': 'missing_candidate_topics',
                        'error': str(e),
                        'question': question,
                    },
                )
            set_operation_status(
                state_dir,
                project_root,
                operation_id,
                'awaiting_clarification',
                'Coordinator did not produce candidate topics; waiting for user clarification.',
            )
            emit_agent_phase(
                state_dir, project_root, operation_id,
                agent_id=coordinator.get('id', ''), role='coordinator',
                phase='awaiting_clarification', status='blocked',
                summary='Waiting for targeted user clarification before selecting research topics.'
            )
            append_operation_event(
                state_dir,
                project_root,
                operation_id,
                'clarification_requested',
                {
                    'clarification_id': clar_id,
                    'reason': 'missing_candidate_topics',
                    'question': question,
                    'choices': choices,
                },
            )
            return

        op = get_operation_state(state_dir, project_root, operation_id)
        budget = (op.get('budget_status') or {}).get('budget') or {}
        max_topics = int(budget.get('max_topics') or 0) or max_topics_default
        selected = []
        for topic in topics:
            action = str(topic.get('recommended_action') or 'monitor')
            if action not in ('deep_research', 'monitor'):
                continue
            selected.append(topic)
            if len(selected) >= max_topics:
                break

        if not selected:
            set_operation_status(state_dir, project_root, operation_id, 'idle', 'No promising topics were selected.')
            return

        emit_agent_phase(
            state_dir, project_root, operation_id,
            agent_id=coordinator.get('id', ''), role='coordinator',
            phase='selecting_topics', status='running',
            summary=f'Selecting {len(selected)} topic(s) for research.'
        )
        set_operation_status(state_dir, project_root, operation_id, 'fanout', f'Selecting {len(selected)} topic(s) for research.')
        spawned_topics: list[str] = []
        for t in selected:
            topic = init_topic(
                state_dir,
                project_root,
                operation_id,
                title=str(t.get('title') or 'Topic'),
                why_interesting=str(t.get('why_interesting') or ''),
                focus_questions=[
                    f'What is new or notable about {str(t.get("title") or "this topic")} in the last few months?',
                    'Why might this matter to the user and broader project goals?',
                    'What evidence supports practical importance or novelty?',
                ],
            )
            try:
                from libris_orchestrator import (
                    gather_source_leads_for_topic,
                    spawn_topic_procurement_shades,
                    wait_for_procurement_contracts,
                    build_procurement_summary_markdown,
                )
                gather_source_leads_for_topic(state_dir, project_root, operation_id, topic, query=str(t.get('title') or topic.get('title') or ''))
                spawn_topic_procurement_shades(state_dir, project_root, operation_id, topic, max_leads=2)
                wait_for_procurement_contracts(state_dir, project_root, operation_id, topic['slug'], min_completed=1, timeout_seconds=18)
                procurement_md = build_procurement_summary_markdown(state_dir, project_root, operation_id, topic['slug'])
                if procurement_md:
                    from libris_runtime import save_evidence
                    save_evidence(
                        state_dir, project_root, operation_id, topic['slug'],
                        markdown=procurement_md,
                        filename=f'{topic["slug"]}-procurement.md',
                    )
                try:
                    from libris_procurement_ingest import ingest_procurement_contracts
                    ingest_procurement_contracts(state_dir, project_root, operation_id, topic['slug'])
                except Exception:
                    pass
                try:
                    from libris_specialists import (
                        spawn_topic_claim_extraction_shades,
                        wait_for_claim_extraction_contracts,
                        ingest_claim_extraction_contracts,
                        spawn_topic_contradiction_check_shades,
                        wait_for_contradiction_check_contracts,
                        ingest_contradiction_check_contracts,
                    )
                    claim_contracts = spawn_topic_claim_extraction_shades(state_dir, project_root, operation_id, topic, max_leads=1)
                    if claim_contracts:
                        wait_for_claim_extraction_contracts(state_dir, project_root, operation_id, topic['slug'], min_completed=1, timeout_seconds=18)
                        ingest_claim_extraction_contracts(state_dir, project_root, operation_id, topic['slug'])
                    contradiction_contracts = spawn_topic_contradiction_check_shades(state_dir, project_root, operation_id, topic, max_claims=6)
                    if contradiction_contracts:
                        wait_for_contradiction_check_contracts(state_dir, project_root, operation_id, topic['slug'], min_completed=1, timeout_seconds=18)
                        ingest_contradiction_check_contracts(state_dir, project_root, operation_id, topic['slug'])
                except Exception:
                    pass
            except Exception:
                pass
            emit_agent_phase(
                state_dir, project_root, operation_id,
                agent_id=coordinator.get('id', ''), role='coordinator',
                phase='spawning', status='running',
                summary=f'Handing off topic {topic["slug"]} to researcher.'
            )
            researcher = spawn_libris_role(
                state_dir,
                project_root,
                role='researcher',
                operation_id=operation_id,
                topic_slug=topic['slug'],
                user_goal=prompt,
                parent_agent_id=coordinator.get('id', ''),
            )
            emit_agent_comm(
                state_dir, project_root, operation_id,
                from_agent_id=coordinator.get('id', ''), to_agent_id=researcher.get('id', ''),
                from_role='coordinator', to_role='researcher', topic_slug=topic['slug'],
                message_kind='topic_assignment', summary=f'Assigned topic {topic["slug"]} to researcher.'
            )
            update_topic_runtime(
                state_dir,
                project_root,
                operation_id,
                topic['slug'],
                status='researching',
                researcher_agent_id=researcher.get('id', ''),
            )
            spawned_topics.append(topic['slug'])
            append_operation_event(state_dir, project_root, operation_id, 'researcher_fanout_spawned', {
                'topic_slug': topic['slug'],
                'researcher_agent_id': researcher.get('id', ''),
            })
            time.sleep(1)

        set_operation_status(state_dir, project_root, operation_id, 'researching', f'Active topics: {len(spawned_topics)}')

        # Supervisor loop: when draft reports appear, spawn judges. When checkpoints appear, mark progress.
        while True:
            op = get_operation_state(state_dir, project_root, operation_id)
            if not op:
                return
            budget_status = op.get('budget_status') or get_budget_status(state_dir, project_root, operation_id)
            if not budget_status.get('continue_running', True):
                set_operation_status(state_dir, project_root, operation_id, 'budget_exhausted', ', '.join(budget_status.get('reasons') or []))
                return

            all_ready = True
            for topic in op.get('topics') or []:
                slug = str(topic.get('slug') or '')
                if not slug:
                    continue
                has_draft = bool(topic.get('draft_report_path'))
                checkpoint_count = int(topic.get('checkpoint_count') or 0)
                judge_id = str(topic.get('judge_agent_id') or '')
                revision_round = int(topic.get('revision_round') or 0)
                judge_round = int(topic.get('judge_round') or 0)
                research_round = int(topic.get('research_round') or 1)
                draft_updated = str(topic.get('draft_report_updated_at') or '')
                latest_checkpoint = topic.get('latest_checkpoint') or {}
                latest_checkpoint_at = str(latest_checkpoint.get('created_at') or '')
                needs_judge = False
                if has_draft and checkpoint_count == 0:
                    needs_judge = True
                elif has_draft and draft_updated and latest_checkpoint_at and draft_updated > latest_checkpoint_at and judge_round < checkpoint_count + 1:
                    needs_judge = True

                if needs_judge and (not judge_id or judge_round < checkpoint_count + 1):
                    judge = spawn_libris_role(
                        state_dir,
                        project_root,
                        role='judge',
                        operation_id=operation_id,
                        topic_slug=slug,
                        user_goal=prompt,
                        parent_agent_id=coordinator.get('id', ''),
                    )
                    emit_agent_comm(
                        state_dir, project_root, operation_id,
                        from_agent_id=str(topic.get('researcher_agent_id') or ''), to_agent_id=judge.get('id', ''),
                        from_role='researcher', to_role='judge', topic_slug=slug,
                        message_kind='draft_for_review', summary='Researcher submitted draft to judge.'
                    )
                    update_topic_runtime(
                        state_dir,
                        project_root,
                        operation_id,
                        slug,
                        status='judging',
                        judge_agent_id=judge.get('id', ''),
                        extras={'judge_round': judge_round + 1},
                    )
                    append_operation_event(state_dir, project_root, operation_id, 'judge_fanout_spawned', {
                        'topic_slug': slug,
                        'judge_agent_id': judge.get('id', ''),
                        'judge_round': judge_round + 1,
                    })
                    all_ready = False
                    continue

                if checkpoint_count == 0:
                    all_ready = False
                    continue

                revision_plan = {'should_revise': False, 'reasons': [], 'metrics': {}}
                try:
                    from libris_convergence import should_request_additional_revision
                    revision_plan = should_request_additional_revision(state_dir, project_root, operation_id, topic)
                except Exception:
                    pass

                if revision_plan.get('should_revise'):
                    try:
                        from libris_runtime import append_operation_event
                        append_operation_event(state_dir, project_root, operation_id, 'topic_revision_requested', {
                            'topic_slug': slug,
                            'revision_round': revision_round + 1,
                            'reasons': revision_plan.get('reasons') or [],
                            'metrics': revision_plan.get('metrics') or {},
                        })
                    except Exception:
                        pass
                    try:
                        from libris_refinement import (
                            plan_critique_followups,
                            wait_for_gap_fill_contracts,
                            ingest_gap_fill_contracts,
                        )
                        followups = plan_critique_followups(state_dir, project_root, operation_id, slug, max_tasks=3, spawn_gap_fill=True)
                        if followups.get('contracts'):
                            wait_for_gap_fill_contracts(state_dir, project_root, operation_id, slug, min_completed=1, timeout_seconds=18)
                            ingest_gap_fill_contracts(state_dir, project_root, operation_id, slug)
                    except Exception:
                        pass
                    researcher = spawn_libris_role(
                        state_dir,
                        project_root,
                        role='researcher',
                        operation_id=operation_id,
                        topic_slug=slug,
                        user_goal=prompt,
                        parent_agent_id=coordinator.get('id', ''),
                    )
                    emit_agent_comm(
                        state_dir, project_root, operation_id,
                        from_agent_id=str(topic.get('judge_agent_id') or ''), to_agent_id=researcher.get('id', ''),
                        from_role='judge', to_role='researcher', topic_slug=slug,
                        message_kind='critique_returned', summary='Judge returned critique and requested another targeted revision.'
                    )
                    update_topic_runtime(
                        state_dir,
                        project_root,
                        operation_id,
                        slug,
                        status='revising',
                        researcher_agent_id=researcher.get('id', ''),
                        judge_agent_id='',
                        extras={'revision_round': revision_round + 1, 'research_round': research_round + 1},
                    )
                    append_operation_event(state_dir, project_root, operation_id, 'research_revision_spawned', {
                        'topic_slug': slug,
                        'researcher_agent_id': researcher.get('id', ''),
                        'revision_round': revision_round + 1,
                    })
                    all_ready = False
                    continue

                if checkpoint_count >= 1:
                    final_status = 'checkpointed'
                    final_note = 'Topic reached bounded convergence for this run.'
                    reasons = revision_plan.get('reasons') or []
                    if 'quality_good_enough' in reasons:
                        final_status = 'ready_high_confidence'
                        final_note = 'Topic reached good-enough quality and does not require another revision.'
                    elif 'score_plateau' in reasons:
                        final_status = 'plateaued'
                        final_note = 'Topic appears to have plateaued under current bounded refinement.'
                    elif 'checkpoint_budget_exhausted' in reasons or 'revision_cap_reached' in reasons:
                        final_status = 'checkpointed'
                        final_note = 'Topic stopped due to bounded revision/checkpoint limits.'
                    update_topic_runtime(
                        state_dir,
                        project_root,
                        operation_id,
                        slug,
                        status=final_status,
                        extras={
                            'convergence_reasons': reasons,
                            'convergence_metrics': revision_plan.get('metrics') or {},
                        },
                    )
                    try:
                        from libris_runtime import append_operation_event
                        append_operation_event(state_dir, project_root, operation_id, 'topic_convergence_decided', {
                            'topic_slug': slug,
                            'status': final_status,
                            'reasons': reasons,
                            'metrics': revision_plan.get('metrics') or {},
                            'note': final_note,
                        })
                    except Exception:
                        pass
                else:
                    all_ready = False

            if all_ready and (op.get('topics') or []):
                emit_agent_phase(
                    state_dir, project_root, operation_id,
                    agent_id=coordinator.get('id', ''), role='coordinator',
                    phase='selecting_final', status='running',
                    summary='Coordinator is selecting final checkpointed reports.'
                )
                update_operation_runtime(
                    state_dir,
                    project_root,
                    operation_id,
                    status='reports_ready',
                    note='All active topics have completed the basic researcher/judge loop.',
                )
                try:
                    from libris_runtime import finalize_operation_selection
                    finalize_operation_selection(state_dir, project_root, operation_id)
                except Exception:
                    pass
                emit_agent_phase(
                    state_dir, project_root, operation_id,
                    agent_id=coordinator.get('id', ''), role='coordinator',
                    phase='delivered', status='idle',
                    summary='Coordinator selected final reports for delivery.'
                )
                return

            if op.get('stop_requested'):
                set_operation_status(state_dir, project_root, operation_id, 'stopped', 'User requested stop.')
                return

            time.sleep(5)
    except Exception as e:
        try:
            from libris_runtime import append_operation_event, set_operation_status
            append_operation_event(state_dir, project_root, operation_id, 'operation_controller_failed', {'error': str(e)})
            set_operation_status(state_dir, project_root, operation_id, 'failed', str(e))
        except Exception:
            pass



def _run_libris_role(
    state_dir: Path,
    project_root: Path,
    agent: dict[str, Any],
    role: str,
    operation_id: str,
    topic_slug: str,
    user_goal: str,
) -> None:
    try:
        from conversation_engine import ConversationEngine
        from model_registry import get_shade_provider_and_model
        from system_prompt_builder import build_system_prompt
        from libris_runtime import record_usage, append_operation_event, get_operation_state, emit_agent_phase
        from worker_provider import request_worker_provider_for_background_flow

        provider_status = request_worker_provider_for_background_flow(
            state_dir,
            purpose='Libris worker tasks',
            agent_id=agent.get('id', ''),
            project_root=project_root,
        )
        if not provider_status.get('ok'):
            append_operation_event(state_dir, project_root, operation_id, f'{role}_blocked_worker_provider', {
                'agent_id': agent.get('id', ''),
                'topic_slug': topic_slug,
                'reason': provider_status.get('reason') or 'no_provider',
                'available_providers': provider_status.get('available_providers') or [],
            })
            emit_agent_phase(
                state_dir, project_root, operation_id,
                agent_id=agent.get('id', ''), role=role,
                phase='awaiting_worker_provider', status='blocked', topic_slug=topic_slug,
                summary='Waiting for worker provider selection before spawning Libris worker.'
            )
            return

        op = get_operation_state(state_dir, project_root, operation_id)
        policy = (op.get('model_policy') or {}) if op else {}
        role_policy = str(policy.get(role) or '').strip().lower()
        complexity = 'complex' if role in ('coordinator', 'judge', 'writer') else 'normal'
        if role_policy in ('strong', 'best', 'high'):
            complexity = 'complex'
        elif role_policy in ('cheap', 'cheap_local', 'local', 'fast'):
            complexity = 'normal'

        provider, model, _ = get_shade_provider_and_model(
            state_dir,
            phase_name='research',
            task_complexity=complexity,
        )

        agent_doc = {
            'id': agent.get('id', ''),
            'name': agent.get('name', ''),
            'role': role,
            'goal': agent.get('goal', ''),
            'project': str(project_root),
            'parent_agent_id': agent.get('parent_agent_id', ''),
        }
        task_doc = {'project': str(project_root)}
        base_prompt = build_system_prompt(state_dir=state_dir, agent=agent_doc, task=task_doc)
        system_prompt = base_prompt + '\n\n' + _role_prompt(role, operation_id, topic_slug, user_goal)

        # A deep research pass reads and verifies many sources before it can save
        # a report; the engine's default 50-turn cap is too low and truncates the
        # run before anything is persisted. Give researchers/judges more headroom.
        role_max_turns = {'researcher': 140, 'judge': 80, 'coordinator': 60, 'writer': 30}.get(role, 60)

        engine = ConversationEngine(
            provider=provider,
            model=model,
            project_root=project_root,
            agent_id=agent.get('id', ''),
            agent_name=agent.get('name', ''),
            system_prompt=system_prompt,
            state_dir=state_dir,
            operation_id=operation_id,
            operation_domain='research',
            work_unit_id=topic_slug,
            operation_role=role,
            runtime_role='background_agent',
            parent_agent_id=agent.get('parent_agent_id', ''),
            max_tokens=16384,
            max_turns=role_max_turns,
        )

        append_operation_event(state_dir, project_root, operation_id, f'{role}_spawned', {
            'agent_id': agent.get('id', ''),
            'topic_slug': topic_slug,
            'model': getattr(model, 'model_id', '') or role,
        })
        emit_agent_phase(
            state_dir, project_root, operation_id,
            agent_id=agent.get('id', ''), role=role,
            phase='starting', status='running', topic_slug=topic_slug,
            summary='Agent spawned and preparing task.'
        )

        instruction = _role_instruction(role, operation_id, topic_slug, user_goal)
        phase_name = 'scouting' if role == 'coordinator' else 'drafting' if role == 'researcher' else 'writing' if role == 'writer' else 'evaluating' if role == 'judge' else 'working'
        emit_agent_phase(
            state_dir, project_root, operation_id,
            agent_id=agent.get('id', ''), role=role,
            phase=phase_name, status='running', topic_slug=topic_slug,
            summary=f'{role.capitalize()} is actively working.'
        )
        response, events = asyncio.run(engine.submit_and_collect(instruction))
        input_tokens, output_tokens = _collect_usage(events)
        try:
            record_usage(
                state_dir,
                project_root,
                operation_id,
                role=role,
                model=getattr(model, 'model_id', '') or role,
                topic_slug=topic_slug,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                note=response[:500],
            )
        except Exception:
            pass

        emit_agent_phase(
            state_dir, project_root, operation_id,
            agent_id=agent.get('id', ''), role=role,
            phase='done', status='idle', topic_slug=topic_slug,
            summary=response[:200]
        )
        append_operation_event(state_dir, project_root, operation_id, f'{role}_completed', {
            'agent_id': agent.get('id', ''),
            'topic_slug': topic_slug,
            'response_preview': response[:500],
            'input_tokens': input_tokens,
            'output_tokens': output_tokens,
        })
    except Exception as e:
        try:
            from libris_runtime import append_operation_event, emit_agent_phase
            emit_agent_phase(
                state_dir, project_root, operation_id,
                agent_id=agent.get('id', ''), role=role,
                phase='failed', status='failed', topic_slug=topic_slug,
                summary=str(e)
            )
            append_operation_event(state_dir, project_root, operation_id, f'{role}_failed', {
                'topic_slug': topic_slug,
                'error': str(e),
            })
        except Exception:
            pass
    finally:
        try:
            from agent_lifecycle import set_status
            set_status(agent.get('id', ''), 'stopped')
        except Exception:
            pass
