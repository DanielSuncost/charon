"""Conversation-room slash-command handlers: conversation, team, devteam,
pause/resume/say/inject/delete-room.

Branch bodies are preserved verbatim from the original ``handle_command``
if/elif router in ``commands_mixin.py``; only the method wrappers and the
trailing ``return UNHANDLED`` are new. See ``CommandsMixin.handle_command``
for the dispatch.
"""
from __future__ import annotations

import shlex

from backend import common
from backend.boat import _terminate_boat_session
from backend.commands_mixin import UNHANDLED


class RoomCommandsMixin:
    """Handlers for the conversation-room command families."""

    def _cmd_conversation(self, command: str, request_id: str | None):
        if command == '/conversation' or command.startswith('/conversation '):
            rest = command[13:].strip() if command.startswith('/conversation ') else ''
            try:
                if not rest:
                    common.emit({'type': 'status', 'message': 'Usage: /conversation <agent-type> [peer|teacher student|debate|researcher reviewer|strategist critic|planner critic|architect reviewer|optimist skeptic|pair-programmers|dialogue|<count>] <topic>', 'request_id': request_id})
                    return
                parts = rest.split()
                provider = (parts[0] if parts else '').strip().lower()
                from charon.conversation.conversation_participants import get_conversation_adapter
                adapter = get_conversation_adapter(provider)
                if not adapter:
                    common.emit({'type': 'error', 'error': f'Unsupported conversation provider for now: {provider}', 'request_id': request_id})
                    return
                if not adapter.capabilities.can_spawn:
                    common.emit({'type': 'error', 'error': f'Conversation spawning is not wired yet for: {provider}', 'request_id': request_id})
                    return
                roles: list[str] = []
                topic = ''
                runner_mode = 'teacher-student'
                if len(parts) >= 4 and parts[1].lower() == 'teacher' and parts[2].lower() == 'student':
                    roles = ['teacher', 'student']
                    topic = ' '.join(parts[3:]).strip()
                    runner_mode = 'teacher-student'
                elif len(parts) >= 3 and parts[1].lower() in ('peer', 'dialogue', 'discuss'):
                    roles = ['peer-1', 'peer-2']
                    topic = ' '.join(parts[2:]).strip()
                    runner_mode = 'peer'
                elif len(parts) >= 4 and parts[1].lower() == 'researcher' and parts[2].lower() == 'reviewer':
                    roles = ['researcher', 'reviewer']
                    topic = ' '.join(parts[3:]).strip()
                    runner_mode = 'researcher-reviewer'
                elif len(parts) >= 3 and parts[1].lower() in ('research', 'researcher-reviewer'):
                    roles = ['researcher', 'reviewer']
                    topic = ' '.join(parts[2:]).strip()
                    runner_mode = 'researcher-reviewer'
                elif len(parts) >= 3 and parts[1].lower() in ('pair-programmers', 'pair-programming', 'pair-programmer'):
                    roles = ['driver', 'navigator']
                    topic = ' '.join(parts[2:]).strip()
                    runner_mode = 'pair-programmers'
                elif len(parts) >= 4 and parts[1].lower() == 'pair' and parts[2].lower() in ('programmers', 'programming'):
                    roles = ['driver', 'navigator']
                    topic = ' '.join(parts[3:]).strip()
                    runner_mode = 'pair-programmers'
                elif len(parts) >= 4 and parts[1].lower() == 'strategist' and parts[2].lower() == 'critic':
                    roles = ['strategist', 'critic']
                    topic = ' '.join(parts[3:]).strip()
                    runner_mode = 'strategist-critic'
                elif len(parts) >= 3 and parts[1].lower() in ('strategist-critic', 'strategy-critique'):
                    roles = ['strategist', 'critic']
                    topic = ' '.join(parts[2:]).strip()
                    runner_mode = 'strategist-critic'
                elif len(parts) >= 4 and parts[1].lower() == 'planner' and parts[2].lower() == 'critic':
                    roles = ['planner', 'critic']
                    topic = ' '.join(parts[3:]).strip()
                    runner_mode = 'planner-critic'
                elif len(parts) >= 3 and parts[1].lower() in ('planner-critic', 'planning-critique'):
                    roles = ['planner', 'critic']
                    topic = ' '.join(parts[2:]).strip()
                    runner_mode = 'planner-critic'
                elif len(parts) >= 4 and parts[1].lower() == 'architect' and parts[2].lower() == 'reviewer':
                    roles = ['architect', 'reviewer']
                    topic = ' '.join(parts[3:]).strip()
                    runner_mode = 'architect-reviewer'
                elif len(parts) >= 3 and parts[1].lower() in ('architect-reviewer', 'architecture-review'):
                    roles = ['architect', 'reviewer']
                    topic = ' '.join(parts[2:]).strip()
                    runner_mode = 'architect-reviewer'
                elif len(parts) >= 4 and parts[1].lower() == 'optimist' and parts[2].lower() == 'skeptic':
                    roles = ['optimist', 'skeptic']
                    topic = ' '.join(parts[3:]).strip()
                    runner_mode = 'optimist-skeptic'
                elif len(parts) >= 3 and parts[1].lower() in ('optimist-skeptic', 'optimism-skepticism'):
                    roles = ['optimist', 'skeptic']
                    topic = ' '.join(parts[2:]).strip()
                    runner_mode = 'optimist-skeptic'
                elif len(parts) >= 3 and parts[1].lower() in ('debate',):
                    roles = ['advocate', 'opposition']
                    topic = ' '.join(parts[2:]).strip()
                    runner_mode = 'debate'
                elif len(parts) >= 3 and str(parts[1]).isdigit():
                    count = max(2, int(parts[1]))
                    roles = ['peer-1', 'peer-2'] if count == 2 else [f'peer-{idx+1}' for idx in range(count)]
                    topic = ' '.join(parts[2:]).strip()
                    runner_mode = 'peer'
                else:
                    roles = ['teacher', 'student']
                    topic = ' '.join(parts[1:]).strip()
                    runner_mode = 'teacher-student'
                topic = topic or 'open discussion'
                onboarding = common._load_json(common.STATE_DIR / 'onboarding.json', {})
                project = str(onboarding.get('project') or str(common.ROOT)).strip()
                if runner_mode == 'dialogue':
                    participants = [
                        {'id': f'hermes-{idx+1}', 'role': role, 'name': f'Hermes {idx+1}'}
                        for idx, role in enumerate(roles)
                    ]
                else:
                    participants = [
                        {'id': f'hermes-{idx+1}', 'role': role, 'name': f'Hermes {idx+1}'}
                        for idx, role in enumerate(roles)
                    ]
                self._create_conversation_room(
                    agent_type=provider,
                    kind='conversation',
                    title=topic,
                    project=project,
                    participants=participants,
                    meta={'provider': provider, 'count': len(participants), 'topic': topic, 'conversation_mode': runner_mode},
                    request_id=request_id,
                    start_runner=True,
                    runner_mode=runner_mode,
                )
                return
            except Exception as e:
                common.emit({'type': 'error', 'error': f'Conversation command failed: {e}', 'request_id': request_id})
                return
        return UNHANDLED

    def _cmd_team(self, command: str, request_id: str | None):
        if command == '/team' or command.startswith('/team '):
            rest = command[5:].strip() if command.startswith('/team ') else ''
            try:
                if not rest:
                    common.emit({'type': 'status', 'message': 'Usage: /team <agent-type> <count> <topic>', 'request_id': request_id})
                    return
                parts = rest.split(None, 2)
                provider = (parts[0] if len(parts) > 0 else '').strip().lower()
                count = int(parts[1]) if len(parts) > 1 and str(parts[1]).isdigit() else 2
                topic = (parts[2] if len(parts) > 2 else '').strip() or 'open discussion'
                from charon.conversation.conversation_participants import get_conversation_adapter
                adapter = get_conversation_adapter(provider)
                if not adapter:
                    common.emit({'type': 'error', 'error': f'Unsupported team provider for now: {provider}', 'request_id': request_id})
                    return
                if not adapter.capabilities.can_spawn:
                    common.emit({'type': 'error', 'error': f'Team spawning is not wired yet for: {provider}', 'request_id': request_id})
                    return

                onboarding = common._load_json(common.STATE_DIR / 'onboarding.json', {})
                project = str(onboarding.get('project') or str(common.ROOT)).strip()
                participants = []
                for idx in range(count):
                    role = f'peer-{idx+1}' if count > 2 else ('peer-1' if idx == 0 else 'peer-2')
                    participants.append({'id': f'hermes-{idx+1}', 'role': role, 'name': f'Hermes {idx+1}'})
                self._create_conversation_room(
                    agent_type=provider,
                    kind='conversation',
                    title=topic,
                    project=project,
                    participants=participants,
                    meta={'provider': provider, 'count': count, 'topic': topic, 'conversation_mode': 'peer'},
                    request_id=request_id,
                    start_runner=True,
                    runner_mode='peer',
                )
                return
            except Exception as e:
                common.emit({'type': 'error', 'error': f'Team command failed: {e}', 'request_id': request_id})
                return
        return UNHANDLED

    def _cmd_devteam(self, command: str, request_id: str | None):
        if command == '/devteam' or command.startswith('/devteam '):
            rest = command[8:].strip() if command.startswith('/devteam ') else ''
            try:
                if not rest:
                    common.emit({'type': 'status', 'message': 'Usage: /devteam <agent-type> <count> <goal>', 'request_id': request_id})
                    return
                parts = rest.split(None, 2)
                provider = (parts[0] if len(parts) > 0 else '').strip().lower()
                count = int(parts[1]) if len(parts) > 1 and str(parts[1]).isdigit() else 2
                goal = (parts[2] if len(parts) > 2 else '').strip() or 'engineering task'
                from charon.conversation.conversation_participants import get_conversation_adapter
                adapter = get_conversation_adapter(provider)
                if not adapter:
                    common.emit({'type': 'error', 'error': f'Unsupported devteam provider for now: {provider}', 'request_id': request_id})
                    return
                if not adapter.capabilities.can_spawn:
                    common.emit({'type': 'error', 'error': f'Devteam spawning is not wired yet for: {provider}', 'request_id': request_id})
                    return
                onboarding = common._load_json(common.STATE_DIR / 'onboarding.json', {})
                project = str(onboarding.get('project') or str(common.ROOT)).strip()
                participants = [
                    {'id': f'hermes-{idx+1}', 'role': 'developer', 'name': f'Hermes {idx+1}'}
                    for idx in range(count)
                ]
                self._create_conversation_room(
                    agent_type=provider,
                    kind='devteam',
                    title=goal,
                    project=project,
                    participants=participants,
                    meta={'provider': provider, 'count': count, 'goal': goal, 'team_mode': 'devteam'},
                    request_id=request_id,
                    start_runner=False,
                )
                return
            except Exception as e:
                common.emit({'type': 'error', 'error': f'Devteam command failed: {e}', 'request_id': request_id})
                return
        return UNHANDLED

    def _cmd_pause_room(self, command: str, request_id: str | None):
        if command == '/pause-room' or command.startswith('/pause-room '):
            room_id = command[12:].strip() if command.startswith('/pause-room ') else ''
            try:
                if not room_id:
                    common.emit({'type': 'status', 'message': 'Usage: /pause-room <room-id>', 'request_id': request_id})
                    return
                from charon.agents.inter_agent_rooms import append_event, load_room, update_room
                room = load_room(common.STATE_DIR, room_id)
                if not room:
                    common.emit({'type': 'error', 'error': f'Unknown room: {room_id}', 'request_id': request_id})
                    return
                update_room(common.STATE_DIR, room_id, status='paused', summary=f'Paused room {room_id}')
                append_event(common.STATE_DIR, room_id, {'type': 'room_paused', 'summary': f'Paused room {room_id}'})
                common.emit({'type': 'status', 'message': f'Paused room: {room_id}', 'request_id': request_id})
                self.handle_refresh(request_id)
                return
            except Exception as e:
                common.emit({'type': 'error', 'error': f'Pause room failed: {e}', 'request_id': request_id})
                return
        return UNHANDLED

    def _cmd_resume_room(self, command: str, request_id: str | None):
        if command == '/resume-room' or command.startswith('/resume-room '):
            room_id = command[13:].strip() if command.startswith('/resume-room ') else ''
            try:
                if not room_id:
                    common.emit({'type': 'status', 'message': 'Usage: /resume-room <room-id>', 'request_id': request_id})
                    return
                from charon.agents.inter_agent_rooms import append_event, load_room, update_room
                room = load_room(common.STATE_DIR, room_id)
                if not room:
                    common.emit({'type': 'error', 'error': f'Unknown room: {room_id}', 'request_id': request_id})
                    return
                update_room(common.STATE_DIR, room_id, status='active', summary=f'Resumed room {room_id}')
                append_event(common.STATE_DIR, room_id, {'type': 'room_resumed', 'summary': f'Resumed room {room_id}'})
                participants = list(room.get('participants') or [])
                if len(participants) >= 2:
                    self._start_conversation_room_runner(
                        room_id,
                        str(room.get('title') or room_id),
                        participants,
                        mode=self._room_runner_mode(room),
                    )
                common.emit({'type': 'status', 'message': f'Resumed room: {room_id}', 'request_id': request_id})
                self.handle_refresh(request_id)
                return
            except Exception as e:
                common.emit({'type': 'error', 'error': f'Resume room failed: {e}', 'request_id': request_id})
                return
        return UNHANDLED

    def _cmd_say_room(self, command: str, request_id: str | None):
        if command == '/say-room' or command.startswith('/say-room '):
            rest = command[10:].strip() if command.startswith('/say-room ') else ''
            try:
                if not rest:
                    common.emit({'type': 'status', 'message': 'Usage: /say-room <room-id> <message>', 'request_id': request_id})
                    return
                parts = shlex.split(rest)
                if len(parts) < 2:
                    common.emit({'type': 'status', 'message': 'Usage: /say-room <room-id> <message>', 'request_id': request_id})
                    return
                room_id = parts[0]
                message = ' '.join(parts[1:]).strip()
                if self._dispatch_libris_room_intervention(room_id, target='whole', when='now', message=message, request_id=request_id, mode='say'):
                    return
                from charon.agents.inter_agent_rooms import append_event, load_room, queue_injection
                room = load_room(common.STATE_DIR, room_id)
                if not room:
                    common.emit({'type': 'error', 'error': f'Unknown room: {room_id}', 'request_id': request_id})
                    return
                item = queue_injection(common.STATE_DIR, room_id, message=message, target='whole', when='now', sender='user')
                if not item:
                    common.emit({'type': 'error', 'error': f'Failed to send room message for: {room_id}', 'request_id': request_id})
                    return
                append_event(common.STATE_DIR, room_id, {
                    'type': 'room_message_sent',
                    'target': 'whole',
                    'summary': message[:240],
                    'message': message,
                })
                if str(room.get('status') or 'active') == 'active' and len(list(room.get('participants') or [])) >= 2:
                    self._start_conversation_room_runner(
                        room_id,
                        str(room.get('title') or room_id),
                        list(room.get('participants') or []),
                        mode=self._room_runner_mode(room),
                    )
                common.emit({'type': 'status', 'message': f'Sent room message to {room_id}: {message[:120]}', 'request_id': request_id})
                self.handle_refresh(request_id)
                return
            except Exception as e:
                common.emit({'type': 'error', 'error': f'Say room failed: {e}', 'request_id': request_id})
                return
        return UNHANDLED

    def _cmd_inject_room(self, command: str, request_id: str | None):
        if command == '/inject-room' or command.startswith('/inject-room '):
            rest = command[13:].strip() if command.startswith('/inject-room ') else ''
            try:
                if not rest:
                    common.emit({'type': 'status', 'message': 'Usage: /inject-room <room-id> [--target whole|teacher|student|<participant>|coordinator|topic:<slug>|node:<agent-id>|researcher:<slug>|judge:<slug>|shade:<agent-id>] [--when now|next] <message>', 'request_id': request_id})
                    return
                parts = shlex.split(rest)
                if not parts:
                    common.emit({'type': 'status', 'message': 'Usage: /inject-room <room-id> [--target whole|teacher|student|<participant>|coordinator|topic:<slug>|node:<agent-id>|researcher:<slug>|judge:<slug>|shade:<agent-id>] [--when now|next] <message>', 'request_id': request_id})
                    return
                room_id = parts[0]
                target = 'whole'
                when = 'next'
                idx = 1
                while idx < len(parts):
                    token = parts[idx]
                    if token == '--target' and idx + 1 < len(parts):
                        target = parts[idx + 1]
                        idx += 2
                        continue
                    if token == '--when' and idx + 1 < len(parts):
                        when = parts[idx + 1]
                        idx += 2
                        continue
                    break
                message = ' '.join(parts[idx:]).strip()
                if not message:
                    common.emit({'type': 'error', 'error': 'Injection message cannot be empty.', 'request_id': request_id})
                    return
                if self._dispatch_libris_room_intervention(room_id, target=target, when=when, message=message, request_id=request_id, mode='inject'):
                    return
                from charon.agents.inter_agent_rooms import append_event, load_room, queue_injection
                room = load_room(common.STATE_DIR, room_id)
                if not room:
                    common.emit({'type': 'error', 'error': f'Unknown room: {room_id}', 'request_id': request_id})
                    return
                item = queue_injection(common.STATE_DIR, room_id, message=message, target=target, when=when, sender='user')
                if not item:
                    common.emit({'type': 'error', 'error': f'Failed to queue injection for room: {room_id}', 'request_id': request_id})
                    return
                append_event(common.STATE_DIR, room_id, {
                    'type': 'room_injection_requested',
                    'target': target,
                    'when': when,
                    'summary': message[:240],
                })
                if str(room.get('status') or 'active') == 'active' and len(list(room.get('participants') or [])) >= 2:
                    self._start_conversation_room_runner(
                        room_id,
                        str(room.get('title') or room_id),
                        list(room.get('participants') or []),
                        mode=self._room_runner_mode(room),
                    )
                common.emit({'type': 'status', 'message': f'Queued room injection for {room_id} target={target} when={when}: {message[:120]}', 'request_id': request_id})
                self.handle_refresh(request_id)
                return
            except Exception as e:
                common.emit({'type': 'error', 'error': f'Inject room failed: {e}', 'request_id': request_id})
                return
        return UNHANDLED

    def _cmd_delete_room(self, command: str, request_id: str | None):
        if command == '/delete-room' or command.startswith('/delete-room '):
            room_id = command[12:].strip() if command.startswith('/delete-room ') else ''
            try:
                if not room_id:
                    common.emit({'type': 'status', 'message': 'Usage: /delete-room <room-id>', 'request_id': request_id})
                    return
                from charon.agents.inter_agent_rooms import delete_room, load_room
                room = load_room(common.STATE_DIR, room_id)
                participant_sessions = list(room.get('participant_sessions') or []) if room else []
                if not participant_sessions and room:
                    participant_sessions = [p.get('session') for p in (room.get('participants') or []) if p.get('session')]
                terminated: list[str] = []
                for session_name in participant_sessions:
                    if session_name and _terminate_boat_session(str(session_name)):
                        terminated.append(str(session_name))
                        self._owned_boat_sessions.discard(str(session_name))
                if delete_room(common.STATE_DIR, room_id):
                    msg = f'Deleted room record: {room_id}'
                    if terminated:
                        msg += '\nClosed sessions: ' + ', '.join(terminated)
                    common.emit({'type': 'status', 'message': msg, 'request_id': request_id})
                    self.handle_refresh(request_id)
                else:
                    common.emit({'type': 'error', 'error': f'Could not delete room record: {room_id}', 'request_id': request_id})
                return
            except Exception as e:
                common.emit({'type': 'error', 'error': f'Delete room failed: {e}', 'request_id': request_id})
                return
        return UNHANDLED
