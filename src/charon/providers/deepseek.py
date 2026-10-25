"""DeepSeek: credential, model rules and wire options for its OpenAI-compatible API.

What the API does, from api-docs.deepseek.com (read 2026-10-24) and a live
unauthenticated probe (``GET /models`` → ``401 Authentication Fails``):

* Models: ``deepseek-flash`` (V4.1-Flash) and ``deepseek-v4-pro``, both 1M
  context. ``deepseek-v4-flash`` is a legacy alias of ``deepseek-flash``.
  ``deepseek-chat`` / ``deepseek-reasoner`` were retired on 2026-07-24.
* Both models do tool calls in thinking and non-thinking mode.
* Thinking is **on by default**. ``thinking: {type: enabled|disabled}`` toggles
  it and ``reasoning_effort`` takes ``low|high|max`` (``none`` turns it off).
* Thinking-mode chain of thought comes back in ``reasoning_content``. When a
  request carries tools, every earlier assistant turn's ``reasoning_content``
  must be sent back, or the API answers 400.
* ``tool_choice`` ``required`` / named is rejected (400) in thinking mode.
* Usage reports ``prompt_cache_hit_tokens`` / ``prompt_cache_miss_tokens``
  (hit is also mirrored in ``prompt_tokens_details.cached_tokens``).
"""
from __future__ import annotations

import json
from pathlib import Path

from charon.providers.provider_secrets import read_secret_env

PROVIDER = 'deepseek'
BASE_URL = 'https://api.deepseek.com'
API_KEY_ENV = 'DEEPSEEK_API_KEY'
API_KEY_FILE_ENV = 'DEEPSEEK_API_KEY_FILE'

# What reasoning_effort accepts once thinking is on, per the docs and the
# listing's effort.supported_levels. Charon's other levels clamp onto these.
EFFORTS: tuple[str, ...] = ('low', 'high', 'max')

# Shown before sign-in / when the listing is unreachable.
STATIC_MODELS: list[dict] = [
    {'id': 'deepseek-flash', 'desc': 'DeepSeek V4.1 Flash — 1M context, tools, effort low/high/max'},
    {'id': 'deepseek-v4-pro', 'desc': 'DeepSeek V4 Pro — 1M context, tools, effort low/high/max'},
]

# Ids the API no longer serves; picking one would fail on the first turn.
RETIRED_MODELS = frozenset({'deepseek-chat', 'deepseek-reasoner'})


def is_deepseek_url(base_url: str) -> bool:
    return 'api.deepseek.com' in str(base_url or '').lower()


def agent_usable(model_id: str) -> bool:
    """Whether a session or shade may run agent (tool-calling) turns on this model."""
    return str(model_id or '').strip().lower() not in RETIRED_MODELS


def read_api_key(state_dir: Path | None = None, auth_store: dict | None = None) -> str:
    """DEEPSEEK_API_KEY, then DEEPSEEK_API_KEY_FILE, then the key Charon stored at sign-in."""
    try:
        key = read_secret_env(API_KEY_ENV, API_KEY_FILE_ENV)
    except ValueError:
        key = ''
    if key:
        return key
    if auth_store is None:
        try:
            auth_store = json.loads(_auth_file(state_dir).read_text())
        except Exception:
            return ''
    entry = (auth_store.get('providers') or {}).get(PROVIDER) or {}
    return str(entry.get('api_key') or '').strip()


def save_api_key(api_key: str) -> None:
    """Store the key in Charon's auth store (auth dir 0700, auth.json 0600)."""
    from charon.providers import charon_auth
    store = charon_auth._load_auth()
    store.setdefault('providers', {})[PROVIDER] = {
        'api_key': api_key.strip(),
        'last_login': charon_auth._now(),
        'auth_type': 'api_key',
    }
    charon_auth._save_auth(store)


def _auth_file(state_dir: Path | None) -> Path:
    if state_dir is not None:
        return Path(state_dir) / 'auth' / 'auth.json'
    from charon.providers import charon_auth
    return charon_auth.AUTH_FILE


def wire_effort(level: str, model_id: str) -> str | None:
    """reasoning_effort to send, or None for thinking disabled."""
    from charon.providers.effort import clamp_effort
    lvl = clamp_effort(level, model_id)
    return None if lvl == 'off' else lvl


def apply_thinking(body: dict, level: str, model_id: str, history_has_reasoning: bool) -> str | None:
    """Set DeepSeek's thinking fields on a chat-completions body.

    Returns why thinking was turned off against the request, if it was.
    Thinking is the server default, so ``off`` is sent explicitly. A tool
    conversation whose earlier assistant turns carry no reasoning (another
    provider's turns, or a checkpoint that stripped it) cannot be replayed in
    thinking mode without a 400, so that request runs non-thinking instead.
    """
    effort = wire_effort(level, model_id)
    downgraded = None
    if effort and body.get('tools') and not history_has_reasoning:
        effort = None
        downgraded = 'earlier assistant turns have no reasoning_content to pass back'
    if effort:
        body['thinking'] = {'type': 'enabled'}
        body['reasoning_effort'] = effort
    else:
        body['thinking'] = {'type': 'disabled'}
        body.pop('reasoning_effort', None)
    return downgraded
