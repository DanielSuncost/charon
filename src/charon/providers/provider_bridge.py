"""Bridge from Charon's onboarding/auth config to the provider system.

Reads .charon_state/onboarding.json and .charon_state/auth/auth.json
to determine which provider and model to use, then returns ready-to-use
Provider and ModelInfo instances.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import os
import urllib.parse
from pathlib import Path

from charon.providers import ModelInfo, get_provider
from charon.providers import Provider
from charon.infra import config

try:
    from charon.infra.diagnostics import record as _diag
except Exception:  # diagnostics is best-effort and must never block import
    def _diag(*args, **kwargs):
        return None


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
    'gpt-5.5': 200000,
    'gpt-5.6': 200000,
    # GPT-5.6 variant line (Jul 2026: bare 'gpt-5.6' was replaced by
    # sol/terra/luna on the ChatGPT-account Codex backend)
    'gpt-5.6-sol': 200000,
    'gpt-5.6-terra': 200000,
    'gpt-5.6-luna': 200000,
    # GPT-6 Astra (Sep 3 2026 flagship). CONTEXT_WINDOWS is an exact-match
    # lookup, so without this entry astra falls back to DEFAULT_CONTEXT_WINDOW
    # (65536) — a 16x undercount against its real window, which would truncate
    # context on the one model whose selling point is the 1M+ window.
    'gpt-6-astra': 1050000,
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

ROUTE_PROVIDER_ALIASES = {
    'anthropic': ('anthropic', 'anthropic'),
    'claude-code': ('anthropic', 'claude-code'),
    'openai': ('openai', 'openai'),
    'codex': ('openai', 'codex'),
    'openai-codex': ('openai', 'codex'),
    'local': ('local', 'local'),
    'lmstudio': ('local', 'lmstudio'),
    'ollama': ('local', 'ollama'),
    'api': ('openai', 'api'),
    'openai-compatible': ('openai', 'api'),
    'opencode': ('openai', 'api'),
}
_CREDENTIAL_CACHE_SALT = os.urandom(32)


class ProviderRouteError(ValueError):
    """An explicit model route cannot be honored safely."""


def _route_credential_domain(provider_raw: str) -> str:
    if provider_raw in {'anthropic', 'claude-code'}:
        return 'anthropic'
    if provider_raw in {'local', 'lmstudio', 'ollama'}:
        return 'local'
    return provider_raw


def _credential_fingerprint(secret: str) -> str:
    return hmac.new(
        _CREDENTIAL_CACHE_SALT,
        str(secret or '').encode('utf-8'),
        hashlib.sha256,
    ).hexdigest()


def _read_json(path: Path, default=None):
    if not path.exists():
        return default or {}
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else (default or {})
    except Exception as e:
        _diag('provider_bridge', 'JSON read failed; using default', error=e, file=str(path))
        return default or {}


def resolve_provider_config(
    state_dir: Path,
    session_id: str | None = None,
    route_override: dict | None = None,
) -> dict:
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
    if route_override is not None:
        return resolve_route_override(
            state_dir,
            route_override,
            session_id=session_id,
        )

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
    supports_thinking = provider_name in ('anthropic', 'codex')

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

    env = config.local_model()
    if env:
        return env.split('/', 1)[1] if '/' in env else env

    return 'qwen3-30b-a3b'


def _detect_base_url(onboarding: dict) -> str:
    """Detect base URL for local/API providers."""
    env = config.local_base_url() or config.lmstudio_base_url()
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
            except Exception as e:
                _diag('provider_bridge', 'pi-agent auth.json unreadable; skipping pi token fallback', error=e)

    # 5. Local providers don't need a key
    if provider_name == 'local':
        return config.local_api_key()

    return ''


def _validate_route_base_url(value: str, provider: str) -> str:
    text = str(value or '').strip().rstrip('/')
    error = (
        f"routed provider {provider!r} requires a credential-free http(s) "
        'base_url without query or fragment'
    )
    try:
        parsed = urllib.parse.urlsplit(text)
        _port = parsed.port
    except ValueError as exc:
        raise ProviderRouteError(error) from exc
    if (
        parsed.scheme not in ('http', 'https')
        or not parsed.netloc
        or not parsed.hostname
        or any(char.isspace() for char in text)
        or parsed.username is not None
        or parsed.password is not None
        or '?' in text
        or '#' in text
    ):
        raise ProviderRouteError(error)
    if parsed.scheme == 'http':
        hostname = str(parsed.hostname or '').lower()
        try:
            loopback = hostname == 'localhost' or ipaddress.ip_address(
                hostname
            ).is_loopback
        except ValueError:
            loopback = hostname == 'localhost'
        if not loopback:
            raise ProviderRouteError(
                f"routed provider {provider!r} requires TLS for "
                'non-loopback base_url'
            )
    return text


def resolve_route_override(
    state_dir: Path,
    route_override: dict,
    *,
    session_id: str | None = None,
) -> dict:
    """Validate an explicit routing decision and resolve its credentials.

    Unlike ordinary onboarding resolution, this path never substitutes a
    different provider or model and never returns ``ready=False``.
    """
    if not isinstance(route_override, dict):
        raise ProviderRouteError('model route must be an object')
    if route_override.get('resolver'):
        raise ProviderRouteError(
            'model route resolver metadata requires agent execution'
        )
    if 'api_key' in route_override:
        raise ProviderRouteError(
            'model routes must not contain api_key; configure credentials '
            'through provider auth'
        )

    provider_alias = str(
        route_override.get('provider')
        or route_override.get('provider_name')
        or ''
    ).strip().lower()
    if provider_alias not in ROUTE_PROVIDER_ALIASES:
        supported = ', '.join(sorted(ROUTE_PROVIDER_ALIASES))
        raise ProviderRouteError(
            f"unsupported routed provider {provider_alias or '(missing)'!r}; "
            f"expected one of: {supported}"
        )
    provider_name, provider_raw = ROUTE_PROVIDER_ALIASES[provider_alias]

    model_id = str(
        route_override.get('model_id') or route_override.get('model') or ''
    ).strip()
    if not model_id:
        raise ProviderRouteError(
            f"routed provider {provider_alias!r} requires model_id"
        )

    context_value = route_override.get('context_window')
    if context_value is None:
        context_window = CONTEXT_WINDOWS.get(model_id, DEFAULT_CONTEXT_WINDOW)
    elif isinstance(context_value, int) and not isinstance(context_value, bool):
        context_window = context_value
    elif isinstance(context_value, str):
        context_text = context_value.strip()
        if not context_text or any(
            char < '0' or char > '9'
            for char in context_text
        ):
            raise ProviderRouteError(
                'routed context_window must be a positive integer'
            )
        context_window = int(context_text)
    else:
        raise ProviderRouteError(
            'routed context_window must be a positive integer'
        )
    if context_window < 1 or context_window > 10_000_000:
        raise ProviderRouteError(
            'routed context_window must be between 1 and 10000000'
        )

    state_dir = Path(state_dir)
    onboarding = _read_json(state_dir / 'onboarding.json')
    session_override = load_session_provider_config(state_dir, session_id)
    auth_store = _read_json(state_dir / 'auth' / 'auth.json')
    effective = dict(onboarding)
    if session_override:
        effective.update({
            key: value for key, value in session_override.items()
            if value is not None
        })

    credential_config = dict(effective)
    configured_alias = str(credential_config.get('provider') or '').strip().lower()
    configured = ROUTE_PROVIDER_ALIASES.get(configured_alias)
    if (
        configured is None
        or _route_credential_domain(configured[1])
        != _route_credential_domain(provider_raw)
    ):
        credential_config.pop('api_key', None)
    if provider_raw == 'codex':
        # The Codex endpoint requires a ChatGPT OAuth token. A regular
        # OPENAI_API_KEY is not a credential for this provider.
        codex_auth = (
            auth_store.get('providers', {})
            .get('openai-codex', {})
        )
        codex_tokens = codex_auth.get('tokens', {})
        api_key = str(
            codex_tokens.get('access_token')
            or codex_auth.get('api_key')
            or (
                credential_config.get('api_key')
                if configured and configured[1] == 'codex'
                else ''
            )
            or ''
        ).strip()
    else:
        api_key = _resolve_api_key(
            provider_name,
            provider_raw,
            credential_config,
            auth_store,
        )

    explicit_base = str(
        route_override.get('base_url')
        or route_override.get('provider_base_url')
        or ''
    ).strip()
    if provider_name == 'anthropic':
        fixed_base = 'https://api.anthropic.com/v1/messages'
        if (
            explicit_base
            and _validate_route_base_url(explicit_base, provider_alias)
            != fixed_base
        ):
            raise ProviderRouteError(
                f"routed provider {provider_alias!r} cannot honor base_url "
                f"{explicit_base!r}"
            )
        base_url = fixed_base
    elif provider_raw == 'codex':
        fixed_base = 'https://chatgpt.com/backend-api/codex/responses'
        if (
            explicit_base
            and _validate_route_base_url(explicit_base, provider_alias)
            != fixed_base
        ):
            raise ProviderRouteError(
                f"routed provider {provider_alias!r} cannot honor base_url "
                f"{explicit_base!r}"
            )
        base_url = fixed_base
    elif provider_raw == 'openai':
        fixed_base = 'https://api.openai.com/v1'
        if (
            explicit_base
            and _validate_route_base_url(explicit_base, provider_alias)
            != fixed_base
        ):
            raise ProviderRouteError(
                f"routed provider {provider_alias!r} cannot honor base_url "
                f"{explicit_base!r}"
            )
        base_url = fixed_base
    elif provider_raw == 'api' and explicit_base:
        base_url = _validate_route_base_url(explicit_base, provider_alias)
    elif provider_raw == 'api':
        raise ProviderRouteError(
            f"routed provider {provider_alias!r} requires base_url"
        )
    elif provider_name == 'local':
        configured_base = (
            config.local_base_url()
            or config.lmstudio_base_url()
            or (
                str(effective.get('provider_base_url') or '').strip()
                if configured is not None
                and _route_credential_domain(configured[1]) == 'local'
                else ''
            )
            or (
                'http://127.0.0.1:11434/v1'
                if (
                    configured_alias == 'ollama'
                    or (
                        configured not in {
                            ('local', 'local'),
                            ('local', 'lmstudio'),
                            ('local', 'ollama'),
                        }
                        and provider_alias == 'ollama'
                    )
                )
                else config.DEFAULT_LOCAL_BASE_URL
            )
        )
        configured_base = _validate_route_base_url(
            configured_base,
            provider_alias,
        )
        if (
            explicit_base
            and _validate_route_base_url(explicit_base, provider_alias)
            != configured_base
        ):
            raise ProviderRouteError(
                f"routed provider {provider_alias!r} base_url is not the "
                'configured local endpoint'
            )
        base_url = configured_base
    else:
        base_url = 'https://api.openai.com/v1'

    if provider_raw == 'api':
        configured_route = ROUTE_PROVIDER_ALIASES.get(configured_alias)
        configured_base_raw = str(
            effective.get('provider_base_url')
            or config.local_base_url()
            or config.lmstudio_base_url()
            or ''
        ).strip()
        if (
            configured_route is None
            or configured_route[1] != 'api'
            or not configured_base_raw
            or _validate_route_base_url(
                configured_base_raw,
                configured_alias,
            )
            != base_url
        ):
            raise ProviderRouteError(
                f"routed provider {provider_alias!r} must match a configured "
                'custom endpoint before credentials can be used'
            )
        configured_auth = (
            auth_store.get('providers', {}).get(configured_alias, {})
        )
        configured_tokens = configured_auth.get('tokens', {})
        api_key = str(
            configured_tokens.get('access_token')
            or configured_auth.get('api_key')
            or effective.get('api_key')
            or ''
        ).strip()

    if provider_name != 'local' and not api_key:
        raise ProviderRouteError(
            f"routed provider {provider_alias!r} is unavailable: "
            'missing credentials'
        )
    if provider_name == 'local' and not api_key:
        api_key = 'not-needed'

    supports_thinking = provider_name == 'anthropic' or provider_raw == 'codex'
    selected_endpoint = {
        'candidate_id': str(route_override.get('candidate_id') or ''),
        'provider': provider_alias,
        'model_id': model_id,
        'context_window': context_window,
        'base_url': base_url,
    }
    return {
        'provider_name': provider_name,
        'provider_raw': provider_raw,
        'route_provider': provider_alias,
        'model_id': model_id,
        'api_key': api_key,
        'base_url': base_url,
        'context_window': context_window,
        'supports_thinking': supports_thinking,
        'ready': True,
        'session_id': session_id or '',
        'session_override': bool(session_override),
        'route_override': True,
        'selected_endpoint': selected_endpoint,
        'credential_fingerprint': _credential_fingerprint(api_key),
    }


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
        except Exception as e:
            _diag('provider_bridge', 'auth store refresh-token read failed', error=e)

    # Claude-backed providers: prefer Claude Code credentials before pi auth.
    if provider_raw in ('claude-code', 'anthropic'):
        claude_creds = Path.home() / '.claude' / '.credentials.json'
        if claude_creds.exists():
            try:
                cred = json.loads(claude_creds.read_text())
                rt = cred.get('claudeAiOauth', {}).get('refreshToken', '').strip()
                if rt:
                    return rt
            except Exception as e:
                _diag('provider_bridge', 'Claude credentials refresh-token read failed', error=e)

    # Check pi-agent's auth store
    pi_auth = Path.home() / '.pi' / 'agent' / 'auth.json'
    if pi_auth.exists():
        try:
            pi_data = json.loads(pi_auth.read_text())
            rt = pi_data.get('anthropic', {}).get('refresh', '').strip()
            if rt:
                return rt
        except Exception as e:
            _diag('provider_bridge', 'pi-agent refresh-token read failed', error=e)

    # Fallback to Claude's credentials for any remaining Anthropic-ish callers
    claude_creds = Path.home() / '.claude' / '.credentials.json'
    if claude_creds.exists():
        try:
            cred = json.loads(claude_creds.read_text())
            rt = cred.get('claudeAiOauth', {}).get('refreshToken', '').strip()
            if rt:
                return rt
        except Exception as e:
            _diag('provider_bridge', 'Claude credentials fallback read failed', error=e)
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
                state_dir = config.state_dir() or (Path.home() / '.charon_state')
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
                    except Exception as e:
                        _diag('provider_bridge', 'failed to write refreshed token to auth store', error=e, provider=provider_raw)

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
                    except Exception as e:
                        _diag('provider_bridge', 'failed to sync refreshed token to .claude credentials', error=e)

                return new_token
    except Exception as e:
        _diag('provider_bridge', 'token refresh flow failed; returning no token', error=e, provider=provider_raw)
    return None


def create_provider_and_model(
    state_dir: Path,
    session_id: str | None = None,
    route_override: dict | None = None,
) -> tuple[Provider, ModelInfo, bool]:
    """Create a Provider and ModelInfo from the current config.

    Returns (provider, model_info, ready).
    ready=False means heuristic mode (no LLM available).
    """
    provider_config = resolve_provider_config(
        state_dir,
        session_id=session_id,
        route_override=route_override,
    )

    model = ModelInfo(
        provider=provider_config.get('route_provider') or provider_config['provider_name'],
        model_id=provider_config['model_id'],
        context_window=provider_config['context_window'],
        supports_thinking=provider_config['supports_thinking'],
    )

    if not provider_config['ready']:
        # Return a local provider as fallback (may or may not be running)
        try:
            provider = get_provider('local')
        except Exception as e:
            _diag('provider_bridge', "get_provider('local') failed; constructing HttpxOpenAIProvider directly", error=e)
            from charon.providers.httpx_openai import HttpxOpenAIProvider
            provider = HttpxOpenAIProvider()
        return provider, model, False

    provider_name = provider_config['provider_name']

    if provider_name == 'anthropic':
        # CRITICAL: share a single Anthropic provider instance
        # OAuth refresh tokens are single-use — multiple instances
        # would race and invalidate each other's tokens
        if provider_config.get('route_override'):
            from charon.providers.httpx_anthropic import HttpxAnthropicProvider
            raw = provider_config.get('provider_raw', 'claude-code')
            refresh_token = _get_refresh_token(state_dir, raw)
            auth_store = str(state_dir / 'auth' / 'auth.json')
            cache = getattr(
                create_provider_and_model,
                '_routed_anthropic_providers',
                {},
            )
            cache_key = (
                raw,
                provider_config['api_key'],
                refresh_token or '',
                auth_store,
            )
            provider = cache.get(cache_key)
            if provider is None:
                provider = HttpxAnthropicProvider(
                    api_key=provider_config['api_key'],
                    refresh_token=refresh_token,
                    auth_store_path=auth_store,
                )
                cache[cache_key] = provider
                create_provider_and_model._routed_anthropic_providers = cache
        elif not hasattr(create_provider_and_model, '_anthropic_provider'):
            from charon.providers.httpx_anthropic import HttpxAnthropicProvider
            raw = provider_config.get('provider_raw', 'claude-code')
            refresh_token = _get_refresh_token(state_dir, raw)
            auth_store = str(state_dir / 'auth' / 'auth.json')
            create_provider_and_model._anthropic_provider = HttpxAnthropicProvider(
                api_key=provider_config['api_key'],
                refresh_token=refresh_token,
                auth_store_path=auth_store,
            )
        if not provider_config.get('route_override'):
            provider = create_provider_and_model._anthropic_provider
    elif provider_name == 'local':
        from charon.providers.httpx_openai import HttpxOpenAIProvider
        provider = HttpxOpenAIProvider(
            base_url=provider_config.get('base_url') or 'http://127.0.0.1:1234/v1',
            api_key=provider_config['api_key'],
        )
    elif provider_name == 'openai' and provider_config.get('provider_raw') == 'codex':
        # Codex OAuth uses chatgpt.com/backend-api/codex/responses (not api.openai.com)
        from charon.providers.httpx_codex import HttpxCodexProvider
        raw = provider_config.get('provider_raw', 'codex')
        refresh_token = _get_refresh_token(state_dir, raw)
        auth_store = str(state_dir / 'auth' / 'auth.json')
        provider = HttpxCodexProvider(
            api_key=provider_config['api_key'],
            refresh_token=refresh_token,
            auth_store_path=auth_store,
        )
    else:
        # OpenAI or any OpenAI-compatible
        from charon.providers.httpx_openai import HttpxOpenAIProvider
        base_url = provider_config.get('base_url') or 'https://api.openai.com/v1'
        provider = HttpxOpenAIProvider(
            base_url=base_url,
            api_key=provider_config['api_key'],
        )

    if provider_config.get('route_override'):
        provider._charon_endpoint = dict(provider_config['selected_endpoint'])
    return provider, model, True


def describe_provider_endpoint(provider: Provider, model: ModelInfo) -> dict:
    """Return the non-secret endpoint that will actually receive a request."""
    routed = getattr(provider, '_charon_endpoint', None)
    if isinstance(routed, dict):
        endpoint = dict(routed)
    else:
        provider_type = type(provider).__name__
        model_provider = str(model.provider or '').strip().lower()
        base_url = str(getattr(provider, '_base_url', '') or '').rstrip('/')
        if provider_type == 'HttpxCodexProvider':
            provider_alias = 'codex'
            base_url = 'https://chatgpt.com/backend-api/codex/responses'
        elif provider_type in {'AnthropicProvider', 'HttpxAnthropicProvider'}:
            provider_alias = 'anthropic'
            base_url = 'https://api.anthropic.com/v1/messages'
        elif provider_type == 'HttpxOpenAIProvider':
            if model_provider in {'local', 'lmstudio', 'ollama'}:
                provider_alias = model_provider
            elif base_url == 'https://api.openai.com/v1':
                provider_alias = 'openai'
            else:
                provider_alias = 'api'
        elif model_provider in ROUTE_PROVIDER_ALIASES:
            provider_alias = model_provider
        else:
            provider_alias = model_provider
        endpoint = {
            'candidate_id': '',
            'provider': provider_alias,
            'model_id': str(model.model_id or ''),
            'context_window': int(model.context_window),
            'base_url': base_url,
        }
    endpoint['model_id'] = str(model.model_id)
    endpoint['context_window'] = int(model.context_window)
    endpoint.setdefault('provider', str(model.provider or ''))
    endpoint.setdefault('base_url', str(getattr(provider, '_base_url', '') or ''))
    endpoint.pop('api_key', None)
    return endpoint


def credential_fingerprint_for_provider(provider: Provider) -> str:
    """Return a process-local, non-reversible provider credential identity."""
    return _credential_fingerprint(str(getattr(provider, '_api_key', '') or ''))
