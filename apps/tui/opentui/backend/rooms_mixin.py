"""Conversation rooms, multi-agent runners, intent/outcome ledger."""
from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
from pathlib import Path

from backend import common
from backend.settings_io import _hermes_conversation_runtime_dir, _write_hermes_runtime_home
from charon.providers.provider_bridge import resolve_provider_config
from charon.infra import config


class RoomsMixin:
    """Conversation rooms, multi-agent runners, and the intent/outcome ledger."""

    def _room_runner_mode(self, room: dict | None, fallback: str = 'teacher-student') -> str:
        meta = room.get('meta') if isinstance(room, dict) and isinstance(room.get('meta'), dict) else {}
        raw = str(meta.get('conversation_mode') or meta.get('runner_mode') or fallback or 'teacher-student').strip().lower()
        if raw in ('peer', 'dialogue', 'discuss'):
            return 'peer'
        if raw in ('debate', 'advocate-opposition', 'advocate', 'opposition'):
            return 'debate'
        if raw in ('researcher-reviewer', 'research', 'review'):
            return 'researcher-reviewer'
        if raw in ('pair-programmers', 'pair-programming', 'pair-programmer', 'driver-navigator'):
            return 'pair-programmers'
        if raw in ('strategist-critic', 'strategist/critic', 'strategist', 'critic'):
            return 'strategist-critic'
        if raw in ('planner-critic', 'planner/critic', 'planner critic', 'planner'):
            return 'planner-critic'
        if raw in ('architect-reviewer', 'architect/reviewer', 'architect reviewer', 'architect'):
            return 'architect-reviewer'
        if raw in ('optimist-skeptic', 'optimist/skeptic', 'optimist skeptic', 'optimist', 'skeptic'):
            return 'optimist-skeptic'
        return 'teacher-student'

    def _initial_room_runner_state(self, room: dict | None, topic: str, participants: list[dict], mode: str) -> dict:
        state = {
            'topic': topic,
            'mode': mode,
            'turn': 1,
            'silent_turns': 0,
            'last_utterance': topic,
            'started': False,
            'participants_len': len(participants or []),
        }
        if mode in ('peer', 'debate', 'researcher-reviewer', 'pair-programmers', 'strategist-critic', 'planner-critic', 'architect-reviewer', 'optimist-skeptic'):
            state['current_idx'] = 0
            return state
        teacher = next((p for p in participants if str(p.get('role') or '') == 'teacher'), participants[0] if participants else None)
        state['current_role'] = 'teacher' if teacher else (str(participants[0].get('role') or 'participant') if participants else 'teacher')
        return state

    def _room_control_notes(self, injections: list[dict]) -> str:
        notes = []
        for item in injections or []:
            if not isinstance(item, dict):
                continue
            msg = ' '.join(str(item.get('message') or '').strip().split())
            if not msg:
                continue
            target = str(item.get('target') or 'whole').strip()
            sender = str(item.get('sender') or 'user').strip()
            notes.append(f'- {sender} to [{target}]: {msg}')
        if not notes:
            return ''
        return (
            '\n\nA user interjection has been added to the room. '
            'Treat it as part of the shared conversation state and respond directly to it when relevant.\n'
            + '\n'.join(notes)
        )

    def _room_recent_context(self, room_id: str, limit: int = 8) -> str:
        try:
            from charon.agents.inter_agent_rooms import list_events
            events = list_events(common.STATE_DIR, room_id, limit=limit * 3)
        except Exception:
            events = []
        lines: list[str] = []
        for ev in events[-limit:]:
            if not isinstance(ev, dict):
                continue
            et = str(ev.get('type') or '')
            if et == 'participant_output':
                who = str(ev.get('speaker_role') or ev.get('participant') or 'participant').strip() or 'participant'
                text = ' '.join(str(ev.get('text') or ev.get('summary') or '').split()).strip()
                if text:
                    lines.append(f'{who}: {text[:300]}')
            elif et in ('room_message_sent', 'room_injection_queued', 'room_injection_requested'):
                target = str(ev.get('target') or 'whole').strip() or 'whole'
                text = ' '.join(str(ev.get('message') or ev.get('summary') or '').split()).strip()
                if text:
                    lines.append(f'user→{target}: {text[:300]}')
        if not lines:
            return ''
        return '\n\nRecent room transcript:\n' + '\n'.join(f'- {line}' for line in lines[-limit:])

    def _build_room_turn_prompt(self, *, room_id: str, mode: str, topic: str, state: dict, participants: list[dict], injections: list[dict]) -> tuple[str, str, str, str]:
        notes = self._room_control_notes(injections)
        recent_context = self._room_recent_context(room_id)
        turn = int(state.get('turn') or 1)
        last_utterance = str(state.get('last_utterance') or topic).strip() or topic

        if mode in ('peer', 'debate', 'researcher-reviewer', 'pair-programmers', 'strategist-critic', 'planner-critic', 'architect-reviewer', 'optimist-skeptic'):
            current_idx = int(state.get('current_idx') or 0)
            default_role_pairs = {
                'peer': ('peer-1', 'peer-2'),
                'debate': ('advocate', 'opposition'),
                'researcher-reviewer': ('researcher', 'reviewer'),
                'pair-programmers': ('driver', 'navigator'),
                'strategist-critic': ('strategist', 'critic'),
                'planner-critic': ('planner', 'critic'),
                'architect-reviewer': ('architect', 'reviewer'),
                'optimist-skeptic': ('optimist', 'skeptic'),
            }
            default_roles = default_role_pairs.get(mode, ('peer-1', 'peer-2'))
            if mode == 'peer' and len(participants) > 2:
                speakers = [
                    (
                        str(p.get('name') or f'Hermes {idx + 1}'),
                        str(p.get('session') or ''),
                        str(p.get('role') or f'peer-{idx + 1}'),
                    )
                    for idx, p in enumerate(participants)
                ]
                if not speakers:
                    return '', '', '', ''
                cur_name, cur_session, cur_role = speakers[current_idx % len(speakers)]
                previous_idx = (current_idx - 1) % len(speakers)
                other_name, _, _ = speakers[previous_idx]
                if turn == 1:
                    prompt = (
                        f"You are {cur_name} in a live multi-agent peer conversation about: {topic}. "
                        "Open with one useful perspective, framing question, or distinction that helps the group think better. "
                        "Keep it natural and concise."
                        f"{recent_context}{notes}\r"
                    )
                else:
                    prompt = (
                        f"You are {cur_name} in a live multi-agent peer conversation about: {topic}. "
                        f"The latest message in the conversation came from {other_name}. Respond naturally to that point below, while helping advance the overall group discussion. "
                        "You may agree, challenge, refine, or redirect, but stay concise and conversational.\n\n"
                        f"Latest message:\n{last_utterance}"
                        f"{recent_context}{notes}\r"
                    )
                return prompt, cur_role, cur_session, cur_name

            p1 = participants[0] if len(participants) > 0 else {}
            p2 = participants[1] if len(participants) > 1 else {}
            speakers = [
                (str(p1.get('name') or 'Hermes 1'), str(p1.get('session') or ''), str(p1.get('role') or default_roles[0])),
                (str(p2.get('name') or 'Hermes 2'), str(p2.get('session') or ''), str(p2.get('role') or default_roles[1])),
            ]
            cur_name, cur_session, cur_role = speakers[current_idx]
            other_name, _, other_role = speakers[1 - current_idx]
            if mode == 'debate':
                if turn == 1:
                    prompt = (
                        f"You are {cur_name}, taking the {cur_role} role in a live debate with {other_name} about: {topic}. "
                        "Open with your strongest concise case. State your position clearly and press one key consideration."
                        f"{recent_context}{notes}\r"
                    )
                else:
                    prompt = (
                        f"You are {cur_name}, taking the {cur_role} role in a live debate with {other_name} about: {topic}. "
                        f"Respond directly to {other_name}'s latest point from the {other_role} side below. Refute weak assumptions, concede only if warranted, and sharpen your case. "
                        "Keep it pointed and concise.\n\n"
                        f"{other_name} said:\n{last_utterance}"
                        f"{recent_context}{notes}\r"
                    )
                return prompt, cur_role, cur_session, cur_name

            if mode == 'researcher-reviewer':
                if cur_role == 'researcher':
                    if turn == 1:
                        prompt = (
                            f"You are {cur_name}, the researcher, in a live research discussion with {other_name}, the reviewer, about: {topic}. "
                            "Open with a hypothesis, framing, or evidence-based angle worth exploring. Keep it rigorous but concise."
                            f"{recent_context}{notes}\r"
                        )
                    else:
                        prompt = (
                            f"You are {cur_name}, the researcher, continuing a live research discussion with {other_name}, the reviewer, about: {topic}. "
                            "Respond to the reviewer's latest critique below. Clarify assumptions, strengthen the argument, and note what evidence would matter most.\n\n"
                            f"Reviewer said:\n{last_utterance}"
                            f"{recent_context}{notes}\r"
                        )
                else:
                    prompt = (
                        f"You are {cur_name}, the reviewer, in a live research discussion with {other_name}, the researcher, about: {topic}. "
                        "Respond to the latest point below by testing assumptions, evidence quality, scope, or missing considerations. Be constructive but rigorous.\n\n"
                        f"Researcher said:\n{last_utterance}"
                        f"{recent_context}{notes}\r"
                    )
                return prompt, cur_role, cur_session, cur_name

            if mode == 'pair-programmers':
                if cur_role == 'driver':
                    if turn == 1:
                        prompt = (
                            f"You are {cur_name}, the driver, in a live pair-programming conversation with {other_name}, the navigator, about this task: {topic}. "
                            "Open by clarifying the goal, proposing a concrete implementation slice, and naming the first step. Keep it practical and concise."
                            f"{recent_context}{notes}\r"
                        )
                    else:
                        prompt = (
                            f"You are {cur_name}, the driver, continuing a live pair-programming conversation with {other_name}, the navigator, about: {topic}. "
                            "Respond to the navigator's latest guidance below. Move the implementation forward, make tradeoffs explicit, and keep the thread concrete.\n\n"
                            f"Navigator said:\n{last_utterance}"
                            f"{recent_context}{notes}\r"
                        )
                else:
                    prompt = (
                        f"You are {cur_name}, the navigator, in a live pair-programming conversation with {other_name}, the driver, about: {topic}. "
                        "Respond to the driver's latest point below by checking approach, risks, tests, edge cases, and next steps. Be concrete and helpful.\n\n"
                        f"Driver said:\n{last_utterance}"
                        f"{recent_context}{notes}\r"
                    )
                return prompt, cur_role, cur_session, cur_name

            if mode == 'strategist-critic':
                if cur_role == 'strategist':
                    if turn == 1:
                        prompt = (
                            f"You are {cur_name}, the strategist, in a live strategist/critic conversation with {other_name}, the critic, about: {topic}. "
                            "Open with a concrete strategic framing, propose a promising direction, and explain why it seems leverageful. Be substantive rather than slogan-like."
                            f"{recent_context}{notes}\r"
                        )
                    else:
                        prompt = (
                            f"You are {cur_name}, the strategist, continuing a live strategist/critic conversation with {other_name}, the critic, about: {topic}. "
                            "Respond to the critic's latest challenge below. Refine the plan, clarify priorities, resolve tradeoffs, and preserve a strong actionable through-line.\n\n"
                            f"Critic said:\n{last_utterance}"
                            f"{recent_context}{notes}\r"
                        )
                else:
                    prompt = (
                        f"You are {cur_name}, the critic, in a live strategist/critic conversation with {other_name}, the strategist, about: {topic}. "
                        "Respond to the strategist's latest point below by stress-testing assumptions, spotting failure modes, identifying blind spots, and pushing for sharper reasoning. Be rigorous but constructive.\n\n"
                        f"Strategist said:\n{last_utterance}"
                        f"{recent_context}{notes}\r"
                    )
                return prompt, cur_role, cur_session, cur_name

            if mode == 'planner-critic':
                if cur_role == 'planner':
                    if turn == 1:
                        prompt = (
                            f"You are {cur_name}, the planner, in a live planner/critic conversation with {other_name}, the critic, about: {topic}. "
                            "Open with a concrete plan, sequencing, milestones, and major dependencies. Focus on execution realism and actionable next steps."
                            f"{recent_context}{notes}\r"
                        )
                    else:
                        prompt = (
                            f"You are {cur_name}, the planner, continuing a live planner/critic conversation with {other_name}, the critic, about: {topic}. "
                            "Respond to the critic's latest challenge below by revising the plan, clarifying sequencing, tightening scope, and making tradeoffs explicit.\n\n"
                            f"Critic said:\n{last_utterance}"
                            f"{recent_context}{notes}\r"
                        )
                else:
                    prompt = (
                        f"You are {cur_name}, the critic, in a live planner/critic conversation with {other_name}, the planner, about: {topic}. "
                        "Respond to the planner's latest point below by identifying unrealistic assumptions, missing dependencies, hidden complexity, schedule risk, or unclear ownership. Be tough but constructive.\n\n"
                        f"Planner said:\n{last_utterance}"
                        f"{recent_context}{notes}\r"
                    )
                return prompt, cur_role, cur_session, cur_name

            if mode == 'architect-reviewer':
                if cur_role == 'architect':
                    if turn == 1:
                        prompt = (
                            f"You are {cur_name}, the architect, in a live architect/reviewer conversation with {other_name}, the reviewer, about: {topic}. "
                            "Open with a concrete architecture, design rationale, interfaces, and key tradeoffs. Aim for a thoughtful systems-level proposal."
                            f"{recent_context}{notes}\r"
                        )
                    else:
                        prompt = (
                            f"You are {cur_name}, the architect, continuing a live architect/reviewer conversation with {other_name}, the reviewer, about: {topic}. "
                            "Respond to the reviewer's latest critique below by defending or revising the design, clarifying constraints, and making the architecture more coherent.\n\n"
                            f"Reviewer said:\n{last_utterance}"
                            f"{recent_context}{notes}\r"
                        )
                else:
                    prompt = (
                        f"You are {cur_name}, the reviewer, in a live architect/reviewer conversation with {other_name}, the architect, about: {topic}. "
                        "Respond to the architect's latest point below by probing design weaknesses, unclear interfaces, operability concerns, failure modes, and maintainability issues. Be rigorous and concrete.\n\n"
                        f"Architect said:\n{last_utterance}"
                        f"{recent_context}{notes}\r"
                    )
                return prompt, cur_role, cur_session, cur_name

            if mode == 'optimist-skeptic':
                if cur_role == 'optimist':
                    if turn == 1:
                        prompt = (
                            f"You are {cur_name}, the optimist, in a live optimist/skeptic conversation with {other_name}, the skeptic, about: {topic}. "
                            "Open by making the strongest case for why this could work out well, what upside is underappreciated, and what enabling factors matter most. Be specific, not gushy."
                            f"{recent_context}{notes}\r"
                        )
                    else:
                        prompt = (
                            f"You are {cur_name}, the optimist, continuing a live optimist/skeptic conversation with {other_name}, the skeptic, about: {topic}. "
                            "Respond to the skeptic's latest point below by addressing the concern, refining the upside case, and distinguishing real risk from over-pessimism.\n\n"
                            f"Skeptic said:\n{last_utterance}"
                            f"{recent_context}{notes}\r"
                        )
                else:
                    prompt = (
                        f"You are {cur_name}, the skeptic, in a live optimist/skeptic conversation with {other_name}, the optimist, about: {topic}. "
                        "Respond to the optimist's latest point below by identifying risks, weak assumptions, downside scenarios, and reasons the upside case may be overstated. Be sharp but fair.\n\n"
                        f"Optimist said:\n{last_utterance}"
                        f"{recent_context}{notes}\r"
                    )
                return prompt, cur_role, cur_session, cur_name

            if turn == 1:
                prompt = (
                    f"You are {cur_name} in a live peer conversation with {other_name} about: {topic}. "
                    "Open with your initial perspective, framing question, or useful distinction. "
                    "Aim for a natural, thoughtful, substantive response rather than a rigid format or just one or two sentences."
                    f"{recent_context}{notes}\r"
                )
            else:
                prompt = (
                    f"You are {cur_name} continuing a live peer conversation with {other_name} about: {topic}. "
                    "Respond naturally to the latest point below. You can agree, challenge, refine, or redirect. "
                    "Keep it thoughtful and substantive; a few solid paragraphs or a well-developed short response is better than a throwaway answer.\n\n"
                    f"{other_name} said:\n{last_utterance}"
                    f"{recent_context}{notes}\r"
                )
            return prompt, cur_role, cur_session, cur_name

        current_role = str(state.get('current_role') or 'teacher')
        teacher = next((p for p in participants if str(p.get('role') or '') == 'teacher'), participants[0] if participants else {})
        student = next((p for p in participants if str(p.get('role') or '') == 'student'), participants[1] if len(participants) > 1 else {})
        teacher_session = str(teacher.get('session') or '')
        student_session = str(student.get('session') or '')
        if current_role == 'teacher':
            if turn == 1:
                prompt = (
                    f"You are the teacher in a live teaching conversation about: {topic}. "
                    "Start naturally. Prioritize clarity, pacing, and concrete understanding over roleplay tropes. "
                    "A short substantive opening is better than a scripted one."
                    f"{recent_context}{notes}\r"
                )
            else:
                prompt = (
                    f"You are the teacher in an ongoing conversation about: {topic}. "
                    "Reply naturally to the student's latest message below. Clarify, extend, or gently correct as needed. "
                    "Ask a follow-up only if it genuinely helps the conversation move.\n\n"
                    f"Student message:\n{last_utterance}"
                    f"{recent_context}{notes}\r"
                )
            return prompt, 'teacher', teacher_session, str(teacher.get('name') or 'Teacher')

        prompt = (
            f"You are the student in an ongoing conversation about: {topic}. "
            "Reply naturally to the teacher's latest message below. Share what made sense, what remains unclear, and where you want to go next. "
            "A question is welcome but not mandatory every turn.\n\n"
            f"Teacher message:\n{last_utterance}"
            f"{recent_context}{notes}\r"
        )
        return prompt, 'student', student_session, str(student.get('name') or 'Student')

    def _advance_room_runner_state(self, state: dict, mode: str, utterance: str) -> dict:
        next_state = dict(state or {})
        next_state['last_utterance'] = utterance
        next_state['silent_turns'] = 0
        next_state['turn'] = int(next_state.get('turn') or 1) + 1
        next_state['started'] = True
        if mode in ('peer', 'debate', 'researcher-reviewer', 'pair-programmers', 'strategist-critic', 'planner-critic', 'architect-reviewer', 'optimist-skeptic'):
            participants_len = max(2, int(next_state.get('participants_len') or 2))
            if mode == 'peer' and participants_len > 2:
                next_state['current_idx'] = (int(next_state.get('current_idx') or 0) + 1) % participants_len
            else:
                next_state['current_idx'] = 1 - int(next_state.get('current_idx') or 0)
        else:
            next_state['current_role'] = 'student' if str(next_state.get('current_role') or 'teacher') == 'teacher' else 'teacher'
        return next_state

    def _start_conversation_room_runner(self, room_id: str, topic: str, participants: list[dict], mode: str = 'teacher-student') -> None:
        rid = str(room_id or '').strip()
        if not rid or rid in self._room_runners:
            return
        self._room_runners.add(rid)

        def _run() -> None:
            try:
                from charon.agents.inter_agent_rooms import (
                    append_event,
                    consume_injections,
                    load_room,
                    load_runner_state,
                    save_runner_state,
                    update_room,
                )

                room = load_room(common.STATE_DIR, rid)
                if not room:
                    return
                mode_local = self._room_runner_mode(room, mode)
                room_participants = list(room.get('participants') or participants or [])
                topic_local = str(room.get('title') or topic or '').strip() or topic
                state = load_runner_state(common.STATE_DIR, rid) or self._initial_room_runner_state(room, topic_local, room_participants, mode_local)
                save_runner_state(common.STATE_DIR, rid, state)

                from charon.conversation.conversation_runtime import runtime_for_participant

                if mode_local in ('peer', 'debate', 'researcher-reviewer', 'pair-programmers', 'strategist-critic', 'planner-critic', 'architect-reviewer', 'optimist-skeptic'):
                    if len(room_participants) < 2:
                        append_event(common.STATE_DIR, rid, {'type': 'runner_error', 'message': 'need at least 2 participants for this conversation'})
                        return
                    for participant in room_participants:
                        runtime = runtime_for_participant(participant)
                        if not runtime.wait_until_ready(timeout=15.0):
                            append_event(common.STATE_DIR, rid, {'type': 'runner_error', 'message': f"participant runtime not ready: {participant.get('name') or participant.get('role') or participant.get('session') or 'unknown'}"})
                            return
                else:
                    teacher = next((p for p in room_participants if str(p.get('role') or '') == 'teacher'), room_participants[0] if room_participants else None)
                    student = next((p for p in room_participants if str(p.get('role') or '') == 'student'), room_participants[1] if len(room_participants) > 1 else None)
                    if not teacher or not student:
                        append_event(common.STATE_DIR, rid, {'type': 'runner_error', 'message': 'missing teacher/student participants'})
                        return
                    if not runtime_for_participant(teacher).wait_until_ready(timeout=15.0) or not runtime_for_participant(student).wait_until_ready(timeout=15.0):
                        append_event(common.STATE_DIR, rid, {'type': 'runner_error', 'message': 'participant runtime not ready'})
                        return

                if not bool(state.get('started')):
                    time.sleep(6.0)
                    append_event(common.STATE_DIR, rid, {
                        'type': 'conversation_started',
                        'topic': topic_local,
                        'mode': (mode_local if mode_local in ('peer', 'debate', 'researcher-reviewer', 'pair-programmers', 'strategist-critic', 'planner-critic', 'architect-reviewer', 'optimist-skeptic') else 'relay'),
                    })
                    common.emit({'type': 'refresh', 'payload': self._get_refresh_payload(), 'request_id': None})

                while True:
                    room = load_room(common.STATE_DIR, rid)
                    if not room:
                        break
                    room_status = str(room.get('status') or 'active')
                    if room_status in ('stopped', 'deleted'):
                        break
                    if room_status == 'paused':
                        time.sleep(0.5)
                        continue

                    room_participants = list(room.get('participants') or room_participants or [])
                    state = load_runner_state(common.STATE_DIR, rid) or state
                    prompt, speaker_role, speaker_session, speaker_name = self._build_room_turn_prompt(
                        room_id=rid,
                        mode=mode_local,
                        topic=topic_local,
                        state=state,
                        participants=room_participants,
                        injections=consume_injections(
                            common.STATE_DIR,
                            rid,
                            speaker_role=(
                                str(room_participants[int(state.get('current_idx') or 0)].get('role') or 'philosopher-1')
                                if mode_local == 'dialogue' and len(room_participants) > 0
                                else str(state.get('current_role') or 'teacher')
                            ),
                            participant=(
                                room_participants[int(state.get('current_idx') or 0)]
                                if mode_local == 'dialogue' and len(room_participants) > 0
                                else next((p for p in room_participants if str(p.get('role') or '') == str(state.get('current_role') or 'teacher')), room_participants[0] if room_participants else None)
                            ),
                        ),
                    )
                    if not speaker_session:
                        append_event(common.STATE_DIR, rid, {'type': 'runner_error', 'message': f'missing session for {speaker_role}'})
                        break

                    turn = int(state.get('turn') or 1)
                    append_event(common.STATE_DIR, rid, {
                        'type': 'conversation_turn_started',
                        'turn': turn,
                        'speaker_role': speaker_role,
                        'session': speaker_session,
                        'summary': prompt.splitlines()[0][:200],
                    })
                    update_room(common.STATE_DIR, rid, summary=f'{speaker_name} turn {turn}: {topic_local}', active_speaker=speaker_role, active_state='thinking')
                    common.emit({'type': 'refresh', 'payload': self._get_refresh_payload(), 'request_id': None})

                    participant_count = len(room_participants)
                    response_start_timeout = 12.0 if participant_count >= 3 else 16.0
                    quiet_period = 0.5 if participant_count >= 3 else 0.7
                    completion_timeout = 8.0 if participant_count >= 3 else 12.0
                    speaker_participant = next((p for p in room_participants if str(p.get('session') or '') == speaker_session), None)
                    runtime = runtime_for_participant(speaker_participant or {'session': speaker_session})
                    turn_runtime = {
                        'phase': 'thinking',
                        'research_started_at': None,
                        'reply_started': False,
                        'nudge_sent': False,
                    }
                    research_nudge_after = 8.0 if participant_count <= 2 else 6.0

                    def _runtime_event(
                        evt: dict,
                        *,
                        # Bind per-turn loop variables so the callback stays correct
                        # even if it were ever invoked after the iteration advances.
                        turn=turn,
                        turn_runtime=turn_runtime,
                        speaker_role=speaker_role,
                        speaker_session=speaker_session,
                        speaker_name=speaker_name,
                        runtime=runtime,
                        research_nudge_after=research_nudge_after,
                    ) -> None:
                        et = str((evt or {}).get('type') or '')
                        now = time.time()
                        if et == 'tool_progress':
                            if turn_runtime['research_started_at'] is None:
                                turn_runtime['research_started_at'] = now
                            turn_runtime['phase'] = 'researching'
                            summary = str((evt or {}).get("summary") or "tool activity")[:160]
                            tool_name = str((evt or {}).get('tool_name') or '').strip()
                            tool_phase = str((evt or {}).get('tool_phase') or '').strip()
                            if tool_name or tool_phase:
                                append_event(common.STATE_DIR, rid, {
                                    'type': 'participant_tool_progress',
                                    'turn': turn,
                                    'speaker_role': speaker_role,
                                    'session': speaker_session,
                                    'tool_name': tool_name,
                                    'tool_phase': tool_phase,
                                    'summary': summary,
                                })
                            update_room(
                                common.STATE_DIR,
                                rid,
                                summary=f'{speaker_name} researching: {summary}',
                                active_speaker=speaker_role,
                                active_state='researching',
                            )
                            if (not turn_runtime['reply_started'] and not turn_runtime['nudge_sent'] and turn_runtime['research_started_at'] is not None and (now - float(turn_runtime['research_started_at'] or now)) >= research_nudge_after):
                                if runtime.send_input('Please reply to the conversation now with what you have so far. If research is incomplete, briefly note the uncertainty and continue.'):
                                    turn_runtime['nudge_sent'] = True
                                    append_event(common.STATE_DIR, rid, {
                                        'type': 'turn_nudged',
                                        'turn': turn,
                                        'speaker_role': speaker_role,
                                        'session': speaker_session,
                                        'summary': f'{speaker_name} was nudged to answer after extended research',
                                    })
                            common.emit({'type': 'refresh', 'payload': self._get_refresh_payload(), 'request_id': None})
                        elif et in ('reply_started', 'reply_progress'):
                            text = str((evt or {}).get('text') or '').strip()
                            if text:
                                turn_runtime['reply_started'] = True
                                turn_runtime['phase'] = 'replying'
                                update_room(
                                    common.STATE_DIR,
                                    rid,
                                    summary=f'{speaker_name} drafting reply: {text[:160]}',
                                    active_speaker=speaker_role,
                                    active_state='replying',
                                )
                                common.emit({'type': 'refresh', 'payload': self._get_refresh_payload(), 'request_id': None})

                    result = runtime.prompt_and_capture(
                        prompt,
                        timeout=response_start_timeout,
                        quiet_period=quiet_period,
                        completion_timeout=completion_timeout,
                        on_event=_runtime_event,
                    ).as_dict()
                    utterance = str(result.get('message_text') or '').strip() or str(result.get('last_line') or '').strip()
                    if utterance:
                        utterance = utterance[:8000]
                        state = self._advance_room_runner_state(state, mode_local, utterance)
                        save_runner_state(common.STATE_DIR, rid, state)
                        append_event(common.STATE_DIR, rid, {
                            'type': 'participant_output',
                            'turn': turn,
                            'speaker_role': speaker_role,
                            'session': speaker_session,
                            'summary': utterance[:240],
                            'text': utterance,
                            'last_line': str(result.get('last_line') or '')[:240],
                        })
                        update_room(common.STATE_DIR, rid, summary=f'{speaker_name} replied on turn {turn}', active_speaker=speaker_role, active_state='handoff')
                        common.emit({'type': 'refresh', 'payload': self._get_refresh_payload(), 'request_id': None})
                        time.sleep(0.15)
                        continue

                    captured = False
                    for wait_idx in range(2):
                        append_event(common.STATE_DIR, rid, {
                            'type': 'turn_waiting',
                            'turn': turn,
                            'speaker_role': speaker_role,
                            'session': speaker_session,
                            'summary': f"still waiting for response{'' if wait_idx == 0 else ' (retry listen)'}",
                        })
                        update_room(common.STATE_DIR, rid, summary=f'{speaker_name} waiting for visible reply', active_speaker=speaker_role, active_state='waiting')
                        common.emit({'type': 'refresh', 'payload': self._get_refresh_payload(), 'request_id': None})
                        result = runtime.capture_output(
                            timeout=(6.0 if participant_count >= 3 else 10.0),
                            prompt_hint=prompt,
                            quiet_period=(0.5 if participant_count >= 3 else 0.7),
                            completion_timeout=(6.0 if participant_count >= 3 else 10.0),
                            on_event=_runtime_event,
                        ).as_dict()
                        utterance = str(result.get('message_text') or '').strip() or str(result.get('last_line') or '').strip()
                        if not utterance:
                            continue
                        utterance = utterance[:8000]
                        state = self._advance_room_runner_state(state, mode_local, utterance)
                        save_runner_state(common.STATE_DIR, rid, state)
                        append_event(common.STATE_DIR, rid, {
                            'type': 'participant_output',
                            'turn': turn,
                            'speaker_role': speaker_role,
                            'session': speaker_session,
                            'summary': utterance[:240],
                            'text': utterance,
                            'last_line': str(result.get('last_line') or '')[:240],
                        })
                        update_room(common.STATE_DIR, rid, summary=f'{speaker_name} replied on turn {turn}', active_speaker=speaker_role, active_state='handoff')
                        common.emit({'type': 'refresh', 'payload': self._get_refresh_payload(), 'request_id': None})
                        time.sleep(0.15)
                        captured = True
                        break
                    if captured:
                        continue

                    silent_turns = int(state.get('silent_turns') or 0) + 1
                    state['silent_turns'] = silent_turns
                    save_runner_state(common.STATE_DIR, rid, state)
                    append_event(common.STATE_DIR, rid, {
                        'type': 'turn_timeout',
                        'turn': turn,
                        'speaker_role': speaker_role,
                        'session': speaker_session,
                        'summary': str(result.get('error') or 'no visible output')[:240],
                    })
                    update_room(common.STATE_DIR, rid, summary=f'{speaker_name} stalled on turn {turn}', active_speaker=speaker_role, active_state='stalled')
                    common.emit({'type': 'refresh', 'payload': self._get_refresh_payload(), 'request_id': None})
                    if silent_turns >= 3:
                        append_event(common.STATE_DIR, rid, {'type': 'conversation_stalled', 'turn': turn, 'speaker_role': speaker_role, 'topic': topic_local})
                        update_room(common.STATE_DIR, rid, status='paused', summary=f'Paused after repeated timeouts on turn {turn}', active_speaker=speaker_role, active_state='paused')
                        break
                    time.sleep(0.5 if participant_count >= 3 else 1.0)

                room = load_room(common.STATE_DIR, rid)
                if room and str(room.get('status') or '') != 'paused':
                    append_event(common.STATE_DIR, rid, {'type': 'conversation_stopped', 'topic': topic_local, 'turns_completed': max(0, int((load_runner_state(common.STATE_DIR, rid) or {}).get('turn') or 1) - 1)})
                    common.emit({'type': 'refresh', 'payload': self._get_refresh_payload(), 'request_id': None})
            finally:
                self._room_runners.discard(rid)

        threading.Thread(target=_run, daemon=True).start()

    def _conversation_role_prompt(self, agent_display: str, role: str, title: str, room_id: str, runner_mode: str) -> str:
        if runner_mode in ('peer', 'dialogue'):
            return (
                f'You are {agent_display} in an ongoing peer conversation about: {title}. '
                'Charon relays the other participant\'s latest message and handles turn-taking. '
                'Use your own judgment about tone, depth, and pacing. '
                'Stay thoughtful, concise, and ready to continue from incoming prompts.'
            )
        if runner_mode == 'debate':
            return (
                f'You are {agent_display} in an ongoing debate about: {title}. '
                'Charon relays the other participant\'s latest message and handles turn-taking. '
                'Take a clear side, respond directly, and keep your arguments sharp but concise.'
            )
        if runner_mode == 'researcher-reviewer':
            return (
                f'You are {agent_display} in an ongoing researcher/reviewer conversation about: {title}. '
                'Charon relays the other participant\'s latest message and handles turn-taking. '
                'Stay rigorous, evidence-oriented, and concise.'
            )
        if runner_mode == 'pair-programmers':
            return (
                f'You are {agent_display} in an ongoing pair-programming conversation about: {title}. '
                'Charon relays the other participant\'s latest message and handles turn-taking. '
                'Stay practical, concrete, and focused on implementation progress.'
            )
        if runner_mode == 'strategist-critic':
            return (
                f'You are {agent_display} in an ongoing strategist/critic conversation about: {title}. '
                'Charon relays the other participant\'s latest message and handles turn-taking. '
                'Aim for substantive strategic reasoning, clear tradeoffs, and constructive pressure-testing rather than shallow one-liners.'
            )
        if runner_mode == 'planner-critic':
            return (
                f'You are {agent_display} in an ongoing planner/critic conversation about: {title}. '
                'Charon relays the other participant\'s latest message and handles turn-taking. '
                'Focus on execution realism, sequencing, dependencies, and constructive challenge rather than shallow one-liners.'
            )
        if runner_mode == 'architect-reviewer':
            return (
                f'You are {agent_display} in an ongoing architect/reviewer conversation about: {title}. '
                'Charon relays the other participant\'s latest message and handles turn-taking. '
                'Aim for thoughtful systems reasoning, concrete interfaces, and rigorous design review.'
            )
        if runner_mode == 'optimist-skeptic':
            return (
                f'You are {agent_display} in an ongoing optimist/skeptic conversation about: {title}. '
                'Charon relays the other participant\'s latest message and handles turn-taking. '
                'Explore upside and downside concretely, with fair but substantive argument rather than caricature.'
            )
        return (
            f'You are {agent_display} in room {room_id} about: {title}. '
            f'Your role is {role}. '
            'Charon handles turn-taking and may occasionally inject steering. '
            'Keep a natural conversational voice, stay responsive to the current turn, and avoid overfitting to rigid phrasing rules.'
        )

    def _create_conversation_room(
        self,
        *,
        agent_type: str,
        kind: str,
        title: str,
        project: str,
        participants: list[dict],
        meta: dict,
        request_id: str | None,
        start_runner: bool,
        runner_mode: str = 'teacher-student',
    ) -> dict:
        from charon.agents.inter_agent_rooms import create_room, append_event, slugify, update_room
        from charon.conversation.conversation_participants import get_conversation_adapter
        import subprocess as _sp

        adapter = get_conversation_adapter(agent_type)
        if not adapter:
            raise RuntimeError(f'Unsupported conversation agent type: {agent_type}')
        if not adapter.capabilities.can_spawn:
            raise RuntimeError(f'Conversation spawning not wired yet for agent type: {agent_type}')

        room_meta = dict(meta or {})
        room_meta.setdefault('provider', agent_type)
        room_meta.setdefault('agent_type', agent_type)
        room = create_room(
            common.STATE_DIR,
            kind=kind,
            title=title,
            project=project,
            participants=participants,
            meta=room_meta,
        )
        append_event(common.STATE_DIR, room['id'], {
            'type': f'{kind}_requested',
            'provider': agent_type,
            'count': len(participants),
            'topic': title,
        })

        room_slug = slugify(title)
        room_id_slug = slugify(str(room.get('id') or room_slug))
        room_short = room_id_slug[-6:] if len(room_id_slug) >= 6 else room_id_slug
        title_short = room_slug[:18].strip('-_.') or 'conversation'
        launched = []
        bound_participants = []
        runtime_status = None
        for idx, participant_seed in enumerate(participants):
            agent_name = f'hc-{title_short}-{room_short}-{agent_type[:2]}{idx+1}'
            role = str(participant_seed.get('role') or 'participant')
            agent_display = participant_seed.get('name') or f'{adapter.display_name} {idx+1}'
            role_prompt = self._conversation_role_prompt(agent_display, role, title, str(room.get('id') or ''), runner_mode)
            child_env = os.environ.copy()
            if agent_type == 'hermes':
                hermes_runtime = self._conversation_hermes_local_runtime(room_meta)
                runtime_home = _hermes_conversation_runtime_dir(room['id'], agent_name)
                _write_hermes_runtime_home(
                    runtime_home,
                    model=str(hermes_runtime.get('model') or 'qwen3-30b-a3b'),
                    base_url=str(hermes_runtime.get('base_url') or 'http://127.0.0.1:1234/v1'),
                )
                child_env['HERMES_HOME'] = str(runtime_home)
                child_env['HERMES_INFERENCE_PROVIDER'] = 'custom'
                child_env['OPENAI_BASE_URL'] = str(hermes_runtime.get('base_url') or 'http://127.0.0.1:1234/v1')
                child_env['OPENAI_API_KEY'] = 'no-key-required'
                child_env['HERMES_MODEL'] = str(hermes_runtime.get('model') or 'qwen3-30b-a3b')
                child_env['LLM_MODEL'] = str(hermes_runtime.get('model') or 'qwen3-30b-a3b')
                child_env.pop('OPENROUTER_API_KEY', None)
                child_env.pop('OPENROUTER_BASE_URL', None)
                runtime_status = hermes_runtime
            cmd = adapter.spawn_command(project_root=common.ROOT, session_name=agent_name, participant=adapter.build_participant(participant_seed))
            _sp.Popen(cmd, cwd=str(common.ROOT), env=child_env, stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
            session_name = f'boat-{agent_name}'
            self._register_owned_boat_session(session_name)
            launched.append(agent_name)
            participant = dict(participant_seed)
            participant['session'] = session_name
            participant['agent_type'] = agent_type
            bound_participants.append(participant)
            append_event(common.STATE_DIR, room['id'], {
                'type': 'participant_spawned',
                'participant': participant.get('name') or f'{adapter.display_name} {idx+1}',
                'role': role,
                'session': session_name,
                'agent_type': agent_type,
                'prompt': role_prompt[:200],
            })
        room = update_room(common.STATE_DIR, room['id'], participants=bound_participants, participant_sessions=[p.get('session') for p in bound_participants]) or room
        from charon.conversation.conversation_runtime import runtime_for_participant
        for participant in bound_participants:
            runtime_for_participant(participant).wait_until_ready(timeout=15.0)
        if start_runner and len(bound_participants) >= 2:
            self._start_conversation_room_runner(room['id'], title, bound_participants, mode=runner_mode)
        common.emit({'type': 'status', 'message': f'Created {adapter.display_name} {kind} room: {room.get("title", title)} ({room["id"]})', 'request_id': request_id})
        if runtime_status:
            common.emit({'type': 'status', 'message': f'{adapter.display_name} conversation runtime forced local: {runtime_status.get("model", "qwen3-30b-a3b")} @ {runtime_status.get("base_url", "http://127.0.0.1:1234/v1")}', 'request_id': request_id})
        common.emit({'type': 'status', 'message': f'Launched wrapped {adapter.display_name} sessions: ' + ', '.join(launched), 'request_id': request_id})
        common.emit({'type': 'refresh', 'payload': self._get_refresh_payload(), 'request_id': request_id})
        return room

    def _outcomes_path(self, session_id: str | None = None) -> Path | None:
        sid = session_id or self._active_agent_id
        if not sid:
            return None
        return common.STATE_DIR / 'conversations' / f'{sid}.outcomes.json'

    def _save_session_outcomes(self) -> None:
        path = self._outcomes_path()
        if not path:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(self._session_tasks, indent=2, ensure_ascii=False))
        except Exception:
            pass

    def _is_ack_message(self, text: str) -> bool:
        t = text.strip().lower()
        if not t:
            return False
        return bool(re.match(
            r'^(ok|okay|thanks|thank you|looks good|great|nice|cool|yep|yes|approved|perfect|that works|sounds good)[!. ]*$',
            t,
        ))

    def _starts_with_ack(self, text: str) -> bool:
        """True if message begins with an ack word followed by more content (e.g. 'okay, do X next')."""
        t = text.strip().lower()
        return bool(re.match(
            r'^(ok|okay|thanks|great|nice|cool|yep|yes|perfect|sounds good)[!.,\s]',
            t,
        ))

    def _is_redirect_message(self, text: str) -> bool:
        t = text.strip().lower()
        if not t:
            return False
        patterns = [
            r'\bno\b', r'\bnot what i meant\b', r'\bthat\'s wrong\b',
            r'\bwhy are you\b', r'\bwhy aren\'t you\b', r'\binstead\b',
            r'\bactually\b', r'\bi meant\b', r'\bchange what\b',
            r'\bwhat happened\b', r'\btry again\b', r'\bthat\'s not\b',
        ]
        return any(re.search(p, t) for p in patterns)

    def _parse_intent(self, text: str) -> tuple[str, str] | None:
        t = ' '.join(text.strip().lower().split())
        if not t or self._is_ack_message(t):
            return None
        t = re.sub(r'^(please\s+|can you\s+|could you\s+|would you\s+|help me\s+|i want you to\s+|let\'s\s+|lets\s+)', '', t)
        mappings = [
            ('fixed', [r'^fix\s+', r'^repair\s+']),
            ('implemented', [r'^implement\s+', r'^add\s+', r'^create\s+', r'^build\s+']),
            ('updated', [r'^update\s+', r'^change\s+', r'^adjust\s+', r'^rename\s+']),
            ('investigated', [r'^investigate\s+', r'^diagnose\s+', r'^debug\s+', r'^look into\s+', r'^figure out\s+', r'^why\b', r'^what happened\b']),
            ('researched', [r'^research\s+', r'^explore\s+', r'^review\s+', r'^inspect\s+']),
            ('prevented', [r'^prevent\s+', r'^block\s+', r'^stop\s+']),
            ('refactored', [r'^refactor\s+', r'^clean up\s+']),
        ]
        for kind, pats in mappings:
            for pat in pats:
                if re.search(pat, t):
                    obj = re.sub(pat, '', t).strip(' .?!')
                    obj = re.sub(r'^(the|a|an)\s+', '', obj)
                    obj = obj[:80] if obj else 'task'
                    return kind, obj
        return None

    def _make_outcome_title(self, kind: str, obj: str, status: str) -> str:
        if status == 'active':
            active_map = {
                'fixed': 'fixing', 'implemented': 'implementing', 'updated': 'updating',
                'investigated': 'investigating', 'researched': 'researching',
                'prevented': 'preventing', 'refactored': 'refactoring',
            }
            return f"{active_map.get(kind, 'working on')} {obj}".strip()
        return f'{kind} {obj}'.strip()

    def _clean_orchestration_topic(self, topic: str) -> str:
        cleaned = ' '.join(str(topic or '').strip().split())
        if not cleaned:
            return ''
        cleaned = re.sub(r'^[\s,:;.-]+', '', cleaned)
        cleaned = re.sub(r'^(?:a|an|the)\s+', '', cleaned, flags=re.I)
        cleaned = re.sub(r'\b(?:and\s+)?the\s+conversation\s+continues?.*$', '', cleaned, flags=re.I)
        cleaned = re.sub(r'\buntil\s+i\s+stop\s+it.*$', '', cleaned, flags=re.I)
        cleaned = re.sub(r'\bto\s+(?:the\s+)?other\b.*$', '', cleaned, flags=re.I)
        cleaned = re.sub(r'\bto\s+(?:a|another)\s+(?:student|agent|hermes\s+agent|learner|beginner)\b.*$', '', cleaned, flags=re.I)
        cleaned = re.sub(r'^(?:another|the\s+other)\s+(?:on|about|for)\s+', '', cleaned, flags=re.I)
        cleaned = re.sub(r'\b(?:who|that)\s+wants?\s+to\s+understand\b.*$', '', cleaned, flags=re.I)
        cleaned = re.sub(r'\bone\s+is\s+the\s+(?:teacher|tutor|expert|mentor)\b.*$', '', cleaned, flags=re.I)
        cleaned = re.sub(r'\bthe\s+other\s+is\s+the\s+(?:student|learner|beginner)\b.*$', '', cleaned, flags=re.I)
        cleaned = re.sub(r'\b(?:please\s+)?keep\s+going\b.*$', '', cleaned, flags=re.I)
        cleaned = re.sub(r'\bbetween\s+two\s+hermes\s+agents\b.*$', '', cleaned, flags=re.I)
        cleaned = re.sub(r'\bwith\s+two\s+hermes\s+agents\b.*$', '', cleaned, flags=re.I)
        cleaned = cleaned.strip(' .,!?:;-')
        if cleaned.lower() in {'this', 'that', 'it'}:
            return ''
        return cleaned

    def _normalize_orchestration_request_text(self, text: str) -> str:
        normalized = ' '.join(str(text or '').strip().split())
        normalized = re.sub(r'^(?:please\s+|can\s+you\s+|could\s+you\s+|would\s+you\s+|i\s+want\s+|i\s+want\s+you\s+to\s+|let\'s\s+|lets\s+)', '', normalized, flags=re.I)
        normalized = re.sub(r'^(?:i\'d\s+like|i\s+would\s+like|we\s+need)\s+', 'start ', normalized, flags=re.I)
        normalized = re.sub(r'\bset\s+up\b', 'start', normalized, flags=re.I)
        normalized = re.sub(r'\bhave\b', 'start', normalized, flags=re.I)
        normalized = re.sub(r'\bget\b', 'start', normalized, flags=re.I)
        return normalized

    def _extract_orchestration_topic_from_text(self, text: str) -> str:
        stripped = ' '.join(str(text or '').strip().split())
        if not stripped:
            return ''
        patterns = [
            r'\b(?:explaining|explain|teaching|teach(?:ing)?|mentoring|mentor(?:ing)?|tutoring|tutor(?:ing)?|walking\s+through|walk\s+through)\s+(.+?)(?:[.?!]|$)',
            r'\b(?:pair\s*program(?:ming)?|implement(?:ing)?|debug(?:ging)?|refactor(?:ing)?)\s+(.+?)(?:[.?!]|$)',
            r'\b(?:about|on|for)\s+(.+?)(?:[.?!]|$)',
            r'\b(?:discuss(?:ing)?|talk(?:ing)?\s+about|chat(?:ting)?\s+about|brainstorm(?:ing)?|debate|debating|riff(?:ing)?\s+on)\s+(.+?)(?:[.?!]|$)',
        ]
        for pat in patterns:
            match = re.search(pat, stripped, re.I)
            if match:
                topic = self._clean_orchestration_topic(match.group(1))
                if topic:
                    return topic
        return ''

    def _last_user_chat_message(self) -> str:
        for msg in reversed(self.chat_history):
            if str(msg.get('role') or '') != 'user':
                continue
            content = str(msg.get('content') or '').strip()
            if not content or content.startswith('/'):
                continue
            return content
        return ''

    def _infer_orchestration_topic(self, text: str, context_text: str = '') -> str:
        direct = self._extract_orchestration_topic_from_text(text)
        if direct:
            return direct
        stripped = ' '.join(str(text or '').strip().split())
        if re.search(r'\b(?:about|on|for|discuss|talk\s+about|chat\s+about|explain|walk\s+through)\s+(?:this|that|it)\b', stripped, re.I):
            context_topic = self._extract_orchestration_topic_from_text(context_text)
            if context_topic:
                return context_topic
            return self._clean_orchestration_topic(context_text)[:160]
        return ''

    def _detect_orchestration_provider(self, lower: str) -> str:
        from charon.conversation.conversation_participants import supported_conversation_agent_types
        candidates = supported_conversation_agent_types()
        for provider in candidates:
            if re.search(rf'\b{re.escape(provider)}\b', lower):
                return provider
        return 'hermes'

    def _parse_orchestration_json(self, text: str) -> dict | None:
        raw = str(text or '').strip()
        if not raw:
            return None
        candidates = [raw]
        fenced = re.findall(r'```(?:json)?\s*(\{.*?\})\s*```', raw, flags=re.S | re.I)
        candidates.extend(fenced)
        brace_match = re.search(r'(\{.*\})', raw, flags=re.S)
        if brace_match:
            candidates.append(brace_match.group(1))
        for candidate in candidates:
            try:
                data = json.loads(candidate)
                if isinstance(data, dict):
                    return data
            except Exception:
                continue
        return None

    def _should_prefer_llm_orchestration(self, text: str) -> bool:
        lower = ' '.join(str(text or '').strip().lower().split())
        if not lower:
            return False
        ambiguous_patterns = [
            r'\bone\s+agent\b.+\b(other|another)\s+agent\b',
            r'\bone\s+.*\bthe\s+other\s+.*',
            r'\bplan\b.+\bcritic',
            r'\bcritique\b.+\bplan',
            r'\barchitecture\s+review\b',
            r'\bdesign\s+review\b',
            r'\bbest\s+case\b.+\bworst\s+case\b',
            r'\boptimistic\b.+\bskeptical\b',
            r'\bstrategic\b.+\bcritic',
            r'\bproposes?\b.+\breviews?\b',
        ]
        return any(re.search(p, lower) for p in ambiguous_patterns)

    def _llm_fallback_orchestration_command(self, text: str) -> tuple[str, str] | None:
        lower = ' '.join(str(text or '').strip().lower().split())
        if not lower:
            return None
        if not re.search(r'\b(start|create|spawn|launch|open|begin|orchestrate|conversation|team|agents?|participants?|room|session)\b', lower):
            return None
        try:
            from charon.conversation.conversation_participants import supported_conversation_agent_types
            from charon.providers.model_registry import get_shade_provider_and_model
            from charon.conversation.conversation_engine import ConversationEngine
        except Exception:
            return None

        provider, model, ready = get_shade_provider_and_model(common.STATE_DIR, phase_name='analysis', task_complexity='normal')
        if not ready:
            return None

        supported = supported_conversation_agent_types()
        archetypes = [
            'peer', 'teacher-student', 'debate', 'researcher-reviewer',
            'pair-programmers', 'strategist-critic', 'planner-critic',
            'architect-reviewer', 'optimist-skeptic',
        ]
        system_prompt = (
            'You are a lightweight orchestration intent parser for Charon. '
            'Return exactly one JSON object and nothing else. '
            'Do not use tools. Do not explain your reasoning. '
            'Infer the best conversation command intent from the user request. '
            'If the request is not clearly about creating a conversation/team room, return '
            '{"intent":"none","confidence":0}. '
            'Valid intents: conversation, team, none. '
            f'Valid providers: {", ".join(supported)}. '
            f'Valid archetypes: {", ".join(archetypes)}. '
            'Use count >= 2. Use team for 3+ generic multi-agent conversations. '
            'For two-agent archetypes use intent=conversation. '
            'JSON schema: '
            '{"intent":"conversation|team|none","provider":"...","count":2,'
            '"archetype":"...","topic":"...","confidence":0.0}.'
        )
        prompt = f'User request: {text.strip()}\nReturn JSON only.'
        try:
            engine = ConversationEngine(
                provider=provider,
                model=model,
                project_root=common.ROOT,
                agent_name='shades-router',
                system_prompt=system_prompt,
                state_dir=common.STATE_DIR,
                max_turns=1,
                max_tool_calls_per_turn=0,
                max_tokens=600,
            )
            reply, _events = asyncio.run(engine.submit_and_collect(prompt))
        except Exception:
            return None

        data = self._parse_orchestration_json(reply)
        if not isinstance(data, dict):
            return None
        intent = str(data.get('intent') or '').strip().lower()
        if intent not in ('conversation', 'team'):
            return None
        confidence = float(data.get('confidence') or 0.0)
        if confidence < 0.55:
            return None
        provider_name = str(data.get('provider') or '').strip().lower() or self._detect_orchestration_provider(lower)
        if provider_name not in supported:
            return None
        count_raw = data.get('count')
        try:
            count = max(2, int(count_raw or 2))
        except Exception:
            count = 2
        archetype = str(data.get('archetype') or '').strip().lower().replace('/', '-').replace(' ', '-')
        topic = self._clean_orchestration_topic(str(data.get('topic') or self._infer_orchestration_topic(text, self._last_user_chat_message()) or 'open discussion'))
        if not topic:
            topic = 'open discussion'

        alias_map = {
            'teacher-student': ('conversation', 'teacher student'),
            'peer': ('conversation', 'peer'),
            'debate': ('conversation', 'debate'),
            'researcher-reviewer': ('conversation', 'researcher reviewer'),
            'pair-programmers': ('conversation', 'pair-programmers'),
            'strategist-critic': ('conversation', 'strategist critic'),
            'planner-critic': ('conversation', 'planner critic'),
            'architect-reviewer': ('conversation', 'architect reviewer'),
            'optimist-skeptic': ('conversation', 'optimist skeptic'),
        }
        if intent == 'team' or count > 2:
            cmd = f'/team {provider_name} {count} {topic}'
            return cmd, f'Routing orchestration request via shades parser to {cmd}'
        if archetype in alias_map:
            _kind, cmd_suffix = alias_map[archetype]
            cmd = f'/conversation {provider_name} {cmd_suffix} {topic}'
            return cmd, f'Routing orchestration request via shades parser to {cmd}'
        if count == 2:
            cmd = f'/conversation {provider_name} peer {topic}'
            return cmd, f'Routing orchestration request via shades parser to {cmd}'
        cmd = f'/team {provider_name} {count} {topic}'
        return cmd, f'Routing orchestration request via shades parser to {cmd}'

    def _match_nl_orchestration_command(self, text: str) -> tuple[str, str] | None:
        context_text = self._last_user_chat_message()
        stripped = self._normalize_orchestration_request_text(text)
        lower = stripped.lower()
        if not stripped:
            return None
        provider = self._detect_orchestration_provider(lower)
        if provider not in lower:
            return None

        action_requested = bool(re.search(r'\b(make|create|spawn|start|launch|open|begin|orchestrate)\b', lower))
        # Only match terms that clearly signal a multi-agent conversation request.
        # Avoid generic words like "agent", "session", "team", "set up" — they trigger
        # false positives on normal requests about server agents, fleet setup, etc.
        roomish_request = bool(re.search(r'\b(room|rooms|conversation|conversations|dialogue|discussion|discussions|participant|participants|exchange)\b', lower))
        roleish_request = bool(re.search(r'\b(teacher|student|tutor|learner|beginner|expert|mentor)\b', lower))
        if not action_requested or not (roomish_request or roleish_request):
            return None

        count = None
        count_match = re.search(r'\b(\d+)\b', lower)
        if count_match:
            count = max(2, int(count_match.group(1)))
        else:
            word_to_num = {'two': 2, 'three': 3, 'four': 4, 'five': 5}
            for word, value in word_to_num.items():
                if re.search(rf'\b{word}\b', lower):
                    count = value
                    break

        teacher_student_hint = (
            ('teacher' in lower and 'student' in lower)
            or any(token in lower for token in [' tutor ', ' learner ', ' beginner ', ' expert ', ' mentor '])
            or bool(re.search(r'\bteacher\s+explain', lower))
            or bool(re.search(r'\bone\s+is\s+the\s+(?:teacher|tutor|expert|mentor)\b', lower))
            or bool(re.search(r'\bthe\s+other\s+is\s+the\s+(?:student|learner|beginner)\b', lower))
            or bool(re.search(r'\b(?:explaining|teaching|mentoring|tutoring)\b.+\b(?:student|learner|beginner)\b', lower))
            or bool(re.search(r'\b(?:expert|teacher|tutor|mentor)\b.+\b(?:beginner|learner|student)\b', lower))
        )
        prefer_llm = self._should_prefer_llm_orchestration(stripped)
        debate_hint = bool(re.search(r'\b(debate|argue|argument|arguing|for\s+and\s+against|pros\s+and\s+cons|advocate|opposition)\b', lower))
        pair_programming_hint = bool(re.search(r'\b(pair\s*program(?:ming)?|pairing|driver|navigator|implement(?:ing)?|debug(?:ging)?|refactor(?:ing)?|coding)\b', lower))
        researcher_reviewer_hint = bool(re.search(r'\b(researcher\s+and\s+reviewer|researcher/reviewer|research\s+and\s+review|review\s+my\s+research|research\s+question)\b', lower))
        strategist_critic_hint = bool(re.search(r'\b(strategist\s+and\s+critic|strategist/critic|strategy\s+and\s+critique|stress[- ]?test\s+the\s+strategy)\b', lower))
        planner_critic_hint = bool(re.search(r'\b(planner\s+and\s+critic|planner/critic|planning\s+and\s+critique|execution\s+plan)\b', lower)) or bool(re.search(r'\bone\s+agent\b.+\bplan(?:s|ning)?\b.+\b(other|another)\s+agent\b.+\bcritic|critique\b', lower))
        architect_reviewer_hint = bool(re.search(r'\b(architect\s+and\s+reviewer|architect/reviewer|architecture\s+review|design\s+review)\b', lower)) or bool(re.search(r'\bone\s+agent\b.+\barchitect(?:ure)?\b.+\b(other|another)\s+agent\b.+\breview\b', lower))
        optimist_skeptic_hint = bool(re.search(r'\b(optimist\s+and\s+skeptic|optimist/skeptic|optimism\s+and\s+skepticism|best\s+case\s+and\s+worst\s+case)\b', lower)) or (('optimistic' in lower or 'optimist' in lower) and ('skeptical' in lower or 'skeptic' in lower))
        topic = self._infer_orchestration_topic(stripped, context_text) or 'open discussion'

        if prefer_llm:
            llm_route = self._llm_fallback_orchestration_command(text)
            if llm_route:
                return llm_route

        if teacher_student_hint:
            return (
                f'/conversation {provider} teacher student {topic}',
                f'Routing orchestration request via fast-path to /conversation {provider} teacher student {topic}',
            )

        if debate_hint:
            return (
                f'/conversation {provider} debate {topic}',
                f'Routing orchestration request via fast-path to /conversation {provider} debate {topic}',
            )

        if researcher_reviewer_hint:
            return (
                f'/conversation {provider} researcher reviewer {topic}',
                f'Routing orchestration request via fast-path to /conversation {provider} researcher reviewer {topic}',
            )

        if strategist_critic_hint:
            return (
                f'/conversation {provider} strategist critic {topic}',
                f'Routing orchestration request via fast-path to /conversation {provider} strategist critic {topic}',
            )

        if planner_critic_hint:
            return (
                f'/conversation {provider} planner critic {topic}',
                f'Routing orchestration request via fast-path to /conversation {provider} planner critic {topic}',
            )

        if architect_reviewer_hint:
            return (
                f'/conversation {provider} architect reviewer {topic}',
                f'Routing orchestration request via fast-path to /conversation {provider} architect reviewer {topic}',
            )

        if optimist_skeptic_hint:
            return (
                f'/conversation {provider} optimist skeptic {topic}',
                f'Routing orchestration request via fast-path to /conversation {provider} optimist skeptic {topic}',
            )

        if pair_programming_hint:
            return (
                f'/conversation {provider} pair-programmers {topic}',
                f'Routing orchestration request via fast-path to /conversation {provider} pair-programmers {topic}',
            )

        if count == 2:
            return (
                f'/conversation {provider} peer {topic}',
                f'Routing orchestration request via fast-path to /conversation {provider} peer {topic}',
            )

        if count and count > 2:
            return (
                f'/team {provider} {count} {topic}',
                f'Routing orchestration request via fast-path to /team {provider} {count} {topic}',
            )

        if re.search(r'\b(team|room|session|sessions|agent|agents|participant|participants)\b', lower):
            return (
                f'/conversation {provider} peer {topic}',
                f'Routing orchestration request via fast-path to /conversation {provider} peer {topic}',
            )

        generic_team_match = re.match(
            rf'^(?:make|create|spawn|start|launch|open|begin|orchestrate)\s+(?:me\s+)?(?:a\s+)?(?:(\d+)\s+)?(?:charons[- ]boat\s+wrapped\s+)?{re.escape(provider)}\s+(?:sessions?|agents?|team|participants?)\s*(?:and\s+have\s+them\s+(?:discuss|chat|talk|brainstorm|debate|riff)(?:\s+back\s+and\s+forth)?\s+|to\s+(?:discuss|chat|talk|brainstorm|debate|riff)\s+|for\s+|about\s+|on\s+)?(.+)?$',
            stripped,
            re.I,
        )
        if generic_team_match:
            fallback_count = int(generic_team_match.group(1) or count or 2)
            fallback_topic = self._clean_orchestration_topic(generic_team_match.group(2) or topic) or self._infer_orchestration_topic(stripped, context_text) or 'open discussion'
            if fallback_count <= 2:
                return (
                    f'/conversation {provider} peer {fallback_topic}',
                    f'Routing orchestration request via fast-path to /conversation {provider} peer {fallback_topic}',
                )
            return (
                f'/team {provider} {fallback_count} {fallback_topic}',
                f'Routing orchestration request via fast-path to /team {provider} {fallback_count} {fallback_topic}',
            )

        return self._llm_fallback_orchestration_command(text)

    def _resolve_pending_outcome(self, status: str) -> None:
        if not self._session_tasks:
            return
        for item in reversed(self._session_tasks):
            if item.get('status') == 'active':
                item['status'] = status
                item['resolved_at'] = time.time()
                if not str(item.get('title') or '').strip():
                    item['title'] = self._make_outcome_title(item.get('kind', 'updated'), item.get('object', 'task'), status)
                self._save_session_outcomes()
                return

    def _is_question_message(self, text: str) -> bool:
        """True if the message is a question or lookup request (no clear action to track as an outcome)."""
        t = ' '.join(text.strip().lower().split())
        if not t:
            return False
        # Pure acks are handled separately
        if self._is_ack_message(t):
            return True
        # Strip leading ack phrase so "okay, so what are..." → "what are..."
        t = re.sub(r'^(ok|okay|thanks|great|nice|cool|yep|yes|perfect|sounds good)[!.,\s]+(so\s+)?', '', t)
        question_patterns = [
            r'^what\b', r'^where\b', r'^when\b', r'^who\b', r'^which\b', r'^how\b',
            r'^show me\b', r'^list\b', r'^tell me\b', r'^can you show\b',
            r'^can you list\b', r'^can you tell\b', r'^do you\b', r'^is there\b',
            r'^are there\b', r'^give me\b', r'^what\'s\b', r'^whats\b',
            r'^any more\b', r'^more\b', r'^what about\b', r'^anything\b',
        ]
        return any(re.match(p, t) for p in question_patterns)

    def _start_outcome_for_message(self, message: str) -> None:
        parsed = self._parse_intent(message)
        if parsed:
            kind, obj = parsed
            title = self._make_outcome_title(kind, obj, 'active')
        elif self._is_question_message(message):
            # Questions and lookups don't produce a trackable outcome — skip.
            return
        else:
            kind, obj = 'working', 'task'
            try:
                from charon.agents.task_summarizer import summarize_instruction_fast
                title = summarize_instruction_fast(message)
            except Exception:
                title = message[:80].strip() or 'Working on task'
        self._session_tasks.append({
            'task_id': f'outcome-{int(time.time() * 1000)}',
            'status': 'active',
            'kind': kind,
            'object': obj,
            'title': title,
            'instruction': message[:200],
            'summary': '',
            'detail': '',
            'tokens_in': 0,
            'tokens_out': 0,
            'tool_calls': 0,
            'turns': 0,
            'files_touched': [],
            'ts': time.time(),
            'resolved_at': 0,
        })
        self._save_session_outcomes()

    def _improve_active_outcome_title_background(self, message: str, request_id: str | None) -> None:
        if not self._session_tasks:
            return
        task_id = str(self._session_tasks[-1].get('task_id') or '')
        if not task_id:
            return

        def _run() -> None:
            try:
                import asyncio as _aio
                from charon.agents.task_summarizer import summarize_instruction_rich, summarize_instruction_fast
                try:
                    from charon.providers.model_registry import get_shade_provider_and_model
                    provider, model, ready = get_shade_provider_and_model(common.STATE_DIR, phase_name='analysis', task_complexity='normal')
                except Exception:
                    provider = model = None
                    ready = False
                if ready and provider is not None and model is not None:
                    title = _aio.run(summarize_instruction_rich(
                        instruction=message,
                        provider=provider,
                        model=model,
                    ))
                else:
                    title = summarize_instruction_fast(message)
                title = str(title or '').strip()[:100]
                if not title:
                    return
                for item in reversed(self._session_tasks):
                    if str(item.get('task_id') or '') != task_id:
                        continue
                    if item.get('status') != 'active':
                        return
                    item['title'] = title
                    item['detail'] = f'Task: {message[:160]}'
                    item['ts'] = time.time()
                    self._save_session_outcomes()
                    common.emit({'type': 'refresh', 'payload': {'session_info': self._get_session_info()}, 'request_id': request_id})
                    return
            except Exception:
                return

        threading.Thread(target=_run, daemon=True).start()

    def _load_tasks_from_ledger(self, agent_id: str | None = None) -> None:
        """Load session-local outcome ledger for the current conversation.

        This is intentionally session-scoped. We only restore outcomes when
        resuming a specific session, not from agent-level memory.
        """
        path = self._outcomes_path(agent_id)
        if not path or not path.exists():
            self._session_tasks = []
            return
        try:
            data = json.loads(path.read_text())
            self._session_tasks = data if isinstance(data, list) else []
        except Exception:
            self._session_tasks = []

    def _project_root_for_rooms(self) -> Path:
        onboarding = common._load_json(common.STATE_DIR / 'onboarding.json', {})
        return Path(str(onboarding.get('project') or str(common.ROOT)).strip() or str(common.ROOT))

    def _load_libris_room(self, room_id: str) -> dict | None:
        rid = str(room_id or '').strip()
        if not rid.startswith('libris-'):
            return None
        op_id = rid[len('libris-'):].strip()
        if not op_id:
            return None
        try:
            from charon.libris.libris_runtime import get_libris_swarm_state
            project_root = self._project_root_for_rooms()
            swarm = get_libris_swarm_state(common.STATE_DIR, project_root, op_id)
            if not swarm:
                return None
            return {
                'id': rid,
                'kind': 'libris',
                'operation_id': op_id,
                'title': str(swarm.get('prompt') or op_id)[:120],
                'status': str(swarm.get('status') or 'active'),
                'participants': [
                    {
                        'id': str(n.get('agent_id') or ''),
                        'name': str(n.get('name') or ''),
                        'role': str(n.get('role') or ''),
                        'topic_slug': str(n.get('topic_slug') or ''),
                    }
                    for n in (swarm.get('nodes') or [])
                    if str(n.get('agent_id') or '').strip()
                ],
                'nodes': swarm.get('nodes') or [],
                'topics': swarm.get('topics') or [],
            }
        except Exception:
            return None

    def _resolve_libris_targets(self, room: dict, target: str) -> tuple[list[dict], str]:
        nodes = [n for n in (room.get('nodes') or []) if isinstance(n, dict)]
        topics = [t for t in (room.get('topics') or []) if isinstance(t, dict)]
        token = str(target or 'whole').strip()
        tl = token.lower()
        if tl in ('', 'whole', 'room', 'all', '*'):
            return [n for n in nodes if str(n.get('agent_id') or '').strip()], 'whole room'
        if tl == 'coordinator':
            out = [n for n in nodes if str(n.get('role') or '').lower() == 'coordinator']
            return out, 'coordinator'
        if tl.startswith('node:'):
            node_id = token.split(':', 1)[1].strip()
            out = [n for n in nodes if str(n.get('agent_id') or '') == node_id]
            return out, f'node:{node_id}'
        if tl.startswith('agent:'):
            node_id = token.split(':', 1)[1].strip()
            out = [n for n in nodes if str(n.get('agent_id') or '') == node_id]
            return out, f'agent:{node_id}'
        if tl.startswith('topic:'):
            slug = token.split(':', 1)[1].strip()
            out: list[dict] = []
            for topic in topics:
                if str(topic.get('topic_slug') or '') != slug:
                    continue
                for key in ('researcher', 'judge'):
                    node = topic.get(key) or {}
                    if str(node.get('agent_id') or '').strip():
                        out.append(node)
                break
            return out, f'topic:{slug}'
        if tl.startswith('researcher:') or tl.startswith('judge:'):
            role, slug = tl.split(':', 1)
            out = [n for n in nodes if str(n.get('role') or '').lower() == role and str(n.get('topic_slug') or '') == slug]
            return out, f'{role}:{slug}'
        if tl.startswith('shade:'):
            shade_id = token.split(':', 1)[1].strip()
            out = [n for n in nodes if str(n.get('role') or '').lower() == 'shade' and str(n.get('agent_id') or '') == shade_id]
            return out, f'shade:{shade_id}'
        out = [
            n for n in nodes
            if tl in str(n.get('agent_id') or '').lower()
            or tl in str(n.get('name') or '').lower()
            or tl == str(n.get('role') or '').lower()
        ]
        return out, token

    def _dispatch_libris_room_intervention(self, room_id: str, *, target: str, when: str, message: str, request_id: str | None, mode: str = 'inject') -> bool:
        room = self._load_libris_room(room_id)
        if not room:
            common.emit({'type': 'error', 'error': f'Unknown room: {room_id}', 'request_id': request_id})
            return True
        targets, target_label = self._resolve_libris_targets(room, target)
        if not targets:
            common.emit({'type': 'error', 'error': f'No Libris targets matched: {target}', 'request_id': request_id})
            return True
        try:
            from charon.agents.session_registry import send_steer
            from charon.libris.libris_runtime import append_operation_event
            project_root = self._project_root_for_rooms()
            sent: list[str] = []
            for node in targets:
                agent_id = str(node.get('agent_id') or '').strip()
                if not agent_id:
                    continue
                if send_steer(common.STATE_DIR, agent_id, message):
                    sent.append(agent_id)
            append_operation_event(
                common.STATE_DIR,
                project_root,
                str(room.get('operation_id') or ''),
                'operator_intervention',
                {
                    'mode': mode,
                    'room_id': room_id,
                    'target': target_label,
                    'requested_target': str(target or ''),
                    'when': str(when or 'next'),
                    'summary': str(message or '')[:240],
                    'message': str(message or ''),
                    'target_agent_ids': sent,
                },
            )
            common.emit({
                'type': 'status',
                'message': f'{"Sent" if mode == "say" else "Queued"} Libris intervention for {room_id} target={target_label} agents={len(sent)}: {message[:120]}',
                'request_id': request_id,
            })
            self.handle_refresh(request_id)
            return True
        except Exception as e:
            common.emit({'type': 'error', 'error': f'Libris intervention failed: {e}', 'request_id': request_id})
            return True

    def _conversation_hermes_local_runtime(self, meta: dict | None = None) -> dict[str, str]:
        meta = dict(meta or {})
        onboarding = common._load_json(common.STATE_DIR / 'onboarding.json', {})
        cfg = resolve_provider_config(common.STATE_DIR, session_id=self._active_agent_id or None)
        base_url = str(meta.get('base_url') or config.local_base_url() or config.lmstudio_base_url() or '').strip().rstrip('/')
        if not base_url:
            base_url = str(cfg.get('base_url') or '').strip().rstrip('/')
        if not base_url:
            base_url = 'http://127.0.0.1:1234/v1'

        model = str(meta.get('model') or '').strip()
        if not model and str(cfg.get('provider_raw') or '').strip().lower() in ('lmstudio', 'local'):
            model = str(cfg.get('model_id') or '').strip()
        if not model:
            detected = self._detect_lmstudio_models()
            if detected:
                model = detected[0]
        if not model:
            raw = str(onboarding.get('provider_model') or onboarding.get('model') or '').strip()
            candidate = raw.split('/', 1)[1] if '/' in raw else raw
            lowered = candidate.lower()
            if candidate and not lowered.startswith('claude-') and not lowered.startswith('gpt-') and lowered not in ('o3', 'o4-mini', 'o3-mini', 'codex-mini-latest'):
                model = candidate
        if not model:
            model = config.local_model() or 'qwen3-30b-a3b'
        return {'provider': 'lmstudio', 'base_url': base_url, 'model': model}
