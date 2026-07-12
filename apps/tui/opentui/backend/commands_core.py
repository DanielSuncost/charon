"""Core slash-command handlers: setup, config, provider, model, settings.

Branch bodies are preserved verbatim from the original ``handle_command``
if/elif router in ``commands_mixin.py``; only the method wrappers and the
trailing ``return UNHANDLED`` are new. See ``CommandsMixin.handle_command``
for the dispatch.
"""
from __future__ import annotations

import time
from pathlib import Path

from backend import common
from backend.commands_mixin import UNHANDLED
from backend.settings_io import _full_messages_from_store, _load_ui_settings, _save_ui_settings
from backend.textutils import _sanitize_saved_messages

try:
    from charon.infra.diagnostics import record as _diag
except Exception:  # diagnostics is best-effort and must never block import
    def _diag(*args, **kwargs):
        return None


class CoreCommandsMixin:
    """Handlers for the setup/config/provider/model/settings command families."""

    def _cmd_help(self, command: str, request_id: str | None):
        # Show suggestions for /help, /setup alone, or unknown commands
        if command in ('/help', '/setup', '/?'):
            suggestions = self._get_suggestions('/setup' if command == '/setup' else '/')
            common.emit({
                'type': 'suggestions',
                'title': 'Setup Commands' if command == '/setup' else 'Available Commands',
                'items': suggestions,
                'request_id': request_id,
            })
            return
        return UNHANDLED

    def _cmd_setup(self, command: str, request_id: str | None):
        # '/setup' with no arguments shows the setup-command suggestions.
        # The shared condition is preserved verbatim from the original router;
        # only '/setup' itself can reach this handler.
        if command in ('/help', '/setup', '/?'):
            suggestions = self._get_suggestions('/setup' if command == '/setup' else '/')
            common.emit({
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
        # NOTE: the shade-* branches below are preserved verbatim from the
        # original router but are unreachable: every '/setup <arg>' command is
        # consumed by the '/setup ' prefix branch above.
        if command.startswith('/setup shade-model '):
            model_name = command[18:].strip()
            if model_name == 'same':
                from charon.providers.model_registry import load_registry, save_registry
                reg = load_registry(common.STATE_DIR)
                reg['shade_model_mode'] = 'same'
                save_registry(common.STATE_DIR, reg)
                common.emit({'type': 'status', 'message': 'Shade model: same as main agent.', 'request_id': request_id})
            elif model_name == 'auto':
                from charon.providers.model_registry import load_registry, save_registry
                reg = load_registry(common.STATE_DIR)
                reg['shade_model_mode'] = 'auto'
                save_registry(common.STATE_DIR, reg)
                common.emit({'type': 'status', 'message': 'Shade model: auto (Charon picks per task).', 'request_id': request_id})
            else:
                from charon.providers.model_registry import load_registry, save_registry
                reg = load_registry(common.STATE_DIR)
                reg['shade_model_mode'] = 'fixed'
                # Parse provider/model format
                if '/' in model_name:
                    parts = model_name.split('/', 1)
                    reg['shade_provider'] = parts[0]
                    reg['shade_model'] = parts[1]
                else:
                    reg['shade_model'] = model_name
                save_registry(common.STATE_DIR, reg)
                common.emit({'type': 'status', 'message': f'Shade model: {model_name}', 'request_id': request_id})
            return
        if command.startswith('/setup shade-url '):
            url = command[17:].strip()
            from charon.providers.model_registry import load_registry, save_registry
            reg = load_registry(common.STATE_DIR)
            reg['shade_base_url'] = url
            save_registry(common.STATE_DIR, reg)
            common.emit({'type': 'status', 'message': f'Shade base URL: {url}', 'request_id': request_id})
            return
        if command.startswith('/setup shade-key '):
            key = command[17:].strip()
            from charon.providers.model_registry import load_registry, save_registry
            reg = load_registry(common.STATE_DIR)
            reg['shade_api_key'] = key
            save_registry(common.STATE_DIR, reg)
            common.emit({'type': 'status', 'message': 'Shade API key saved.', 'request_id': request_id})
            return
        return UNHANDLED

    def _cmd_resume(self, command: str, request_id: str | None):
        if command == '/resume' or command.startswith('/resume '):
            arg = command[8:].strip() if command.startswith('/resume ') else ''
            try:
                from charon.conversation.conversation_store import list_conversations, load_conversation, dict_to_message, message_to_dict
                convos = list_conversations(common.STATE_DIR)
                if arg:
                    # Direct resume — must reset the engine so it gets the
                    # correct agent_id for the target session.
                    self._active_agent_id = arg
                    self.engine = None  # force re-creation with new agent_id
                    engine, _ = self._ensure_engine()
                    restored_count = 0
                    saved = None

                    # Try lossless store first — full raw history.
                    # Query with arg directly in case engine.agent_id differs.
                    store_msgs = _full_messages_from_store(arg)
                    if store_msgs:
                        restored_count = len(store_msgs)
                        if engine:
                            engine.messages = list(store_msgs)
                        saved = [message_to_dict(m) for m in store_msgs]

                    if not restored_count:
                        # Fall back to JSONL
                        saved = _sanitize_saved_messages(load_conversation(common.STATE_DIR, arg))
                        if saved and engine:
                            msgs = [dict_to_message(m) for m in saved]
                            engine.messages = msgs
                            # Migrate into lossless store
                            if engine.has_lossless_store:
                                engine.import_into_store(msgs)

                    if saved:
                        self._load_tasks_from_ledger(arg)
                        common.emit({
                            'type': 'conversation_restored',
                            'messages': saved,
                            'count': len(saved),
                            'agent_id': arg,
                        })
                        # Push session info so task pane updates immediately
                        common.emit({
                            'type': 'refresh',
                            'payload': {'session_info': self._get_session_info()},
                        })
                    else:
                        common.emit({'type': 'error', 'error': f'No saved conversation for {arg}', 'request_id': request_id})
                elif convos:
                    # Show session picker with last user message preview
                    # Pre-load SQLite counts + last user messages for accuracy
                    _store_info = {}
                    try:
                        from charon.infra.store_adapter import get_db
                        _sdb = get_db(common.STATE_DIR)
                        # Batch: get counts per agent
                        for row in _sdb.fetchall(
                            "SELECT agent_id, COUNT(*) as cnt FROM conversation_messages GROUP BY agent_id"
                        ):
                            _store_info[row['agent_id']] = {'count': row['cnt'], 'preview': ''}
                        # Batch: get last user message per agent (for preview)
                        for aid, info in _store_info.items():
                            row = _sdb.fetchone(
                                "SELECT content FROM conversation_messages "
                                "WHERE agent_id = ? AND role = 'user' AND content != '' "
                                "ORDER BY seq DESC LIMIT 1",
                                (aid,),
                            )
                            if row and row['content']:
                                first_line = row['content'].strip().split('\n')[0]
                                info['preview'] = first_line[:60] + ('…' if len(first_line) > 60 else '')
                    except Exception as exc:
                        _diag('commands_mixin', 'sqlite conversation preload failed; resume picker falls back to jsonl previews', error=exc)

                    items = []
                    for c in sorted(convos, key=lambda x: x.get('last_timestamp', 0), reverse=True):
                        age = ''
                        if c.get('last_timestamp'):
                            secs = time.time() - c['last_timestamp']
                            if secs < 60:
                                age = f'{int(secs)}s ago'
                            elif secs < 3600:
                                age = f'{int(secs/60)}m ago'
                            elif secs < 86400:
                                age = f'{int(secs/3600)}h ago'
                            else:
                                age = f'{int(secs/86400)}d ago'
                        # Use SQLite info when available and more complete
                        aid = c['agent_id']
                        si = _store_info.get(aid)
                        msg_count = c.get('message_count', 0)
                        preview = ''
                        if si and si['count'] >= msg_count:
                            msg_count = si['count']
                            preview = si.get('preview', '')
                        if not preview:
                            try:
                                saved = load_conversation(common.STATE_DIR, aid)
                                for msg in reversed(saved):
                                    if msg.get('role') == 'user' and msg.get('content', '').strip():
                                        first_line = msg['content'].strip().split('\n')[0]
                                        preview = first_line[:60]
                                        if len(first_line) > 60:
                                            preview += '…'
                                        break
                            except Exception:
                                pass
                        items.append({
                            'id': aid,
                            'desc': f"{preview or '(no messages)'}",
                            'age': f"{msg_count}msg  {age}",
                        })
                    if items:
                        common.emit({
                            'type': 'model_picker',
                            'models': items,
                            'provider': 'resume',
                            'request_id': request_id,
                        })
                    else:
                        common.emit({'type': 'status', 'message': 'No other saved conversations found.', 'request_id': request_id})
                else:
                    common.emit({'type': 'status', 'message': 'No saved conversations found.', 'request_id': request_id})
            except Exception as e:
                common.emit({'type': 'error', 'error': f'Resume failed: {e}', 'request_id': request_id})
            return
        return UNHANDLED

    def _cmd_hotkeys(self, command: str, request_id: str | None):
        if command == '/hotkeys':
            common.emit({
                'type': 'suggestions',
                'title': 'Keyboard Shortcuts',
                'items': [
                    {'cmd': 'F1', 'desc': 'Switch to Chat view'},
                    {'cmd': 'F2', 'desc': 'Switch to Dashboard view'},
                    {'cmd': 'F3', 'desc': 'Switch to Session Grid view'},
                    {'cmd': 'F4', 'desc': 'Switch to Room Controls view'},
                    {'cmd': 'd', 'desc': 'F4 Rooms: delete selected room and close its participant sessions'},
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
        return UNHANDLED

    def _cmd_timestamps(self, command: str, request_id: str | None):
        if command == '/timestamps':
            common.emit({'type': 'toggle_timestamps', 'request_id': request_id})
            return
        return UNHANDLED

    def _cmd_interrupt(self, command: str, request_id: str | None):
        if command in ('/interrupt', '/abort'):
            self.handle_abort(request_id)
            return
        return UNHANDLED

    def _cmd_thoughts(self, command: str, request_id: str | None):
        if command == '/thoughts':
            self.visible_thoughts = not self.visible_thoughts
            try:
                settings = _load_ui_settings()
                settings['visible_thoughts'] = self.visible_thoughts
                _save_ui_settings(settings)
            except Exception as exc:
                _diag('commands_mixin', 'ui settings save failed; visible-thoughts toggle not persisted', error=exc)
            common.emit({
                'type': 'toggle_visible_thoughts',
                'enabled': self.visible_thoughts,
                'supported': self._thoughts_supported(),
                'provider': self._current_provider_name(),
                'request_id': request_id,
            })
            return
        return UNHANDLED

    def _cmd_models(self, command: str, request_id: str | None):
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
                onboarding = common._load_json(common.STATE_DIR / 'onboarding.json', {})
                current = onboarding.get('model') or onboarding.get('provider_model') or 'none'
                lines.append(f'Current model: {current}')
                lines.append(f'Provider: {onboarding.get("provider", "none")}')

                try:
                    from charon.providers.model_registry import load_registry
                    reg = load_registry(common.STATE_DIR)
                    shade_model = reg.get('shade_model') or '(same as main)'
                    lines.append(f'Shade model: {shade_model}')
                except Exception as exc:
                    _diag('commands_mixin', 'model registry unreadable; shade model omitted from /models output', error=exc)

                if lines:
                    common.emit({'type': 'status', 'message': '\n'.join(lines), 'request_id': request_id})
                else:
                    common.emit({'type': 'status', 'message': 'No local model servers detected.', 'request_id': request_id})
            except Exception as e:
                common.emit({'type': 'error', 'error': str(e), 'request_id': request_id})
            return
        return UNHANDLED

    def _cmd_settings(self, command: str, request_id: str | None):
        if command == '/settings' or command == '/config':
            try:
                lines = ['# Charon Settings', '']

                # Provider
                onboarding = common._load_json(common.STATE_DIR / 'onboarding.json', {})
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
                    from charon.providers.model_registry import load_registry
                    reg = load_registry(common.STATE_DIR)
                    shade_mode = reg.get('shade_model_mode', 'auto')
                    shade_model = reg.get('shade_model') or '(same as main)'
                    shade_provider = reg.get('shade_provider') or '(same as main)'
                    shade_url = reg.get('shade_base_url') or '(default)'
                    lines.append(f'Shade model mode: {shade_mode}')
                    lines.append(f'Shade model: {shade_model}')
                    lines.append(f'Shade provider: {shade_provider}')
                    lines.append(f'Shade URL: {shade_url}')
                    lines.append('Shade model is also used for lightweight orchestration/NL parsing fallback.')
                except Exception as exc:
                    _diag('commands_mixin', 'model registry unreadable; /settings shows shade model as not configured', error=exc)
                    lines.append('Shade model: (not configured)')
                lines.append('')

                # Autonomous mode
                try:
                    from charon.agents.autonomous import load_autonomous_config
                    auto = load_autonomous_config(common.STATE_DIR)
                    lines.append(f'Autonomous mode: {"ON" if auto.get("enabled") else "OFF"}')
                    tb = auto.get('time_budget_minutes')
                    lines.append(f'Time budget: {tb} min' if tb else 'Time budget: unlimited')
                    lines.append(f'Git checkpoints: {"on" if auto.get("git_checkpoint") else "off"}')
                except Exception as exc:
                    _diag('commands_mixin', 'autonomous config unreadable; /settings assumes autonomous mode OFF', error=exc)
                    lines.append('Autonomous mode: OFF')
                lines.append('')

                # Consolidation
                try:
                    from charon.memory.consolidation import load_config
                    con = load_config(common.STATE_DIR)
                    lines.append(f'Consolidation: {"on" if con.get("enabled") else "off"}')
                    lines.append(f'Consolidation model: {con.get("model_tier", "fast")}')
                    lines.append(f'Consolidation interval: {con.get("scan_interval_heartbeats", 50)} heartbeats')
                except Exception as exc:
                    _diag('commands_mixin', 'consolidation config unreadable; /settings shows defaults', error=exc)
                    lines.append('Consolidation: on (default)')
                lines.append('')

                # Approval
                try:
                    from charon.infra.tool_approval import get_approval_status
                    status = get_approval_status(self._active_agent_id or 'default')
                    skip = status.get('skip_all', False)
                    approved = status.get('session_approved', [])
                    lines.append(f'Approval: {"DISABLED" if skip else "enabled"}')
                    if approved:
                        lines.append(f'Session approved: {", ".join(approved[:5])}')
                except Exception as exc:
                    _diag('commands_mixin', 'approval status unreadable; /settings assumes approval enabled', error=exc)
                    lines.append('Approval: enabled')
                lines.append('')

                # Agent
                try:
                    from charon.agents.agent_lifecycle import list_agents
                    agents = [a for a in list_agents() if a.get('role') == 'charon']
                    lines.append(f'Agents: {len(agents)}')
                    for a in agents[:5]:
                        lines.append(f'  {a.get("name", a.get("id", "?"))} ({a.get("id")}) — {a.get("status", "?")}')
                except Exception as exc:
                    _diag('commands_mixin', 'agent listing failed; /settings omits agents section', error=exc)
                lines.append('')

                # Tools
                from charon.tools import ALL_TOOL_DEFS
                built_in = [t['name'] for t in ALL_TOOL_DEFS]
                lines.append(f'Tools ({len(built_in)}): {", ".join(built_in)}')
                try:
                    from charon.tools.dynamic_loader import list_dynamic_tools
                    dynamic = list_dynamic_tools()
                    if dynamic:
                        lines.append(f'Dynamic tools ({len(dynamic)}): {", ".join(t["name"] for t in dynamic)}')
                except Exception as exc:
                    _diag('commands_mixin', 'dynamic tool listing failed; /settings omits dynamic tools', error=exc)

                common.emit({'type': 'status', 'message': '\n'.join(lines), 'request_id': request_id})
            except Exception as e:
                common.emit({'type': 'error', 'error': str(e), 'request_id': request_id})
            return
        return UNHANDLED

    def _cmd_provider(self, command: str, request_id: str | None):
        if command.startswith('/provider ') or command == '/provider':
            if command == '/provider':
                onboarding = common._load_json(common.STATE_DIR / 'onboarding.json', {})
                provider = str(onboarding.get('provider') or 'none')
                model = str(onboarding.get('model') or onboarding.get('provider_model') or 'none')
                common.emit({'type': 'status', 'message': f'Current provider: {provider}/{model}', 'request_id': request_id})
            else:
                new_provider = command[10:].strip()
                if not new_provider:
                    common.emit({'type': 'error', 'error': 'Usage: /provider <name>', 'request_id': request_id})
                    return

                current_provider = self._current_provider_name()
                if new_provider == current_provider:
                    common.emit({'type': 'status', 'message': f'Already on {new_provider}.', 'request_id': request_id})
                    return

                if self._has_transferable_context():
                    self._prompt_provider_switch(new_provider, request_id, source='provider')
                else:
                    self._switch_provider_fresh(new_provider, request_id)
            return
        # NOTE: the branches below are preserved verbatim from the original
        # router but are unreachable: the combined branch above consumes both
        # '/provider' and '/provider <name>'.
        if command == '/provider':
            onboarding = common._load_json(common.STATE_DIR / 'onboarding.json', {})
            provider = str(onboarding.get('provider') or 'none')
            common.emit({'type': 'status', 'message': f'Current provider: {provider}', 'request_id': request_id})
            # Show provider picker as menu
            common.emit({
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
        return UNHANDLED

    def _cmd_tools(self, command: str, request_id: str | None):
        if command == '/tools' or command == '/tools list':
            from charon.tools import ALL_TOOL_DEFS, FAILED_TOOL_IMPORTS
            lines = ['Built-in tools:']
            for t in ALL_TOOL_DEFS:
                lines.append(f'  {t["name"]}: {t["description"][:60]}')
            if FAILED_TOOL_IMPORTS:
                lines.append('\nBroken tools (import failed, excluded from registry):')
                for f in FAILED_TOOL_IMPORTS:
                    lines.append(f'  {f["tool"]}: {f["error"]}')
            try:
                from charon.tools.dynamic_loader import list_dynamic_tools, get_load_errors
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
            except Exception as exc:
                _diag('commands_mixin', 'dynamic tool listing failed; /tools shows built-ins only', error=exc)
            common.emit({'type': 'status', 'message': '\n'.join(lines), 'request_id': request_id})
            return
        if command == '/tools reload':
            try:
                from charon.tools.dynamic_loader import load_dynamic_tools
                onboarding = common._load_json(common.STATE_DIR / 'onboarding.json', {})
                project = str(onboarding.get('project') or str(common.ROOT)).strip()
                defs, executors, errors = load_dynamic_tools(common.STATE_DIR, Path(project))
                msg = f'Reloaded: {len(defs)} dynamic tool(s)'
                if errors:
                    msg += f', {len(errors)} error(s)'
                    for e in errors:
                        msg += f'\n  {e["error"]}'
                # Refresh engine tools
                if self.engine:
                    from charon.tools.dynamic_loader import get_all_tool_defs
                    self.engine.tools = get_all_tool_defs(common.STATE_DIR, Path(project))
                common.emit({'type': 'status', 'message': msg, 'request_id': request_id})
            except Exception as e:
                common.emit({'type': 'error', 'error': f'Reload failed: {e}', 'request_id': request_id})
            return
        return UNHANDLED

    def _cmd_history(self, command: str, request_id: str | None):
        if command == '/history' or command.startswith('/history '):
            agent_id = command[9:].strip() if command.startswith('/history ') else ''
            self.handle_agent_ledger(agent_id, request_id)
            return
        return UNHANDLED

    def _cmd_consolidation(self, command: str, request_id: str | None):
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
                common.emit({'type': 'error', 'error': 'Interval must be a number.', 'request_id': request_id})
            return
        if command == '/consolidation off':
            self.handle_consolidation_config({'action': 'set', 'config': {'enabled': False}}, request_id)
            return
        if command == '/consolidation on':
            self.handle_consolidation_config({'action': 'set', 'config': {'enabled': True}}, request_id)
            return
        return UNHANDLED

    def _cmd_reset(self, command: str, request_id: str | None):
        if command == '/reset':
            if self.engine:
                self.engine.reset()
            self.chat_history = []
            common.emit({'type': 'status', 'message': 'Conversation cleared.', 'request_id': request_id})
            return
        return UNHANDLED

    def _cmd_model(self, command: str, request_id: str | None):
        if command == '/model':
            onboarding = common._load_json(common.STATE_DIR / 'onboarding.json', {})
            model = str(onboarding.get('model') or onboarding.get('provider_model') or 'none')
            provider = str(onboarding.get('provider') or 'none')
            # Show current model and trigger picker
            common.emit({'type': 'status', 'message': f'Current: {provider}/{model}', 'request_id': request_id})
            self._run_setup_command('model', request_id)
            return
        if command.startswith('/model '):
            model_name = command[7:].strip()
            if model_name:
                self._run_setup_command(f'model {model_name}', request_id)
            return
        return UNHANDLED
