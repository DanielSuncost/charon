"""charon_chat.py resolves its default model from configuration, not literals.

claude-sonnet-4-20250514 (retired 2026-06-15) and gpt-4o were hardcoded
defaults; requests to a retired model fail outright.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

from charon.providers import provider_bridge
from charon.providers.model_catalog import STATIC_MODELS

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'charon_chat.py'


def _load():
    spec = importlib.util.spec_from_file_location('charon_chat_default_model_test', SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_no_retired_or_stale_literal_defaults():
    source = SCRIPT.read_text(encoding='utf-8')
    assert "'claude-sonnet-4-20250514'" not in source
    assert "'gpt-4o'" not in source
    assert 'DEFAULT_MODELS' not in source


def test_unconfigured_providers_take_the_catalog_head(tmp_path, monkeypatch):
    chat = _load()
    monkeypatch.delenv('CHARON_LOCAL_MODEL', raising=False)
    monkeypatch.setattr(provider_bridge, 'resolve_provider_config', lambda state_dir: {})
    assert chat.default_model('anthropic', tmp_path) == STATIC_MODELS['anthropic'][0]['id']
    assert chat.default_model('deepseek', tmp_path) == STATIC_MODELS['deepseek'][0]['id']
    # No built-in OpenAI API listing: ask for --model rather than guess.
    assert chat.default_model('openai', tmp_path) == ''
    assert chat.default_model('local', tmp_path) == chat.LOCAL_FALLBACK_MODEL


def test_configured_model_wins_and_local_env_is_honoured(tmp_path, monkeypatch):
    chat = _load()
    monkeypatch.setattr(provider_bridge, 'resolve_provider_config',
                        lambda state_dir: {'provider_name': 'anthropic', 'model_id': 'claude-haiku-5-5'})
    assert chat.default_model('anthropic', tmp_path) == 'claude-haiku-5-5'
    # A configured provider never leaks its model into another provider.
    monkeypatch.setenv('CHARON_LOCAL_MODEL', 'lmstudio/my-local-model')
    assert chat.default_model('local', tmp_path) == 'my-local-model'


def test_current_claude_models_get_their_context_window():
    for entry in STATIC_MODELS['anthropic']:
        assert provider_bridge.CONTEXT_WINDOWS.get(entry['id']) == 200000, entry['id']
