#!/usr/bin/env python3
"""Backend process for the OpenTUI chat view.

Runs the ConversationEngine directly (no daemon) and streams events
to the TypeScript frontend via newline-delimited JSON on stdout.

Protocol:
  Frontend → Backend (stdin):
    { "type": "chat", "message": "...", "request_id": "..." }
    { "type": "command", "command": "/setup ...", "request_id": "..." }
    { "type": "refresh", "request_id": "..." }
    { "type": "abort", "request_id": "..." }

  Backend → Frontend (stdout):
    { "type": "chat_delta", "text": "...", "request_id": "..." }
    { "type": "thinking_start", "request_id": "..." }
    { "type": "thinking_delta", "text": "...", "request_id": "..." }
    { "type": "tool_call", "tool_name": "...", "arguments": {...}, "request_id": "..." }
    { "type": "tool_result_delta", "tool_name": "...", "content": "...", "chunk": "...", "request_id": "..." }
    { "type": "tool_result", "tool_name": "...", "content": "...", "is_error": bool, "request_id": "..." }
    { "type": "turn_complete", "request_id": "..." }
    { "type": "chat_complete", "summary": "...", "request_id": "..." }
    { "type": "error", "error": "...", "request_id": "..." }
    { "type": "refresh", "payload": {...}, "request_id": "..." }
    { "type": "status", "message": "...", "request_id": "..." }
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import socket
import sys
import threading
import time
from pathlib import Path

# Suppress noisy library output that would corrupt the JSON protocol
os.environ['TRANSFORMERS_VERBOSITY'] = 'error'
os.environ['TOKENIZERS_PARALLELISM'] = 'false'
os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / 'apps' / 'core-daemon'))

from provider_bridge import (
    create_provider_and_model,
    resolve_provider_config,
    save_session_provider_config,
    load_session_provider_config,
)
from conversation_engine import ConversationEngine
from tools import ALL_TOOL_DEFS

STATE_DIR = ROOT / '.charon_state'


_emit_lock = threading.Lock()


def emit(event: dict):
    """Send a JSON event to the frontend. Thread-safe."""
    with _emit_lock:
        sys.stdout.write(json.dumps(event, ensure_ascii=False) + '\n')
        sys.stdout.flush()


def _load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def _boat_registry_path(session_name: str) -> Path:
    name = str(session_name or '').strip()
    if name and not name.startswith('boat-'):
        name = f'boat-{name}'
    return Path.home() / '.charon' / 'boats' / f'{name}.json'


def _boat_socket_for_session(session_name: str) -> str:
    try:
        reg = _load_json(_boat_registry_path(session_name), {})
        return str(reg.get('socket') or '').strip()
    except Exception:
        return ''


def _boat_send_input(session_name: str, text: str) -> bool:
    sock_path = _boat_socket_for_session(session_name)
    if not sock_path:
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(2.0)
            sock.connect(sock_path)
            payload = {
                'type': 'input',
                'data': base64.b64encode(text.encode('utf-8')).decode('ascii'),
            }
            sock.sendall((json.dumps(payload) + '\n').encode('utf-8'))
        return True
    except Exception:
        return False


def _wait_for_boat_socket(session_name: str, timeout: float = 15.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        sock = _boat_socket_for_session(session_name)
        if sock and Path(sock).exists():
            return True
        time.sleep(0.2)
    return False


def _clean_restored_text(text: str) -> str:
    """Strip leaked thinking tags from persisted assistant content."""
    if not isinstance(text, str) or not text:
        return text
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'(?im)^\s*</?think>\s*$', '', text)
    text = text.replace('<think>', '').replace('</think>', '')
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def _sanitize_saved_messages(messages: list[dict]) -> list[dict]:
    cleaned: list[dict] = []
    for msg in messages or []:
        if not isinstance(msg, dict):
            continue
        item = dict(msg)
        if isinstance(item.get('content'), str):
            item['content'] = _clean_restored_text(item['content'])
        cleaned.append(item)
    return cleaned


def _ui_settings_path() -> Path:
    return STATE_DIR / 'ui_settings.json'


def _load_ui_settings() -> dict:
    return _load_json(_ui_settings_path(), {}) or {}


def _save_ui_settings(settings: dict) -> None:
    path = _ui_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2, ensure_ascii=False))


def _projects_registry_path() -> Path:
    return STATE_DIR / 'projects.json'


def _load_project_registry() -> list[dict]:
    data = _load_json(_projects_registry_path(), [])
    return data if isinstance(data, list) else []


def _save_project_registry(projects: list[dict]) -> None:
    path = _projects_registry_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(projects, indent=2, ensure_ascii=False))


def _project_slug(text: str) -> str:
    import re
    slug = re.sub(r'[^a-zA-Z0-9._-]+', '-', str(text or '').strip()).strip('-_.').lower()
    return slug[:96] or 'project'


class ChatBackend:
    def __init__(self):
        self.engine: ConversationEngine | None = None
        self.chat_history: list[dict] = []
        self._engine_lock = threading.Lock()
        self._active_agent_id: str | None = None
        self.agent_mode: str = 'interactive'  # interactive, autonomous, delegating, idle
        self._notified_batches: set[str] = set()
        self._session_tasks: list[dict] = []
        self._pending_provider_switch: dict | None = None
        self._pending_libris_intake: dict | None = None
        self.visible_thoughts: bool = bool(_load_ui_settings().get('visible_thoughts', False))
        self._goal_inference_token_estimate: int = 0
        self._room_runners: set[str] = set()

    def _start_conversation_room_runner(self, room_id: str, topic: str, participants: list[dict]) -> None:
        rid = str(room_id or '').strip()
        if not rid or rid in self._room_runners:
            return
        self._room_runners.add(rid)

        def _run() -> None:
            try:
                from inter_agent_rooms import append_event, update_room
                teacher = next((p for p in participants if str(p.get('role') or '') == 'teacher'), participants[0] if participants else None)
                student = next((p for p in participants if str(p.get('role') or '') == 'student'), participants[1] if len(participants) > 1 else None)
                if not teacher or not student:
                    append_event(STATE_DIR, rid, {'type': 'runner_error', 'message': 'missing teacher/student participants'})
                    return
                teacher_session = str(teacher.get('session') or '')
                student_session = str(student.get('session') or '')
                if not teacher_session or not student_session:
                    append_event(STATE_DIR, rid, {'type': 'runner_error', 'message': 'missing participant sessions'})
                    return
                if not _wait_for_boat_socket(teacher_session) or not _wait_for_boat_socket(student_session):
                    append_event(STATE_DIR, rid, {'type': 'runner_error', 'message': 'boat session socket did not appear'})
                    return

                steps = [
                    ('teacher', teacher_session,
                     f"You are the teacher in a live two-agent conversation about advanced reinforcement learning. The student is another Hermes agent in room {rid}. Begin teaching with a concise but content-rich introduction to advanced RL, focusing on intuition, core ideas, and one motivating example. End by explicitly asking the student one concrete question.\n"),
                    ('student', student_session,
                     f"You are the student in a live two-agent conversation about advanced reinforcement learning in room {rid}. The teacher has begun teaching you. Ask one strong clarifying question about advanced RL, then briefly state what part still feels confusing.\n"),
                    ('teacher', teacher_session,
                     f"Continue the teacher/student conversation in room {rid}. Answer the student's likely confusion with a clearer explanation of advanced RL, including policy gradients, value estimation, exploration, or model-based RL as appropriate. End with a short exercise or checkpoint question.\n"),
                    ('student', student_session,
                     f"Continue as the student in room {rid}. Give a short summary of what you learned about advanced RL, answer the teacher's checkpoint if possible, and ask one final advanced follow-up question.\n"),
                ]
                append_event(STATE_DIR, rid, {'type': 'conversation_started', 'topic': topic, 'turns_planned': len(steps)})
                for idx, (speaker_role, session_name, prompt) in enumerate(steps, start=1):
                    ok = _boat_send_input(session_name, prompt + '\n')
                    append_event(STATE_DIR, rid, {
                        'type': 'conversation_turn_started' if ok else 'conversation_turn_failed',
                        'turn': idx,
                        'speaker_role': speaker_role,
                        'session': session_name,
                        'summary': prompt.splitlines()[0][:200],
                    })
                    update_room(STATE_DIR, rid, summary=f'{speaker_role} turn {idx}: {topic}')
                    emit({'type': 'refresh', 'payload': self._get_refresh_payload(), 'request_id': None})
                    time.sleep(8.0 if idx == 1 else 10.0)
                append_event(STATE_DIR, rid, {'type': 'conversation_script_complete', 'topic': topic})
                emit({'type': 'refresh', 'payload': self._get_refresh_payload(), 'request_id': None})
            finally:
                self._room_runners.discard(rid)

        threading.Thread(target=_run, daemon=True).start()

    def _create_hermes_room(
        self,
        *,
        kind: str,
        title: str,
        project: str,
        participants: list[dict],
        meta: dict,
        request_id: str | None,
        start_runner: bool,
    ) -> dict:
        from inter_agent_rooms import create_room, append_event, slugify, update_room
        import subprocess as _sp

        room = create_room(
            STATE_DIR,
            kind=kind,
            title=title,
            project=project,
            participants=participants,
            meta=meta,
        )
        append_event(STATE_DIR, room['id'], {
            'type': f'{kind}_requested',
            'provider': 'hermes',
            'count': len(participants),
            'topic': title,
        })

        boat = ROOT / 'tools' / 'charons-boat' / 'charons-boat'
        room_slug = slugify(title)
        launched = []
        bound_participants = []
        for idx, participant_seed in enumerate(participants):
            agent_name = f'{room_slug}-hermes-{idx+1}'
            role = str(participant_seed.get('role') or 'participant')
            role_prompt = (
                f'You are {participant_seed.get("name") or f"Hermes {idx+1}"} in room {room["id"]} about: {title}. '
                f'Your role is {role}. '
                'Charon will coordinate the conversation by sending follow-up turns. '
                'Stay concise, conversational, and ready to continue from incoming prompts.'
            )
            cmd = [str(boat), 'wrap', '--name', agent_name, '--', 'hermes']
            _sp.Popen(cmd, cwd=str(ROOT), stdout=_sp.DEVNULL, stderr=_sp.DEVNULL)
            session_name = f'boat-{agent_name}'
            launched.append(agent_name)
            participant = dict(participant_seed)
            participant['session'] = session_name
            bound_participants.append(participant)
            append_event(STATE_DIR, room['id'], {
                'type': 'participant_spawned',
                'participant': participant.get('name') or f'Hermes {idx+1}',
                'role': role,
                'session': session_name,
                'prompt': role_prompt[:200],
            })
        room = update_room(STATE_DIR, room['id'], participants=bound_participants, participant_sessions=[p.get('session') for p in bound_participants]) or room
        for participant in bound_participants:
            role_prompt = (
                f'You are {participant.get("name") or participant.get("id")}, role={participant.get("role")}, in room {room["id"]} on topic: {title}. '
                'Wait for Charon turn prompts and respond in plain conversational text.'
            )
            _wait_for_boat_socket(str(participant.get('session') or ''), timeout=15.0)
            _boat_send_input(str(participant.get('session') or ''), role_prompt + '\n')
        if start_runner and len(bound_participants) == 2:
            self._start_conversation_room_runner(room['id'], title, bound_participants)
        emit({'type': 'status', 'message': f'Created Hermes {kind} room: {room.get("title", title)} ({room["id"]})', 'request_id': request_id})
        emit({'type': 'status', 'message': 'Launched wrapped Hermes sessions: ' + ', '.join(launched), 'request_id': request_id})
        emit({'type': 'refresh', 'payload': self._get_refresh_payload(), 'request_id': request_id})
        return room

    def _outcomes_path(self, session_id: str | None = None) -> Path | None:
        sid = session_id or self._active_agent_id
        if not sid:
            return None
        return STATE_DIR / 'conversations' / f'{sid}.outcomes.json'

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

    def _start_outcome_for_message(self, message: str) -> None:
        parsed = self._parse_intent(message)
        if parsed:
            kind, obj = parsed
            title = self._make_outcome_title(kind, obj, 'active')
        else:
            kind, obj = 'working', 'task'
            try:
                from task_summarizer import summarize_instruction_fast
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
                from task_summarizer import summarize_instruction_rich, summarize_instruction_fast
                try:
                    from model_registry import get_shade_provider_and_model, load_registry
                    reg = load_registry(STATE_DIR)
                    provider, model, ready = get_shade_provider_and_model(STATE_DIR, reg=reg)
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
                    emit({'type': 'refresh', 'payload': {'session_info': self._get_session_info()}, 'request_id': request_id})
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

    def _ensure_engine(self) -> tuple[ConversationEngine | None, str]:
        """Create or return the conversation engine.
        Returns (engine, error_message).
        """
        # Register approval callback so tool calls can ask for permission
        try:
            from tools import set_approval_callback
            def _emit_approval(tool_name, params_summary, risk, reason):
                emit({
                    'type': 'approval_request',
                    'tool': tool_name,
                    'params': params_summary,
                    'risk': risk,
                    'reason': reason,
                })
            set_approval_callback(_emit_approval)
        except Exception:
            pass

        if self.engine is not None:
            return self.engine, ''

        self._ensure_session_id()

        try:
            provider, model, ready = create_provider_and_model(STATE_DIR, self._active_agent_id)
        except Exception as e:
            return None, f'Provider setup failed: {e}'

        if not ready:
            return None, 'No provider configured. Use /setup provider <name> to configure.'

        project = str(ROOT)
        onboarding = _load_json(STATE_DIR / 'onboarding.json', {})
        configured_project = str(onboarding.get('project') or '').strip()
        if configured_project:
            project = configured_project

        # Build enriched system prompt with memory, goals, coordination.
        # Fresh launches should stay fresh: do NOT silently bind to an existing
        # persistent agent unless the user explicitly requested one via
        # CHARON_AGENT / --agent.
        system_prompt = ''
        try:
            from system_prompt_builder import build_system_prompt as build_layered_prompt
            agent_info = {'id': '', 'name': 'Charon', 'role': 'charon', 'goal': '', 'project': project}
            requested_agent = os.environ.get('CHARON_AGENT', '').strip()
            self._bound_agent_id = None
            if requested_agent:
                try:
                    from agent_lifecycle import list_agents
                    for a in list_agents():
                        if a.get('id') == requested_agent or a.get('name') == requested_agent:
                            agent_info = a
                            self._bound_agent_id = a.get('id') or None
                            break
                except Exception:
                    pass
            task_info = {'project': project}
            system_prompt = build_layered_prompt(
                state_dir=STATE_DIR, agent=agent_info, task=task_info,
            )
        except Exception as e:
            import traceback
            sys.stderr.write(f'System prompt builder failed: {e}\n')
            traceback.print_exc(file=sys.stderr)

        # Session ID is created before provider resolution so provider selection can be session-scoped.
        self._ensure_session_id()

        # Use an explicitly bound persistent agent only when requested.
        # Otherwise the engine runs as this fresh session's own identity.
        engine_agent_id = getattr(self, '_bound_agent_id', None) or self._active_agent_id
        self.engine = ConversationEngine(
            provider=provider,
            model=model,
            project_root=project,
            agent_id=engine_agent_id,
            agent_name='Charon',
            system_prompt=system_prompt,
            state_dir=STATE_DIR,
            max_tokens=32768,
        )

        # Apply provider handoff transfer if present.
        try:
            from context_transfer import load_pending_transfer, apply_transfer_to_engine, clear_pending_transfer, record_transfer_event
            pending_transfer = load_pending_transfer(STATE_DIR)
            if pending_transfer:
                apply_transfer_to_engine(self.engine, pending_transfer)
                clear_pending_transfer(STATE_DIR)
                record_transfer_event(STATE_DIR, {
                    'ts': pending_transfer.get('created_at', ''),
                    'type': 'transfer_applied',
                    'bundle_id': pending_transfer.get('id', ''),
                    'source_provider': pending_transfer.get('source', {}).get('provider', ''),
                    'target_provider': pending_transfer.get('target', {}).get('provider', ''),
                    'session_id': pending_transfer.get('source', {}).get('session_id', ''),
                })
                emit({
                    'type': 'status',
                    'message': f'Applied context transfer {pending_transfer.get("id", "")}. Session continued on new provider.',
                })
        except Exception:
            pass

        # Session registration deferred until first message is sent
        # (don't clutter the session list with empty sessions)

        # Only resume when explicitly requested via --resume flag or /resume command
        if self._active_agent_id and os.environ.get('CHARON_RESUME', '').strip():
            try:
                from conversation_store import load_conversation, dict_to_message
                saved = _sanitize_saved_messages(load_conversation(STATE_DIR, self._active_agent_id))
                if saved:
                    self.engine.messages = [dict_to_message(m) for m in saved]
                    self._load_tasks_from_ledger(self._active_agent_id)
                    emit({
                        'type': 'conversation_restored',
                        'messages': saved,
                        'count': len(saved),
                        'agent_id': self._active_agent_id,
                    })
            except Exception:
                pass

        return self.engine, ''

    def _get_refresh_payload(self) -> dict:
        onboarding = _load_json(STATE_DIR / 'onboarding.json', {})
        session_cfg = self._session_provider_state()
        provider = str(session_cfg.get('provider_raw') or onboarding.get('provider') or '').strip()
        model = str(session_cfg.get('model_id') or onboarding.get('model') or onboarding.get('provider_model') or '').strip()
        complete = bool(session_cfg.get('ready') or onboarding.get('complete'))
        if self.engine is not None:
            provider = str(getattr(self.engine, 'provider_name', '') or provider).strip()
            model = str(getattr(getattr(self.engine, 'model', None), 'model_id', '') or model).strip()
            complete = True

        # Load agents
        agents = []
        try:
            from agent_lifecycle import list_agents
            for a in list_agents():
                agent_id = a.get('id', '')
                # Load recent actions from inbox
                recent_actions = []
                inbox_path = STATE_DIR / 'agents' / agent_id / 'inbox.jsonl'
                if inbox_path.exists():
                    try:
                        inbox_lines = inbox_path.read_text().splitlines()[-8:]
                        for line in inbox_lines:
                            try:
                                rec = json.loads(line)
                                evt = rec.get('event_type', '')
                                payload = rec.get('payload', {})
                                summary = payload.get('summary', payload.get('instruction', ''))
                                if summary:
                                    recent_actions.append(f"{evt}: {str(summary)[:60]}")
                                elif evt:
                                    recent_actions.append(evt)
                            except Exception:
                                pass
                    except Exception:
                        pass

                # Load working memory for goal info
                memory_path = STATE_DIR / 'agents' / agent_id / 'working_memory.json'
                memory = _load_json(memory_path, {})
                last_summary = memory.get('last_task_summary', '')
                notes = memory.get('notes', [])

                agents.append({
                    'id': agent_id,
                    'name': a.get('name', ''),
                    'status': a.get('status', 'idle'),
                    'role': a.get('role', 'charon'),
                    'goal': a.get('goal', ''),
                    'specialization': a.get('specialization', ''),
                    'project': a.get('project', ''),
                    'mode': a.get('mode', 'persistent'),
                    'visibility': a.get('visibility', 'user'),
                    'last_active': a.get('last_active', ''),
                    'parent_agent_id': a.get('parent_agent_id', ''),
                    'tmux_session': a.get('tmux_session', ''),
                    'recent_actions': recent_actions,
                    'last_summary': str(last_summary)[:120] if last_summary else '',
                    'memory_notes': len(notes),
                })

                # Add ledger entries for rear-view
                try:
                    from task_ledger import get_agent_ledger
                    ledger = get_agent_ledger(STATE_DIR, agent_id, limit=10)
                    agents[-1]['ledger'] = ledger
                except Exception:
                    agents[-1]['ledger'] = []

                # Add shade usage stats
                try:
                    from shade_stats import get_agent_shade_stats
                    agents[-1]['shade_stats'] = get_agent_shade_stats(STATE_DIR, agent_id)
                except Exception:
                    agents[-1]['shade_stats'] = {}
        except Exception:
            pass

        # Derive projects from agents
        project_map: dict[str, dict] = {}
        for a in agents:
            proj = a.get('project', '').strip()
            if not proj:
                continue
            name = proj.split('/')[-1] or proj
            if name not in project_map:
                project_map[name] = {
                    'name': name,
                    'path': proj,
                    'agents': [],
                    'agent_details': [],
                    'last_active': '',
                    'started': '',
                }
            project_map[name]['agents'].append(a.get('name', a.get('id', '')))
            project_map[name]['agent_details'].append({
                'name': a.get('name', ''),
                'id': a.get('id', ''),
                'status': a.get('status', 'idle'),
                'role': a.get('role', 'charon'),
            })
            ts = a.get('last_active', '')
            if ts > project_map[name].get('last_active', ''):
                project_map[name]['last_active'] = ts
            created = a.get('created_at', '')
            if not project_map[name]['started'] or (created and created < project_map[name]['started']):
                project_map[name]['started'] = created
        projects = list(project_map.values())

        # Merge explicit project objects from registry
        try:
            registry = _load_project_registry()
            by_name = {p.get('name', ''): p for p in projects}
            for entry in registry:
                if not isinstance(entry, dict):
                    continue
                name = str(entry.get('name') or '').strip()
                path = str(entry.get('path') or '').strip()
                if not name:
                    continue
                proj = by_name.get(name)
                if proj is None:
                    proj = {
                        'name': name,
                        'path': path,
                        'agents': [],
                        'agent_details': [],
                        'last_active': '',
                        'started': str(entry.get('created_at') or ''),
                        'active': False,
                        'explicit': True,
                        'description': str(entry.get('description') or ''),
                    }
                    projects.append(proj)
                    by_name[name] = proj
                else:
                    proj['explicit'] = True
                    if path and not proj.get('path'):
                        proj['path'] = path
                    if entry.get('description') and not proj.get('description'):
                        proj['description'] = str(entry.get('description') or '')
            onboarding_project = str(onboarding.get('project') or '').strip()
            for p in projects:
                p['active'] = any(ad.get('status') == 'running' for ad in p.get('agent_details', []))
                p['selected'] = bool(onboarding_project and str(p.get('path') or '').strip() == onboarding_project)
        except Exception:
            for p in projects:
                p['active'] = any(ad.get('status') == 'running' for ad in p.get('agent_details', []))

        # Derive sessions — discover ALL tmux sessions, match to agents where possible
        sessions = []
        live_tmux: dict[str, dict] = {}
        claimed_tmux: set[str] = set()
        try:
            from tmux_capture import list_sessions as tmux_list
            for ts in tmux_list():
                live_tmux[ts.name] = {
                    'name': ts.name,
                    'windows': ts.windows,
                    'attached': ts.attached,
                }
        except Exception:
            pass

        # First: add Charon agents that have tmux sessions
        for a in agents:
            tmux_name = a.get('tmux_session', '')
            has_tmux = tmux_name in live_tmux
            if tmux_name:
                claimed_tmux.add(tmux_name)
            sessions.append({
                'id': f"session-{a['id']}",
                'agentId': a['id'],
                'agentName': a['name'],
                'sessionLabel': a['name'],
                'status': a['status'] if has_tmux else 'stopped',
                'project': a['project'].split('/')[-1] if a.get('project') else '',
                'location': 'local',
                'lastActivity': a.get('last_active', ''),
                'tmuxSession': tmux_name,
                'tmux_session': tmux_name,
                'hasTmux': has_tmux,
                'role': a.get('role', 'charon'),
                'source': 'charon',
            })

        # Boat-wrapped sessions (fast path for Hermes/Pi demo sessions)
        try:
            boat_dir = Path.home() / '.charon' / 'boats'
            if boat_dir.exists():
                for reg_file in sorted(boat_dir.glob('*.json')):
                    try:
                        reg = json.loads(reg_file.read_text())
                    except Exception:
                        continue
                    tmux_name = str(reg.get('session') or '').strip()
                    if not tmux_name:
                        continue
                    transport = str(reg.get('transport') or '').strip().lower()
                    reg_status = str(reg.get('status') or 'idle').strip() or 'idle'
                    has_tmux = tmux_name in live_tmux
                    sock_path = Path(str(reg.get('socket') or ''))
                    if transport in ('pty', 'charon'):
                        if reg_status not in ('running', 'starting') or not sock_path.exists():
                            continue
                    elif not has_tmux:
                        continue
                    if has_tmux and tmux_name in claimed_tmux:
                        continue
                    raw_name = str(reg.get('name') or tmux_name).strip() or tmux_name
                    command = str(reg.get('command') or '').strip()
                    base = command.split()[0] if command else raw_name
                    agent_target = Path(base).name.lower()
                    if agent_target.startswith('boat-'):
                        agent_target = raw_name.lower()
                    if transport == 'charon' or 'charon' in agent_target:
                        agent_name = 'Charon'
                        process_target = 'charon'
                    elif 'hermes' in agent_target:
                        agent_name = 'Hermes'
                        process_target = 'hermes'
                    elif agent_target == 'pi':
                        agent_name = 'Pi'
                        process_target = 'pi'
                    else:
                        agent_name = raw_name.split('-')[0].capitalize() or 'Agent'
                        process_target = agent_target or 'external'
                    if has_tmux:
                        claimed_tmux.add(tmux_name)
                    session_label = raw_name
                    if transport == 'charon' and raw_name and not raw_name.startswith('charon'):
                        session_label = f'charon-{raw_name}'
                    sessions.append({
                        'id': f'boat-{tmux_name}',
                        'agentId': f'boat-{tmux_name}',
                        'agentName': agent_name,
                        'sessionLabel': session_label,
                        'status': 'running' if has_tmux else reg_status,
                        'project': '',
                        'location': 'local',
                        'lastActivity': str(reg.get('created') or ''),
                        'tmuxSession': tmux_name,
                        'tmux_session': tmux_name,
                        'hasTmux': has_tmux,
                        'role': 'external',
                        'source': 'boat',
                        'processTarget': process_target,
                        'hasBoat': True,
                        'supportsCharonBoat': True,
                        'boatSessionId': raw_name,
                        'command': command[:80],
                        'transport': transport or ('pty' if sock_path.exists() else ''),
                        'socket': str(sock_path) if sock_path else '',
                    })
        except Exception:
            pass

        # Second: discover ALL running agent processes (pi, hermes, claude, etc.)
        # and match them to tmux sessions where possible
        detected_agents: list[dict] = []
        tmux_pane_map: dict[int, str] = {}  # pid → tmux session name
        try:
            import subprocess as _sp
            # Get tmux pane PIDs and commands to match processes to sessions
            result = _sp.run(
                ['tmux', 'list-panes', '-a', '-F', '#{session_name} #{pane_pid} #{pane_current_command}'],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0:
                pane_pids: list[tuple[int, str]] = []
                for line in result.stdout.strip().splitlines():
                    parts = line.split(None, 2)
                    if len(parts) >= 2:
                        try:
                            pane_pids.append((int(parts[1]), parts[0]))
                            tmux_pane_map[int(parts[1])] = parts[0]
                        except ValueError:
                            pass
                # Map child and grandchild PIDs to their pane's tmux session
                for pane_pid, sess_name in pane_pids:
                    try:
                        cr = _sp.run(['pgrep', '-P', str(pane_pid)], capture_output=True, text=True, timeout=3)
                        if cr.returncode == 0:
                            for cl in cr.stdout.strip().splitlines():
                                try:
                                    cpid = int(cl.strip())
                                    tmux_pane_map[cpid] = sess_name
                                    # Grandchildren
                                    gr = _sp.run(['pgrep', '-P', str(cpid)], capture_output=True, text=True, timeout=3)
                                    if gr.returncode == 0:
                                        for gl in gr.stdout.strip().splitlines():
                                            try:
                                                tmux_pane_map[int(gl.strip())] = sess_name
                                            except ValueError:
                                                pass
                                except ValueError:
                                    pass
                    except Exception:
                        pass
        except Exception:
            pass

        try:
            sys.path.insert(0, str(ROOT / 'apps' / 'tui'))
            from process_inspector import detect_agent_processes
            for proc in detect_agent_processes():
                # Skip if this PID belongs to a Charon agent tmux we already listed
                tmux_session = tmux_pane_map.get(proc.pid, '')
                if tmux_session in claimed_tmux:
                    continue
                agent_name = f"{proc.target}"
                if tmux_session:
                    agent_name = f"{proc.target} ({tmux_session})"
                    claimed_tmux.add(tmux_session)
                detected_agents.append({
                    'id': f"proc-{proc.pid}",
                    'agentId': f"proc-{proc.pid}",
                    'agentName': agent_name,
                    'status': 'running',
                    'project': '',
                    'location': 'local',
                    'lastActivity': '',
                    'tmuxSession': tmux_session,
                    'tmux_session': tmux_session,
                    'hasTmux': bool(tmux_session),
                    'role': 'external',
                    'source': 'detected',
                    'processTarget': proc.target,
                    'hasBoat': bool(getattr(proc, 'has_boat', False)),
                    'supportsCharonBoat': bool(getattr(proc, 'has_boat', False)),
                    'pid': proc.pid,
                    'command': proc.args[:80],
                })
        except Exception:
            pass

        sessions.extend(detected_agents)

        # Also add Charon agents as virtual sessions (viewable in grid as chat history)
        for a in agents:
            if a.get('role') != 'charon':
                continue
            aid = a['id']
            if any(s.get('agentId') == aid for s in sessions):
                continue  # already has a session
            sessions.append({
                'id': f"virtual-{aid}",
                'agentId': aid,
                'agentName': a['name'],
                'status': a['status'],
                'project': a['project'].split('/')[-1] if a.get('project') else '',
                'location': 'local',
                'lastActivity': a.get('last_active', ''),
                'tmuxSession': '',
                'tmux_session': '',
                'hasTmux': False,
                'role': 'charon',
                'source': 'virtual',
                'isVirtual': True,
            })

        # Third: add any remaining tmux sessions not claimed by agents or detected processes
        for tmux_name, tmux_info in live_tmux.items():
            if tmux_name in claimed_tmux:
                continue
            sessions.append({
                'id': f"tmux-{tmux_name}",
                'agentId': f"tmux-{tmux_name}",
                'agentName': f"tmux:{tmux_name}",
                'status': 'running',
                'project': '',
                'location': 'local',
                'lastActivity': '',
                'tmuxSession': tmux_name,
                'tmux_session': tmux_name,
                'hasTmux': True,
                'role': 'external',
                'source': 'tmux',
            })

        # Recent activity from run log
        activity = []
        run_log = STATE_DIR / 'run.log'
        if run_log.exists():
            try:
                lines = run_log.read_text().splitlines()[-15:]
                for line in lines:
                    try:
                        rec = json.loads(line)
                        evt = rec.get('event', '?')
                        tid = rec.get('task_id', '')
                        reason = rec.get('reason', '')
                        activity.append(f"{evt}: {tid} {reason}".strip())
                    except Exception:
                        pass
            except Exception:
                pass

        transfer_events = []
        try:
            from context_transfer import list_transfer_events
            transfer_events = list_transfer_events(STATE_DIR, limit=12)
        except Exception:
            transfer_events = []

        inter_agent_rooms = []
        try:
            from inter_agent_rooms import list_rooms, list_events
            for room in list_rooms(STATE_DIR, limit=40):
                rid = str(room.get('id') or '')
                if not rid:
                    continue
                item = dict(room)
                item['events'] = list_events(STATE_DIR, rid, limit=80)
                inter_agent_rooms.append(item)
        except Exception:
            inter_agent_rooms = []

        # Map Libris operations into the shared F4 room list so F4 can render
        # them with a graph-first layout later.
        try:
            from libris_runtime import rebuild_project_index, get_libris_swarm_state
            project_root = Path(str(onboarding.get('project') or str(ROOT)).strip() or str(ROOT))
            idx = rebuild_project_index(STATE_DIR, project_root)
            for op in idx.get('operations') or []:
                op_id = str(op.get('operation_id') or '').strip()
                if not op_id:
                    continue
                swarm = get_libris_swarm_state(STATE_DIR, project_root, op_id)
                if not swarm:
                    continue
                inter_agent_rooms.append({
                    'id': f'libris-{op_id}',
                    'kind': 'libris',
                    'title': str(op.get('prompt') or op_id)[:120],
                    'project': str(project_root),
                    'status': str(swarm.get('status') or op.get('status') or 'active'),
                    'created_at': str(op.get('created_at') or ''),
                    'updated_at': str(op.get('updated_at') or ''),
                    'last_activity': str(op.get('updated_at') or op.get('created_at') or ''),
                    'participants': [
                        {
                            'id': str(n.get('agent_id') or ''),
                            'name': str(n.get('name') or ''),
                            'role': str(n.get('role') or ''),
                            'status': str(n.get('status') or ''),
                        }
                        for n in (swarm.get('nodes') or [])
                    ],
                    'summary': str(swarm.get('prompt') or '')[:200],
                    'operation_id': op_id,
                    'nodes': swarm.get('nodes') or [],
                    'edges': swarm.get('edges') or [],
                    'topics': swarm.get('topics') or [],
                    'budget_status': swarm.get('budget_status') or {},
                    'promising_sources': swarm.get('promising_sources') or [],
                    'final_selection_markdown': swarm.get('final_selection_markdown') or '',
                    'events': swarm.get('events_tail') or [],
                })
        except Exception:
            pass

        session_lookup = {}
        for s in sessions:
            sid = str(s.get('tmuxSession') or s.get('tmux_session') or s.get('id') or '').strip()
            if sid:
                session_lookup[sid] = s
            raw_boat = str(s.get('boatSessionId') or '').strip()
            if raw_boat:
                session_lookup[raw_boat if raw_boat.startswith('boat-') else f'boat-{raw_boat}'] = s
        for room in inter_agent_rooms:
            participant_sessions = room.get('participant_sessions') or []
            participants = room.get('participants') or []
            if not participant_sessions and participants:
                participant_sessions = [p.get('session') for p in participants if p.get('session')]
                room['participant_sessions'] = participant_sessions
            room['session_details'] = [session_lookup[s] for s in participant_sessions if s in session_lookup]

        payload = {
            'onboarding': {
                'complete': complete,
                'provider': provider,
                'model': model,
                'step': onboarding.get('step', 'provider-mode'),
                'project': str(onboarding.get('project') or '').strip(),
            },
            'agents': agents,
            'projects': projects,
            'sessions': sessions,
            'activity': activity,
            'transfer_events': transfer_events,
            'inter_agent_rooms': inter_agent_rooms,
            'chat_history': self.chat_history[-200:],
            'engine_ready': self.engine is not None,
            'message_count': len(self.engine.messages) if self.engine else 0,
            'agent_mode': self.agent_mode,
            'session_info': self._get_session_info(),
            'batch_progress': self._get_batch_progress(),
        }

        # Include recent consolidation traces for dashboard
        try:
            from consolidation import list_traces
            payload['consolidation_traces'] = list_traces(STATE_DIR, limit=5)
        except Exception:
            payload['consolidation_traces'] = []

        return payload

    def handle_refresh(self, request_id: str | None):
        payload = self._get_refresh_payload()
        payload['session_id'] = self._active_agent_id or ''
        payload['visible_thoughts'] = self.visible_thoughts
        payload['thoughts_supported'] = self._thoughts_supported()

        # Check for incoming steering messages from other Charon instances
        if self._active_agent_id:
            try:
                from session_registry import read_steers
                steers = read_steers(STATE_DIR, self._active_agent_id)
                for steer in steers:
                    msg = steer.get('message', '')
                    if msg:
                        emit({
                            'type': 'status',
                            'message': f'📡 Message from another Charon: {msg[:80]}',
                            'request_id': request_id,
                        })
                        # Submit as a regular chat message so the agent responds
                        import threading
                        threading.Thread(
                            target=self.handle_chat,
                            args=(f'[Steering from another Charon session] {msg}', request_id),
                            daemon=True,
                        ).start()
            except Exception:
                pass

        # Heartbeat + include live Charon sessions
        try:
            from session_registry import heartbeat, list_live_sessions
            if self._active_agent_id:
                heartbeat(STATE_DIR, self._active_agent_id)
            live = list_live_sessions(STATE_DIR)
            # Add live sessions as agents (if not already present)
            for ls in live:
                sid = ls.get('session_id', '')
                if sid == self._active_agent_id:
                    continue  # skip self
                if not ls.get('alive', False):
                    continue
                # Add as a session entry
                payload.setdefault('sessions', []).append({
                    'id': f'live-{sid}',
                    'agentId': f'live-{sid}',
                    'agentName': f'charon ({sid.split("-")[-1][:6]})',
                    'status': 'running',
                    'project': '',
                    'location': 'local',
                    'lastActivity': '',
                    'tmuxSession': '',
                    'tmux_session': '',
                    'hasTmux': False,
                    'role': 'charon',
                    'source': 'live',
                    'isLive': True,
                    'liveSessionId': sid,
                })
        except Exception:
            pass

        emit({
            'type': 'refresh',
            'request_id': request_id,
            'payload': payload,
        })

    def _current_provider_name(self) -> str:
        try:
            from provider_bridge import resolve_provider_config
            cfg = resolve_provider_config(STATE_DIR)
            return str(cfg.get('provider_name') or cfg.get('provider_raw') or '').strip().lower()
        except Exception:
            onboarding = _load_json(STATE_DIR / 'onboarding.json', {})
            return str(onboarding.get('provider') or '').strip().lower()

    def _thoughts_supported(self) -> bool:
        name = self._current_provider_name()
        # Anthropic has native thinking. Local/OpenAI-compatible providers may
        # also surface thoughts via reasoning fields or inline <think> blocks.
        return name in {'anthropic', 'openai', 'local'}

    def _libris_project_root(self) -> str:
        onboarding = _load_json(STATE_DIR / 'onboarding.json', {})
        configured_project = str(onboarding.get('project') or '').strip()
        return configured_project or str(ROOT)

    def _libris_goal_options(self, prompt: str) -> list[str]:
        lower = (prompt or '').lower()
        if 'computer vision' in lower or 'vision' in lower:
            return [
                'Identify the most practically important new techniques worth implementing or prototyping.',
                'Focus on the highest-novelty research directions from the last few months, even if speculative.',
                'Prioritize methods with strong evidence, benchmarks, code availability, and likely near-term impact.',
            ]
        if 'reinforcement learning' in lower or 'rl' in lower:
            return [
                'Prioritize techniques most likely to improve our current RL work in practice.',
                'Focus on the most novel and strategically important RL directions from recent months.',
                'Prefer methods with strong empirical evidence, code, and realistic implementation paths.',
            ]
        return [
            'Prioritize practical, high-impact techniques we could plausibly act on.',
            'Focus on novelty and strategic importance, even if implementation is less immediate.',
            'Prefer evidence-backed methods with code, benchmarks, and clear adoption signals.',
        ]

    def _libris_extract_stop(self, text: str) -> str:
        t = (text or '').strip()
        m = re.search(r'(stop after .+|run for .+|for \d+ (?:hours?|days?|weeks?)|until i stop you|until stopped|cap(?: it)? at .+ tokens?|under .+ tokens?)', t, re.I)
        return m.group(1).strip() if m else ''

    def _libris_parse_budget(self, stop_condition: str) -> dict:
        t = (stop_condition or '').lower().strip()
        out: dict = {}
        if not t:
            return out
        m = re.search(r'(\d+)\s*(hour|hours|day|days|week|weeks)', t)
        if m:
            n = int(m.group(1))
            unit = m.group(2)
            hours = n
            if 'day' in unit:
                hours = n * 24
            elif 'week' in unit:
                hours = n * 24 * 7
            out['max_wall_hours'] = hours
        m = re.search(r'(\d+(?:[\.,]\d+)?)\s*(m|million)?\s*tokens?', t)
        if m:
            num = float(m.group(1).replace(',', ''))
            if m.group(2) == 'm':
                num *= 1_000_000
            out['max_total_tokens'] = int(num)
        m = re.search(r'\$\s*(\d+(?:\.\d+)?)', t)
        if m:
            out['max_total_cost_usd'] = float(m.group(1))
        return out

    def _libris_has_clear_goal(self, text: str) -> bool:
        t = (text or '').lower()
        patterns = [
            r'priorit', r'focus on', r'looking for', r'goal is', r'what we care about',
            r'practical', r'novel', r'implementation', r'actionable', r'benchmark',
        ]
        return any(re.search(p, t) for p in patterns)

    def _emit_libris_intake(self, request_id: str | None) -> None:
        pending = self._pending_libris_intake or {}
        options = pending.get('goal_options') or []
        prompt = pending.get('prompt') or ''
        stop = pending.get('stop_condition') or '(none set)'
        lines = [
            'Libris intake: before starting, I want to make sure we have a clear research standard.',
            '',
            f'Research prompt: {prompt}',
            f'Stop condition: {stop}',
            '',
            'Suggested research goals:',
        ]
        for i, opt in enumerate(options, 1):
            lines.append(f'{i}. {opt}')
        lines.extend([
            '',
            'Reply with one of:',
            '/libris use 1      choose a suggested goal',
            '/libris use 2',
            '/libris use 3',
            '/libris custom <goal>',
            '/libris stop <condition>',
            '/libris go         start with the current selections',
        ])
        emit({'type': 'status', 'message': '\n'.join(lines), 'request_id': request_id})

    def _start_libris_from_pending(self, request_id: str | None) -> None:
        pending = self._pending_libris_intake or {}
        prompt = str(pending.get('prompt') or '').strip()
        if not prompt:
            emit({'type': 'error', 'error': 'No pending Libris intake.', 'request_id': request_id})
            return
        selected_goal = str(pending.get('selected_goal') or '').strip()
        stop_condition = str(pending.get('stop_condition') or '').strip()
        budget = self._libris_parse_budget(stop_condition)
        full_prompt = prompt
        if selected_goal:
            full_prompt += f'\n\nResearch goal standard: {selected_goal}'
        if stop_condition:
            full_prompt += f'\n\nStop condition: {stop_condition}'
        try:
            from libris_agents import start_autonomous_libris_research
            res = start_autonomous_libris_research(
                STATE_DIR,
                Path(self._libris_project_root()),
                prompt=full_prompt,
                parent_agent_id=self._active_agent_id or '',
                budget=budget,
            )
            op = res.get('operation') or {}
            coord = res.get('coordinator') or {}
            self._pending_libris_intake = None
            emit({
                'type': 'status',
                'message': (
                    f'Libris research started.\n'
                    f'Operation: {op.get("operation_id")}\n'
                    f'Coordinator: {coord.get("id")} ({coord.get("name")})\n'
                    f'Budget: {budget or "(none set)"}\n'
                    f'Use /libris status {op.get("operation_id")} to inspect swarm state.'
                ),
                'request_id': request_id,
            })
            emit({'type': 'refresh', 'payload': {'session_info': self._get_session_info()}, 'request_id': request_id})
            emit({'type': 'libris_started', 'operation_id': op.get('operation_id'), 'request_id': request_id})
        except Exception as e:
            emit({'type': 'error', 'error': f'Failed to start Libris research: {e}', 'request_id': request_id})

    def _command_catalog(self) -> list[dict]:
        """Return available commands with descriptions."""
        return [
            {'cmd': '/help', 'desc': 'Show available commands'},
            {'cmd': '/setup', 'desc': 'Show setup commands'},
            {'cmd': '/setup status', 'desc': 'Show current configuration'},
            {'cmd': '/setup reset', 'desc': 'Reset all configuration'},
            {'cmd': '/setup provider lmstudio', 'desc': 'Use local LM Studio'},
            {'cmd': '/setup provider claude-code', 'desc': 'Use Anthropic Claude (OAuth)'},
            {'cmd': '/setup provider codex', 'desc': 'Use OpenAI Codex (OAuth)'},
            {'cmd': '/setup provider api', 'desc': 'Use custom API endpoint'},
            {'cmd': '/setup model <name>', 'desc': 'Set model name'},
            {'cmd': '/setup api-key <key>', 'desc': 'Set API key directly'},
            {'cmd': '/setup project <path>', 'desc': 'Set project directory'},
            {'cmd': '/setup complete', 'desc': 'Finish setup'},
            {'cmd': '/setup no-provider', 'desc': 'Skip LLM setup (heuristic only)'},
            {'cmd': '/model', 'desc': 'Show current model'},
            {'cmd': '/reset', 'desc': 'Clear conversation'},
            {'cmd': '/dashboard', 'desc': 'Switch to dashboard (F2)'},
            {'cmd': '/sessions', 'desc': 'Switch to sessions (F3)'},
            {'cmd': '/chat', 'desc': 'Switch to chat (F1)'},
            {'cmd': '/hermes', 'desc': 'Launch a wrapped Hermes session in the background'},
            {'cmd': '/pi', 'desc': 'Launch a wrapped pi session in the background'},
            {'cmd': '/conversation hermes teacher student <topic>', 'desc': 'Start a teacher/student Hermes conversation room'},
            {'cmd': '/conversation hermes 2 <topic>', 'desc': 'Start a 2-agent Hermes conversation room'},
            {'cmd': '/team hermes <count> <topic>', 'desc': 'Create a Hermes discussion room/team'},
            {'cmd': '/devteam hermes <count> <goal>', 'desc': 'Create a Hermes developer team room'},
            {'cmd': '/libris <prompt>', 'desc': 'Start a Libris research intake for a broad research prompt'},
            {'cmd': '/libris status <operation_id>', 'desc': 'Inspect Libris swarm state for an operation'},
        ]

    def _get_suggestions(self, prefix: str) -> list[dict]:
        """Get matching commands for a prefix."""
        prefix = prefix.strip().lower()
        catalog = self._command_catalog()
        if not prefix or prefix == '/':
            return catalog[:10]

        starts = [c for c in catalog if c['cmd'].lower().startswith(prefix)]
        if starts:
            return starts[:10]

        token_matches: list[dict] = []
        needle = prefix.lstrip('/')
        for item in catalog:
            cmd = item['cmd'].lower()
            parts = [p for p in cmd.replace('/', ' ').replace('<', ' ').replace('>', ' ').split() if p]
            if any(part.startswith(needle) for part in parts):
                token_matches.append(item)
        if token_matches:
            return token_matches[:10]

        return []

    def _ensure_session_id(self) -> str:
        if not self._active_agent_id:
            import time, hashlib
            raw = f'{time.time()}-{os.getpid()}'
            short = hashlib.md5(raw.encode()).hexdigest()[:6]
            self._active_agent_id = f'session-{short}-{int(time.time())}'
        return self._active_agent_id

    def _session_provider_state(self) -> dict:
        try:
            session_id = self._active_agent_id or None
            return resolve_provider_config(STATE_DIR, session_id=session_id)
        except Exception:
            onboarding = _load_json(STATE_DIR / 'onboarding.json', {})
            return {
                'provider_raw': str(onboarding.get('provider') or '').strip(),
                'model_id': str(onboarding.get('model') or onboarding.get('provider_model') or '').strip(),
                'ready': bool(onboarding.get('complete')),
            }

    def _current_provider_name(self) -> str:
        if self.engine is not None:
            return str(getattr(self.engine, 'provider_name', '') or '').strip()
        state = self._session_provider_state()
        return str(state.get('provider_raw') or state.get('provider_name') or '').strip()

    def _has_transferable_context(self) -> bool:
        try:
            from context_transfer import session_has_transferable_context
            return bool(self.engine and session_has_transferable_context(self.engine.messages))
        except Exception:
            return bool(self.engine and len(self.engine.messages) >= 4)

    def _prompt_provider_switch(self, target_provider: str, request_id: str | None, source: str):
        current_provider = self._current_provider_name() or 'current provider'
        self._pending_provider_switch = {
            'target_provider': target_provider,
            'source_provider': current_provider,
            'source': source,
        }
        emit({
            'type': 'status',
            'message': (
                f'Switching from {current_provider} to {target_provider}. '
                'Choose whether to continue this session or start fresh.'
            ),
            'request_id': request_id,
        })
        emit({
            'type': 'suggestions',
            'title': 'Provider Switch',
            'items': [
                {
                    'cmd': '/1',
                    'label': '1',
                    'desc': f'Continue this session with {target_provider} using context transfer',
                },
                {
                    'cmd': '/2',
                    'label': '2',
                    'desc': f'Start a new {target_provider} session',
                },
            ],
            'request_id': request_id,
        })

    def _switch_provider_with_transfer(self, target_provider: str, request_id: str | None):
        bundle = None
        if self.engine and self._active_agent_id:
            try:
                from context_transfer import create_transfer_bundle, record_pending_transfer, record_transfer_event
                bundle = create_transfer_bundle(
                    state_dir=STATE_DIR,
                    session_id=self._active_agent_id,
                    agent_id=(getattr(self, '_bound_agent_id', None) or self._active_agent_id),
                    project_root=self.engine.project_root,
                    source_provider=self._current_provider_name() or 'unknown',
                    target_provider=target_provider,
                    messages=self.engine.messages,
                )
                record_pending_transfer(STATE_DIR, bundle)
                record_transfer_event(STATE_DIR, {
                    'ts': bundle.get('created_at', ''),
                    'type': 'provider_switch_continue',
                    'bundle_id': bundle.get('id', ''),
                    'source_provider': self._current_provider_name() or 'unknown',
                    'target_provider': target_provider,
                    'session_id': self._active_agent_id,
                })
                emit({
                    'type': 'status',
                    'message': f'Preparing context transfer to {target_provider} ({bundle.get("id", "")})...',
                    'request_id': request_id,
                })
                emit({
                    'type': 'status',
                    'message': f'Context transfer ready. Switching provider to {target_provider}...',
                    'request_id': request_id,
                })
            except Exception as e:
                emit({
                    'type': 'status',
                    'message': f'Context transfer prep failed ({e}). Falling back to fresh switch.',
                    'request_id': request_id,
                })
        self._run_setup_command(f'provider {target_provider}', request_id, skip_prompt=True)

    def _switch_provider_fresh(self, target_provider: str, request_id: str | None):
        emit({
            'type': 'status',
            'message': f'Starting a fresh {target_provider} session...',
            'request_id': request_id,
        })
        try:
            from context_transfer import clear_pending_transfer, record_transfer_event
            clear_pending_transfer(STATE_DIR)
            record_transfer_event(STATE_DIR, {
                'ts': __import__('time').strftime('%Y-%m-%dT%H:%M:%SZ', __import__('time').gmtime()),
                'type': 'provider_switch_fresh',
                'source_provider': self._current_provider_name() or 'unknown',
                'target_provider': target_provider,
                'session_id': self._active_agent_id or '',
            })
        except Exception:
            pass
        self._run_setup_command(f'provider {target_provider}', request_id, skip_prompt=True)

    def handle_command(self, command: str, request_id: str | None):
        """Handle /setup and other slash commands."""
        command = command.strip()
        if not command:
            return

        try:
            if self._pending_provider_switch and command in ('/1', '/2'):
                pending = self._pending_provider_switch
                self._pending_provider_switch = None
                if command == '/1':
                    self._switch_provider_with_transfer(str(pending.get('target_provider') or ''), request_id)
                else:
                    self._switch_provider_fresh(str(pending.get('target_provider') or ''), request_id)
                return

            # Show suggestions for /help, /setup alone, or unknown commands
            if command in ('/help', '/setup', '/?'):
                suggestions = self._get_suggestions('/setup' if command == '/setup' else '/')
                emit({
                    'type': 'suggestions',
                    'title': 'Setup Commands' if command == '/setup' else 'Available Commands',
                    'items': suggestions,
                    'request_id': request_id,
                })
                return

            if command.startswith('/setup '):
                rest = command[7:].strip()
                self._run_setup_command(rest, request_id)
                return
            if command == '/libris' or command.startswith('/libris '):
                rest = command[8:].strip() if command.startswith('/libris ') else ''
                if not rest:
                    self._pending_libris_intake = {
                        'prompt': '',
                        'goal_options': [],
                        'selected_goal': '',
                        'stop_condition': '',
                    }
                    emit({'type': 'status', 'message': 'Usage: /libris <broad research prompt>', 'request_id': request_id})
                    return
                if rest.startswith('status '):
                    op_id = rest[7:].strip()
                    try:
                        from libris_runtime import get_libris_swarm_state
                        swarm = get_libris_swarm_state(STATE_DIR, Path(self._libris_project_root()), op_id)
                        if not swarm:
                            emit({'type': 'error', 'error': f'No Libris operation found: {op_id}', 'request_id': request_id})
                            return
                        lines = [
                            f'Operation: {swarm.get("operation_id")}',
                            f'Status: {swarm.get("status")}',
                            f'Topics: {len(swarm.get("topics") or [])}',
                        ]
                        coord = swarm.get('coordinator') or {}
                        if coord:
                            lines.append(f'Coordinator: {coord.get("name")} [{coord.get("status")}]')
                        for topic in swarm.get('topics') or []:
                            lines.append(f'- {topic.get("title")} [{topic.get("status")}/{topic.get("phase")}]')
                        emit({'type': 'status', 'message': '\n'.join(lines), 'request_id': request_id})
                    except Exception as e:
                        emit({'type': 'error', 'error': f'Libris status failed: {e}', 'request_id': request_id})
                    return
                if rest.startswith('use '):
                    choice = rest[4:].strip()
                    pending = self._pending_libris_intake or {}
                    options = list(pending.get('goal_options') or [])
                    if choice.isdigit() and 1 <= int(choice) <= len(options):
                        pending['selected_goal'] = options[int(choice) - 1]
                        self._pending_libris_intake = pending
                        emit({'type': 'status', 'message': f'Selected Libris goal: {pending["selected_goal"]}', 'request_id': request_id})
                    else:
                        emit({'type': 'error', 'error': f'Invalid Libris goal option: {choice}', 'request_id': request_id})
                    return
                if rest.startswith('custom '):
                    goal = rest[7:].strip()
                    if not goal:
                        emit({'type': 'error', 'error': 'Custom Libris goal cannot be empty.', 'request_id': request_id})
                        return
                    pending = self._pending_libris_intake or {}
                    pending['selected_goal'] = goal
                    self._pending_libris_intake = pending
                    emit({'type': 'status', 'message': f'Set custom Libris goal: {goal}', 'request_id': request_id})
                    return
                if rest.startswith('stop '):
                    cond = rest[5:].strip()
                    pending = self._pending_libris_intake or {}
                    pending['stop_condition'] = cond
                    self._pending_libris_intake = pending
                    emit({'type': 'status', 'message': f'Set Libris stop condition: {cond}', 'request_id': request_id})
                    return
                if rest == 'go':
                    self._start_libris_from_pending(request_id)
                    return

                pending = {
                    'prompt': rest,
                    'goal_options': self._libris_goal_options(rest),
                    'selected_goal': '',
                    'stop_condition': self._libris_extract_stop(rest),
                }
                self._pending_libris_intake = pending
                if self._libris_has_clear_goal(rest):
                    pending['selected_goal'] = rest
                self._emit_libris_intake(request_id)
                return
            if command == '/project' or command.startswith('/project '):
                rest = command[8:].strip() if command.startswith('/project ') else ''
                try:
                    onboarding = _load_json(STATE_DIR / 'onboarding.json', {})
                    registry = _load_project_registry()
                    if not rest or rest == 'list':
                        if not registry:
                            emit({'type': 'status', 'message': 'No explicit projects yet. Use /project create <name> [path]', 'request_id': request_id})
                        else:
                            lines = ['Explicit projects:']
                            current = str(onboarding.get('project') or '').strip()
                            for p in registry[:20]:
                                name = str(p.get('name') or '')
                                path = str(p.get('path') or '')
                                mark = '*' if current and path == current else ' '
                                lines.append(f' {mark} {name} — {path}')
                            emit({'type': 'status', 'message': '\n'.join(lines), 'request_id': request_id})
                        emit({'type': 'refresh', 'payload': self._get_refresh_payload(), 'request_id': request_id})
                        return
                    if rest.startswith('create '):
                        body = rest[7:].strip()
                        if not body:
                            emit({'type': 'error', 'error': 'Usage: /project create <name> [path]', 'request_id': request_id})
                            return
                        parts = body.split(None, 1)
                        name = parts[0].strip()
                        path = parts[1].strip() if len(parts) > 1 else str(onboarding.get('project') or str(ROOT)).strip()
                        slug = _project_slug(name)
                        display_name = name.replace('-', ' ').replace('_', ' ').strip() or slug
                        existing = next((p for p in registry if str(p.get('name') or '').strip() == display_name or _project_slug(p.get('name') or '') == slug), None)
                        if existing is None:
                            registry.append({
                                'id': f'project-{slug}',
                                'name': display_name,
                                'slug': slug,
                                'path': path,
                                'description': '',
                                'created_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                            })
                            _save_project_registry(registry)
                            emit({'type': 'status', 'message': f'Created project {display_name} at {path}', 'request_id': request_id})
                        else:
                            emit({'type': 'status', 'message': f'Project already exists: {existing.get("name", display_name)}', 'request_id': request_id})
                        emit({'type': 'refresh', 'payload': self._get_refresh_payload(), 'request_id': request_id})
                        return
                    if rest.startswith('use '):
                        choice = rest[4:].strip()
                        target = next((p for p in registry if str(p.get('name') or '').strip() == choice or str(p.get('id') or '').strip() == choice or str(p.get('slug') or '').strip() == _project_slug(choice)), None)
                        if not target:
                            emit({'type': 'error', 'error': f'Unknown project: {choice}', 'request_id': request_id})
                            return
                        onboarding['project'] = str(target.get('path') or '').strip() or str(ROOT)
                        (STATE_DIR / 'onboarding.json').write_text(json.dumps(onboarding, indent=2, ensure_ascii=False))
                        emit({'type': 'status', 'message': f'Selected project {target.get("name", "project")}', 'request_id': request_id})
                        emit({'type': 'refresh', 'payload': self._get_refresh_payload(), 'request_id': request_id})
                        return
                    emit({'type': 'error', 'error': 'Usage: /project [list|create|use]', 'request_id': request_id})
                    return
                except Exception as e:
                    emit({'type': 'error', 'error': f'Project command failed: {e}', 'request_id': request_id})
                    return

            if command in ('/hermes', '/pi'):
                try:
                    from external_session_launcher import launch_wrapped_session
                    onboarding = _load_json(STATE_DIR / 'onboarding.json', {})
                    project = str(onboarding.get('project') or str(ROOT)).strip()
                    agent_kind = command[1:]
                    result = launch_wrapped_session(
                        state_dir=STATE_DIR,
                        project_root=project,
                        agent_kind=agent_kind,
                    )
                    if result.get('ok'):
                        display_name = str(result.get('display_name') or agent_kind)
                        session_name = str(result.get('tmux_session') or result.get('session_name') or '')
                        emit({'type': 'status', 'message': f'✓ {display_name} session created: {session_name}', 'request_id': request_id})
                        emit({'type': 'status', 'message': 'To view or interact with it, press F3 for the sessions grid.', 'request_id': request_id})
                        self.handle_refresh(request_id)
                    else:
                        emit({'type': 'error', 'error': f'Failed to create {agent_kind} session: {result.get("error", "unknown error")}', 'request_id': request_id})
                    return
                except Exception as e:
                    emit({'type': 'error', 'error': f'Failed to create external session: {e}', 'request_id': request_id})
                    return

            if command == '/conversation' or command.startswith('/conversation '):
                rest = command[13:].strip() if command.startswith('/conversation ') else ''
                try:
                    if not rest:
                        emit({'type': 'status', 'message': 'Usage: /conversation hermes [teacher student|<count>] <topic>', 'request_id': request_id})
                        return
                    parts = rest.split()
                    provider = (parts[0] if parts else '').strip().lower()
                    if provider != 'hermes':
                        emit({'type': 'error', 'error': f'Unsupported conversation provider for now: {provider}', 'request_id': request_id})
                        return
                    roles: list[str] = []
                    topic = ''
                    if len(parts) >= 4 and parts[1].lower() == 'teacher' and parts[2].lower() == 'student':
                        roles = ['teacher', 'student']
                        topic = ' '.join(parts[3:]).strip()
                    elif len(parts) >= 3 and str(parts[1]).isdigit():
                        count = max(2, int(parts[1]))
                        roles = ['teacher', 'student'] if count == 2 else ['participant'] * count
                        topic = ' '.join(parts[2:]).strip()
                    else:
                        roles = ['teacher', 'student']
                        topic = ' '.join(parts[1:]).strip()
                    topic = topic or 'open discussion'
                    onboarding = _load_json(STATE_DIR / 'onboarding.json', {})
                    project = str(onboarding.get('project') or str(ROOT)).strip()
                    participants = [
                        {'id': f'hermes-{idx+1}', 'role': role, 'name': f'Hermes {idx+1}'}
                        for idx, role in enumerate(roles)
                    ]
                    self._create_hermes_room(
                        kind='conversation',
                        title=topic,
                        project=project,
                        participants=participants,
                        meta={'provider': 'hermes', 'count': len(participants), 'topic': topic, 'conversation_mode': 'scripted-teacher-student' if roles == ['teacher', 'student'] else 'scripted-team'},
                        request_id=request_id,
                        start_runner=(roles == ['teacher', 'student']),
                    )
                    return
                except Exception as e:
                    emit({'type': 'error', 'error': f'Conversation command failed: {e}', 'request_id': request_id})
                    return

            if command == '/team' or command.startswith('/team '):
                rest = command[5:].strip() if command.startswith('/team ') else ''
                try:
                    if not rest:
                        emit({'type': 'status', 'message': 'Usage: /team hermes <count> <topic>', 'request_id': request_id})
                        return
                    parts = rest.split(None, 2)
                    provider = (parts[0] if len(parts) > 0 else '').strip().lower()
                    count = int(parts[1]) if len(parts) > 1 and str(parts[1]).isdigit() else 2
                    topic = (parts[2] if len(parts) > 2 else '').strip() or 'open discussion'
                    if provider != 'hermes':
                        emit({'type': 'error', 'error': f'Unsupported team provider for now: {provider}', 'request_id': request_id})
                        return

                    onboarding = _load_json(STATE_DIR / 'onboarding.json', {})
                    project = str(onboarding.get('project') or str(ROOT)).strip()
                    participants = []
                    for idx in range(count):
                        role = 'participant'
                        if count == 2:
                            role = 'teacher' if idx == 0 else 'student'
                        participants.append({'id': f'hermes-{idx+1}', 'role': role, 'name': f'Hermes {idx+1}'})
                    self._create_hermes_room(
                        kind='conversation',
                        title=topic,
                        project=project,
                        participants=participants,
                        meta={'provider': 'hermes', 'count': count, 'topic': topic, 'conversation_mode': 'scripted-teacher-student' if count == 2 else 'scripted-team'},
                        request_id=request_id,
                        start_runner=(count == 2),
                    )
                    return
                except Exception as e:
                    emit({'type': 'error', 'error': f'Team command failed: {e}', 'request_id': request_id})
                    return

            if command == '/devteam' or command.startswith('/devteam '):
                rest = command[8:].strip() if command.startswith('/devteam ') else ''
                try:
                    if not rest:
                        emit({'type': 'status', 'message': 'Usage: /devteam hermes <count> <goal>', 'request_id': request_id})
                        return
                    parts = rest.split(None, 2)
                    provider = (parts[0] if len(parts) > 0 else '').strip().lower()
                    count = int(parts[1]) if len(parts) > 1 and str(parts[1]).isdigit() else 2
                    goal = (parts[2] if len(parts) > 2 else '').strip() or 'engineering task'
                    if provider != 'hermes':
                        emit({'type': 'error', 'error': f'Unsupported devteam provider for now: {provider}', 'request_id': request_id})
                        return
                    onboarding = _load_json(STATE_DIR / 'onboarding.json', {})
                    project = str(onboarding.get('project') or str(ROOT)).strip()
                    participants = [
                        {'id': f'hermes-{idx+1}', 'role': 'developer', 'name': f'Hermes {idx+1}'}
                        for idx in range(count)
                    ]
                    self._create_hermes_room(
                        kind='devteam',
                        title=goal,
                        project=project,
                        participants=participants,
                        meta={'provider': 'hermes', 'count': count, 'goal': goal, 'team_mode': 'devteam'},
                        request_id=request_id,
                        start_runner=False,
                    )
                    return
                except Exception as e:
                    emit({'type': 'error', 'error': f'Devteam command failed: {e}', 'request_id': request_id})
                    return

            if command == '/resume' or command.startswith('/resume '):
                arg = command[8:].strip() if command.startswith('/resume ') else ''
                try:
                    from conversation_store import list_conversations, load_conversation, dict_to_message
                    convos = list_conversations(STATE_DIR)
                    if arg:
                        # Direct resume
                        saved = _sanitize_saved_messages(load_conversation(STATE_DIR, arg))
                        if saved:
                            self._active_agent_id = arg
                            engine, _ = self._ensure_engine()
                            if engine:
                                engine.messages = [dict_to_message(m) for m in saved]
                            self._load_tasks_from_ledger(arg)
                            emit({
                                'type': 'conversation_restored',
                                'messages': saved,
                                'count': len(saved),
                                'agent_id': arg,
                            })
                            # Push session info so task pane updates immediately
                            emit({
                                'type': 'refresh',
                                'payload': {'session_info': self._get_session_info()},
                            })
                        else:
                            emit({'type': 'error', 'error': f'No saved conversation for {arg}', 'request_id': request_id})
                    elif convos:
                        # Show session picker with last user message preview
                        import time
                        items = []
                        for c in sorted(convos, key=lambda x: x.get('last_timestamp', 0), reverse=True)[:5]:
                            age = ''
                            if c.get('last_timestamp'):
                                secs = time.time() - c['last_timestamp']
                                if secs < 60: age = f'{int(secs)}s ago'
                                elif secs < 3600: age = f'{int(secs/60)}m ago'
                                elif secs < 86400: age = f'{int(secs/3600)}h ago'
                                else: age = f'{int(secs/86400)}d ago'
                            # Find last user message for preview
                            preview = ''
                            try:
                                saved = load_conversation(STATE_DIR, c['agent_id'])
                                for msg in reversed(saved):
                                    if msg.get('role') == 'user' and msg.get('content', '').strip():
                                        first_line = msg['content'].strip().split('\n')[0]
                                        preview = first_line[:60]
                                        if len(first_line) > 60:
                                            preview += '…'
                                        break
                            except Exception:
                                pass
                            msg_count = c.get('message_count', 0)
                            items.append({
                                'id': c['agent_id'],
                                'desc': f"{preview or '(no messages)'}",
                                'age': f"{msg_count}msg  {age}",
                            })
                        if items:
                            emit({
                                'type': 'model_picker',
                                'models': items,
                                'provider': 'resume',
                                'request_id': request_id,
                            })
                        else:
                            emit({'type': 'status', 'message': 'No other saved conversations found.', 'request_id': request_id})
                    else:
                        emit({'type': 'status', 'message': 'No saved conversations found.', 'request_id': request_id})
                except Exception as e:
                    emit({'type': 'error', 'error': f'Resume failed: {e}', 'request_id': request_id})
                return
            if command == '/hotkeys':
                emit({
                    'type': 'suggestions',
                    'title': 'Keyboard Shortcuts',
                    'items': [
                        {'cmd': 'F1', 'desc': 'Switch to Chat view'},
                        {'cmd': 'F2', 'desc': 'Switch to Dashboard view'},
                        {'cmd': 'F3', 'desc': 'Switch to Session Grid view'},
                        {'cmd': 'Tab', 'desc': 'Dashboard: switch agents/projects | Sessions: cycle panes'},
                        {'cmd': '↑↓', 'desc': 'Navigate lists, menus, grid'},
                        {'cmd': '←→', 'desc': 'Navigate session grid horizontally'},
                        {'cmd': 'Enter', 'desc': 'Select menu item / enter session / submit input'},
                        {'cmd': 'Escape', 'desc': 'Close menu / exit session'},
                        {'cmd': 'Ctrl+F', 'desc': 'Zoom/unzoom session in grid'},
                        {'cmd': 'Ctrl+T', 'desc': 'Toggle timestamps'},
                        {'cmd': 'Ctrl+Y', 'desc': 'Toggle visible thoughts'},
                        {'cmd': 'Ctrl+C', 'desc': 'Exit Charon'},
                        {'cmd': '/', 'desc': 'Open command menu'},
                    ],
                    'request_id': request_id,
                })
                return
            if command == '/timestamps':
                emit({'type': 'toggle_timestamps', 'request_id': request_id})
                return
            if command in ('/interrupt', '/abort'):
                self.handle_abort(request_id)
                return
            if command == '/thoughts':
                self.visible_thoughts = not self.visible_thoughts
                try:
                    settings = _load_ui_settings()
                    settings['visible_thoughts'] = self.visible_thoughts
                    _save_ui_settings(settings)
                except Exception:
                    pass
                emit({
                    'type': 'toggle_visible_thoughts',
                    'enabled': self.visible_thoughts,
                    'supported': self._thoughts_supported(),
                    'provider': self._current_provider_name(),
                    'request_id': request_id,
                })
                return
            if command.startswith('/setup shade-model '):
                model_name = command[18:].strip()
                if model_name == 'same':
                    from model_registry import load_registry, save_registry
                    reg = load_registry(STATE_DIR)
                    reg['shade_model_mode'] = 'same'
                    save_registry(STATE_DIR, reg)
                    emit({'type': 'status', 'message': 'Shade model: same as main agent.', 'request_id': request_id})
                elif model_name == 'auto':
                    from model_registry import load_registry, save_registry
                    reg = load_registry(STATE_DIR)
                    reg['shade_model_mode'] = 'auto'
                    save_registry(STATE_DIR, reg)
                    emit({'type': 'status', 'message': 'Shade model: auto (Charon picks per task).', 'request_id': request_id})
                else:
                    from model_registry import load_registry, save_registry
                    reg = load_registry(STATE_DIR)
                    reg['shade_model_mode'] = 'fixed'
                    # Parse provider/model format
                    if '/' in model_name:
                        parts = model_name.split('/', 1)
                        reg['shade_provider'] = parts[0]
                        reg['shade_model'] = parts[1]
                    else:
                        reg['shade_model'] = model_name
                    save_registry(STATE_DIR, reg)
                    emit({'type': 'status', 'message': f'Shade model: {model_name}', 'request_id': request_id})
                return
            if command.startswith('/setup shade-url '):
                url = command[17:].strip()
                from model_registry import load_registry, save_registry
                reg = load_registry(STATE_DIR)
                reg['shade_base_url'] = url
                save_registry(STATE_DIR, reg)
                emit({'type': 'status', 'message': f'Shade base URL: {url}', 'request_id': request_id})
                return
            if command.startswith('/setup shade-key '):
                key = command[17:].strip()
                from model_registry import load_registry, save_registry
                reg = load_registry(STATE_DIR)
                reg['shade_api_key'] = key
                save_registry(STATE_DIR, reg)
                emit({'type': 'status', 'message': 'Shade API key saved.', 'request_id': request_id})
                return
            if command == '/models' or command == '/models list':
                try:
                    import httpx
                    lines = []

                    # Local models (LM Studio / Ollama)
                    for name, url in [('LM Studio', 'http://127.0.0.1:1234/v1'), ('Ollama', 'http://127.0.0.1:11434/v1')]:
                        try:
                            resp = httpx.get(f'{url}/models', timeout=3)
                            if resp.status_code == 200:
                                data = resp.json()
                                models = [m.get('id', '?') for m in data.get('data', [])]
                                lines.append(f'{name} ({url}):')
                                for m in models:
                                    lines.append(f'  {m}')
                                lines.append('')
                        except Exception:
                            pass

                    # Current config
                    onboarding = _load_json(STATE_DIR / 'onboarding.json', {})
                    current = onboarding.get('model') or onboarding.get('provider_model') or 'none'
                    lines.append(f'Current model: {current}')
                    lines.append(f'Provider: {onboarding.get("provider", "none")}')

                    try:
                        from model_registry import load_registry
                        reg = load_registry(STATE_DIR)
                        shade_model = reg.get('shade_model') or '(same as main)'
                        lines.append(f'Shade model: {shade_model}')
                    except Exception:
                        pass

                    if lines:
                        emit({'type': 'status', 'message': '\n'.join(lines), 'request_id': request_id})
                    else:
                        emit({'type': 'status', 'message': 'No local model servers detected.', 'request_id': request_id})
                except Exception as e:
                    emit({'type': 'error', 'error': str(e), 'request_id': request_id})
                return
            if command == '/settings' or command == '/config':
                try:
                    lines = ['# Charon Settings', '']

                    # Provider
                    onboarding = _load_json(STATE_DIR / 'onboarding.json', {})
                    provider = str(onboarding.get('provider') or 'none')
                    model = str(onboarding.get('model') or onboarding.get('provider_model') or 'none')
                    project = str(onboarding.get('project') or 'none')
                    lines.append(f'Provider: {provider}')
                    lines.append(f'Model: {model}')
                    lines.append(f'Project: {project}')
                    lines.append(f'Setup complete: {onboarding.get("complete", False)}')
                    lines.append('')

                    # Shade model
                    try:
                        from model_registry import load_registry
                        reg = load_registry(STATE_DIR)
                        shade_mode = reg.get('shade_model_mode', 'auto')
                        shade_model = reg.get('shade_model') or '(same as main)'
                        shade_provider = reg.get('shade_provider') or '(same as main)'
                        shade_url = reg.get('shade_base_url') or '(default)'
                        lines.append(f'Shade model mode: {shade_mode}')
                        lines.append(f'Shade model: {shade_model}')
                        lines.append(f'Shade provider: {shade_provider}')
                        lines.append(f'Shade URL: {shade_url}')
                    except Exception:
                        lines.append('Shade model: (not configured)')
                    lines.append('')

                    # Autonomous mode
                    try:
                        from autonomous import load_autonomous_config
                        auto = load_autonomous_config(STATE_DIR)
                        lines.append(f'Autonomous mode: {"ON" if auto.get("enabled") else "OFF"}')
                        tb = auto.get('time_budget_minutes')
                        lines.append(f'Time budget: {tb} min' if tb else 'Time budget: unlimited')
                        lines.append(f'Git checkpoints: {"on" if auto.get("git_checkpoint") else "off"}')
                    except Exception:
                        lines.append('Autonomous mode: OFF')
                    lines.append('')

                    # Consolidation
                    try:
                        from consolidation import load_config
                        con = load_config(STATE_DIR)
                        lines.append(f'Consolidation: {"on" if con.get("enabled") else "off"}')
                        lines.append(f'Consolidation model: {con.get("model_tier", "fast")}')
                        lines.append(f'Consolidation interval: {con.get("scan_interval_heartbeats", 50)} heartbeats')
                    except Exception:
                        lines.append('Consolidation: on (default)')
                    lines.append('')

                    # Approval
                    try:
                        from tool_approval import get_approval_status
                        status = get_approval_status(self._active_agent_id or 'default')
                        skip = status.get('skip_all', False)
                        approved = status.get('session_approved', [])
                        lines.append(f'Approval: {"DISABLED" if skip else "enabled"}')
                        if approved:
                            lines.append(f'Session approved: {", ".join(approved[:5])}')
                    except Exception:
                        lines.append('Approval: enabled')
                    lines.append('')

                    # Agent
                    try:
                        from agent_lifecycle import list_agents
                        agents = [a for a in list_agents() if a.get('role') == 'charon']
                        lines.append(f'Agents: {len(agents)}')
                        for a in agents[:5]:
                            lines.append(f'  {a.get("name", a.get("id", "?"))} ({a.get("id")}) — {a.get("status", "?")}')
                    except Exception:
                        pass
                    lines.append('')

                    # Tools
                    from tools import ALL_TOOL_DEFS
                    built_in = [t['name'] for t in ALL_TOOL_DEFS]
                    lines.append(f'Tools ({len(built_in)}): {", ".join(built_in)}')
                    try:
                        from tools.dynamic_loader import list_dynamic_tools
                        dynamic = list_dynamic_tools()
                        if dynamic:
                            lines.append(f'Dynamic tools ({len(dynamic)}): {", ".join(t["name"] for t in dynamic)}')
                    except Exception:
                        pass

                    emit({'type': 'status', 'message': '\n'.join(lines), 'request_id': request_id})
                except Exception as e:
                    emit({'type': 'error', 'error': str(e), 'request_id': request_id})
                return
            if command == '/approve' or command.startswith('/approve '):
                try:
                    from tool_approval import approve_tool_for_session, approve_for_session, get_approval_status
                    arg = command[9:].strip() if command.startswith('/approve ') else ''
                    # Approve for all possible session IDs to avoid mismatch
                    session_ids = set()
                    session_ids.add(self._active_agent_id or 'default')
                    session_ids.add('default')
                    # Also add the actual agent ID from the engine
                    if self.engine and self.engine.agent_id:
                        session_ids.add(self.engine.agent_id)
                    session_id = self._active_agent_id or 'default'

                    if arg == 'status':
                        status = get_approval_status(session_id)
                        skip = '(ALL CHECKS DISABLED)' if status['skip_all'] else ''
                        lines = [f'Approval status {skip}']
                        if status['session_approved']:
                            lines.append('Session approved:')
                            for a in status['session_approved']:
                                lines.append(f'  ✓ {a}')
                        if status['permanent_approved']:
                            lines.append('Permanently approved:')
                            for a in status['permanent_approved']:
                                lines.append(f'  ✓ {a}')
                        if not status['session_approved'] and not status['permanent_approved']:
                            lines.append('  (no approvals granted)')
                        emit({'type': 'status', 'message': '\n'.join(lines), 'request_id': request_id})
                    elif not arg or arg == 'all':
                        # Approve all tools for all session IDs
                        for sid in session_ids:
                            for tool in ('Web', 'Http', 'Write', 'Edit', 'Bash', 'Git', 'SpawnBatch', 'SpawnShade', 'Browser', 'X'):
                                approve_tool_for_session(sid, tool)
                        emit({'type': 'status', 'message': '✓ All tools approved for this session.', 'request_id': request_id})
                    elif arg.startswith('network') or arg.startswith('web') or arg.startswith('http'):
                        for sid in session_ids:
                            approve_tool_for_session(sid, 'Web')
                            approve_tool_for_session(sid, 'Http')
                            approve_tool_for_session(sid, 'Browser')
                            approve_tool_for_session(sid, 'X')
                        emit({'type': 'status', 'message': '✓ Network tools approved for this session.', 'request_id': request_id})
                    elif arg.startswith('write') or arg.startswith('edit') or arg.startswith('file'):
                        for sid in session_ids:
                            approve_tool_for_session(sid, 'Write')
                            approve_tool_for_session(sid, 'Edit')
                            approve_tool_for_session(sid, 'Git')
                        emit({'type': 'status', 'message': '✓ File modification tools approved for this session.', 'request_id': request_id})
                    else:
                        for sid in session_ids:
                            approve_for_session(sid, arg)
                        emit({'type': 'status', 'message': f'✓ Approved: {arg}', 'request_id': request_id})
                except Exception as e:
                    emit({'type': 'error', 'error': str(e), 'request_id': request_id})
                return
            if command.startswith('/provider ') or command == '/provider':
                if command == '/provider':
                    onboarding = _load_json(STATE_DIR / 'onboarding.json', {})
                    provider = str(onboarding.get('provider') or 'none')
                    model = str(onboarding.get('model') or onboarding.get('provider_model') or 'none')
                    emit({'type': 'status', 'message': f'Current provider: {provider}/{model}', 'request_id': request_id})
                else:
                    new_provider = command[10:].strip()
                    if not new_provider:
                        emit({'type': 'error', 'error': 'Usage: /provider <name>', 'request_id': request_id})
                        return

                    current_provider = self._current_provider_name()
                    if new_provider == current_provider:
                        emit({'type': 'status', 'message': f'Already on {new_provider}.', 'request_id': request_id})
                        return

                    if self._has_transferable_context():
                        self._prompt_provider_switch(new_provider, request_id, source='provider')
                    else:
                        self._switch_provider_fresh(new_provider, request_id)
                return
            if command == '/shades' or command == '/shade stats':
                try:
                    from shade_stats import get_shade_stats, format_stats
                    stats = get_shade_stats(STATE_DIR)
                    if stats:
                        emit({'type': 'status', 'message': format_stats(stats), 'request_id': request_id})
                    else:
                        emit({'type': 'status', 'message': 'No shade usage recorded yet.', 'request_id': request_id})
                except Exception as e:
                    emit({'type': 'error', 'error': str(e), 'request_id': request_id})
                return
            if command == '/batch' or command.startswith('/batch '):
                batch_id = command[7:].strip() if command.startswith('/batch ') else ''
                try:
                    from batch_orchestrator import get_batch, list_batches, summarize_batch
                    if batch_id:
                        batch = get_batch(STATE_DIR, batch_id)
                        if batch:
                            lines = [summarize_batch(batch)]
                            for t in batch.get('tasks', []):
                                icon = {'completed': '✓', 'failed': '✗', 'in_progress': '◆', 'pending': '○'}.get(t.get('status', ''), '·')
                                model = t.get('model_used') or ''
                                complexity = t.get('complexity', 'normal')
                                model_tag = f' [{model}]' if model else ''
                                cx_tag = f' ({complexity})' if complexity != 'normal' else ''
                                summary = t.get('result_summary') or t.get('error') or ''
                                lines.append(f'  {icon} {t.get("title", "")}{cx_tag}{model_tag}: {summary[:50]}')
                            emit({'type': 'status', 'message': '\n'.join(lines), 'request_id': request_id})
                        else:
                            emit({'type': 'error', 'error': f'Batch not found: {batch_id}', 'request_id': request_id})
                    else:
                        batches = list_batches(STATE_DIR)
                        if batches:
                            lines = [f'{len(batches)} batch(es):']
                            for b in batches[-10:]:
                                lines.append(f'  {summarize_batch(b)}')
                            emit({'type': 'status', 'message': '\n'.join(lines), 'request_id': request_id})
                        else:
                            emit({'type': 'status', 'message': 'No batches.', 'request_id': request_id})
                except Exception as e:
                    emit({'type': 'error', 'error': str(e), 'request_id': request_id})
                return
            if command == '/tools' or command == '/tools list':
                from tools import ALL_TOOL_DEFS
                lines = ['Built-in tools:']
                for t in ALL_TOOL_DEFS:
                    lines.append(f'  {t["name"]}: {t["description"][:60]}')
                try:
                    from tools.dynamic_loader import list_dynamic_tools, get_load_errors
                    dynamic = list_dynamic_tools()
                    if dynamic:
                        lines.append('\nDynamic tools:')
                        for t in dynamic:
                            lines.append(f'  {t["name"]}: {t["description"]}')
                            lines.append(f'    source: {t["source"]}')
                    errors = get_load_errors()
                    if errors:
                        lines.append('\nLoad errors:')
                        for e in errors:
                            lines.append(f'  {e["path"]}: {e["error"]}')
                except Exception:
                    pass
                emit({'type': 'status', 'message': '\n'.join(lines), 'request_id': request_id})
                return
            if command == '/tools reload':
                try:
                    from tools.dynamic_loader import load_dynamic_tools
                    onboarding = _load_json(STATE_DIR / 'onboarding.json', {})
                    project = str(onboarding.get('project') or str(ROOT)).strip()
                    defs, executors, errors = load_dynamic_tools(STATE_DIR, Path(project))
                    msg = f'Reloaded: {len(defs)} dynamic tool(s)'
                    if errors:
                        msg += f', {len(errors)} error(s)'
                        for e in errors:
                            msg += f'\n  {e["error"]}'
                    # Refresh engine tools
                    if self.engine:
                        from tools.dynamic_loader import get_all_tool_defs
                        self.engine.tools = get_all_tool_defs(STATE_DIR, Path(project))
                    emit({'type': 'status', 'message': msg, 'request_id': request_id})
                except Exception as e:
                    emit({'type': 'error', 'error': f'Reload failed: {e}', 'request_id': request_id})
                return
            if command == '/autonomous' or command == '/autonomous status':
                try:
                    from autonomous import load_autonomous_config, get_proposed_goals, get_goals_by_status
                    config = load_autonomous_config(STATE_DIR)
                    onboarding = _load_json(STATE_DIR / 'onboarding.json', {})
                    project = str(onboarding.get('project') or str(ROOT)).strip()
                    proposed = get_proposed_goals(STATE_DIR, project=project)
                    confirmed = get_goals_by_status(STATE_DIR, project=project, status='confirmed')
                    executing = get_goals_by_status(STATE_DIR, project=project, status='executing')
                    msg = (
                        f'Autonomous mode: {"ON" if config.get("enabled") else "OFF"}\n'
                        f'Time budget: {config.get("time_budget_minutes") or "unlimited"} min\n'
                        f'Token budget: {config.get("token_budget") or "unlimited"}\n'
                        f'Git checkpoints: {"on" if config.get("git_checkpoint") else "off"}\n'
                        f'Goals — proposed: {len(proposed)}, confirmed: {len(confirmed)}, executing: {len(executing)}'
                    )
                    emit({'type': 'status', 'message': msg, 'request_id': request_id})
                except Exception as e:
                    emit({'type': 'error', 'error': str(e), 'request_id': request_id})
                return
            if command == '/autonomous on':
                from autonomous import load_autonomous_config, save_autonomous_config
                config = load_autonomous_config(STATE_DIR)
                config['enabled'] = True
                save_autonomous_config(STATE_DIR, config)
                emit({'type': 'status', 'message': 'Autonomous mode ON. Agent will self-assign from confirmed goals.', 'request_id': request_id})
                return
            if command == '/autonomous off':
                from autonomous import load_autonomous_config, save_autonomous_config
                config = load_autonomous_config(STATE_DIR)
                config['enabled'] = False
                save_autonomous_config(STATE_DIR, config)
                emit({'type': 'status', 'message': 'Autonomous mode OFF.', 'request_id': request_id})
                return
            if command.startswith('/autonomous time '):
                try:
                    minutes = int(command[16:].strip())
                    from autonomous import load_autonomous_config, save_autonomous_config
                    config = load_autonomous_config(STATE_DIR)
                    config['time_budget_minutes'] = minutes
                    save_autonomous_config(STATE_DIR, config)
                    emit({'type': 'status', 'message': f'Time budget set to {minutes} minutes.', 'request_id': request_id})
                except ValueError:
                    emit({'type': 'error', 'error': 'Usage: /autonomous time <minutes>', 'request_id': request_id})
                return
            if command.startswith('/autonomous tokens '):
                try:
                    tokens = int(command[18:].strip())
                    from autonomous import load_autonomous_config, save_autonomous_config
                    config = load_autonomous_config(STATE_DIR)
                    config['token_budget'] = tokens
                    save_autonomous_config(STATE_DIR, config)
                    emit({'type': 'status', 'message': f'Token budget set to {tokens}.', 'request_id': request_id})
                except ValueError:
                    emit({'type': 'error', 'error': 'Usage: /autonomous tokens <count>', 'request_id': request_id})
                return
            if command == '/confirm' or command.startswith('/confirm '):
                goal_id = command[9:].strip() if command.startswith('/confirm ') else ''
                try:
                    from autonomous import confirm_goal, get_proposed_goals
                    onboarding = _load_json(STATE_DIR / 'onboarding.json', {})
                    project = str(onboarding.get('project') or str(ROOT)).strip()
                    if not goal_id:
                        proposed = get_proposed_goals(STATE_DIR, project=project)
                        if proposed:
                            goal_id = proposed[0].get('goal_id', '')
                        else:
                            emit({'type': 'status', 'message': 'No proposed goals to confirm.', 'request_id': request_id})
                            return
                    result = confirm_goal(STATE_DIR, project=project, goal_id=goal_id)
                    if result:
                        emit({'type': 'status', 'message': f'Goal confirmed: {result.get("title", "")[:80]}', 'request_id': request_id})
                        emit({'type': 'refresh', 'payload': {'session_info': self._get_session_info()}, 'request_id': request_id})
                    else:
                        emit({'type': 'error', 'error': f'Goal not found: {goal_id}', 'request_id': request_id})
                except Exception as e:
                    emit({'type': 'error', 'error': str(e), 'request_id': request_id})
                return
            if command == '/reject' or command.startswith('/reject '):
                goal_id = command[8:].strip() if command.startswith('/reject ') else ''
                try:
                    from autonomous import reject_goal, get_proposed_goals
                    onboarding = _load_json(STATE_DIR / 'onboarding.json', {})
                    project = str(onboarding.get('project') or str(ROOT)).strip()
                    if not goal_id:
                        proposed = get_proposed_goals(STATE_DIR, project=project)
                        if proposed:
                            goal_id = proposed[0].get('goal_id', '')
                    if goal_id:
                        reject_goal(STATE_DIR, project=project, goal_id=goal_id)
                        emit({'type': 'status', 'message': f'Goal rejected (moved to backlog).', 'request_id': request_id})
                        emit({'type': 'refresh', 'payload': {'session_info': self._get_session_info()}, 'request_id': request_id})
                    else:
                        emit({'type': 'status', 'message': 'No proposed goals to reject.', 'request_id': request_id})
                except Exception as e:
                    emit({'type': 'error', 'error': str(e), 'request_id': request_id})
                return
            if command == '/history' or command.startswith('/history '):
                agent_id = command[9:].strip() if command.startswith('/history ') else ''
                self.handle_agent_ledger(agent_id, request_id)
                return
            if command == '/consolidation' or command == '/consolidation status':
                self.handle_consolidation_traces(request_id)
                return
            if command == '/consolidation run':
                self.handle_consolidation_run(request_id)
                return
            if command.startswith('/consolidation model '):
                model_name = command[20:].strip()
                self.handle_consolidation_config({'action': 'set', 'config': {'model_tier': model_name}}, request_id)
                return
            if command.startswith('/consolidation interval '):
                try:
                    interval = int(command[23:].strip())
                    self.handle_consolidation_config({'action': 'set', 'config': {'scan_interval_heartbeats': interval}}, request_id)
                except ValueError:
                    emit({'type': 'error', 'error': 'Interval must be a number.', 'request_id': request_id})
                return
            if command == '/consolidation off':
                self.handle_consolidation_config({'action': 'set', 'config': {'enabled': False}}, request_id)
                return
            if command == '/consolidation on':
                self.handle_consolidation_config({'action': 'set', 'config': {'enabled': True}}, request_id)
                return
            if command == '/reset':
                if self.engine:
                    self.engine.reset()
                self.chat_history = []
                emit({'type': 'status', 'message': 'Conversation cleared.', 'request_id': request_id})
                return
            if command == '/model':
                onboarding = _load_json(STATE_DIR / 'onboarding.json', {})
                model = str(onboarding.get('model') or onboarding.get('provider_model') or 'none')
                provider = str(onboarding.get('provider') or 'none')
                # Show current model and trigger picker
                emit({'type': 'status', 'message': f'Current: {provider}/{model}', 'request_id': request_id})
                self._run_setup_command('model', request_id)
                return
            if command.startswith('/model '):
                model_name = command[7:].strip()
                if model_name:
                    self._run_setup_command(f'model {model_name}', request_id)
                return
            if command == '/provider':
                onboarding = _load_json(STATE_DIR / 'onboarding.json', {})
                provider = str(onboarding.get('provider') or 'none')
                emit({'type': 'status', 'message': f'Current provider: {provider}', 'request_id': request_id})
                # Show provider picker as menu
                emit({
                    'type': 'model_picker',
                    'models': [
                        {'id': 'claude-code', 'desc': 'Anthropic Claude (OAuth)'},
                        {'id': 'codex', 'desc': 'OpenAI Codex (OAuth)'},
                        {'id': 'lmstudio', 'desc': 'Local LM Studio'},
                        {'id': 'api', 'desc': 'Custom API endpoint'},
                    ],
                    'provider': 'switch',
                    'request_id': request_id,
                })
                return
            if command.startswith('/provider '):
                provider_name = command[10:].strip()
                if provider_name:
                    self._run_setup_command(f'provider {provider_name}', request_id)
                return

            # Unknown command — show suggestions
            suggestions = self._get_suggestions(command)
            if suggestions:
                emit({
                    'type': 'suggestions',
                    'title': f'Did you mean?',
                    'items': suggestions,
                    'request_id': request_id,
                })
            else:
                emit({'type': 'error', 'error': f'Unknown command: {command}', 'request_id': request_id})
        except Exception as e:
            emit({'type': 'error', 'error': str(e), 'request_id': request_id})

    def _detect_lmstudio_models(self) -> list[str]:
        models: list[str] = []
        try:
            import httpx
            resp = httpx.get('http://127.0.0.1:1234/v1/models', timeout=3.0)
            if resp.status_code == 200:
                for m in resp.json().get('data', []):
                    mid = str(m.get('id') or '').strip()
                    if mid and mid not in models:
                        models.append(mid)
        except Exception:
            pass
        return models

    def _run_setup_command(self, rest: str, request_id: str | None, *, skip_prompt: bool = False):
        """Execute setup subcommands."""
        import importlib.util
        # Directly update onboarding state
        onboarding = _load_json(STATE_DIR / 'onboarding.json', {})

        parts = rest.split(maxsplit=1)
        subcmd = parts[0] if parts else ''
        arg = parts[1].strip() if len(parts) > 1 else ''

        session_id = self._active_agent_id or None
        session_override = load_session_provider_config(STATE_DIR, session_id) if session_id else {}

        if subcmd == 'status':
            status_payload = dict(onboarding)
            if session_override:
                status_payload['session_override'] = session_override
                status_payload['session_id'] = session_id
            emit({'type': 'status', 'message': json.dumps(status_payload, indent=2), 'request_id': request_id})
        elif subcmd == 'reset':
            onboarding = {'complete': False, 'step': 'provider-mode', 'provider_mode': '', 'provider': '', 'model': ''}
            self._save_onboarding(onboarding)
            self.engine = None  # force re-creation
            emit({'type': 'status', 'message': 'Setup reset.', 'request_id': request_id})
        elif subcmd == 'provider':
            # Parse: /setup provider claude-code [--force]
            arg_parts = arg.split()
            force_oauth = '--force' in arg_parts
            arg = arg_parts[0] if arg_parts else ''
            allowed = {'codex', 'claude-code', 'opencode', 'api', 'lmstudio'}
            if arg not in allowed:
                emit({'type': 'error', 'error': f'Unknown provider: {arg}. Options: {", ".join(sorted(allowed))}', 'request_id': request_id})
                return
            current_provider = self._current_provider_name()
            if not skip_prompt and current_provider and current_provider != arg and self._has_transferable_context():
                self._prompt_provider_switch(arg, request_id, source='setup-provider')
                return

            use_session_override = bool(self._active_agent_id)
            target_state = dict(session_override) if use_session_override else dict(onboarding)
            target_state['provider_mode'] = 'provider'
            target_state['provider'] = arg
            target_state['complete'] = False

            def _persist_target_state() -> None:
                if use_session_override and self._active_agent_id:
                    save_session_provider_config(STATE_DIR, self._active_agent_id, target_state)
                else:
                    onboarding.update(target_state)
                    self._save_onboarding(onboarding)

            def _revert_target_state() -> None:
                if use_session_override and self._active_agent_id:
                    if session_override:
                        save_session_provider_config(STATE_DIR, self._active_agent_id, session_override)
                    else:
                        from provider_bridge import clear_session_provider_config
                        clear_session_provider_config(STATE_DIR, self._active_agent_id)
                else:
                    self._save_onboarding(onboarding)

            # For OAuth providers, try to find existing credentials first
            if arg in ('claude-code', 'codex'):
                target_state['step'] = 'provider-auth'
                _persist_target_state()
                provider_map = {'claude-code': 'anthropic', 'codex': 'openai-codex'}
                provider_id = provider_map[arg]

                # Try to find existing Claude credentials
                # First check our own auth store, then Claude's credentials file
                # Use /setup provider claude-code --force to skip and do fresh OAuth
                existing_token = None
                if arg == 'claude-code' and not force_oauth:
                    # Check Charon's own auth store first
                    existing_token = self._find_charon_auth_token('anthropic')
                    # Then try Claude Code's credentials file
                    if not existing_token:
                        existing_token = self._find_claude_credentials()
                    if existing_token:
                        # Save existing token to charon auth store
                        try:
                            import charon_auth
                            store = charon_auth._load_auth()
                            store['active_provider'] = 'anthropic'
                            store.setdefault('providers', {})
                            store['providers']['anthropic'] = {
                                'tokens': {'access_token': existing_token},
                                'last_login': charon_auth._now(),
                                'auth_type': 'existing_claude',
                            }
                            charon_auth._save_auth(store)

                            target_state['provider_auth'] = 'existing'
                            target_state['step'] = 'model'
                            _persist_target_state()

                            emit({'type': 'status', 'message': '✓ Found existing Claude credentials! Token imported.', 'request_id': request_id})
                            # Auto-trigger model picker
                            self._run_setup_command('model', request_id)
                            return
                        except Exception as e:
                            emit({'type': 'status', 'message': f'Found credentials but import failed: {e}. Falling back to OAuth.', 'request_id': request_id})

                # Run OAuth with local callback server in a background thread
                try:
                    import charon_auth
                    import threading

                    emit({'type': 'status', 'message': f'Setting up OAuth for {arg}...', 'request_id': request_id})

                    def _run_oauth():
                        try:
                            def _status(msg: str):
                                if msg.startswith('AUTH_URL::'):
                                    url = msg.split('AUTH_URL::', 1)[1].strip()
                                    emit({'type': 'auth_url', 'url': url, 'provider': arg, 'request_id': request_id})
                                elif msg.startswith('AUTH_INFO::'):
                                    emit({'type': 'status', 'message': msg.split('AUTH_INFO::', 1)[1].strip(), 'request_id': request_id})
                                else:
                                    emit({'type': 'status', 'message': msg, 'request_id': request_id})

                            token_data = charon_auth.login_oauth(provider_id, status_cb=_status)

                            target_state['provider_auth'] = 'oauth'
                            target_state['step'] = 'model'
                            _persist_target_state()

                            emit({'type': 'status', 'message': f'✓ Authentication successful!', 'request_id': request_id})
                            # Auto-trigger model picker
                            self._run_setup_command('model', request_id)
                        except Exception as e:
                            _revert_target_state()
                            emit({'type': 'error', 'error': f'Auth failed: {e}', 'request_id': request_id})
                            emit({'type': 'status', 'message': 'Provider switch interrupted. Restored previous provider for this session.', 'request_id': request_id})
                            emit({'type': 'status', 'message': 'You can also try: /setup api-key <your-key>', 'request_id': request_id})

                    t = threading.Thread(target=_run_oauth, daemon=True)
                    t.start()
                    emit({'type': 'status', 'message': f'Starting {arg} authentication... Opening browser.', 'request_id': request_id})
                except Exception as e:
                    _revert_target_state()
                    emit({'type': 'error', 'error': f'Auth setup failed: {e}', 'request_id': request_id})
                    emit({'type': 'status', 'message': 'Provider switch interrupted. Restored previous provider for this session.', 'request_id': request_id})
            else:
                if arg == 'lmstudio':
                    detected = self._detect_lmstudio_models()
                    current_model = str(target_state.get('model') or target_state.get('provider_model') or '').strip()
                    chosen_model = ''
                    if current_model and current_model in detected:
                        chosen_model = current_model
                    elif detected:
                        chosen_model = detected[0]
                    else:
                        chosen_model = os.environ.get('CHARON_LOCAL_MODEL', '').strip() or 'qwen3-30b-a3b'

                    target_state['model'] = chosen_model
                    target_state['provider_model'] = chosen_model
                    target_state['complete'] = True
                    target_state['step'] = 'done'
                    _persist_target_state()
                    self.engine = None
                    if detected:
                        emit({'type': 'status', 'message': f'Provider set to {arg}. Auto-selected model {chosen_model}.', 'request_id': request_id})
                    else:
                        emit({'type': 'status', 'message': f'Provider set to {arg}. No local model list detected, using {chosen_model}.', 'request_id': request_id})
                    effective_onboarding = dict(onboarding)
                    effective_onboarding.update(target_state)
                    self._on_setup_complete(effective_onboarding, request_id)
                else:
                    target_state['step'] = 'model'
                    _persist_target_state()
                    emit({'type': 'status', 'message': f'Provider set to {arg}. Now run /setup model <model_name>', 'request_id': request_id})
        elif subcmd == 'model':
            provider_state = dict(onboarding)
            if session_override:
                provider_state.update(session_override)
            provider = str(provider_state.get('provider') or '').strip()
            # Known models per provider
            known_models = {
                'claude-code': [
                    # 4.6 (latest)
                    {'id': 'claude-sonnet-4-6', 'desc': 'Sonnet 4.6 — latest, fast'},
                    {'id': 'claude-opus-4-6', 'desc': 'Opus 4.6 — latest, most capable'},
                    # 4.5
                    {'id': 'claude-sonnet-4-5', 'desc': 'Sonnet 4.5'},
                    {'id': 'claude-opus-4-5', 'desc': 'Opus 4.5'},
                    # 4.1
                    {'id': 'claude-opus-4-1', 'desc': 'Opus 4.1'},
                    # 4.0
                    {'id': 'claude-sonnet-4-20250514', 'desc': 'Sonnet 4.0'},
                    {'id': 'claude-opus-4-20250514', 'desc': 'Opus 4.0'},
                    # Haiku
                    {'id': 'claude-haiku-4-5', 'desc': 'Haiku 4.5 — fastest'},
                    # 3.x
                    {'id': 'claude-3-7-sonnet-20250219', 'desc': 'Sonnet 3.7'},
                    {'id': 'claude-3-5-sonnet-20241022', 'desc': 'Sonnet 3.5 v2'},
                    {'id': 'claude-3-5-haiku-20241022', 'desc': 'Haiku 3.5'},
                ],
                'codex': [
                    {'id': 'gpt-5.4', 'desc': 'GPT 5.4 — most capable (recommended)'},
                    {'id': 'gpt-5', 'desc': 'GPT 5'},
                    # Note: o3, o4-mini, gpt-4.1, gpt-4o, codex-mini etc. are NOT supported
                    # with Codex OAuth (ChatGPT subscription). Only gpt-5 family works.
                ],
                'lmstudio': [],  # dynamic — detected from LM Studio
                'api': [],
                'opencode': [],
            }

            # For local providers, try to detect available models
            if provider == 'lmstudio' and not known_models['lmstudio']:
                try:
                    import httpx
                    resp = httpx.get('http://127.0.0.1:1234/v1/models', timeout=3.0)
                    if resp.status_code == 200:
                        for m in resp.json().get('data', []):
                            mid = m.get('id', '')
                            if mid:
                                known_models['lmstudio'].append({'id': mid, 'desc': 'Local model'})
                except Exception:
                    pass
            models = known_models.get(provider, [])

            if not arg:
                # No model specified — show model picker
                if models:
                    emit({
                        'type': 'model_picker',
                        'models': models,
                        'provider': provider,
                        'request_id': request_id,
                    })
                else:
                    emit({'type': 'error', 'error': 'Usage: /setup model <model_name>', 'request_id': request_id})
                return

            # Validate model name — warn but don't reject custom names
            model_ids = [m['id'] for m in models]
            if models and arg not in model_ids:
                close = [m for m in model_ids if arg.lower() in m.lower()]
                if close:
                    emit({'type': 'status', 'message': f'Model "{arg}" not in known list. Close matches: {", ".join(close)}', 'request_id': request_id})
                else:
                    emit({'type': 'status', 'message': f'Model "{arg}" not in known list. Using it anyway.', 'request_id': request_id})

            target_state = dict(session_override) if session_override else dict(onboarding)
            target_state['model'] = arg
            target_state['provider_model'] = arg

            # Auto-complete if project is already set (from previous setup or default)
            project = str(target_state.get('project') or onboarding.get('project') or '').strip()
            if not project:
                project = str(ROOT)
                target_state['project'] = project

            target_state['complete'] = True
            target_state['step'] = 'done'
            if session_override and self._active_agent_id:
                save_session_provider_config(STATE_DIR, self._active_agent_id, target_state)
            else:
                onboarding.update(target_state)
                self._save_onboarding(onboarding)
            self.engine = None
            emit({'type': 'status', 'message': f'✓ Model set to {arg}. Setup complete.', 'request_id': request_id})
            effective_onboarding = dict(onboarding)
            effective_onboarding.update(target_state)
            self._on_setup_complete(effective_onboarding, request_id)
        elif subcmd == 'shade-provider':
            if not arg:
                # Show shade provider picker
                emit({
                    'type': 'shade_provider_picker',
                    'options': [
                        {'id': 'same', 'desc': 'Same as main provider (default)'},
                        {'id': 'lmstudio', 'desc': 'LM Studio (local)'},
                        {'id': 'api', 'desc': 'OpenAI-compatible API'},
                    ],
                    'request_id': request_id,
                })
                return
            if arg in ('same', 'skip'):
                from model_registry import load_registry, save_registry
                reg = load_registry(STATE_DIR)
                reg['shade_model_mode'] = 'same'
                save_registry(STATE_DIR, reg)
                onboarding['step'] = 'complete'
                self._save_onboarding(onboarding)
                emit({'type': 'status', 'message': '✓ Shade will use same provider as main agent.', 'request_id': request_id})
                self._run_setup_command('complete', request_id)
            elif arg == 'api':
                onboarding['shade_provider'] = arg
                onboarding['step'] = 'shade-url'
                self._save_onboarding(onboarding)
                emit({'type': 'status', 'message': f'Shade provider set to {arg}. Now provide the base URL: /setup shade-url <url>', 'request_id': request_id})
            else:
                onboarding['shade_provider'] = arg
                onboarding['step'] = 'shade-model'
                self._save_onboarding(onboarding)
                emit({'type': 'status', 'message': f'Shade provider set to {arg}. Now pick a model: /setup shade-model <model_name>', 'request_id': request_id})
                self._run_setup_command('shade-model', request_id)
        elif subcmd == 'shade-url':
            if not arg:
                emit({'type': 'error', 'error': 'Usage: /setup shade-url <url>', 'request_id': request_id})
                return
            onboarding['shade_base_url'] = arg
            onboarding['step'] = 'shade-model'
            self._save_onboarding(onboarding)
            emit({'type': 'status', 'message': f'Shade base URL set to {arg}. Now pick a model: /setup shade-model <model_name>', 'request_id': request_id})
            self._run_setup_command('shade-model', request_id)
        elif subcmd == 'shade-model':
            shade_provider = str(onboarding.get('shade_provider') or '').strip()
            if not arg:
                # Show model picker
                if shade_provider == 'lmstudio':
                    try:
                        import httpx
                        resp = httpx.get('http://127.0.0.1:1234/v1/models', timeout=3.0)
                        if resp.status_code == 200:
                            models = [{'id': m.get('id', ''), 'desc': 'Local model'} for m in resp.json().get('data', []) if m.get('id')]
                            if models:
                                emit({'type': 'model_picker', 'models': models, 'provider': shade_provider, 'context': 'shade', 'request_id': request_id})
                                return
                    except Exception:
                        pass
                emit({'type': 'error', 'error': 'Usage: /setup shade-model <model_name>', 'request_id': request_id})
                return
            # Save model to registry
            from model_registry import load_registry, save_registry
            reg = load_registry(STATE_DIR)
            reg['shade_model_mode'] = 'fixed'
            # Parse provider/model format (e.g., lmstudio/qwen3-30b)
            if '/' in arg:
                parts = arg.split('/', 1)
                reg['shade_provider'] = parts[0]
                reg['shade_model'] = parts[1]
            else:
                reg['shade_model'] = arg
                reg['shade_provider'] = shade_provider or 'openai'
            if reg.get('shade_provider') in ('lmstudio', 'local', 'ollama'):
                reg['shade_base_url'] = 'http://127.0.0.1:1234/v1'
                reg['shade_api_key'] = 'not-needed'
            elif shade_provider == 'api':
                shade_base_url = str(onboarding.get('shade_base_url') or '').strip()
                if shade_base_url:
                    reg['shade_base_url'] = shade_base_url
            save_registry(STATE_DIR, reg)
            # Don't touch onboarding — shade config is independent
            emit({'type': 'status', 'message': f'✓ Shade model set to {arg} (provider: {reg.get("shade_provider", "auto")})', 'request_id': request_id})
            self._run_setup_command('complete', request_id)
        elif subcmd == 'project':
            onboarding['project'] = arg or str(ROOT)
            onboarding['step'] = 'complete'
            self._save_onboarding(onboarding)
            emit({'type': 'status', 'message': f'Project set to {arg or str(ROOT)}.', 'request_id': request_id})
        elif subcmd == 'auth-code':
            if not arg:
                emit({'type': 'error', 'error': 'Paste the authorization code: /setup auth-code <CODE>', 'request_id': request_id})
                return
            if not hasattr(self, '_pending_auth') or not self._pending_auth:
                emit({'type': 'error', 'error': 'No pending auth. Run /setup provider claude-code first.', 'request_id': request_id})
                return
            try:
                import charon_auth
                import urllib.parse

                pa = self._pending_auth
                provider = charon_auth.PROVIDERS[pa['provider_id']]

                # Parse the code (might be a full URL or just the code)
                code = arg.strip()
                if '?' in code:
                    parsed = urllib.parse.parse_qs(urllib.parse.urlparse(code).query)
                    code = parsed.get('code', [code])[0]
                if '#' in code:
                    parts = code.split('#', 1)
                    code = parts[0]

                emit({'type': 'status', 'message': 'Exchanging code for tokens...', 'request_id': request_id})

                token_data = charon_auth._exchange_code_json(
                    provider, code, pa['verifier'], state=pa['state'],
                )

                # Save auth
                store = charon_auth._load_auth()
                store['active_provider'] = provider.id
                store.setdefault('providers', {})
                store['providers'][provider.id] = {
                    'tokens': token_data,
                    'last_login': charon_auth._now(),
                    'auth_type': 'oauth',
                }
                charon_auth._save_auth(store)

                onboarding['provider_auth'] = 'oauth'
                onboarding['step'] = 'model'
                self._save_onboarding(onboarding)
                self._pending_auth = None
                self.engine = None

                emit({'type': 'status', 'message': '✓ Authentication successful! Now run /setup model <model_name>', 'request_id': request_id})
            except Exception as e:
                emit({'type': 'error', 'error': f'Token exchange failed: {e}', 'request_id': request_id})
                emit({'type': 'status', 'message': 'You can also set an API key directly: /setup api-key <key>', 'request_id': request_id})
        elif subcmd == 'complete':
            onboarding['complete'] = True
            onboarding['step'] = 'done'
            self._save_onboarding(onboarding)
            self.engine = None  # force re-creation with new config
            self._on_setup_complete(onboarding, request_id)
        elif subcmd in ('api-key',):
            onboarding['api_key'] = arg
            if not onboarding.get('provider'):
                onboarding['provider'] = 'api'
            onboarding['provider_mode'] = 'provider'
            self._save_onboarding(onboarding)
            self.engine = None
            emit({'type': 'status', 'message': 'API key saved.', 'request_id': request_id})
        elif subcmd == 'no-provider':
            onboarding['provider_mode'] = 'no-provider'
            onboarding['provider'] = ''
            onboarding['complete'] = True
            onboarding['step'] = 'done'
            self._save_onboarding(onboarding)
            self.engine = None
            self._on_setup_complete(onboarding, request_id)
        else:
            emit({'type': 'error', 'error': f'Unknown setup command: {subcmd}', 'request_id': request_id})

    def _repair_incomplete_onboarding_startup(self) -> dict:
        onboarding = _load_json(STATE_DIR / 'onboarding.json', {})
        if not isinstance(onboarding, dict):
            return {}
        if onboarding.get('complete') or str(onboarding.get('provider_mode') or '').strip().lower() != 'provider':
            return onboarding

        auth_store = _load_json(STATE_DIR / 'auth' / 'auth.json', {})
        providers = auth_store.get('providers', {}) if isinstance(auth_store, dict) else {}
        model = str(onboarding.get('model') or onboarding.get('provider_model') or '').strip()
        provider = str(onboarding.get('provider') or '').strip().lower()

        repaired_provider = ''
        repaired_auth = str(onboarding.get('provider_auth') or '').strip()
        if (provider == 'claude-code' or model.startswith('claude-')) and self._find_charon_auth_token('anthropic'):
            repaired_provider = 'claude-code'
            repaired_auth = repaired_auth or 'existing'
        elif (provider == 'codex' or model.startswith('gpt-') or model in ('o3', 'o4-mini', 'o3-mini', 'codex-mini-latest')) and self._find_charon_auth_token('openai-codex'):
            repaired_provider = 'codex'
            repaired_auth = repaired_auth or 'oauth'
        elif provider in ('lmstudio', 'local', 'ollama') and model and not model.startswith('claude-') and not model.startswith('gpt-') and model not in ('o3', 'o4-mini', 'o3-mini', 'codex-mini-latest'):
            repaired_provider = 'lmstudio'
            repaired_auth = repaired_auth or 'local'

        if not repaired_provider:
            return onboarding

        repaired = dict(onboarding)
        repaired['provider'] = repaired_provider
        repaired['provider_mode'] = 'provider'
        repaired['provider_auth'] = repaired_auth
        repaired['complete'] = True
        repaired['step'] = 'done'
        if model:
            repaired['model'] = model
            repaired['provider_model'] = model
        self._save_onboarding(repaired)
        emit({'type': 'status', 'message': f'Restored provider config: {repaired_provider}/{model or "(default model)"}'})
        return repaired

    def _find_charon_auth_token(self, provider_id: str) -> str | None:
        """Check Charon's own auth store for a valid token."""
        auth_file = STATE_DIR / 'auth' / 'auth.json'
        if not auth_file.exists():
            return None
        try:
            store = json.loads(auth_file.read_text())
            provider_auth = store.get('providers', {}).get(provider_id, {})
            tokens = provider_auth.get('tokens', {})
            access_token = tokens.get('access_token', '').strip()
            if access_token:
                return access_token
        except Exception:
            pass
        return None

    def _find_claude_credentials(self) -> str | None:
        """Look for existing Claude Code credentials on this machine.
        Auto-refreshes expired tokens using the refresh token.
        """
        import os, time
        cred_path = os.path.expanduser('~/.claude/.credentials.json')
        if not os.path.exists(cred_path):
            return None
        try:
            data = json.loads(open(cred_path).read())
            oauth = data.get('claudeAiOauth', {})
            token = oauth.get('accessToken', '')
            refresh_token = oauth.get('refreshToken', '')
            expires_at = oauth.get('expiresAt', 0)

            if not token or not token.startswith('sk-ant-'):
                return None

            # Check if token is expired
            now_ms = time.time() * 1000
            if expires_at and expires_at < now_ms and refresh_token:
                # Token expired — try to refresh
                refreshed = self._refresh_anthropic_token(refresh_token)
                if refreshed:
                    # Update the credentials file
                    oauth['accessToken'] = refreshed['access_token']
                    if refreshed.get('refresh_token'):
                        oauth['refreshToken'] = refreshed['refresh_token']
                    oauth['expiresAt'] = int(time.time() * 1000) + refreshed.get('expires_in', 3600) * 1000
                    data['claudeAiOauth'] = oauth
                    try:
                        with open(cred_path, 'w') as f:
                            json.dump(data, f)
                        os.chmod(cred_path, 0o600)
                    except Exception:
                        pass
                    return refreshed['access_token']
                return None  # refresh failed

            return token
        except Exception:
            pass
        return None

    def _refresh_anthropic_token(self, refresh_token: str) -> dict | None:
        """Refresh an expired Anthropic OAuth token."""
        try:
            import httpx
            resp = httpx.post(
                'https://platform.claude.com/v1/oauth/token',
                json={
                    'grant_type': 'refresh_token',
                    'client_id': '9d1c250a-e61b-44d9-88ed-5944d1962f5e',
                    'refresh_token': refresh_token,
                },
                headers={'Accept': 'application/json'},
                timeout=30.0,
            )
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            pass
        return None

    def _save_onboarding(self, state: dict):
        from datetime import datetime, timezone
        state['updated_at'] = datetime.now(timezone.utc).isoformat()
        path = STATE_DIR / 'onboarding.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state, indent=2))

    def _on_setup_complete(self, onboarding: dict, request_id: str | None):
        """Post-setup: create default agent, detect other agents, report results.

        Mirrors pi-agent behavior: once configured, you're immediately ready to go.
        """
        provider_mode = str(onboarding.get('provider_mode') or '').lower()
        provider = str(onboarding.get('provider') or '').lower()
        project = str(onboarding.get('project') or str(ROOT)).strip()
        model = str(onboarding.get('model') or onboarding.get('provider_model') or '').strip()

        results = []

        # 1. Create default agent (unless no-provider mode or agent already exists)
        agent_created = None
        if provider_mode != 'no-provider':
            try:
                from agent_lifecycle import list_agents, create_agent
                existing = list_agents()
                has_charon = any(
                    a.get('role') == 'charon' and a.get('status') != 'stopped'
                    for a in existing
                )
                if not has_charon:
                    agent_created = create_agent(
                        name='',  # auto-name: charon-<project>-01
                        mode='persistent',
                        goal=f'Primary agent for {project.split("/")[-1] or "project"}',
                        project=project,
                        role='charon',
                        visibility='user',
                        require_tmux=False,  # don't require tmux for auto-created agent
                    )
                    results.append(f'Created agent {agent_created["name"]} ({agent_created["id"]})')
                else:
                    results.append(f'Agent already exists ({len(existing)} agents)')
            except Exception as e:
                results.append(f'Agent creation failed: {e}')
        else:
            results.append('No-provider mode — skipped agent creation')

        # 2. Detect running agent processes
        detected = []
        try:
            sys.path.insert(0, str(ROOT / 'apps' / 'tui'))
            from process_inspector import detect_agent_processes, summarize_agent_processes
            procs = detect_agent_processes()
            if procs:
                detected = summarize_agent_processes(procs)
                results.append(f'Detected {len(procs)} running agent process(es)')
            else:
                results.append('No other agent processes detected')
        except Exception as e:
            results.append(f'Process detection failed: {e}')

        # 3. Sync to SQLite store
        try:
            from store_adapter import get_db, onboarding_set as db_onboarding_set
            db = get_db(STATE_DIR)
            db_onboarding_set(db, onboarding)
        except Exception:
            pass

        # 4. Emit setup complete event
        emit({
            'type': 'setup_complete',
            'provider': provider,
            'model': model,
            'agent': agent_created.get('name') if agent_created else None,
            'request_id': request_id,
        })

        # 5. Refresh the UI so dashboard shows the new agent
        self.handle_refresh(request_id)

    def handle_chat(self, message: str, request_id: str | None):
        """Handle a chat message — run through conversation engine with streaming."""
        stripped = message.strip()
        if self._pending_provider_switch and stripped in {'1', '2'}:
            self.handle_command(f'/{stripped}', request_id)
            return
        if message.startswith('/'):
            self.handle_command(message, request_id)
            return

        # Natural-language Libris trigger
        libris_match = re.match(r'^(?:start|run|launch)\s+(?:a\s+)?libris\s+(?:research\s+project|research|project)?\s*(?:on|for)?\s+(.+)$', stripped, re.I)
        if libris_match:
            topic_prompt = libris_match.group(1).strip()
            self.handle_command(f'/libris {topic_prompt}', request_id)
            return

        # Strong orchestration heuristic: if the user asks to make/spawn/start
        # agents, sessions, teams, or conversations, prefer room/session creation
        # over answering directly in chat.
        lower = stripped.lower()
        if (
            re.search(r'\b(make|create|spawn|start|launch)\b', lower)
            and 'hermes' in lower
            and ('conversation' in lower or 'discuss' in lower or 'talk' in lower)
            and 'teacher' in lower
            and 'student' in lower
        ):
            topic_prompt = ''
            for pat in [r'\b(?:about|on|in)\s+(.+)$', r'\bteaches?\s+(?:the\s+)?student\s+(.+)$']:
                m = re.search(pat, stripped, re.I)
                if m:
                    topic_prompt = m.group(1).strip().rstrip('.!?')
                    break
            if not topic_prompt:
                topic_prompt = 'open discussion'
            emit({'type': 'status', 'message': f'Routing orchestration request to /conversation hermes teacher student {topic_prompt}', 'request_id': request_id})
            self.handle_command(f'/conversation hermes teacher student {topic_prompt}', request_id)
            return

        # Natural-language Hermes team/conversation trigger
        hermes_team_match = re.match(
            r'^(?:make|create|spawn|start|launch)\s+(?:me\s+)?(?:(\d+)\s+)?(?:charons[- ]boat\s+wrapped\s+)?hermes\s+(?:sessions?|agents?|team)\s*(?:and\s+have\s+them\s+discuss(?:\s+back\s+and\s+forth)?\s+|to\s+discuss\s+|for\s+)?(.+)?$',
            stripped,
            re.I,
        )
        if hermes_team_match:
            count = int(hermes_team_match.group(1) or 2)
            topic_prompt = (hermes_team_match.group(2) or 'open discussion').strip().rstrip('.!?') or 'open discussion'
            if count == 2 and re.search(r'\b(teacher|student|teach)\b', stripped, re.I):
                emit({'type': 'status', 'message': f'Routing orchestration request to /conversation hermes teacher student {topic_prompt}', 'request_id': request_id})
                self.handle_command(f'/conversation hermes teacher student {topic_prompt}', request_id)
            else:
                emit({'type': 'status', 'message': f'Routing orchestration request to /team hermes {count} {topic_prompt}', 'request_id': request_id})
                self.handle_command(f'/team hermes {count} {topic_prompt}', request_id)
            return

        engine, error = self._ensure_engine()
        if not engine:
            emit({'type': 'error', 'error': error, 'request_id': request_id})
            return
        # _ensure_engine may assign a fresh session id; persist any in-memory
        # outcome state now that we have one.
        self._save_session_outcomes()

        # Session outcome ledger: resolve the previous active task based on
        # how the user is steering now, then start a new active task if this
        # message is a concrete request.
        if self._session_tasks:
            if self._is_ack_message(message):
                self._resolve_pending_outcome('completed')
            elif self._is_redirect_message(message):
                self._resolve_pending_outcome('failed')
            elif self._parse_intent(message):
                self._resolve_pending_outcome('completed')
        self._start_outcome_for_message(message)
        self._improve_active_outcome_title_background(message, request_id)
        emit({
            'type': 'refresh',
            'payload': {'session_info': self._get_session_info()},
            'request_id': request_id,
        })

        self.chat_history.append({'role': 'user', 'content': message})

        async def _run():
            text_parts = []
            thinking_started = False
            _tool_calls_record = []
            _total_input_tokens = 0
            _total_output_tokens = 0
            _total_turns = 0
            try:
                async for event in engine.submit(message):
                    if event.type == 'thinking_delta':
                        if not thinking_started:
                            emit({'type': 'thinking_start', 'request_id': request_id})
                            thinking_started = True
                        if self.visible_thoughts and self._thoughts_supported():
                            emit({
                                'type': 'thinking_delta',
                                'text': event.data.get('text', ''),
                                'request_id': request_id,
                            })
                    elif event.type == 'text_delta':
                        text = event.data.get('text', '')
                        text_parts.append(text)
                        emit({'type': 'chat_delta', 'text': text, 'request_id': request_id})
                    elif event.type == 'tool_call':
                        _tool_calls_record.append({
                            'tool': event.data.get('tool_name', ''),
                            'arguments': event.data.get('arguments', {}),
                        })
                        emit({
                            'type': 'tool_call',
                            'tool_name': event.data.get('tool_name', ''),
                            'arguments': event.data.get('arguments', {}),
                            'tool_call_id': event.data.get('tool_call_id', ''),
                            'request_id': request_id,
                        })
                    elif event.type == 'tool_execution_output':
                        emit({
                            'type': 'tool_result_delta',
                            'tool_name': event.data.get('tool_name', ''),
                            'content': event.data.get('content', ''),
                            'chunk': event.data.get('chunk', ''),
                            'tool_call_id': event.data.get('tool_call_id', ''),
                            'request_id': request_id,
                        })
                    elif event.type == 'tool_execution_end':
                        # Update the last tool call record with result
                        if _tool_calls_record:
                            _tool_calls_record[-1]['result'] = event.data.get('content', '')[:500]
                            _tool_calls_record[-1]['is_error'] = event.data.get('is_error', False)
                        emit({
                            'type': 'tool_result',
                            'tool_name': event.data.get('tool_name', ''),
                            'content': event.data.get('content', ''),
                            'is_error': event.data.get('is_error', False),
                            'truncated': event.data.get('truncated', False),
                            'tool_call_id': event.data.get('tool_call_id', ''),
                            'request_id': request_id,
                        })
                    elif event.type == 'turn_end':
                        _total_turns += 1
                    elif event.type == 'message_end':
                        usage = event.data.get('usage', {})
                        input_tokens = int(usage.get('input_tokens', 0) or 0)
                        output_tokens = int(usage.get('output_tokens', 0) or 0)

                        # Fallback for providers like LM Studio that may not emit usage
                        # in streamed responses: estimate from current context + response.
                        if self.engine and input_tokens <= 0:
                            try:
                                input_tokens = sum(
                                    len((getattr(m, 'content', '') or '')) // 4
                                    for m in self.engine.messages[:-1]
                                )
                            except Exception:
                                input_tokens = 0
                        if output_tokens <= 0:
                            output_tokens = len((event.data.get('content', '') or '')) // 4

                        _total_input_tokens += input_tokens
                        _total_output_tokens += output_tokens

                        # Context = input + output tokens from this request
                        # (input_tokens = entire context sent to the API)
                        context_tokens = input_tokens + output_tokens
                        context_pct = 0
                        context_window = 200000

                        if self.engine:
                            try:
                                context_window = int(getattr(self.engine.model, 'context_window', 200000) or 200000)
                            except Exception:
                                context_window = 200000

                        if context_tokens > 0:
                            context_pct = min(100, int(context_tokens * 100 / max(1, context_window)))
                        elif self.engine:
                            # Fallback: estimate from message content
                            msg_tokens = sum(
                                len(getattr(m, 'content', '') or '') // 4
                                for m in self.engine.messages
                            )
                            context_pct = min(100, int(msg_tokens * 100 / max(1, context_window)))
                            context_tokens = msg_tokens

                        emit({
                            'type': 'usage',
                            'input_tokens': input_tokens,
                            'output_tokens': output_tokens,
                            'context_tokens': context_tokens,
                            'context_pct': context_pct,
                            'context_window': context_window,
                            'request_id': request_id,
                        })
                    elif event.type == 'turn_end':
                        emit({
                            'type': 'turn_complete',
                            'stop_reason': event.data.get('stop_reason', ''),
                            'turn': event.data.get('turn', 0),
                            'request_id': request_id,
                        })
                    elif event.type == 'error':
                        emit({
                            'type': 'error',
                            'error': event.data.get('error', 'unknown error'),
                            'request_id': request_id,
                        })
                    elif event.type == 'steer_delivered':
                        emit({
                            'type': 'steer_delivered',
                            'content': event.data.get('content', ''),
                            'skipped_tools': event.data.get('skipped_tools', 0),
                            'request_id': request_id,
                        })
                    elif event.type == 'follow_up_delivered':
                        emit({
                            'type': 'follow_up_delivered',
                            'content': event.data.get('content', ''),
                            'request_id': request_id,
                        })
                    elif event.type == 'retry':
                        attempt = event.data.get('attempt', 1)
                        wait = event.data.get('wait_seconds', 3)
                        emit({'type': 'status', 'message': f'⟳ Retrying (attempt {attempt}/2, waiting {wait}s)...', 'request_id': request_id})
                    elif event.type == 'compaction_start':
                        emit({'type': 'status', 'message': 'Compacting context...', 'request_id': request_id})
                    elif event.type == 'compaction_end':
                        emit({'type': 'status', 'message': 'Context compacted.', 'request_id': request_id})

            except Exception as e:
                emit({'type': 'error', 'error': str(e), 'request_id': request_id})

            full_text = ''.join(text_parts)
            self.chat_history.append({'role': 'assistant', 'content': full_text})

            # Record task in working memory + task queue (zero LLM cost)
            if self._active_agent_id and engine:
                try:
                    from task_summarizer import summarize_fast
                    from agent_runtime import update_working_memory
                    from execution_memory import create_task_episode
                    import uuid as _uuid

                    task_id = f'chat-{_uuid.uuid4().hex[:8]}'
                    # Get the user message that triggered this
                    user_msg = message[:200] if message else ''

                    summary = summarize_fast(
                        instruction=user_msg,
                        tool_calls=_tool_calls_record,
                        response_text=full_text,
                        errors=[],
                        total_turns=_total_turns,
                    )

                    # Only write to persistent agent memory when this session
                    # was explicitly bound to a persistent agent. Fresh sessions
                    # should not silently share working memory.
                    memory_agent_id = getattr(self, '_bound_agent_id', None)
                    if memory_agent_id:
                        update_working_memory(
                            STATE_DIR, memory_agent_id,
                            task_id=task_id, summary=summary,
                        )

                    # Update the session outcome ledger entry for this task.
                    if not hasattr(self, '_session_tasks'):
                        self._session_tasks = []
                    files_touched = []
                    try:
                        for tc in _tool_calls_record:
                            args = tc.get('arguments', {}) or {}
                            for key in ('path', 'oldText', 'newText'):
                                val = args.get(key)
                                if key == 'path' and isinstance(val, str) and val and val not in files_touched:
                                    files_touched.append(val)
                    except Exception:
                        files_touched = []

                    updated = False
                    for item in reversed(self._session_tasks):
                        if item.get('status') == 'active':
                            item['summary'] = summary
                            item['detail'] = (
                                f'Task: {user_msg[:100]}\n'
                                f'Outcome: {item.get("title", "")}\n'
                                f'Result: {summary}\n'
                                f'Tools: {len(_tool_calls_record)} calls, {_total_turns} turns\n'
                                f'Tokens: {_total_input_tokens}↑ {_total_output_tokens}↓'
                            )
                            item['tokens_in'] = _total_input_tokens
                            item['tokens_out'] = _total_output_tokens
                            item['tool_calls'] = len(_tool_calls_record)
                            item['turns'] = _total_turns
                            item['files_touched'] = files_touched
                            item['ts'] = time.time()
                            updated = True
                            break
                    if not updated and self._parse_intent(user_msg):
                        self._start_outcome_for_message(user_msg)
                        if self._session_tasks:
                            self._session_tasks[-1]['summary'] = summary
                            self._session_tasks[-1]['detail'] = f'Task: {user_msg[:100]}\nResult: {summary}'
                            self._session_tasks[-1]['tokens_in'] = _total_input_tokens
                            self._session_tasks[-1]['tokens_out'] = _total_output_tokens
                            self._session_tasks[-1]['tool_calls'] = len(_tool_calls_record)
                            self._session_tasks[-1]['turns'] = _total_turns
                            self._session_tasks[-1]['files_touched'] = files_touched
                    self._save_session_outcomes()
                    emit({
                        'type': 'refresh',
                        'payload': {'session_info': self._get_session_info()},
                        'request_id': request_id,
                    })

                    try:
                        create_task_episode(
                            STATE_DIR,
                            session_id=self._active_agent_id,
                            agent_id=memory_agent_id or '',
                            project_root=str(engine.project_root),
                            provider=str(self._current_provider_name() or getattr(engine, 'provider_name', 'unknown')),
                            objective=user_msg,
                            summary=summary,
                            tool_calls=_tool_calls_record,
                            response_text=full_text,
                            total_turns=_total_turns,
                            input_tokens=_total_input_tokens,
                            output_tokens=_total_output_tokens,
                        )
                    except Exception:
                        pass
                except Exception:
                    pass

            # Persist conversation
            if self._active_agent_id and engine:
                try:
                    from conversation_store import save_conversation, message_to_dict
                    save_conversation(STATE_DIR, self._active_agent_id,
                        [message_to_dict(m) for m in engine.messages])
                    # Register session on first save (not on startup)
                    if not hasattr(self, '_session_registered'):
                        self._session_registered = True
                        try:
                            from session_registry import register_session
                            register_session(STATE_DIR, self._active_agent_id)
                        except Exception:
                            pass
                except Exception:
                    pass

            emit({
                'type': 'chat_complete',
                'summary': full_text[:200],
                'request_id': request_id,
            })

            # Write to persistent agent inbox only for explicitly bound agents.
            try:
                bound_agent_id = getattr(self, '_bound_agent_id', None)
                if bound_agent_id:
                    from store_adapter import get_db
                    from libs.store import agent_inbox_push
                    db = get_db(STATE_DIR)
                    agent_inbox_push(db, bound_agent_id,
                        event_type='task_received',
                        payload={'instruction': message[:200], 'summary': full_text[:200]})
            except Exception:
                pass

        asyncio.run(_run())

    def _get_session_info(self) -> dict:
        """Build session info for the right-side pane.
        
        Three tabs:
        1. Session outcome ledger
        2. Estimated goal structure
        3. User model
        Plus token usage breakdown at the bottom.
        """
        info = {
            'tasks': [],
            'goals': [],
            'goal_summary': {
                'active_goal_id': '',
                'session_total': 0,
                'project_total': 0,
                'proposed': 0,
                'confirmed': 0,
                'executing': 0,
                'verifying': 0,
                'active': 0,
                'backlog': 0,
                'blocked': 0,
                'completed_recent': 0,
                'failed_recent': 0,
            },
            'user_model': '',
            'transfer': {},
            'binding': {
                'session_id': self._active_agent_id or '',
                'agent_id': getattr(self, '_bound_agent_id', None) or '',
                'mode': 'bound-agent' if getattr(self, '_bound_agent_id', None) else 'fresh-session',
            },
            'tokens': {
                'chat_in': 0,
                'chat_out': 0,
                'summary_tokens': 0,
                'goal_inference_tokens': int(getattr(self, '_goal_inference_token_estimate', 0) or 0),
                'consolidation_tokens': 0,
                'max_context': 0,
            },
        }

        # Session-local outcome ledger
        if hasattr(self, '_session_tasks'):
            info['tasks'] = self._session_tasks[-50:]
            info['tokens']['chat_in'] = sum(t.get('tokens_in', 0) for t in self._session_tasks)
            info['tokens']['chat_out'] = sum(t.get('tokens_out', 0) for t in self._session_tasks)

        try:
            if self.engine and getattr(self.engine, 'model', None):
                info['tokens']['max_context'] = int(getattr(self.engine.model, 'context_window', 0) or 0)
        except Exception:
            pass

        # Goals: session-level + project-level
        try:
            from goal_runtime import list_goals, _safe_id, _read_json, _session_path, _default_session_doc
            onboarding = _load_json(STATE_DIR / 'onboarding.json', {})
            project = str(onboarding.get('project') or str(ROOT)).strip()
            import time as _time
            cutoff = _time.time() - 86400

            # Session goals (current session)
            if self._active_agent_id:
                session_id = _safe_id(self._active_agent_id, 'session')
                ses_doc = _read_json(_session_path(STATE_DIR, session_id), {})
                ses_goals = [g for g in (ses_doc.get('goals') or []) if isinstance(g, dict)]
                info['goal_summary']['active_goal_id'] = str(ses_doc.get('active_goal_id') or '')
                info['goal_summary']['session_total'] = len(ses_goals)
                for g in ses_goals[-10:]:
                    info['goals'].append({
                        'id': g.get('goal_id', ''),
                        'title': g.get('title', '')[:80],
                        'status': g.get('status', ''),
                        'intent_type': g.get('intent_type', ''),
                        'criteria': g.get('acceptance_criteria', []),
                        'scope': 'session',
                    })

            # Project goals (active/recent only, skip duplicates from session)
            session_ids = {g['id'] for g in info['goals']}
            all_goals = list_goals(STATE_DIR, project=project)
            info['goal_summary']['project_total'] = len(all_goals)
            for g in all_goals:
                status = str(g.get('status') or '')
                if status == 'proposed':
                    info['goal_summary']['proposed'] += 1
                elif status == 'confirmed':
                    info['goal_summary']['confirmed'] += 1
                elif status == 'executing':
                    info['goal_summary']['executing'] += 1
                elif status == 'verifying':
                    info['goal_summary']['verifying'] += 1
                elif status == 'active':
                    info['goal_summary']['active'] += 1
                elif status == 'backlog':
                    info['goal_summary']['backlog'] += 1
                elif status == 'blocked':
                    info['goal_summary']['blocked'] += 1
                elif status == 'completed':
                    info['goal_summary']['completed_recent'] += 1
                elif status == 'failed':
                    info['goal_summary']['failed_recent'] += 1
            stale_cutoff = _time.time() - 7 * 86400  # 7 days
            stale_iso = __import__('datetime').datetime.fromtimestamp(stale_cutoff).isoformat()
            all_goals = [g for g in all_goals if 
                g.get('goal_id', '') not in session_ids and (
                    (g.get('status') in ('active', 'backlog', 'proposed', 'confirmed') 
                     and g.get('created_at', '') > stale_iso) or
                    (g.get('status') == 'completed' and g.get('completed_at', '') > 
                        __import__('datetime').datetime.fromtimestamp(cutoff).isoformat())
                )]
            for g in all_goals[-10:]:
                info['goals'].append({
                    'id': g.get('goal_id', ''),
                    'title': g.get('title', '')[:80],
                    'status': g.get('status', ''),
                    'intent_type': g.get('intent_type', ''),
                    'criteria': g.get('acceptance_criteria', []),
                    'scope': 'project',
                })
        except Exception:
            pass

        # User model (rendered)
        try:
            from user_model_structured import load_structured, render_for_prompt
            model = load_structured(STATE_DIR)
            info['user_model'] = render_for_prompt(model)
        except Exception:
            pass

        # Active transfer metadata, if current engine was resumed via transfer
        try:
            if self.engine and getattr(self.engine, 'transfer_bundle', None):
                bundle = self.engine.transfer_bundle
                compiled = getattr(self.engine, 'transfer_compiled', {}) or {}
                info['transfer'] = {
                    'id': bundle.get('id', ''),
                    'source_provider': bundle.get('source', {}).get('provider', ''),
                    'target_provider': bundle.get('target', {}).get('provider', ''),
                    'full_message_count': bundle.get('history', {}).get('full_message_count', 0),
                    'full_transcript_path': bundle.get('history', {}).get('full_transcript_path', ''),
                    'profile_name': compiled.get('profile_name', ''),
                    'tier': compiled.get('tier', ''),
                    'budget_tokens': compiled.get('budget_tokens', 0),
                    'applied_tokens_estimate': compiled.get('applied_tokens_estimate', 0),
                    'replayed_messages': compiled.get('replayed_messages', 0),
                    'tool_history_mode': compiled.get('strategy', {}).get('tool_history_mode', ''),
                    'message_mode': compiled.get('strategy', {}).get('message_mode', ''),
                    'omitted': compiled.get('omitted', {}),
                }
        except Exception:
            pass

        # Token usage from consolidation traces
        try:
            from consolidation import list_traces
            traces = list_traces(STATE_DIR, limit=10)
            # Rough estimate: each consolidation uses ~1K tokens
            info['tokens']['consolidation_tokens'] = len(traces) * 1000
        except Exception:
            pass

        return info

    def _get_batch_progress(self) -> str:
        """Short progress string for active batches."""
        try:
            from batch_orchestrator import list_batches
            running = [b for b in list_batches(STATE_DIR) if b.get('status') == 'running']
            if not running:
                return ''
            total_done = sum(b.get('completed_count', 0) for b in running)
            total_all = sum(b.get('total', 0) for b in running)
            total_failed = sum(b.get('failed_count', 0) for b in running)
            parts = [f'({total_done}/{total_all})']
            if total_failed:
                parts.append(f'{total_failed}✗')
            if len(running) > 1:
                parts.append(f'{len(running)} batches')
            return ' '.join(parts)
        except Exception:
            return ''

    def _chat_worker(self, message: str, request_id: str | None):
        """Run handle_chat on a worker thread."""
        try:
            self.handle_chat(message, request_id)
        finally:
            self._chat_busy = False

    def _start_background_worker(self):
        """Start a daemon thread that runs periodic background tasks.

        Runs consolidation, goal inference, and emits heartbeat events
        even while the chat engine is busy processing a message.
        """
        def _bg_loop():
            import time as _time
            cycle = 0
            consolidation_interval = 50   # ~100 seconds
            goal_inference_interval = 30  # ~60 seconds
            last_consolidation = 0
            last_goal_inference = 0

            while True:
                _time.sleep(2)
                cycle += 1

                # Heartbeat event for the run log (so dashboard activity picks it up)
                if cycle % 30 == 0:
                    try:
                        from store_adapter import get_db
                        from libs.store import run_log_append
                        db = get_db(STATE_DIR)
                        run_log_append(db, 'heartbeat', cycle=cycle,
                                       uptime_seconds=cycle * 2)
                    except Exception:
                        pass

                # Consolidation check
                if cycle - last_consolidation >= consolidation_interval:
                    last_consolidation = cycle
                    try:
                        from consolidation import load_config, should_run, run_consolidation
                        config = load_config(STATE_DIR)
                        if config.get('enabled', True) and should_run(STATE_DIR, config):
                            result = run_consolidation(STATE_DIR, config)
                            changes = result.get('changes', [])
                            if changes:
                                emit({
                                    'type': 'status',
                                    'message': f'🧠 User model updated: {len(changes)} change(s)',
                                })
                    except Exception:
                        pass

                # Goal inference — always runs when there are enough messages
                # (independent of autonomous mode, which controls self-assignment)
                if cycle - last_goal_inference >= goal_inference_interval:
                    last_goal_inference = cycle
                    try:
                        from autonomous import (
                            load_autonomous_config, infer_goals_from_conversation,
                            propose_goal, get_proposed_goals,
                        )
                        auto_config = load_autonomous_config(STATE_DIR)
                        if (self.engine and
                                len(self.engine.messages) >= 4 and
                                not self._chat_busy):
                            # Only infer if there are enough messages and we're not mid-chat
                            import asyncio as _aio
                            onboarding = _load_json(STATE_DIR / 'onboarding.json', {})
                            project = str(onboarding.get('project') or str(ROOT)).strip()

                            # Check if we already have proposed goals waiting
                            existing_proposed = get_proposed_goals(STATE_DIR, project=project)
                            if len(existing_proposed) < 3:  # don't spam proposals
                                from provider_bridge import create_provider_and_model
                                provider, model, ready = create_provider_and_model(STATE_DIR)
                                if ready:
                                    self._goal_inference_token_estimate += 1000
                                    goals = _aio.run(infer_goals_from_conversation(
                                        STATE_DIR,
                                        agent_id=self._active_agent_id or '',
                                        messages=self.engine.messages,
                                        provider=provider,
                                        model=model,
                                    ))
                                    for g in goals[:2]:  # max 2 proposals per cycle
                                        proposed = propose_goal(
                                            STATE_DIR,
                                            agent_id=self._active_agent_id or '',
                                            project=project,
                                            title=g.get('title', ''),
                                            acceptance_criteria=g.get('acceptance_criteria', []),
                                            plan=g.get('plan', []),
                                        )
                                        emit({
                                            'type': 'status',
                                            'message': (
                                                f'💡 Goal proposed: {proposed["title"][:80]}\n'
                                                f'   /confirm to approve, /reject to defer'
                                            ),
                                        })
                                        emit({'type': 'refresh', 'payload': {'session_info': self._get_session_info()}})
                    except Exception:
                        pass

                # Process queued shade phase + cron tasks (when not chatting)
                if cycle % 3 == 0 and not self._chat_busy:
                    try:
                        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
                        import uuid as _uuid
                        from conversation_runtime import load_queue, save_queue
                        queue = load_queue(STATE_DIR)
                        now_iso = _dt.now(_tz.utc).isoformat()

                        def _is_due(t: dict) -> bool:
                            nb = t.get('not_before')
                            return (not nb) or (str(nb) <= now_iso)

                        def _is_cron_task(t: dict) -> bool:
                            return str(t.get('correlation_id') or '').startswith('cron:')

                        pending = [
                            t for t in queue
                            if t.get('status') == 'pending'
                            and _is_due(t)
                            and (t.get('shade_phase') or _is_cron_task(t))
                        ]

                        if pending:
                            task = pending[0]
                            agent_id = task.get('owner_agent_id') or task.get('actor_agent_id', '')
                            if agent_id:
                                from agent_lifecycle import list_agents
                                agent = None
                                for a in list_agents():
                                    if a.get('id') == agent_id:
                                        agent = a
                                        break
                                if agent:
                                    task['status'] = 'in_progress'
                                    task['started_at'] = now_iso
                                    save_queue(STATE_DIR, queue)

                                    from agent_runtime import run_task_tick
                                    ok, result = run_task_tick(STATE_DIR, task, agent=agent)

                                    task['status'] = 'completed' if ok else 'failed'
                                    if ok:
                                        task['result_summary'] = (result or {}).get('summary', '')
                                    else:
                                        task['last_error'] = result
                                    task['completed_at'] = _dt.now(_tz.utc).isoformat()
                                    task['updated_at'] = task['completed_at']
                                    save_queue(STATE_DIR, queue)

                                    # TUI-side recurrence for cron tasks, mirroring charon_loop behavior.
                                    if ok and _is_cron_task(task):
                                        interval = task.get('interval_minutes')
                                        if interval and isinstance(interval, (int, float)) and interval > 0:
                                            next_run = _dt.now(_tz.utc) + _td(minutes=float(interval))
                                            recurring_copy = {
                                                'id': f"{str(task.get('id') or 'task').split('-')[0]}-{_uuid.uuid4().hex[:8]}",
                                                'title': task.get('title', ''),
                                                'instruction': task.get('instruction', ''),
                                                'status': 'pending',
                                                'task_type': task.get('task_type', 'agent_task'),
                                                'owner_agent_id': task.get('owner_agent_id'),
                                                'actor_agent_id': task.get('actor_agent_id'),
                                                'project': task.get('project'),
                                                'priority': task.get('priority', 'normal'),
                                                'created_at': _dt.now(_tz.utc).isoformat(),
                                                'updated_at': _dt.now(_tz.utc).isoformat(),
                                                'attempt_count': 0,
                                                'max_attempts': int(task.get('max_attempts') or 3),
                                                'interval_minutes': interval,
                                                'not_before': next_run.isoformat(),
                                                'correlation_id': task.get('correlation_id'),
                                                'constraints': task.get('constraints') or [],
                                                'expected_outputs': task.get('expected_outputs') or [],
                                            }
                                            queue.append(recurring_copy)
                                            save_queue(STATE_DIR, queue)
                    except Exception:
                        pass

                # Monitor batches and report completion
                if cycle % 5 == 0:  # check every 10 seconds
                    try:
                        from batch_orchestrator import list_batches, summarize_batch, get_batch
                        # _notified_batches is pre-populated at startup

                        all_batches = list_batches(STATE_DIR)
                        has_running = False
                        for b in all_batches:
                            bid = b.get('id', '')
                            status = b.get('status', '')

                            if status == 'running':
                                has_running = True

                            # Notify on completion (only once per batch)
                            if status in ('completed', 'partial') and bid not in self._notified_batches:
                                self._notified_batches.add(bid)
                                done = b.get('completed_count', 0)
                                failed = b.get('failed_count', 0)
                                total = b.get('total', 0)

                                # Build per-task results
                                lines = [f'⚡ Batch complete: {summarize_batch(b)}']
                                for t in b.get('tasks', []):
                                    icon = '✓' if t.get('status') == 'completed' else '✗'
                                    summary = (t.get('result_summary') or t.get('error') or '')[:60]
                                    lines.append(f'  {icon} {t.get("title", "")}: {summary}')

                                emit({'type': 'status', 'message': '\n'.join(lines)})

                        if not has_running and self.agent_mode == 'delegating':
                            self.agent_mode = 'interactive'
                    except Exception:
                        pass

                    # Update agent mode based on state
                    try:
                        from batch_orchestrator import list_batches
                        from autonomous import load_autonomous_config
                        running_batches = list_batches(STATE_DIR, status='running')
                        auto_cfg = load_autonomous_config(STATE_DIR)

                        if self._chat_busy:
                            # User is actively chatting — always interactive
                            # (batch progress still shows separately in status bar)
                            self.agent_mode = 'interactive'
                        elif running_batches:
                            self.agent_mode = 'delegating'
                        elif auto_cfg.get('enabled'):
                            self.agent_mode = 'autonomous'
                        else:
                            self.agent_mode = 'idle' if not self.engine else 'interactive'
                    except Exception:
                        pass

        t = threading.Thread(target=_bg_loop, daemon=True)
        t.start()

    def _save_conversation_now(self):
        """Save current conversation state immediately (called on exit)."""
        if self._active_agent_id and self.engine and self.engine.messages:
            try:
                from conversation_store import save_conversation, message_to_dict
                save_conversation(STATE_DIR, self._active_agent_id,
                    [message_to_dict(m) for m in self.engine.messages])
            except Exception:
                pass
        # Unregister live session
        if self._active_agent_id:
            try:
                from session_registry import unregister_session
                unregister_session(STATE_DIR, self._active_agent_id)
            except Exception:
                pass

    def handle_abort(self, request_id: str | None):
        stopped_tool = False
        try:
            from tools import abort_running_bash
            stopped_tool = abort_running_bash()
        except Exception:
            stopped_tool = False
        if self.engine:
            self.engine.abort()
            msg = 'Aborted current run.'
            if stopped_tool:
                msg += ' Active bash command killed.'
            emit({'type': 'status', 'message': msg, 'request_id': request_id})
        elif stopped_tool:
            emit({'type': 'status', 'message': 'Killed active bash command.', 'request_id': request_id})

    def handle_steer(self, message: str, request_id: str | None):
        """Interrupt the agent mid-execution with a new instruction."""
        if self.engine:
            self.engine.steer(message)
            emit({'type': 'steer_queued', 'message': message,
                  'pending': self.engine.pending_messages,
                  'request_id': request_id})
        else:
            emit({'type': 'error', 'error': 'No active engine to steer.',
                  'request_id': request_id})

    def handle_follow_up(self, message: str, request_id: str | None):
        """Queue a message for after the agent finishes."""
        if self.engine:
            self.engine.follow_up(message)
            emit({'type': 'follow_up_queued', 'message': message,
                  'pending': self.engine.pending_messages,
                  'request_id': request_id})
        else:
            emit({'type': 'error', 'error': 'No active engine for follow-up.',
                  'request_id': request_id})

    def run(self):
        # Check if already set up — auto-initialize if so
        onboarding = self._repair_incomplete_onboarding_startup()
        requested_provider = os.environ.get('CHARON_PROVIDER', '').strip()
        requested_resume = os.environ.get('CHARON_RESUME', '').strip()
        requested_agent = os.environ.get('CHARON_AGENT', '').strip()

        # Resume a specific agent's conversation
        if requested_resume:
            try:
                from conversation_store import load_conversation, list_conversations
                if requested_resume == 'latest':
                    convos = list_conversations(STATE_DIR)
                    if convos:
                        convos.sort(key=lambda c: c.get('last_timestamp', 0), reverse=True)
                        requested_resume = convos[0]['agent_id']
                if requested_resume and requested_resume != 'latest':
                    self._active_agent_id = requested_resume
                    self._load_tasks_from_ledger(requested_resume)
                    emit({'type': 'status', 'message': f'Resuming conversation with {requested_resume}...'})
            except Exception:
                pass

        if onboarding.get('complete') and not requested_provider:
            # Already configured, no specific provider requested — ensure engine is ready
            try:
                self._ensure_engine()
                if requested_agent:
                    emit({'type': 'status', 'message': f'Started fresh session bound to agent {requested_agent}.'})
                elif not requested_resume:
                    emit({'type': 'status', 'message': 'Started fresh session (not resumed, no agent binding).'})
            except Exception:
                pass
            # Silently ensure an agent exists
            try:
                from agent_lifecycle import list_agents, create_agent
                existing = list_agents()
                has_charon = any(
                    a.get('role') == 'charon' and a.get('status') != 'stopped'
                    for a in existing
                )
                if not has_charon:
                    project = str(onboarding.get('project') or str(ROOT)).strip()
                    create_agent(
                        name='', mode='persistent',
                        goal=f'Primary agent for {project.split("/")[-1] or "project"}',
                        project=project, role='charon', visibility='user',
                        require_tmux=False,
                    )
            except Exception:
                pass
        elif requested_provider:
            # Specific provider requested (e.g. charon claude-code)
            # Auto-start onboarding for that provider
            emit({'type': 'status', 'message': f'Starting with provider: {requested_provider}'})
            self.handle_command(f'/setup provider {requested_provider}', None)
        self.handle_refresh(None)

        # Pre-populate notified batches so old completions don't spam on startup
        try:
            from batch_orchestrator import list_batches
            for b in list_batches(STATE_DIR):
                if b.get('status') in ('completed', 'partial'):
                    self._notified_batches.add(b.get('id', ''))
        except Exception:
            pass

        # Save conversation on exit
        import atexit
        atexit.register(self._save_conversation_now)

        # Start background worker for consolidation, goal inference, etc.
        self._chat_busy = False
        self._start_background_worker()

        while True:
            try:
                line = sys.stdin.buffer.readline()
            except (EOFError, KeyboardInterrupt):
                self._save_conversation_now()
                break
            if not line:
                self._save_conversation_now()
                break

            try:
                msg = json.loads(line.decode('utf-8'))
            except Exception:
                continue

            req_type = msg.get('type', '')
            request_id = msg.get('request_id')

            if req_type == 'chat':
                # Run chat on a worker thread so main loop stays responsive
                self._chat_busy = True
                t = threading.Thread(target=self._chat_worker, args=(msg.get('message', ''), request_id), daemon=True)
                t.start()
            elif req_type == 'command':
                self.handle_command(msg.get('command', ''), request_id)
            elif req_type == 'refresh':
                self.handle_refresh(request_id)
            elif req_type == 'abort':
                self.handle_abort(request_id)
            elif req_type == 'task_detail':
                task_id = msg.get('task_id', '')
                for t in getattr(self, '_session_tasks', []):
                    if t.get('task_id') == task_id:
                        emit({
                            'type': 'task_detail',
                            'task_id': task_id,
                            'detail': t.get('detail', t.get('summary', '')),
                            'request_id': request_id,
                        })
                        break
            elif req_type == 'approval_response':
                try:
                    from tools import respond_to_approval
                    respond_to_approval(msg.get('approved', False))
                except Exception:
                    pass
            elif req_type == 'steer':
                self.handle_steer(msg.get('message', ''), request_id)
            elif req_type == 'follow_up':
                self.handle_follow_up(msg.get('message', ''), request_id)
            elif req_type == 'send_steer':
                target = msg.get('target_session', '')
                steer_msg = msg.get('message', '')
                if target and steer_msg:
                    try:
                        from session_registry import send_steer
                        ok = send_steer(STATE_DIR, target, steer_msg)
                        emit({'type': 'status', 'message': f'📡 Sent to {target.split("-")[-1][:6]}: {steer_msg[:40]}', 'request_id': request_id})
                    except Exception as e:
                        emit({'type': 'error', 'error': f'Steer failed: {e}', 'request_id': request_id})
            elif req_type == 'live_conv':
                # Load conversation preview for a live session
                session_id = msg.get('session_id', '')
                if session_id:
                    try:
                        from conversation_store import load_conversation
                        msgs = load_conversation(STATE_DIR, session_id)
                        # Format conversation with tool calls, streaming feel
                        preview_lines = []
                        for m in msgs[-30:]:
                            role = m.get('role', '')
                            content = m.get('content', '')
                            tool_calls = m.get('tool_calls', [])
                            if role == 'user' and content:
                                preview_lines.append('')
                                for line in content.split('\n'):
                                    preview_lines.append(f'❯ {line}')
                            elif role == 'assistant':
                                if content:
                                    preview_lines.append('')
                                    for line in content.split('\n'):
                                        preview_lines.append(f'  {line}')
                                for tc in tool_calls:
                                    name = tc.get('name', '')
                                    args = tc.get('arguments', {})
                                    if name == 'Bash':
                                        preview_lines.append(f'  ⚡ {name}  {str(args.get("command",""))[:50]}')
                                    elif name == 'Read':
                                        preview_lines.append(f'  📄 {name}  {args.get("path","")}')
                                    elif name == 'Write':
                                        preview_lines.append(f'  ✏️ {name}  {args.get("path","")}')
                                    elif name == 'Edit':
                                        preview_lines.append(f'  🔧 {name}  {args.get("path","")}')
                                    else:
                                        preview_lines.append(f'  ⚙ {name}')
                            elif role == 'tool_result':
                                tool = m.get('tool_name', '')
                                is_err = m.get('is_error', False)
                                first_line = (content or '').split('\n')[0][:50]
                                icon = '✗' if is_err else '✓'
                                preview_lines.append(f'    {icon} {first_line}')
                        emit({
                            'type': 'live_conv',
                            'session_id': session_id,
                            'preview': '\n'.join(preview_lines[-40:]),
                            'message_count': len(msgs),
                            'request_id': request_id,
                        })
                    except Exception:
                        pass
            elif req_type == 'tmux_capture':
                self.handle_tmux_capture(msg.get('session', ''), request_id)
            elif req_type == 'tmux_send':
                self.handle_tmux_send(msg.get('session', ''), msg.get('keys', ''), msg.get('literal', False), request_id)
            elif req_type == 'consolidation_traces':
                self.handle_consolidation_traces(request_id)
            elif req_type == 'consolidation_config':
                self.handle_consolidation_config(msg, request_id)
            elif req_type == 'consolidation_run':
                self.handle_consolidation_run(request_id)
            elif req_type == 'agent_ledger':
                self.handle_agent_ledger(msg.get('agent_id', ''), request_id)


    def handle_consolidation_traces(self, request_id: str | None):
        """Return recent consolidation scan traces for dashboard display."""
        try:
            from consolidation import list_traces
            traces = list_traces(STATE_DIR, limit=20)
            emit({
                'type': 'consolidation_traces',
                'traces': traces,
                'request_id': request_id,
            })
        except Exception as e:
            emit({'type': 'error', 'error': f'Failed to load traces: {e}', 'request_id': request_id})

    def handle_consolidation_config(self, msg: dict, request_id: str | None):
        """Get or update consolidation config."""
        from consolidation import load_config, save_config
        action = msg.get('action', 'get')
        if action == 'get':
            config = load_config(STATE_DIR)
            emit({
                'type': 'consolidation_config',
                'config': config,
                'request_id': request_id,
            })
        elif action == 'set':
            updates = msg.get('config', {})
            config = load_config(STATE_DIR)
            config.update(updates)
            save_config(STATE_DIR, config)
            emit({
                'type': 'consolidation_config',
                'config': config,
                'message': 'Config updated.',
                'request_id': request_id,
            })

    def handle_agent_ledger(self, agent_id: str, request_id: str | None):
        """Return task history for an agent."""
        if not agent_id:
            # Default to the primary charon agent
            try:
                from agent_lifecycle import list_agents
                for a in list_agents():
                    if a.get('role') == 'charon' and a.get('status') != 'stopped':
                        agent_id = a.get('id', '')
                        break
            except Exception:
                pass
        try:
            from task_ledger import get_agent_ledger_summary
            result = get_agent_ledger_summary(STATE_DIR, agent_id)
            emit({
                'type': 'agent_ledger',
                'agent_id': agent_id,
                'entries': result['entries'],
                'stats': result['stats'],
                'request_id': request_id,
            })
        except Exception as e:
            emit({'type': 'error', 'error': f'Ledger failed: {e}', 'request_id': request_id})

    def handle_consolidation_run(self, request_id: str | None):
        """Manually trigger a consolidation scan."""
        try:
            from consolidation import load_config, run_consolidation
            config = load_config(STATE_DIR)
            result = run_consolidation(STATE_DIR, config)
            changes = result.get('changes', [])
            emit({
                'type': 'consolidation_result',
                'trace': result,
                'message': f'Consolidation complete: {len(changes)} changes, {result.get("events_processed", 0)} events processed.',
                'request_id': request_id,
            })
        except Exception as e:
            emit({'type': 'error', 'error': f'Consolidation failed: {e}', 'request_id': request_id})

    def _detect_session_state(self, content: str) -> tuple[str, str]:
        """Heuristic: detect session state and generate summary from tmux content.
        Returns (state, summary).
        """
        lines = [l.strip() for l in content.splitlines() if l.strip()]
        if not lines:
            return 'idle', 'empty session'

        last_lines = lines[-5:]
        last_text = ' '.join(last_lines).lower()

        # Waiting for input?
        if any(p in last_text for p in ['[y/n]', '(y/n)', 'confirm', 'approve', 'continue?', 'proceed?']):
            return 'waiting', 'waiting for confirmation'
        if last_text.rstrip().endswith('?'):
            return 'waiting', 'question pending'

        # Error?
        if any(p in last_text for p in ['error:', 'failed', 'traceback', 'exception', 'panic']):
            return 'running', 'error detected'

        # At a prompt? (idle)
        last_line = lines[-1].strip() if lines else ''
        # Strip ANSI for pattern matching
        import re
        clean_last = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', last_line)
        if clean_last.endswith('$') or clean_last.endswith('❯') or clean_last.endswith('>') or clean_last.endswith('#'):
            return 'idle', 'at prompt'

        # Agent working patterns
        if any(p in last_text for p in ['thinking', 'reading', 'writing', 'editing', 'running', 'searching']):
            return 'running', 'working...'
        if any(p in last_text for p in ['tool_call', 'bash', 'executing']):
            return 'running', 'executing tools'
        if any(p in last_text for p in ['streaming', 'generating', '...']):
            return 'running', 'generating response'

        return 'running', 'active'

    # Cache for session state detection
    _session_states: dict[str, tuple[str, str]] = {}
    _session_hashes: dict[str, str] = {}

    def handle_tmux_capture(self, session_name: str, request_id: str | None):
        """Capture tmux pane content for the session grid."""
        try:
            from tmux_capture import capture_pane
            content = capture_pane(session_name, width=120, height=40)

            # Detect state (only re-detect if content changed)
            import hashlib
            content_hash = hashlib.md5(content.encode()).hexdigest()[:8]
            if self._session_hashes.get(session_name) != content_hash:
                self._session_hashes[session_name] = content_hash
                state, summary = self._detect_session_state(content)
                self._session_states[session_name] = (state, summary)

            state, summary = self._session_states.get(session_name, ('idle', ''))

            emit({
                'type': 'tmux_capture',
                'session': session_name,
                'content': content,
                'state': state,
                'summary': summary,
                'request_id': request_id,
            })
        except Exception as e:
            emit({'type': 'error', 'error': f'Capture failed: {e}', 'request_id': request_id})

    def handle_tmux_send(self, session_name: str, keys: str, literal: bool, request_id: str | None):
        """Send keys to a tmux session."""
        try:
            from tmux_capture import send_keys, send_key_literal
            if literal:
                ok = send_key_literal(session_name, keys)
            else:
                ok = send_keys(session_name, keys)
            emit({
                'type': 'tmux_send_result',
                'session': session_name,
                'ok': ok,
                'request_id': request_id,
            })
        except Exception as e:
            emit({'type': 'error', 'error': f'Send failed: {e}', 'request_id': request_id})


if __name__ == '__main__':
    backend = ChatBackend()
    backend.run()
