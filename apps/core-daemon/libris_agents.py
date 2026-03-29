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
            'Your job is to investigate one topic deeply and produce a useful draft report.',
            'Workflow:',
            '1. Read topic state and focus questions.',
            '2. Search the web and available sources, including Paper and SourceDiscovery when useful.',
            '3. Consult the promising-source index for your topic before deep reading.',
            '4. Save strong sources with Research.add_source.',
            '5. Save concrete claims with Research.add_claim.',
            '6. Write an evidence summary with Research.save_evidence.',
            '7. Write a draft report with Research.save_report_draft.',
            'Do not create checkpoints yourself unless explicitly instructed; that is usually the judge\'s job.',
            'Your output should be actionable, well-structured, and traceable to sources.',
        ])
    elif role == 'judge':
        base.extend([
            'ROLE: Judge',
            'Your job is to critique the latest topic draft report from the perspective of the user\'s goals and preferences.',
            'Workflow:',
            '1. Read topic state and the latest draft report.',
            '2. Evaluate relevance, evidence quality, actionability, novelty, and user fit.',
            '3. Write a detailed critique and a concise topline summary.',
            '4. Save a checkpoint with Research.save_checkpoint using the current draft report as the report body.',
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
            f'Then perform one disciplined research pass: use Web search/extract, Paper, SourceDiscovery, or Browser as needed, '
            f'consult the promising-source index, save meaningful sources, save claims, write an evidence markdown artifact, '
            f'and save a structured draft report.\n\n'
            f'The draft report should include: summary, key findings, source-backed claims, why it matters, open questions, and next research steps. '
            f'If revising, explicitly address the judge\'s weaknesses and required fixes.'
        )
    if role == 'judge':
        return (
            f'Judge topic `{topic_slug}` in operation `{operation_id}`.\n\n'
            f'First call Research.get_topic_state to find the draft report path. Read the draft report. '
            f'Then produce: (1) a detailed critique markdown, (2) a concise topline summary markdown, '
            f'and (3) save a checkpoint via Research.save_checkpoint using the report markdown you reviewed.\n\n'
            f'Score the report on relevance, citation_quality, actionability, novelty, and user_fit. '
            f'If evidence is weak, say so clearly.'
        )
    return (
        f'Coordinate operation `{operation_id}` for the research goal: {user_goal}.\n\n'
        f'Perform a broad scouting pass, then save a ranked list of candidate topics with Research.save_candidate_topics. '
        f'Only initialize topics that look genuinely promising and relevant.'
    )


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
            infer_candidate_topics,
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
        # Wait for the coordinator to possibly produce candidate topics itself.
        waited = 0
        topics = []
        while waited < 18:
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
            # Fall back early if the coordinator has not produced candidate topics quickly.
            if waited >= 9:
                break
            time.sleep(3)
            waited += 3

        if not topics:
            max_topics = int(((get_operation_state(state_dir, project_root, operation_id).get('budget_status') or {}).get('budget') or {}).get('max_topics') or 0) or max_topics_default
            topics = infer_candidate_topics(prompt, limit=max_topics)
            save_candidate_topics(
                state_dir,
                project_root,
                operation_id,
                topics,
                plan_markdown='# Fallback candidate topic generation\n\nCoordinator did not save topics in time; using prompt-derived candidate leads.',
            )
            emit_agent_phase(
                state_dir, project_root, operation_id,
                agent_id=coordinator.get('id', ''), role='coordinator',
                phase='ranking', status='running',
                summary='Fallback candidate topics generated from the prompt.'
            )
            append_operation_event(state_dir, project_root, operation_id, 'candidate_topics_fallback_generated', {'count': len(topics)})

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

                # One bounded revision loop after the first critique.
                if checkpoint_count >= 1 and revision_round < 1:
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
                        message_kind='critique_returned', summary='Judge returned critique for bounded revision.'
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

                if checkpoint_count >= 2:
                    update_topic_runtime(state_dir, project_root, operation_id, slug, status='checkpointed')
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

        op = get_operation_state(state_dir, project_root, operation_id)
        policy = (op.get('model_policy') or {}) if op else {}
        role_policy = str(policy.get(role) or '').strip().lower()
        complexity = 'complex' if role in ('coordinator', 'judge') else 'normal'
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

        engine = ConversationEngine(
            provider=provider,
            model=model,
            project_root=project_root,
            agent_id=agent.get('id', ''),
            agent_name=agent.get('name', ''),
            system_prompt=system_prompt,
            state_dir=state_dir,
            max_tokens=16384,
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
        phase_name = 'scouting' if role == 'coordinator' else 'drafting' if role == 'researcher' else 'evaluating' if role == 'judge' else 'working'
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
