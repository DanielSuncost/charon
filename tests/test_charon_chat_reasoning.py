from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts' / 'charon_chat.py'


def _load_chat_module():
    spec = importlib.util.spec_from_file_location('charon_chat_reasoning_test', SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cli_passes_explicit_effort_and_state_dir_to_engine(tmp_path, monkeypatch):
    chat = _load_chat_module()
    captured = {}

    class FakeEngine:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    async def fake_one_shot(engine, query):
        assert isinstance(engine, FakeEngine)
        assert query == 'do work'
        return 0

    state = tmp_path / 'state'
    monkeypatch.setenv('CHARON_STATE_DIR', str(state))
    # Seed through monkeypatch so its teardown also removes the values that
    # main() intentionally changes for the lifetime of the CLI process.
    monkeypatch.setenv('CHARON_REQUIRE_REASONING_EFFORT', '0')
    monkeypatch.setenv('CHARON_EMIT_REQUEST_CONFIG', '0')
    monkeypatch.setattr(chat, 'get_provider', lambda name: object())
    monkeypatch.setattr(chat, 'ConversationEngine', FakeEngine)
    monkeypatch.setattr(chat, 'one_shot', fake_one_shot)
    monkeypatch.setattr(sys, 'argv', [
        str(SCRIPT),
        '--provider', 'openrouter',
        '--model', 'vendor/model',
        '--reasoning-effort', 'xhigh',
        '--verify-reasoning-effort',
        '-q', 'do work',
    ])

    with pytest.raises(SystemExit) as exc:
        chat.main()

    assert exc.value.code == 0
    assert captured['state_dir'] == state
    assert captured['thinking_level'] == 'xhigh'
    assert captured['model'].model_id == 'vendor/model'
    assert os.environ['CHARON_REQUIRE_REASONING_EFFORT'] == '1'
    assert os.environ['CHARON_EMIT_REQUEST_CONFIG'] == '1'


def test_cli_rejects_unknown_effort_before_provider_lookup(monkeypatch):
    chat = _load_chat_module()
    monkeypatch.setattr(sys, 'argv', [
        str(SCRIPT), '--reasoning-effort', 'impossible', '-q', 'do work',
    ])

    with pytest.raises(SystemExit) as exc:
        chat.main()

    assert exc.value.code == 2


def test_verification_requires_explicit_effort(monkeypatch):
    chat = _load_chat_module()
    monkeypatch.setattr(sys, 'argv', [
        str(SCRIPT), '--verify-reasoning-effort', '-q', 'do work',
    ])

    with pytest.raises(SystemExit) as exc:
        chat.main()

    assert exc.value.code == 2
