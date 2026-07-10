"""/setup and onboarding mixin."""
from __future__ import annotations

import json
import os
import sys
import time

from backend import common
from charon.providers.provider_bridge import load_session_provider_config, save_session_provider_config


class SetupMixin:
    """The /setup command, onboarding repair, and auth-token discovery."""

    def _run_setup_command(self, rest: str, request_id: str | None, *, skip_prompt: bool = False):
        """Execute setup subcommands."""
        # Directly update onboarding state
        onboarding = common._load_json(common.STATE_DIR / 'onboarding.json', {})

        parts = rest.split(maxsplit=1)
        subcmd = parts[0] if parts else ''
        arg = parts[1].strip() if len(parts) > 1 else ''

        session_id = self._active_agent_id or None
        session_override = load_session_provider_config(common.STATE_DIR, session_id) if session_id else {}

        if subcmd == 'status':
            status_payload = dict(onboarding)
            if session_override:
                status_payload['session_override'] = session_override
                status_payload['session_id'] = session_id
            common.emit({'type': 'status', 'message': json.dumps(status_payload, indent=2), 'request_id': request_id})
        elif subcmd == 'reset':
            onboarding = {'complete': False, 'step': 'provider-mode', 'provider_mode': '', 'provider': '', 'model': ''}
            self._save_onboarding(onboarding)
            self.engine = None  # force re-creation
            common.emit({'type': 'status', 'message': 'Setup reset.', 'request_id': request_id})
        elif subcmd == 'provider':
            # Parse: /setup provider claude-code [--force]
            arg_parts = arg.split()
            force_oauth = '--force' in arg_parts
            arg = arg_parts[0] if arg_parts else ''
            allowed = {'codex', 'claude-code', 'opencode', 'api', 'lmstudio'}
            if arg not in allowed:
                common.emit({'type': 'error', 'error': f'Unknown provider: {arg}. Options: {", ".join(sorted(allowed))}', 'request_id': request_id})
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
                onboarding.update({
                    'provider_mode': target_state.get('provider_mode', onboarding.get('provider_mode', '')),
                    'provider': target_state.get('provider', onboarding.get('provider', '')),
                    'provider_auth': target_state.get('provider_auth', onboarding.get('provider_auth', '')),
                    'model': target_state.get('model', onboarding.get('model', '')),
                    'provider_model': target_state.get('provider_model', onboarding.get('provider_model', '')),
                    'project': target_state.get('project', onboarding.get('project', '')),
                    'complete': target_state.get('complete', onboarding.get('complete', False)),
                    'step': target_state.get('step', onboarding.get('step', 'provider-mode')),
                })
                self._save_onboarding(onboarding)
                if use_session_override and self._active_agent_id:
                    save_session_provider_config(common.STATE_DIR, self._active_agent_id, target_state)

            def _revert_target_state() -> None:
                if use_session_override and self._active_agent_id:
                    if session_override:
                        save_session_provider_config(common.STATE_DIR, self._active_agent_id, session_override)
                    else:
                        from charon.providers.provider_bridge import clear_session_provider_config
                        clear_session_provider_config(common.STATE_DIR, self._active_agent_id)
                else:
                    self._save_onboarding(onboarding)

            def _continue_provider_setup_after_auth() -> None:
                existing_model = str(target_state.get('model') or target_state.get('provider_model') or '').strip()
                if existing_model:
                    self._run_setup_command(f'model {existing_model}', request_id)
                else:
                    self._run_setup_command('model', request_id)

            # For OAuth providers, try to find existing credentials first
            if arg in ('claude-code', 'codex'):
                target_state['step'] = 'provider-auth'
                _persist_target_state()
                provider_map = {'claude-code': 'anthropic', 'codex': 'openai-codex'}
                provider_id = provider_map[arg]

                # Try to find existing credentials before running full OAuth.
                # Use /setup provider <name> --force to skip and do fresh OAuth.
                existing_token = None
                existing_tokens = {}
                if not force_oauth:
                    # Check Charon's own auth store for this provider
                    existing_tokens = self._find_charon_auth_tokens(provider_id)
                    existing_token = str(existing_tokens.get('access_token') or '').strip()
                    if existing_token and self._is_jwt_expired(existing_token) and not str(existing_tokens.get('refresh_token') or '').strip():
                        common.emit({'type': 'status', 'message': f'Existing {arg} token is expired and has no refresh token. Starting fresh OAuth.', 'request_id': request_id})
                        existing_token = None
                        existing_tokens = {}
                    # For claude-code, also check Claude Code's credentials file
                    if not existing_token and arg == 'claude-code':
                        existing_token = self._find_claude_credentials()
                        if existing_token:
                            existing_tokens = {'access_token': existing_token}
                    if existing_token:
                        try:
                            from charon.providers import charon_auth
                            store = charon_auth._load_auth()
                            store['active_provider'] = provider_id
                            store.setdefault('providers', {})
                            if not existing_tokens:
                                existing_tokens = {'access_token': existing_token}
                            store['providers'][provider_id] = {
                                'tokens': existing_tokens,
                                'last_login': charon_auth._now(),
                                'auth_type': 'existing',
                            }
                            charon_auth._save_auth(store)

                            target_state['provider_auth'] = 'existing'
                            target_state['step'] = 'model'
                            _persist_target_state()

                            common.emit({'type': 'status', 'message': f'✓ Found existing {arg} credentials! Token imported.', 'request_id': request_id})
                            _continue_provider_setup_after_auth()
                            return
                        except Exception as e:
                            common.emit({'type': 'status', 'message': f'Found credentials but import failed: {e}. Falling back to OAuth.', 'request_id': request_id})

                # Run OAuth with local callback server in a background thread
                try:
                    from charon.providers import charon_auth
                    import threading

                    common.emit({'type': 'status', 'message': f'Setting up OAuth for {arg}...', 'request_id': request_id})

                    def _run_oauth():
                        try:
                            def _status(msg: str):
                                if msg.startswith('AUTH_URL::'):
                                    url = msg.split('AUTH_URL::', 1)[1].strip()
                                    common.emit({'type': 'auth_url', 'url': url, 'provider': arg, 'request_id': request_id})
                                elif msg.startswith('AUTH_INFO::'):
                                    common.emit({'type': 'status', 'message': msg.split('AUTH_INFO::', 1)[1].strip(), 'request_id': request_id})
                                else:
                                    common.emit({'type': 'status', 'message': msg, 'request_id': request_id})

                            charon_auth.login_oauth(provider_id, status_cb=_status)

                            target_state['provider_auth'] = 'oauth'
                            target_state['step'] = 'model'
                            _persist_target_state()

                            common.emit({'type': 'status', 'message': '✓ Authentication successful!', 'request_id': request_id})
                            _continue_provider_setup_after_auth()
                        except Exception as e:
                            _revert_target_state()
                            common.emit({'type': 'error', 'error': f'Auth failed: {e}', 'request_id': request_id})
                            common.emit({'type': 'status', 'message': 'Provider switch interrupted. Restored previous provider for this session.', 'request_id': request_id})
                            common.emit({'type': 'status', 'message': 'You can also try: /setup api-key <your-key>', 'request_id': request_id})

                    t = threading.Thread(target=_run_oauth, daemon=True)
                    t.start()
                    common.emit({'type': 'status', 'message': f'Starting {arg} authentication... Opening browser.', 'request_id': request_id})
                except Exception as e:
                    _revert_target_state()
                    common.emit({'type': 'error', 'error': f'Auth setup failed: {e}', 'request_id': request_id})
                    common.emit({'type': 'status', 'message': 'Provider switch interrupted. Restored previous provider for this session.', 'request_id': request_id})
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
                        common.emit({'type': 'status', 'message': f'Provider set to {arg}. Auto-selected model {chosen_model}.', 'request_id': request_id})
                    else:
                        common.emit({'type': 'status', 'message': f'Provider set to {arg}. No local model list detected, using {chosen_model}.', 'request_id': request_id})
                    effective_onboarding = dict(onboarding)
                    effective_onboarding.update(target_state)
                    self._on_setup_complete(effective_onboarding, request_id)
                else:
                    target_state['step'] = 'model'
                    _persist_target_state()
                    common.emit({'type': 'status', 'message': f'Provider set to {arg}. Now run /setup model <model_name>', 'request_id': request_id})
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
                    {'id': 'gpt-5.5', 'desc': 'GPT 5.5 — latest, most capable'},
                    {'id': 'gpt-5.4', 'desc': 'GPT 5.4'},
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
                    common.emit({
                        'type': 'model_picker',
                        'models': models,
                        'provider': provider,
                        'request_id': request_id,
                    })
                else:
                    common.emit({'type': 'error', 'error': 'Usage: /setup model <model_name>', 'request_id': request_id})
                return

            # Validate model name — warn but don't reject custom names
            model_ids = [m['id'] for m in models]
            if models and arg not in model_ids:
                close = [m for m in model_ids if arg.lower() in m.lower()]
                if close:
                    common.emit({'type': 'status', 'message': f'Model "{arg}" not in known list. Close matches: {", ".join(close)}', 'request_id': request_id})
                else:
                    common.emit({'type': 'status', 'message': f'Model "{arg}" not in known list. Using it anyway.', 'request_id': request_id})

            target_state = dict(session_override) if session_override else dict(onboarding)
            target_state['model'] = arg
            target_state['provider_model'] = arg

            # Auto-complete if project is already set (from previous setup or default)
            project = str(target_state.get('project') or onboarding.get('project') or '').strip()
            if not project:
                project = str(common.ROOT)
                target_state['project'] = project

            target_state['complete'] = True
            target_state['step'] = 'done'
            onboarding.update(target_state)
            self._save_onboarding(onboarding)
            if session_override and self._active_agent_id:
                save_session_provider_config(common.STATE_DIR, self._active_agent_id, target_state)
            self.engine = None
            common.emit({'type': 'status', 'message': f'✓ Model set to {arg}. Setup complete.', 'request_id': request_id})
            effective_onboarding = dict(onboarding)
            effective_onboarding.update(target_state)
            self._on_setup_complete(effective_onboarding, request_id)
        elif subcmd == 'shade-provider':
            if not arg:
                # Show shade provider picker
                common.emit({
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
                from charon.providers.model_registry import load_registry, save_registry
                reg = load_registry(common.STATE_DIR)
                reg['shade_model_mode'] = 'same'
                save_registry(common.STATE_DIR, reg)
                onboarding['step'] = 'complete'
                self._save_onboarding(onboarding)
                common.emit({'type': 'status', 'message': '✓ Shade will use same provider as main agent.', 'request_id': request_id})
                self._run_setup_command('complete', request_id)
            elif arg == 'api':
                onboarding['shade_provider'] = arg
                onboarding['step'] = 'shade-url'
                self._save_onboarding(onboarding)
                common.emit({'type': 'status', 'message': f'Shade provider set to {arg}. Now provide the base URL: /setup shade-url <url>', 'request_id': request_id})
            else:
                onboarding['shade_provider'] = arg
                onboarding['step'] = 'shade-model'
                self._save_onboarding(onboarding)
                common.emit({'type': 'status', 'message': f'Shade provider set to {arg}. Now pick a model: /setup shade-model <model_name>', 'request_id': request_id})
                self._run_setup_command('shade-model', request_id)
        elif subcmd == 'shade-url':
            if not arg:
                common.emit({'type': 'error', 'error': 'Usage: /setup shade-url <url>', 'request_id': request_id})
                return
            onboarding['shade_base_url'] = arg
            onboarding['step'] = 'shade-model'
            self._save_onboarding(onboarding)
            common.emit({'type': 'status', 'message': f'Shade base URL set to {arg}. Now pick a model: /setup shade-model <model_name>', 'request_id': request_id})
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
                                common.emit({'type': 'model_picker', 'models': models, 'provider': shade_provider, 'context': 'shade', 'request_id': request_id})
                                return
                    except Exception:
                        pass
                common.emit({'type': 'error', 'error': 'Usage: /setup shade-model <model_name>', 'request_id': request_id})
                return
            # Save model to registry
            from charon.providers.model_registry import load_registry, save_registry
            reg = load_registry(common.STATE_DIR)
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
            save_registry(common.STATE_DIR, reg)
            # Don't touch onboarding — shade config is independent
            common.emit({'type': 'status', 'message': f'✓ Shade model set to {arg} (provider: {reg.get("shade_provider", "auto")})', 'request_id': request_id})
            self._run_setup_command('complete', request_id)
        elif subcmd == 'project':
            onboarding['project'] = arg or str(common.ROOT)
            onboarding['step'] = 'complete'
            self._save_onboarding(onboarding)
            common.emit({'type': 'status', 'message': f'Project set to {arg or str(common.ROOT)}.', 'request_id': request_id})
        elif subcmd == 'auth-code':
            if not arg:
                common.emit({'type': 'error', 'error': 'Paste the authorization code: /setup auth-code <CODE>', 'request_id': request_id})
                return
            if not hasattr(self, '_pending_auth') or not self._pending_auth:
                common.emit({'type': 'error', 'error': 'No pending auth. Run /setup provider claude-code first.', 'request_id': request_id})
                return
            try:
                from charon.providers import charon_auth
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

                common.emit({'type': 'status', 'message': 'Exchanging code for tokens...', 'request_id': request_id})

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

                common.emit({'type': 'status', 'message': '✓ Authentication successful! Now run /setup model <model_name>', 'request_id': request_id})
            except Exception as e:
                common.emit({'type': 'error', 'error': f'Token exchange failed: {e}', 'request_id': request_id})
                common.emit({'type': 'status', 'message': 'You can also set an API key directly: /setup api-key <key>', 'request_id': request_id})
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
            common.emit({'type': 'status', 'message': 'API key saved.', 'request_id': request_id})
        elif subcmd == 'no-provider':
            onboarding['provider_mode'] = 'no-provider'
            onboarding['provider'] = ''
            onboarding['complete'] = True
            onboarding['step'] = 'done'
            self._save_onboarding(onboarding)
            self.engine = None
            self._on_setup_complete(onboarding, request_id)
        else:
            common.emit({'type': 'error', 'error': f'Unknown setup command: {subcmd}', 'request_id': request_id})

    def _repair_incomplete_onboarding_startup(self) -> dict:
        onboarding = common._load_json(common.STATE_DIR / 'onboarding.json', {})
        if not isinstance(onboarding, dict):
            return {}
        if onboarding.get('complete') or str(onboarding.get('provider_mode') or '').strip().lower() != 'provider':
            return onboarding

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
        common.emit({'type': 'status', 'message': f'Restored provider config: {repaired_provider}/{model or "(default model)"}'})
        return repaired

    def _find_charon_auth_token(self, provider_id: str) -> str | None:
        """Check Charon's own auth store for a valid token."""
        tokens = self._find_charon_auth_tokens(provider_id)
        access_token = str(tokens.get('access_token') or '').strip()
        if not access_token:
            return None
        if self._is_jwt_expired(access_token) and not str(tokens.get('refresh_token') or '').strip():
            return None
        return access_token

    def _find_charon_auth_tokens(self, provider_id: str) -> dict:
        """Return Charon's stored OAuth token bundle when it is reusable."""
        auth_file = common.STATE_DIR / 'auth' / 'auth.json'
        if not auth_file.exists():
            return {}
        try:
            store = json.loads(auth_file.read_text())
            provider_auth = store.get('providers', {}).get(provider_id, {})
            tokens = provider_auth.get('tokens', {})
            if isinstance(tokens, dict) and str(tokens.get('access_token') or '').strip():
                return dict(tokens)
        except Exception:
            pass
        return {}

    def _is_jwt_expired(self, token: str, *, skew_seconds: int = 60) -> bool:
        """Best-effort JWT expiry check. Non-JWT tokens are treated as not expired."""
        try:
            import base64
            parts = str(token or '').split('.')
            if len(parts) != 3:
                return False
            payload = parts[1] + '=' * (-len(parts[1]) % 4)
            data = json.loads(base64.urlsafe_b64decode(payload.encode('utf-8')))
            exp = int(data.get('exp') or 0)
            return bool(exp and exp <= int(time.time()) + skew_seconds)
        except Exception:
            return False

    def _find_claude_credentials(self) -> str | None:
        """Look for existing Claude Code credentials on this machine.
        Auto-refreshes expired tokens using the refresh token.
        """
        import os
        import time
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
        path = common.STATE_DIR / 'onboarding.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False))
        tmp.replace(path)

    def _on_setup_complete(self, onboarding: dict, request_id: str | None):
        """Post-setup: create default agent, detect other agents, report results.

        Mirrors pi-agent behavior: once configured, you're immediately ready to go.
        """
        provider_mode = str(onboarding.get('provider_mode') or '').lower()
        provider = str(onboarding.get('provider') or '').lower()
        project = str(onboarding.get('project') or str(common.ROOT)).strip()
        model = str(onboarding.get('model') or onboarding.get('provider_model') or '').strip()

        results = []

        # 1. Create default agent (unless no-provider mode or agent already exists)
        agent_created = None
        if provider_mode != 'no-provider':
            try:
                from charon.agents.agent_lifecycle import list_agents, create_agent
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
        try:
            sys.path.insert(0, str(common.ROOT / 'apps' / 'tui'))
            from process_inspector import detect_agent_processes
            procs = detect_agent_processes()
            if procs:
                results.append(f'Detected {len(procs)} running agent process(es)')
            else:
                results.append('No other agent processes detected')
        except Exception as e:
            results.append(f'Process detection failed: {e}')

        # 3. Sync to SQLite store
        try:
            from charon.infra.store_adapter import get_db, onboarding_set as db_onboarding_set
            db = get_db(common.STATE_DIR)
            db_onboarding_set(db, onboarding)
        except Exception:
            pass

        # 4. Emit setup complete event
        common.emit({
            'type': 'setup_complete',
            'provider': provider,
            'model': model,
            'agent': agent_created.get('name') if agent_created else None,
            'request_id': request_id,
        })

        # 5. Refresh the UI so dashboard shows the new agent
        self.handle_refresh(request_id)
