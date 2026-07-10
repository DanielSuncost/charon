"""Engine/provider lifecycle mixin."""
from __future__ import annotations

import os
import sys

from backend import common
from backend.settings_io import _full_messages_from_store
from backend.textutils import _sanitize_saved_messages
from charon.conversation.conversation_engine import ConversationEngine
from charon.providers.provider_bridge import create_provider_and_model, resolve_provider_config


class ProvidersMixin:
    """Engine/provider lifecycle: creation, switching, and context transfer."""

    def _ensure_engine(self) -> tuple[ConversationEngine | None, str]:
        """Create or return the conversation engine.
        Returns (engine, error_message).
        """
        # Register approval callback so tool calls can ask for permission
        try:
            from charon.tools import set_approval_callback
            def _emit_approval(tool_name, params_summary, risk, reason):
                common.emit({
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
            provider, model, ready = create_provider_and_model(common.STATE_DIR, self._active_agent_id)
        except Exception as e:
            return None, f'Provider setup failed: {e}'

        if not ready:
            return None, 'No provider configured. Use /setup provider <name> to configure.'

        project = str(common.ROOT)
        onboarding = common._load_json(common.STATE_DIR / 'onboarding.json', {})
        configured_project = str(onboarding.get('project') or '').strip()
        if configured_project:
            project = configured_project

        # Build enriched system prompt with memory, goals, coordination.
        # Fresh launches should stay fresh: do NOT silently bind to an existing
        # persistent agent unless the user explicitly requested one via
        # CHARON_AGENT / --agent.
        system_prompt = ''
        try:
            from charon.context.system_prompt_builder import build_system_prompt as build_layered_prompt
            agent_info = {'id': '', 'name': 'Charon', 'role': 'charon', 'goal': '', 'project': project}
            requested_agent = os.environ.get('CHARON_AGENT', '').strip()
            self._bound_agent_id = None
            if requested_agent:
                try:
                    from charon.agents.agent_lifecycle import list_agents
                    for a in list_agents():
                        if a.get('id') == requested_agent or a.get('name') == requested_agent:
                            agent_info = a
                            self._bound_agent_id = a.get('id') or None
                            break
                except Exception:
                    pass
            task_info = {'project': project}
            system_prompt = build_layered_prompt(
                state_dir=common.STATE_DIR, agent=agent_info, task=task_info,
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
            state_dir=common.STATE_DIR,
            max_tokens=32768,
        )

        # Apply provider handoff transfer if present.
        try:
            from charon.context.context_transfer import load_pending_transfer, apply_transfer_to_engine, clear_pending_transfer, record_transfer_event
            pending_transfer = load_pending_transfer(common.STATE_DIR)
            if pending_transfer:
                apply_transfer_to_engine(self.engine, pending_transfer)
                clear_pending_transfer(common.STATE_DIR)
                record_transfer_event(common.STATE_DIR, {
                    'ts': pending_transfer.get('created_at', ''),
                    'type': 'transfer_applied',
                    'bundle_id': pending_transfer.get('id', ''),
                    'source_provider': pending_transfer.get('source', {}).get('provider', ''),
                    'target_provider': pending_transfer.get('target', {}).get('provider', ''),
                    'session_id': pending_transfer.get('source', {}).get('session_id', ''),
                })
                common.emit({
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
                saved = None
                aid = self._active_agent_id

                # Try lossless store first — query by agent_id directly
                store_msgs = _full_messages_from_store(aid)
                if store_msgs:
                    if self.engine:
                        self.engine.messages = list(store_msgs)
                    from charon.conversation.conversation_store import message_to_dict
                    saved = [message_to_dict(m) for m in store_msgs]
                else:
                    # Fall back to JSONL
                    from charon.conversation.conversation_store import load_conversation, dict_to_message
                    saved = _sanitize_saved_messages(load_conversation(common.STATE_DIR, aid))
                    if saved and self.engine:
                        msgs = [dict_to_message(m) for m in saved]
                        self.engine.messages = msgs
                        # Migrate JSONL messages into lossless store for future resumes
                        if self.engine.has_lossless_store:
                            self.engine.import_into_store(msgs)

                if saved:
                    self._load_tasks_from_ledger(aid)
                    common.emit({
                        'type': 'conversation_restored',
                        'messages': saved,
                        'count': len(saved),
                        'agent_id': aid,
                    })
            except Exception:
                pass

        return self.engine, ''

    def _ensure_session_id(self) -> str:
        if not self._active_agent_id:
            import time
            import hashlib
            raw = f'{time.time()}-{os.getpid()}'
            short = hashlib.md5(raw.encode()).hexdigest()[:6]
            self._active_agent_id = f'session-{short}-{int(time.time())}'
        return self._active_agent_id

    def _session_provider_state(self) -> dict:
        try:
            session_id = self._active_agent_id or None
            return resolve_provider_config(common.STATE_DIR, session_id=session_id)
        except Exception:
            onboarding = common._load_json(common.STATE_DIR / 'onboarding.json', {})
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
            from charon.context.context_transfer import session_has_transferable_context
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
        common.emit({
            'type': 'status',
            'message': (
                f'Switching from {current_provider} to {target_provider}. '
                'Choose whether to continue this session or start fresh.'
            ),
            'request_id': request_id,
        })
        common.emit({
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
                from charon.context.context_transfer import create_transfer_bundle, record_pending_transfer, record_transfer_event
                bundle = create_transfer_bundle(
                    state_dir=common.STATE_DIR,
                    session_id=self._active_agent_id,
                    agent_id=(getattr(self, '_bound_agent_id', None) or self._active_agent_id),
                    project_root=self.engine.project_root,
                    source_provider=self._current_provider_name() or 'unknown',
                    target_provider=target_provider,
                    messages=self.engine.messages,
                )
                record_pending_transfer(common.STATE_DIR, bundle)
                record_transfer_event(common.STATE_DIR, {
                    'ts': bundle.get('created_at', ''),
                    'type': 'provider_switch_continue',
                    'bundle_id': bundle.get('id', ''),
                    'source_provider': self._current_provider_name() or 'unknown',
                    'target_provider': target_provider,
                    'session_id': self._active_agent_id,
                })
                common.emit({
                    'type': 'status',
                    'message': f'Preparing context transfer to {target_provider} ({bundle.get("id", "")})...',
                    'request_id': request_id,
                })
                common.emit({
                    'type': 'status',
                    'message': f'Context transfer ready. Switching provider to {target_provider}...',
                    'request_id': request_id,
                })
            except Exception as e:
                common.emit({
                    'type': 'status',
                    'message': f'Context transfer prep failed ({e}). Falling back to fresh switch.',
                    'request_id': request_id,
                })
        self._run_setup_command(f'provider {target_provider}', request_id, skip_prompt=True)

    def _switch_provider_fresh(self, target_provider: str, request_id: str | None):
        common.emit({
            'type': 'status',
            'message': f'Starting a fresh {target_provider} session...',
            'request_id': request_id,
        })
        try:
            from charon.context.context_transfer import clear_pending_transfer, record_transfer_event
            clear_pending_transfer(common.STATE_DIR)
            record_transfer_event(common.STATE_DIR, {
                'ts': __import__('time').strftime('%Y-%m-%dT%H:%M:%SZ', __import__('time').gmtime()),
                'type': 'provider_switch_fresh',
                'source_provider': self._current_provider_name() or 'unknown',
                'target_provider': target_provider,
                'session_id': self._active_agent_id or '',
            })
        except Exception:
            pass
        self._run_setup_command(f'provider {target_provider}', request_id, skip_prompt=True)

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

    def _thoughts_supported(self) -> bool:
        name = self._current_provider_name()
        # Anthropic has native thinking. Local/OpenAI-compatible providers may
        # also surface thoughts via reasoning fields or inline <think> blocks.
        return name in {'anthropic', 'openai', 'local'}
