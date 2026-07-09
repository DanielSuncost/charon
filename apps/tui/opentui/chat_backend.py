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

import json
import os
import signal
import sys
import threading

from backend import common
from conversation_engine import ConversationEngine

# Backward-compat re-exports: these names were historically importable
# from chat_backend (tests and tooling rely on some of them).
from backend.common import ROOT, STATE_DIR, emit, _load_json  # noqa: F401
from tools import ALL_TOOL_DEFS  # noqa: F401
from backend.boat import (
    _terminate_boat_session,
)  # noqa: F401
from backend.settings_io import (
    _load_ui_settings,
)  # noqa: F401
from backend.dashboard import _collect_devop_rooms  # noqa: F401
from backend.nlparse import _parse_interval_phrase, _natural_language_to_cron  # noqa: F401

from backend.providers_mixin import ProvidersMixin
from backend.chat_mixin import ChatMixin
from backend.commands_mixin import CommandsMixin
from backend.rooms_mixin import RoomsMixin
from backend.libris_mixin import LibrisMixin
from backend.dashboard import DashboardMixin
from backend.harvest_mixin import HarvestMixin
from backend.fleet_mixin import FleetSetupMixin
from backend.setup_mixin import SetupMixin
from backend.consolidation_mixin import ConsolidationMixin
from backend.tmux_mixin import TmuxMixin


class ChatBackend(ProvidersMixin, ChatMixin, CommandsMixin, RoomsMixin, LibrisMixin, DashboardMixin, HarvestMixin, FleetSetupMixin, SetupMixin, ConsolidationMixin, TmuxMixin):
    def __init__(self):
        try:
            from automation_scheduler import start_scheduler
            start_scheduler(common.STATE_DIR, poll_seconds=2.0)
        except Exception:
            pass
        self.engine: ConversationEngine | None = None
        self.chat_history: list[dict] = []
        self._engine_lock = threading.Lock()
        self._active_agent_id: str | None = None
        self.agent_mode: str = 'interactive'  # interactive, autonomous, delegating, idle
        self._notified_batches: set[str] = set()
        self._session_tasks: list[dict] = []
        self._pending_provider_switch: dict | None = None
        self._pending_libris_intake: dict | None = None
        self._pending_remote_onboard: dict | None = None
        self._pending_fleet_setup: dict | None = None
        self.visible_thoughts: bool = bool(_load_ui_settings().get('visible_thoughts', False))
        self._goal_inference_token_estimate: int = 0
        self._room_runners: set[str] = set()
        self._last_orchestration_parse: dict = {}
        self._owned_boat_sessions: set[str] = set()
        self._shutdown_cleaned = False

    def _register_owned_boat_session(self, session_name: str | None) -> None:
        name = str(session_name or '').strip()
        if name:
            self._owned_boat_sessions.add(name)

    def _cleanup_owned_sessions(self) -> None:
        if self._shutdown_cleaned:
            return
        self._shutdown_cleaned = True
        for session_name in list(self._owned_boat_sessions):
            try:
                _terminate_boat_session(session_name)
            except Exception:
                pass
        self._owned_boat_sessions.clear()

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
                    convos = list_conversations(common.STATE_DIR)
                    if convos:
                        convos.sort(key=lambda c: c.get('last_timestamp', 0), reverse=True)
                        requested_resume = convos[0]['agent_id']
                if requested_resume and requested_resume != 'latest':
                    self._active_agent_id = requested_resume
                    self._load_tasks_from_ledger(requested_resume)
                    common.emit({'type': 'status', 'message': f'Resuming conversation with {requested_resume}...'})
            except Exception:
                pass

        if onboarding.get('complete') and not requested_provider:
            # Already configured, no specific provider requested — ensure engine is ready
            try:
                self._ensure_engine()
                if requested_agent:
                    common.emit({'type': 'status', 'message': f'Started fresh session bound to agent {requested_agent}.'})
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
                    project = str(onboarding.get('project') or str(common.ROOT)).strip()
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
            common.emit({'type': 'status', 'message': f'Starting with provider: {requested_provider}'})
            self.handle_command(f'/setup provider {requested_provider}', None)
        self.handle_refresh(None)

        # Pre-populate notified batches so old completions don't spam on startup
        try:
            from batch_orchestrator import list_batches
            for b in list_batches(common.STATE_DIR):
                if b.get('status') in ('completed', 'partial'):
                    self._notified_batches.add(b.get('id', ''))
        except Exception:
            pass

        # Save conversation and cleanup owned sessions on exit
        import atexit
        atexit.register(self._cleanup_owned_sessions)
        atexit.register(self._save_conversation_now)

        def _shutdown_handler(signum, frame):
            self._cleanup_owned_sessions()
            self._save_conversation_now()
            raise SystemExit(0)

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, _shutdown_handler)
            except Exception:
                pass

        # Start background worker for consolidation, goal inference, etc.
        self._chat_busy = False
        self._start_background_worker()

        while True:
            try:
                line = sys.stdin.buffer.readline()
            except (EOFError, KeyboardInterrupt):
                self._cleanup_owned_sessions()
                self._save_conversation_now()
                break
            if not line:
                self._cleanup_owned_sessions()
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
                        common.emit({
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
                        send_steer(common.STATE_DIR, target, steer_msg)
                        common.emit({'type': 'status', 'message': f'📡 Sent to {target.split("-")[-1][:6]}: {steer_msg[:40]}', 'request_id': request_id})
                    except Exception as e:
                        common.emit({'type': 'error', 'error': f'Steer failed: {e}', 'request_id': request_id})
            elif req_type == 'live_conv':
                # Load conversation preview for a live session
                session_id = msg.get('session_id', '')
                if session_id:
                    try:
                        from conversation_store import load_conversation
                        msgs = load_conversation(common.STATE_DIR, session_id)
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
                                is_err = m.get('is_error', False)
                                first_line = (content or '').split('\n')[0][:50]
                                icon = '✗' if is_err else '✓'
                                preview_lines.append(f'    {icon} {first_line}')
                        common.emit({
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


if __name__ == '__main__':
    backend = ChatBackend()
    backend.run()
