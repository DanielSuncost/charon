import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'apps' / 'tui' / 'opentui'))
sys.path.insert(0, str(ROOT / 'apps' / 'core-daemon'))

import chat_backend
from chat_backend import ChatBackend
from provider_bridge import save_session_provider_config


def test_natural_language_devop_prompt_routes_to_devop_command(monkeypatch):
    backend = ChatBackend()
    captured = {}

    def fake_handle_command(command, request_id):
        captured['command'] = command
        captured['request_id'] = request_id

    monkeypatch.setattr(backend, 'handle_command', fake_handle_command)
    backend.handle_chat('Start a software project that builds a local-first kanban app', 'req-1')

    assert captured['request_id'] == 'req-1'
    assert captured['command'] == '/devop builds a local-first kanban app'


def test_devop_command_listed_in_suggestions():
    backend = ChatBackend()
    cmds = [item['cmd'] for item in backend._command_catalog()]
    assert '/devop <prompt>' in cmds
    assert '/devop status <operation_id>' in cmds


def test_refresh_payload_uses_session_override_onboarding_step(tmp_path, monkeypatch):
    state_dir = tmp_path / 'state'
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(chat_backend, 'STATE_DIR', state_dir)

    (state_dir / 'onboarding.json').write_text(json.dumps({
        'complete': True,
        'step': 'done',
        'provider_mode': 'provider',
        'provider': 'lmstudio',
        'model': 'qwen3-30b-a3b',
        'project': '/global-project',
    }))

    save_session_provider_config(state_dir, 'sess-auth', {
        'complete': False,
        'step': 'provider-auth',
        'provider_mode': 'provider',
        'provider': 'codex',
        'project': '/session-project',
    })

    backend = ChatBackend()
    backend._active_agent_id = 'sess-auth'

    payload = backend._get_refresh_payload()

    assert payload['onboarding']['step'] == 'provider-auth'
    assert payload['onboarding']['provider'] == 'codex'
    assert payload['onboarding']['project'] == '/session-project'


def test_setup_provider_persists_global_onboarding_even_for_session_override(tmp_path, monkeypatch):
    import types

    state_dir = tmp_path / 'state'
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(chat_backend, 'STATE_DIR', state_dir)

    (state_dir / 'onboarding.json').write_text(json.dumps({
        'complete': True,
        'step': 'done',
        'provider_mode': 'provider',
        'provider': 'lmstudio',
        'model': 'qwen3-30b-a3b',
    }))

    fake_charon_auth = types.SimpleNamespace(login_oauth=lambda provider_id, status_cb=None: {'access_token': 'test-token'})
    monkeypatch.setitem(sys.modules, 'charon_auth', fake_charon_auth)

    backend = ChatBackend()
    backend._active_agent_id = 'sess-auth'

    backend.handle_command('/setup provider codex', 'req-1')

    onboarding = json.loads((state_dir / 'onboarding.json').read_text())
    session_override = json.loads((state_dir / 'session_providers' / 'sess-auth.json').read_text())

    assert onboarding['provider'] == 'codex'
    assert onboarding['step'] in ('provider-auth', 'model')
    assert session_override['provider'] == 'codex'
    assert session_override['step'] in ('provider-auth', 'model')
