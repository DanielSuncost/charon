"""Which models a signed-in provider actually offers, from its own listing.

The model picker used to be a hand-maintained list per provider, so every new
release meant a code change. This module asks the provider instead:

* ``claude-code`` / ``anthropic`` — ``GET https://api.anthropic.com/v1/models``.
  An API key uses ``x-api-key``; a Claude subscription OAuth token is sent the
  way ``httpx_anthropic`` sends it for inference (Bearer + the oauth beta).
* ``codex`` — ``GET https://chatgpt.com/backend-api/codex/models``, the listing
  the Codex CLI itself reads, with the ChatGPT OAuth token and account id. Each
  entry carries ``supported_reasoning_levels``, which is fed to
  ``charon.providers.effort`` so a new model gets its real ladder.
* ``openai`` / ``api`` / ``opencode`` / ``openrouter`` / ``lmstudio`` — the
  OpenAI-compatible ``GET {base_url}/models`` (OpenRouter's is public).
* ``deepseek`` — ``GET https://api.deepseek.com/models`` with the API key (401
  without one). Entries carry ``context_window`` and ``effort.supported_levels``,
  fed to ``charon.providers.effort`` like Codex's.

Results are cached in ``<state>/model_catalog.json`` with a TTL. When a fetch
fails (offline, outage, an endpoint that refuses the token) the picker falls
back to the last good listing, then to ``STATIC_MODELS``; the failure is kept
in the cache entry so the UI can say plainly that discovery did not work
instead of presenting the static list as live.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from charon.providers import deepseek, effort

try:
    from charon.infra.diagnostics import record as _diag
except Exception:  # diagnostics is best-effort and must never block import
    def _diag(*args, **kwargs):
        return None

CACHE_FILE = 'model_catalog.json'
DEFAULT_TTL_SECONDS = 24 * 3600
# After a failed fetch, don't retry on every picker open (an offline run would
# otherwise stall each /model for the full timeout).
FAILURE_BACKOFF_SECONDS = 10 * 60
FETCH_TIMEOUT_SECONDS = 8.0

ANTHROPIC_MODELS_URL = 'https://api.anthropic.com/v1/models'
CODEX_MODELS_URL = 'https://chatgpt.com/backend-api/codex/models'
# The Codex listing hides models newer than the client asks for.
CODEX_CLIENT_VERSION = '0.163.0'
OPENROUTER_BASE_URL = 'https://openrouter.ai/api/v1'
DEEPSEEK_BASE_URL = deepseek.BASE_URL
OPENAI_BASE_URL = 'https://api.openai.com/v1'
LMSTUDIO_BASE_URL = 'http://127.0.0.1:1234/v1'

# Offline / pre-sign-in fallback. Order matters: the first entry is what the
# picker highlights. Discovery replaces this list whenever it succeeds.
STATIC_MODELS: dict[str, list[dict[str, str]]] = {
    'claude-code': [
        {'id': 'claude-opus-5-5', 'desc': 'Opus 5.5'},
        {'id': 'claude-sonnet-5-5', 'desc': 'Sonnet 5.5'},
        {'id': 'claude-haiku-5-5', 'desc': 'Haiku 5.5 — fastest'},
        {'id': 'claude-fable-5-1', 'desc': 'Fable 5.1'},
        {'id': 'claude-sonnet-4-6', 'desc': 'Sonnet 4.6'},
        {'id': 'claude-opus-4-6', 'desc': 'Opus 4.6'},
        {'id': 'claude-sonnet-4-5', 'desc': 'Sonnet 4.5'},
        {'id': 'claude-opus-4-5', 'desc': 'Opus 4.5'},
        {'id': 'claude-haiku-4-5', 'desc': 'Haiku 4.5'},
    ],
    'codex': [
        # Ladders per model come from charon.providers.effort; /setup effort shows them.
        {'id': 'gpt-6-astra', 'desc': 'GPT-6 Astra — most capable, 1.05M context, effort up to max'},
        {'id': 'gpt-5.6-sol', 'desc': 'GPT 5.6 Sol — effort up to max'},
        {'id': 'gpt-5.5', 'desc': 'GPT 5.5 — effort up to xhigh'},
        {'id': 'gpt-5.4', 'desc': 'GPT 5.4'},
        {'id': 'gpt-5', 'desc': 'GPT 5'},
        # o3, o4-mini, gpt-4.1, gpt-4o, codex-mini etc. are NOT available with
        # Codex OAuth (ChatGPT subscription).
    ],
}
STATIC_MODELS['anthropic'] = STATIC_MODELS['claude-code']
STATIC_MODELS['deepseek'] = deepseek.STATIC_MODELS

DISCOVERABLE = frozenset({
    'claude-code', 'anthropic', 'codex', 'openai', 'api', 'opencode', 'openrouter', 'lmstudio',
    'deepseek',
})
# Providers whose listing needs no credential.
_PUBLIC_LISTING = frozenset({'openrouter', 'lmstudio'})


@dataclass
class CatalogResult:
    models: list[dict[str, Any]]
    source: str                 # 'live' | 'cache' | 'stale-cache' | 'static' | 'none'
    error: str = ''             # why discovery did not produce this list, if it didn't
    fetched_at: float = 0.0
    extra: dict[str, Any] = field(default_factory=dict)


class DiscoveryError(RuntimeError):
    pass


# -- fetchers -----------------------------------------------------------------

def _http_get(url: str, headers: dict[str, str], params: dict | None = None) -> Any:
    import httpx
    resp = httpx.get(url, headers=headers, params=params, timeout=FETCH_TIMEOUT_SECONDS)
    if resp.status_code != 200:
        body = resp.text[:300].replace('\n', ' ')
        raise DiscoveryError(f'HTTP {resp.status_code} from {url}: {body}')
    return resp.json()


def _fetch_anthropic(api_key: str, get: Callable = _http_get) -> list[dict]:
    from charon.providers.httpx_anthropic import ANTHROPIC_VERSION
    headers = {'anthropic-version': ANTHROPIC_VERSION, 'accept': 'application/json'}
    if 'sk-ant-oat' in api_key:
        headers.update({
            'Authorization': f'Bearer {api_key}',
            'anthropic-beta': 'oauth-2025-04-20',
            'user-agent': 'claude-cli/2.1.80',
            'x-app': 'cli',
        })
    else:
        headers['x-api-key'] = api_key
    models: list[dict] = []
    params: dict[str, Any] = {'limit': 1000}
    for _ in range(20):  # pagination guard
        data = get(ANTHROPIC_MODELS_URL, headers, params)
        for m in data.get('data') or []:
            mid = str(m.get('id') or '').strip()
            if mid:
                models.append({'id': mid, 'desc': str(m.get('display_name') or mid)})
        if not data.get('has_more') or not data.get('last_id'):
            break
        params = {'limit': 1000, 'after_id': data['last_id']}
    return models


def _fetch_codex(api_key: str, get: Callable = _http_get) -> list[dict]:
    from charon.providers.httpx_codex import _extract_account_id
    headers = {
        'Authorization': f'Bearer {api_key}',
        'chatgpt-account-id': _extract_account_id(api_key),
        'originator': 'charon',
        'User-Agent': 'charon/0.1',
        'Accept': 'application/json',
    }
    version = os.environ.get('CHARON_CODEX_CLIENT_VERSION', '').strip() or CODEX_CLIENT_VERSION
    data = get(CODEX_MODELS_URL, headers, {'client_version': version})
    entries = sorted(data.get('models') or [], key=lambda m: m.get('priority', 1_000))
    models: list[dict] = []
    for m in entries:
        slug = str(m.get('slug') or '').strip()
        if not slug or m.get('visibility') == 'hide':
            continue
        levels = [
            str(lvl.get('effort') if isinstance(lvl, dict) else lvl).strip().lower()
            for lvl in m.get('supported_reasoning_levels') or []
        ]
        # 'ultra' is a CLI mode (max + the CLI's fan-out), not a wire value.
        wire = [lvl for lvl in levels if lvl in effort.EFFORT_LADDER and lvl != 'ultra']
        desc = str(m.get('display_name') or slug)
        if m.get('description'):
            desc += f' — {m["description"]}'
        if wire:
            desc += f' (effort up to {wire[-1]})'
        entry: dict[str, Any] = {'id': slug, 'desc': desc}
        if wire:
            entry['efforts'] = wire
        if m.get('default_reasoning_level'):
            entry['default_effort'] = str(m['default_reasoning_level'])
        if m.get('context_window'):
            entry['context_window'] = int(m['context_window'])
        models.append(entry)
    return models


def _fetch_openai_compatible(base_url: str, api_key: str, get: Callable = _http_get) -> list[dict]:
    headers = {'Accept': 'application/json'}
    if api_key and api_key != 'not-needed':
        headers['Authorization'] = f'Bearer {api_key}'
    data = get(base_url.rstrip('/') + '/models', headers, None)
    models: list[dict] = []
    for m in data.get('data') or []:
        mid = str(m.get('id') or '').strip()
        if mid:
            models.append({'id': mid, 'desc': str(m.get('name') or m.get('owned_by') or mid)})
    return models


def _fetch_deepseek(api_key: str, get: Callable = _http_get) -> list[dict]:
    data = get(DEEPSEEK_BASE_URL + '/models', {'Accept': 'application/json', 'Authorization': f'Bearer {api_key}'}, None)
    models: list[dict] = []
    for m in data.get('data') or []:
        mid = str(m.get('id') or '').strip()
        if not mid or not deepseek.agent_usable(mid):
            continue
        levels = [str(lvl).strip().lower() for lvl in (m.get('effort') or {}).get('supported_levels') or []]
        wire = [lvl for lvl in levels if lvl in effort.EFFORT_LADDER]
        desc = str(m.get('name') or mid)
        if m.get('context_window'):
            desc += f' — {int(m["context_window"]) // 1024}K context'
        if wire:
            desc += f', effort {"/".join(wire)}'
        entry: dict[str, Any] = {'id': mid, 'desc': desc}
        if wire:
            entry['efforts'] = wire
        if (m.get('effort') or {}).get('default_level'):
            entry['default_effort'] = str(m['effort']['default_level'])
        if m.get('context_window'):
            entry['context_window'] = int(m['context_window'])
        models.append(entry)
    return models


def discover(provider_raw: str, api_key: str = '', base_url: str = '', get: Callable = _http_get) -> list[dict]:
    """Ask the provider for its model list. Raises DiscoveryError on failure."""
    p = provider_raw
    if p not in DISCOVERABLE:
        raise DiscoveryError(f'{p} has no model listing')
    if not api_key and p not in _PUBLIC_LISTING:
        raise DiscoveryError(f'not signed in to {p}')
    try:
        if p in ('claude-code', 'anthropic'):
            return _fetch_anthropic(api_key, get)
        if p == 'codex':
            return _fetch_codex(api_key, get)
        if p == 'deepseek':
            return _fetch_deepseek(api_key, get)
        default_base = {
            'openrouter': OPENROUTER_BASE_URL, 'lmstudio': LMSTUDIO_BASE_URL, 'openai': OPENAI_BASE_URL,
        }.get(p, '')
        url = base_url or default_base
        if not url:
            raise DiscoveryError(f'{p} has no base URL configured')
        return _fetch_openai_compatible(url, api_key, get)
    except DiscoveryError:
        raise
    except Exception as e:
        raise DiscoveryError(f'{type(e).__name__}: {e}') from e


# -- cache --------------------------------------------------------------------

def _cache_path(state_dir: Path) -> Path:
    return Path(state_dir) / CACHE_FILE


def _load_cache(state_dir: Path) -> dict:
    try:
        data = json.loads(_cache_path(state_dir).read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_cache(state_dir: Path, cache: dict) -> None:
    path = _cache_path(state_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps(cache, indent=2))
        tmp.replace(path)
    except Exception as e:
        _diag('model_catalog', 'could not write model catalog cache', error=e)


def _register_efforts(models: list[dict]) -> None:
    for m in models:
        if m.get('efforts'):
            effort.learn_efforts(m['id'], m['efforts'])


def resolve_credentials(state_dir: Path, provider_raw: str, session_id: str | None = None) -> tuple[str, str]:
    """(api_key, base_url) Charon would use for `provider_raw`, signed in or not."""
    from charon.providers import provider_bridge as pb
    state_dir = Path(state_dir)
    effective = dict(pb._read_json(state_dir / 'onboarding.json') or {})
    effective.update(pb.load_session_provider_config(state_dir, session_id) or {})
    auth_store = pb._read_json(state_dir / 'auth' / 'auth.json') or {}
    provider_name = pb.PROVIDER_MAP.get(provider_raw, 'openai' if provider_raw == 'openrouter' else 'local')
    if provider_raw == 'openrouter':
        api_key = os.environ.get('OPENROUTER_API_KEY', '').strip()
    else:
        api_key = pb._resolve_api_key(provider_name, provider_raw, effective, auth_store)
    base_url = ''
    if provider_raw in ('api', 'opencode'):
        base_url = str(effective.get('provider_base_url') or '').strip()
    elif provider_raw in ('lmstudio', 'local', 'ollama'):
        base_url = pb._detect_base_url(effective)
    return api_key or '', base_url


def cached_models(state_dir: Path, provider_raw: str) -> CatalogResult:
    """What the picker can show without touching the network."""
    entry = _load_cache(state_dir).get(provider_raw) or {}
    models = entry.get('models') or []
    if models:
        _register_efforts(models)
        fresh = time.time() - float(entry.get('fetched_at') or 0) < DEFAULT_TTL_SECONDS
        return CatalogResult(models, 'cache' if fresh else 'stale-cache', entry.get('error', ''), float(entry.get('fetched_at') or 0))
    static = STATIC_MODELS.get(provider_raw, [])
    return CatalogResult(list(static), 'static' if static else 'none', entry.get('error', ''))


def get_models(
    state_dir: Path,
    provider_raw: str,
    *,
    refresh: bool = False,
    session_id: str | None = None,
    ttl: float = DEFAULT_TTL_SECONDS,
    credentials: tuple[str, str] | None = None,
    get: Callable = _http_get,
) -> CatalogResult:
    """Models for `provider_raw`: fresh cache, else a live fetch, else fallback.

    ``refresh=True`` forces a fetch (manual refresh / sign-in). A failed fetch
    never empties the list: the last good listing, then STATIC_MODELS, is
    returned with ``error`` set.
    """
    provider_raw = str(provider_raw or '').strip().lower()
    cache = _load_cache(state_dir)
    entry = cache.get(provider_raw) or {}
    now = time.time()
    if not refresh and entry.get('models') and now - float(entry.get('fetched_at') or 0) < ttl:
        return cached_models(state_dir, provider_raw)
    if provider_raw not in DISCOVERABLE:
        return cached_models(state_dir, provider_raw)
    if not refresh and entry.get('error') and now - float(entry.get('error_at') or 0) < FAILURE_BACKOFF_SECONDS:
        return cached_models(state_dir, provider_raw)

    api_key, base_url = credentials if credentials is not None else resolve_credentials(state_dir, provider_raw, session_id)
    try:
        models = discover(provider_raw, api_key, base_url, get)
        if not models:
            raise DiscoveryError('listing returned no models')
    except DiscoveryError as e:
        error = str(e)
        if api_key:
            error = error.replace(api_key, '[REDACTED]')
        _diag('model_catalog', 'model discovery failed; using fallback', provider=provider_raw, error=error)
        entry = dict(entry)
        entry.update({'error': error, 'error_at': now})
        cache[provider_raw] = entry
        _save_cache(state_dir, cache)
        return cached_models(state_dir, provider_raw)

    cache[provider_raw] = {'models': models, 'fetched_at': now, 'error': '', 'error_at': 0}
    _save_cache(state_dir, cache)
    _register_efforts(models)
    return CatalogResult(models, 'live', '', now)


def describe_source(result: CatalogResult, provider_raw: str) -> str:
    """One line for the UI saying where the list came from."""
    if result.source == 'live':
        return f'{len(result.models)} models from {provider_raw}\'s listing.'
    if result.source == 'cache':
        return f'{len(result.models)} models from {provider_raw}\'s listing (cached). /model refresh to re-fetch.'
    why = f' Discovery failed: {result.error}' if result.error else ''
    if result.source == 'stale-cache':
        return f'Showing the last listing from {provider_raw} (out of date).{why}'
    if result.source == 'static':
        return f'Showing Charon\'s built-in list for {provider_raw}, not a live listing.{why}'
    return f'No model list for {provider_raw}.{why}'
