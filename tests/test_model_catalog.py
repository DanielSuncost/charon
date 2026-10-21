"""Model discovery: provider listings feed the picker and effort ladders, and a
failed fetch falls back to the last listing, then the built-in list."""
from __future__ import annotations

import base64
import json

import pytest

from charon.providers import effort, model_catalog
from charon.providers.model_catalog import DiscoveryError, get_models


@pytest.fixture(autouse=True)
def _clean_learned(monkeypatch):
    monkeypatch.setattr(effort, '_LEARNED_EFFORTS', {})


def _codex_token() -> str:
    claims = {'https://api.openai.com/auth': {'chatgpt_account_id': 'acct-1'}}
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip('=')
    return f'h.{payload}.s'


CODEX_LISTING = {'models': [
    {'slug': 'gpt-9-nova', 'display_name': 'GPT-9 Nova', 'priority': 2, 'visibility': 'list',
     'default_reasoning_level': 'medium', 'context_window': 400000,
     'supported_reasoning_levels': [{'effort': e} for e in ('low', 'medium', 'high', 'xhigh', 'max', 'ultra')]},
    {'slug': 'gpt-9-mini', 'display_name': 'GPT-9 Mini', 'priority': 1, 'visibility': 'list',
     'supported_reasoning_levels': [{'effort': e} for e in ('low', 'medium')]},
    {'slug': 'internal-reviewer', 'visibility': 'hide', 'priority': 0},
]}


def _fake_get(responses: dict, calls: list):
    def get(url, headers, params):
        calls.append((url, dict(headers), params))
        result = responses[url]
        if isinstance(result, Exception):
            raise result
        return result
    return get


def test_codex_listing_drives_picker_order_and_effort_ladders(tmp_path):
    calls: list = []
    get = _fake_get({model_catalog.CODEX_MODELS_URL: CODEX_LISTING}, calls)
    result = get_models(tmp_path, 'codex', credentials=(_codex_token(), ''), get=get)

    assert result.source == 'live'
    assert [m['id'] for m in result.models] == ['gpt-9-mini', 'gpt-9-nova']   # priority order, hidden dropped
    assert calls[0][1]['chatgpt-account-id'] == 'acct-1'
    assert calls[0][2]['client_version']
    # 'ultra' is a CLI mode, so the wire ladder stops at max.
    assert effort.supported_efforts('gpt-9-nova') == ('low', 'medium', 'high', 'xhigh', 'max')
    assert effort.clamp_effort('ultra', 'gpt-9-mini') == 'medium'


def test_unknown_model_keeps_a_safe_default_ladder():
    assert effort.supported_efforts('gpt-never-seen') == effort.CODEX_DEFAULT_EFFORTS
    assert effort.clamp_effort('max', 'gpt-never-seen') == 'high'


def test_fresh_cache_is_served_without_a_fetch(tmp_path):
    calls: list = []
    get = _fake_get({model_catalog.CODEX_MODELS_URL: CODEX_LISTING}, calls)
    get_models(tmp_path, 'codex', credentials=(_codex_token(), ''), get=get)
    again = get_models(tmp_path, 'codex', credentials=(_codex_token(), ''), get=get)
    assert again.source == 'cache' and len(calls) == 1
    get_models(tmp_path, 'codex', refresh=True, credentials=(_codex_token(), ''), get=get)
    assert len(calls) == 2


def test_failed_refresh_keeps_last_listing_and_reports_why(tmp_path):
    ok = _fake_get({model_catalog.CODEX_MODELS_URL: CODEX_LISTING}, [])
    get_models(tmp_path, 'codex', credentials=(_codex_token(), ''), get=ok)
    down = _fake_get({model_catalog.CODEX_MODELS_URL: DiscoveryError('HTTP 503 from codex')}, [])
    result = get_models(tmp_path, 'codex', refresh=True, credentials=(_codex_token(), ''), get=down)
    assert [m['id'] for m in result.models] == ['gpt-9-mini', 'gpt-9-nova']
    assert 'HTTP 503' in result.error


def test_never_discovered_falls_back_to_builtin_list_and_backs_off(tmp_path):
    calls: list = []
    get = _fake_get({model_catalog.ANTHROPIC_MODELS_URL: DiscoveryError('HTTP 401 from anthropic: secret-key')}, calls)
    result = get_models(tmp_path, 'claude-code', credentials=('secret-key', ''), get=get)
    assert result.source == 'static'
    assert result.models[0]['id'] == model_catalog.STATIC_MODELS['claude-code'][0]['id']
    assert 'HTTP 401' in result.error and 'secret-key' not in result.error
    get_models(tmp_path, 'claude-code', credentials=('secret-key', ''), get=get)
    assert len(calls) == 1          # backoff: offline runs don't refetch on every /model


def test_not_signed_in_does_not_touch_the_network(tmp_path):
    calls: list = []
    result = get_models(tmp_path, 'codex', credentials=('', ''), get=_fake_get({}, calls))
    assert calls == [] and result.source == 'static' and 'not signed in' in result.error
    assert result.models[0]['id'] == 'gpt-6-astra'


def test_anthropic_oauth_and_api_key_headers_and_pagination(tmp_path):
    pages = iter([
        {'data': [{'id': 'claude-opus-9', 'display_name': 'Opus 9'}], 'has_more': True, 'last_id': 'claude-opus-9'},
        {'data': [{'id': 'claude-haiku-9', 'display_name': 'Haiku 9'}], 'has_more': False},
    ])
    calls: list = []

    def get(url, headers, params):
        calls.append((headers, params))
        return next(pages)

    result = get_models(tmp_path, 'claude-code', credentials=('sk-ant-oat01-x', ''), get=get)
    assert [m['id'] for m in result.models] == ['claude-opus-9', 'claude-haiku-9']
    assert calls[0][0]['Authorization'] == 'Bearer sk-ant-oat01-x'
    assert 'oauth-2025-04-20' in calls[0][0]['anthropic-beta']
    assert calls[1][1]['after_id'] == 'claude-opus-9'

    seen: list = []
    model_catalog.discover('anthropic', 'sk-ant-api03-y',
                           get=lambda u, h, p: seen.append(h) or {'data': [{'id': 'm'}]})
    assert seen[0]['x-api-key'] == 'sk-ant-api03-y' and 'Authorization' not in seen[0]


def test_openrouter_listing_is_public(tmp_path):
    calls: list = []
    get = _fake_get({'https://openrouter.ai/api/v1/models': {'data': [{'id': 'anthropic/claude-opus-9', 'name': 'Opus 9'}]}}, calls)
    result = get_models(tmp_path, 'openrouter', credentials=('', ''), get=get)
    assert result.models == [{'id': 'anthropic/claude-opus-9', 'desc': 'Opus 9'}]
    assert 'Authorization' not in calls[0][1]


def test_effort_ladder_learned_from_per_model_rejection_only():
    per_model = ("Unsupported value: 'max' is not supported with the 'gpt-9-x' model. "
                 "Supported values are: 'none', 'low', 'medium', 'high', and 'xhigh'.")
    assert effort.parse_supported_efforts(per_model, 'gpt-9-x') == ('low', 'medium', 'high', 'xhigh')
    assert effort.clamp_effort('max', 'gpt-9-x') == 'xhigh'
    generic = ("Invalid value: 'ultra'. Supported values are: 'none', 'minimal', 'low', "
               "'medium', 'high', 'xhigh', and 'max'.")
    assert effort.parse_supported_efforts(generic, 'gpt-9-y') is None
    assert effort.parse_supported_efforts(per_model, 'some-other-model') is None
