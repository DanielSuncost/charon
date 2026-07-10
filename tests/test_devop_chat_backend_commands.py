import json
import sys
import base64
import time
import types

from backend import common
from chat_backend import ChatBackend
from charon.providers.provider_bridge import save_session_provider_config


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
    monkeypatch.setattr(common, 'STATE_DIR', state_dir)

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
    state_dir = tmp_path / 'state'
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(common, 'STATE_DIR', state_dir)

    (state_dir / 'onboarding.json').write_text(json.dumps({
        'complete': True,
        'step': 'done',
        'provider_mode': 'provider',
        'provider': 'lmstudio',
        'model': 'qwen3-30b-a3b',
    }))

    fake_charon_auth = types.SimpleNamespace(login_oauth=lambda provider_id, status_cb=None: {'access_token': 'test-token'})
    import charon.providers as _providers_pkg
    monkeypatch.setattr(_providers_pkg, 'charon_auth', fake_charon_auth, raising=False)
    monkeypatch.setitem(sys.modules, 'charon.providers.charon_auth', fake_charon_auth)

    backend = ChatBackend()
    backend._active_agent_id = 'sess-auth'

    backend.handle_command('/setup provider codex', 'req-1')

    onboarding = json.loads((state_dir / 'onboarding.json').read_text())
    session_override = json.loads((state_dir / 'session_providers' / 'sess-auth.json').read_text())

    assert onboarding['provider'] == 'codex'
    assert onboarding['step'] in ('provider-auth', 'model')
    assert session_override['provider'] == 'codex'
    assert session_override['step'] in ('provider-auth', 'model')


def _jwt_with_exp(exp: int) -> str:
    def enc(obj: dict) -> str:
        raw = json.dumps(obj, separators=(',', ':')).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b'=').decode()

    return f'{enc({"alg": "none"})}.{enc({"exp": exp, "https://api.openai.com/auth": {"chatgpt_account_id": "acct"}})}.sig'


def test_codex_existing_expired_access_only_token_is_not_reused(tmp_path, monkeypatch):
    state_dir = tmp_path / 'state'
    auth_dir = state_dir / 'auth'
    auth_dir.mkdir(parents=True)
    monkeypatch.setattr(common, 'STATE_DIR', state_dir)

    expired = _jwt_with_exp(int(time.time()) - 60)
    (auth_dir / 'auth.json').write_text(json.dumps({
        'providers': {
            'openai-codex': {
                'tokens': {'access_token': expired},
                'auth_type': 'existing',
            },
        },
    }))

    backend = ChatBackend()

    assert backend._find_charon_auth_token('openai-codex') is None


def test_setup_provider_codex_preserves_existing_refresh_token(tmp_path, monkeypatch):
    state_dir = tmp_path / 'state'
    auth_dir = state_dir / 'auth'
    auth_dir.mkdir(parents=True)
    monkeypatch.setattr(common, 'STATE_DIR', state_dir)

    (state_dir / 'onboarding.json').write_text(json.dumps({
        'complete': False,
        'step': 'provider-mode',
    }))
    token_bundle = {
        'access_token': _jwt_with_exp(int(time.time()) + 3600),
        'refresh_token': 'refresh-token',
        'expires_in': 3600,
    }
    auth_path = auth_dir / 'auth.json'
    auth_path.write_text(json.dumps({
        'active_provider': 'openai-codex',
        'providers': {
            'openai-codex': {
                'tokens': token_bundle,
                'auth_type': 'oauth',
            },
        },
    }))

    fake_charon_auth = types.SimpleNamespace(
        _load_auth=lambda: json.loads(auth_path.read_text()),
        _save_auth=lambda store: auth_path.write_text(json.dumps(store)),
        _now=lambda: 'now',
    )
    import charon.providers as _providers_pkg
    monkeypatch.setattr(_providers_pkg, 'charon_auth', fake_charon_auth, raising=False)
    monkeypatch.setitem(sys.modules, 'charon.providers.charon_auth', fake_charon_auth)

    backend = ChatBackend()
    backend.handle_command('/setup provider codex', 'req-1')

    saved = json.loads(auth_path.read_text())
    assert saved['providers']['openai-codex']['tokens']['refresh_token'] == 'refresh-token'
    assert saved['providers']['openai-codex']['tokens']['access_token'] == token_bundle['access_token']


def test_setup_provider_codex_completes_when_existing_model_is_known(tmp_path, monkeypatch):
    state_dir = tmp_path / 'state'
    auth_dir = state_dir / 'auth'
    auth_dir.mkdir(parents=True)
    monkeypatch.setattr(common, 'STATE_DIR', state_dir)

    (state_dir / 'onboarding.json').write_text(json.dumps({
        'provider_mode': 'provider',
        'provider': 'codex',
        'provider_auth': 'oauth',
        'model': 'gpt-5.5',
        'provider_model': 'gpt-5.5',
        'complete': False,
        'step': 'model',
    }))
    auth_path = auth_dir / 'auth.json'
    auth_path.write_text(json.dumps({
        'active_provider': 'openai-codex',
        'providers': {
            'openai-codex': {
                'tokens': {
                    'access_token': _jwt_with_exp(int(time.time()) + 3600),
                    'refresh_token': 'refresh-token',
                },
                'auth_type': 'oauth',
            },
        },
    }))

    fake_charon_auth = types.SimpleNamespace(
        _load_auth=lambda: json.loads(auth_path.read_text()),
        _save_auth=lambda store: auth_path.write_text(json.dumps(store)),
        _now=lambda: 'now',
    )
    import charon.providers as _providers_pkg
    monkeypatch.setattr(_providers_pkg, 'charon_auth', fake_charon_auth, raising=False)
    monkeypatch.setitem(sys.modules, 'charon.providers.charon_auth', fake_charon_auth)

    backend = ChatBackend()
    backend.handle_command('/setup provider codex', 'req-1')

    onboarding = json.loads((state_dir / 'onboarding.json').read_text())
    assert onboarding['provider'] == 'codex'
    assert onboarding['model'] == 'gpt-5.5'
    assert onboarding['complete'] is True
    assert onboarding['step'] == 'done'
