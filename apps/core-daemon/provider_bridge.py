"""Bridge from Charon's onboarding/auth config to the provider system.

Reads .charon_state/onboarding.json and .charon_state/auth/auth.json
to determine which provider and model to use, then returns ready-to-use
Provider and ModelInfo instances.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from providers import ModelInfo, get_provider
from providers import Provider


def _session_provider_dir(state_dir: Path) -> Path:
    d = Path(state_dir) / 'session_providers'
    d.mkdir(parents=True, exist_ok=True)
    return d


def _safe_session_id(session_id: str) -> str:
    return ''.join(c if c.isalnum() or c in ('-', '_', '.') else '_' for c in str(session_id or '').strip())


def _session_provider_path(state_dir: Path, session_id: str) -> Path:
    return _session_provider_dir(state_dir) / f'{_safe_session_id(session_id)}.json'


def load_session_provider_config(state_dir: Path, session_id: str | None) -> dict:
    if not session_id:
        return {}
    return _read_json(_session_provider_path(Path(state_dir), session_id), {})


def save_session_provider_config(state_dir: Path, session_id: str, config: dict) -> None:
    path = _session_provider_path(Path(state_dir), session_id)
    path.write_text(json.dumps(config, indent=2))


def clear_session_provider_config(state_dir: Path, session_id: str) -> None:
    try:
        _session_provider_path(Path(state_dir), session_id).unlink()
    except FileNotFoundError:
        pass


# Known model context windows (conservative defaults)
CONTEXT_WINDOWS = {
    # Anthropic — all Claude models support 200k context
    'claude-sonnet-4-6': 200000,
    'claude-opus-4-6': 200000,
    'claude-sonnet-4-5': 200000,
    'claude-opus-4-5': 200000,
    'claude-opus-4-1': 200000,
    'claude-sonnet-4-20250514': 200000,
    'claude-opus-4-20250514': 200000,
    'claude-haiku-4.5': 200000,
    'claude-3-7-sonnet-20250219': 200000,
    'claude-3-5-sonnet-20241022': 200000,
    'claude-3-5-haiku-20241022': 200000,
    # OpenAI
    'gpt-4.1': 1000000,
    'gpt-4o': 128000,
    'gpt-4o-mini': 128000,
    'o3': 200000,
    'o4-mini': 200000,
    'o3-mini': 200000,
    'codex-mini-latest': 200000,
    # GPT-5 family
    'gpt-5': 200000,
    'gpt-5.4': 200000,
    # Local (conservative defaults)
    'qwen3-30b-a3b': 65536,
}

DEFAULT_CONTEXT_WINDOW = 65536

# Provider ID → our provider name mapping
PROVIDER_MAP = {
    'claude-code': 'anthropic',
    'anthropic': 'anthropic',
    'codex': 'openai',
    'openai': 'openai',
    'openai-codex': 'openai',
    'lmstudio': 'local',
    'local': 'local',
    'ollama': 'local',
    'api': 'openai',        # generic API → OpenAI-compatible
    'opencode': 'openai',   # opencode uses OpenAI-compat
}

# Default models per provider
DEFAULT_MODELS = {
    'anthropic': 'claude-sonnet-4-20250514',
    'openai': 'gpt-4o',
    'local': 'qwen3-30b-a3b',
}


def _read_json(path: Path, default=None):
    if not path.exists():
        return default or {}
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else (default or {})
    except Exception:
        return default or {}


def resolve_provider_config(state_dir: Path, session_id: str | None = None) -> dict:
    """Read onboarding + optional session override auth config and return a unified config dict.

    Returns:
        {
            'provider_name': str,   # 'anthropic', 'openai', 'local'
            'model_id': str,        # 'claude-sonnet-4-20250514', etc.
            'api_key': str,         # resolved API key
            'base_url': str | None, # for OpenAI-compatible endpoints
            'context_window': int,
            'supports_thinking': bool,
            'ready': bool,          # True if we have enough to make LLM calls
        }
    """
    state_dir = Path(state_dir)
    onboarding = _read_json(state_dir / 'onboarding.json')
    session_override = load_session_provider_config(state_dir, session_id)
    auth_store = _read_json(state_dir / 'auth' / 'auth.json')

    effective = dict(onboarding)
    if session_override:
        effective.update({k: v for k, v in session_override.items() if v is not None})

    provider_mode = str(effective.get('provider_mode') or '').strip().lower()
    provider_raw = str(effective.get('provider') or '').strip().lower()
    complete = bool(effective.get('complete'))

    # Not configured yet
    if not complete or provider_mode == 'no-provider' or not provider_raw:
        return {
            'provider_name': 'local',
            'model_id': _detect_model_fallback(effective),
            'api_key': 'not-needed',
            'base_url': _detect_base_url(effective),
            'context_window': DEFAULT_CONTEXT_WINDOW,
            'supports_thinking': False,
            'ready': False,
        }

    provider_name = PROVIDER_MAP.get(provider_raw, 'local')
    model_id = _detect_model(effective, provider_name)
    api_key = _resolve_api_key(provider_name, provider_raw, effective, auth_store)
    base_url = _detect_base_url(effective) if provider_name == 'local' else None
    context_window = CONTEXT_WINDOWS.get(model_id, DEFAULT_CONTEXT_WINDOW)
    supports_thinking = provider_name == 'anthropic'

    # For API/opencode providers, we might need a custom base URL
    if provider_raw in ('api', 'opencode') and not base_url:
        base_url = str(effective.get('provider_base_url') or '').strip() or None

    return {
        'provider_name': provider_name,
        'provider_raw': provider_raw,
        'model_id': model_id,
        'api_key': api_key,
        'base_url': base_url,
        'context_window': context_window,
        'supports_thinking': supports_thinking,
        'ready': bool(api_key),
        'session_id': session_id or '',
        'session_override': bool(session_override),
    }


def _detect_model(onboarding: dict, provider_name: str) -> str:
    """Resolve model from onboarding config."""
    for key in ('provider_model', 'model', 'opencode_model'):
        val = str(onboarding.get(key) or '').strip()
        if val:
            # Strip provider prefix like "lmstudio/model-name"
            if '/' in val:
                val = val.split('/', 1)[1]
            return val
    return DEFAULT_MODELS.get(provider_name, 'gpt-4o')


def _detect_model_fallback(onboarding: dict) -> str:
    """Detect model for unconfigured setups."""
    for key in ('provider_model', 'model', 'opencode_model'):
        val = str(onboarding.get(key) or '').strip()
        if val:
            if '/' in val:
                val = val.split('/', 1)[1]
            return val

    env = os.environ.get('CHARON_LOCAL_MODEL', '').strip()
    if env:
        return env.split('/', 1)[1] if '/' in env else env

    return 'qwen3-30b-a3b'


def _detect_base_url(onboarding: dict) -> str:
    """Detect base URL for local/API providers."""
    env = os.environ.get('CHARON_LOCAL_BASE_URL') or os.environ.get('CHARON_LMSTUDIO_BASE_URL')
    if env:
        return env.strip().rstrip('/')

    url = str(onboarding.get('provider_base_url') or '').strip()
    if url:
        return url.rstrip('/')

    return 'http://127.0.0.1:1234/v1'


def _resolve_api_key(
    provider_name: str,
    provider_raw: str,
    onboarding: dict,
    auth_store: dict,
) -> str:
    """Resolve API key from env vars, auth store, or onboarding config."""

    # 1. Environment variables (highest priority)
    env_keys = {
        'anthropic': 'ANTHROPIC_API_KEY',
        'openai': 'OPENAI_API_KEY',
    }
    env_var = env_keys.get(provider_name)
    if env_var:
        val = os.environ.get(env_var, '').strip()
        if val:
            return val

    # 2. Auth store (OAuth tokens)
    providers_store = auth_store.get('providers', {})

    # Map provider_raw to auth store key
    auth_keys = {
        'claude-code': 'anthropic',
        'codex': 'openai-codex',
    }
    auth_key = auth_keys.get(provider_raw, provider_raw)
    provider_auth = providers_store.get(auth_key, {})

    if provider_auth:
        tokens = provider_auth.get('tokens', {})
        # OAuth access token
        access_token = tokens.get('access_token', '').strip()
        if access_token:
            return access_token
        # Direct API key
        api_key = provider_auth.get('api_key', '').strip()
        if api_key:
            return api_key

    # 3. Onboarding config (direct API key entry)
    api_key = str(onboarding.get('api_key') or '').strip()
    if api_key:
        return api_key

    # 4. Pi-agent's auth store — only use if we have NO Charon token at all
    # (pi-agent may refresh its token independently, causing conflicts)
    if provider_name == 'anthropic' and not api_key:
        pi_auth_file = Path.home() / '.pi' / 'agent' / 'auth.json'
        if pi_auth_file.exists():
            try:
                pi_data = json.loads(pi_auth_file.read_text())
                pi_token = pi_data.get('anthropic', {}).get('access', '').strip()
                if pi_token:
                    return pi_token
            except Exception:
                pass

    # 5. Local providers don't need a key
    if provider_name == 'local':
        return os.environ.get('CHARON_LOCAL_API_KEY', 'not-needed')

    return ''


def _get_refresh_token(state_dir: Path, provider_raw: str) -> str | None:
    """Get the refresh token for a provider from auth stores.

    Important: for Claude/Anthropic flows, prefer Claude's own credentials over
    pi-agent's auth store. pi may have its own independently-rotated Anthropic
    refresh token, and using that for Charon's Claude flow causes refresh/auth
    mismatches.
    """
    state_dir = Path(state_dir)
    # Check Charon's auth store
    auth_file = state_dir / 'auth' / 'auth.json'
    if auth_file.exists():
        try:
            store = json.loads(auth_file.read_text())
            auth_keys = {'claude-code': 'anthropic', 'codex': 'openai-codex'}
            auth_key = auth_keys.get(provider_raw, provider_raw)
            tokens = store.get('providers', {}).get(auth_key, {}).get('tokens', {})
            rt = tokens.get('refresh_token', '').strip()
            if rt:
                return rt
        except Exception:
            pass

    # Claude-backed providers: prefer Claude Code credentials before pi auth.
    if provider_raw in ('claude-code', 'anthropic'):
        claude_creds = Path.home() / '.claude' / '.credentials.json'
        if claude_creds.exists():
            try:
                cred = json.loads(claude_creds.read_text())
                rt = cred.get('claudeAiOauth', {}).get('refreshToken', '').strip()
                if rt:
                    return rt
            except Exception:
                pass

    # Check pi-agent's auth store
    pi_auth = Path.home() / '.pi' / 'agent' / 'auth.json'
    if pi_auth.exists():
        try:
            pi_data = json.loads(pi_auth.read_text())
            rt = pi_data.get('anthropic', {}).get('refresh', '').strip()
            if rt:
                return rt
        except Exception:
            pass

    # Fallback to Claude's credentials for any remaining Anthropic-ish callers
    claude_creds = Path.home() / '.claude' / '.credentials.json'
    if claude_creds.exists():
        try:
            cred = json.loads(claude_creds.read_text())
            rt = cred.get('claudeAiOauth', {}).get('refreshToken', '').strip()
            if rt:
                return rt
        except Exception:
            pass
    return None


def _refresh_token(provider_raw: str, refresh_token: str) -> str | None:
    """Refresh an expired OAuth token. Returns new access token or None."""
    try:
        import httpx

        token_urls = {
            'claude-code': 'https://platform.claude.com/v1/oauth/token',
            'codex': 'https://auth.openai.com/oauth/token',
        }
        client_ids = {
            'claude-code': '9d1c250a-e61b-44d9-88ed-5944d1962f5e',
            'codex': 'app_EMoamEEZ73f0CkXaXp7hrann',
        }

        token_url = token_urls.get(provider_raw)
        client_id = client_ids.get(provider_raw)
        if not token_url or not client_id:
            return None

        if provider_raw == 'claude-code':
            resp = httpx.post(token_url, json={
                'grant_type': 'refresh_token',
                'client_id': client_id,
                'refresh_token': refresh_token,
            }, headers={'Accept': 'application/json'}, timeout=30.0)
        else:
            resp = httpx.post(token_url, data={
                'grant_type': 'refresh_token',
                'client_id': client_id,
                'refresh_token': refresh_token,
            }, timeout=30.0)

        if resp.status_code == 200:
            data = resp.json()
            new_token = data.get('access_token', '')
            if new_token:
                # Update auth store
                import time
                state_dir = Path.home() / '.charon_state' if not os.environ.get('CHARON_STATE_DIR') else Path(os.environ['CHARON_STATE_DIR'])
                auth_file = state_dir / 'auth' / 'auth.json'
                if auth_file.exists():
                    try:
                        store = json.loads(auth_file.read_text())
                        auth_keys = {'claude-code': 'anthropic', 'codex': 'openai-codex'}
                        auth_key = auth_keys.get(provider_raw, provider_raw)
                        if auth_key in store.get('providers', {}):
                            store['providers'][auth_key]['tokens']['access_token'] = new_token
                            if data.get('refresh_token'):
                                store['providers'][auth_key]['tokens']['refresh_token'] = data['refresh_token']
                            store['providers'][auth_key]['last_login'] = time.strftime('%Y-%m-%dT%H:%M:%S+00:00')
                            auth_file.write_text(json.dumps(store, indent=2))
                    except Exception:
                        pass

                # Also update Claude's credentials file
                if provider_raw == 'claude-code':
                    try:
                        cred_path = Path.home() / '.claude' / '.credentials.json'
                        if cred_path.exists():
                            cred_data = json.loads(cred_path.read_text())
                            cred_data['claudeAiOauth']['accessToken'] = new_token
                            if data.get('refresh_token'):
                                cred_data['claudeAiOauth']['refreshToken'] = data['refresh_token']
                            cred_data['claudeAiOauth']['expiresAt'] = int(time.time() * 1000) + data.get('expires_in', 3600) * 1000
                            cred_path.write_text(json.dumps(cred_data))
                    except Exception:
                        pass

                return new_token
    except Exception:
        pass
    return None


def create_provider_and_model(state_dir: Path, session_id: str | None = None) -> tuple[Provider, ModelInfo, bool]:
    """Create a Provider and ModelInfo from the current config.

    Returns (provider, model_info, ready).
    ready=False means heuristic mode (no LLM available).
    """
    config = resolve_provider_config(state_dir, session_id=session_id)

    model = ModelInfo(
        provider=config['provider_name'],
        model_id=config['model_id'],
        context_window=config['context_window'],
        supports_thinking=config['supports_thinking'],
    )

    if not config['ready']:
        # Return a local provider as fallback (may or may not be running)
        try:
            provider = get_provider('local')
        except Exception:
            from providers.httpx_openai import HttpxOpenAIProvider
            provider = HttpxOpenAIProvider()
        return provider, model, False

    provider_name = config['provider_name']

    if provider_name == 'anthropic':
        # CRITICAL: share a single Anthropic provider instance
        # OAuth refresh tokens are single-use — multiple instances
        # would race and invalidate each other's tokens
        if not hasattr(create_provider_and_model, '_anthropic_provider'):
            from providers.httpx_anthropic import HttpxAnthropicProvider
            raw = config.get('provider_raw', 'claude-code')
            refresh_token = _get_refresh_token(state_dir, raw)
            auth_store = str(state_dir / 'auth' / 'auth.json')
            create_provider_and_model._anthropic_provider = HttpxAnthropicProvider(
                api_key=config['api_key'],
                refresh_token=refresh_token,
                auth_store_path=auth_store,
            )
        provider = create_provider_and_model._anthropic_provider
    elif provider_name == 'local':
        from providers.httpx_openai import HttpxOpenAIProvider
        provider = HttpxOpenAIProvider(
            base_url=config.get('base_url') or 'http://127.0.0.1:1234/v1',
            api_key=config['api_key'],
        )
    elif provider_name == 'openai' and config.get('provider_raw') == 'codex':
        # Codex OAuth uses chatgpt.com/backend-api/codex/responses (not api.openai.com)
        from providers.httpx_codex import HttpxCodexProvider
        raw = config.get('provider_raw', 'codex')
        refresh_token = _get_refresh_token(state_dir, raw)
        auth_store = str(state_dir / 'auth' / 'auth.json')
        provider = HttpxCodexProvider(
            api_key=config['api_key'],
            refresh_token=refresh_token,
            auth_store_path=auth_store,
        )
    else:
        # OpenAI or any OpenAI-compatible
        from providers.httpx_openai import HttpxOpenAIProvider
        base_url = config.get('base_url') or 'https://api.openai.com/v1'
        provider = HttpxOpenAIProvider(
            base_url=base_url,
            api_key=config['api_key'],
        )

    return provider, model, True
