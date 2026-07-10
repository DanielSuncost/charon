

from charon.providers import charon_auth


def test_anthropic_oauth_url_shape_and_local_callback_flow(monkeypatch, tmp_path):
    monkeypatch.setattr(charon_auth, 'AUTH_DIR', tmp_path / 'auth')
    monkeypatch.setattr(charon_auth, 'AUTH_FILE', tmp_path / 'auth' / 'auth.json')

    captured = {'url': ''}

    def _status(msg: str):
        if msg.startswith('AUTH_URL::'):
            captured['url'] = msg.split('AUTH_URL::', 1)[1].strip()

    # Mock the callback server to return a code immediately
    monkeypatch.setattr(
        charon_auth,
        '_run_callback_server',
        lambda host, port, timeout=180, callback_path='/callback': ('fake-code', 'fake-state'),
    )

    # Mock the token exchange
    monkeypatch.setattr(
        charon_auth,
        '_exchange_code_json',
        lambda provider, code, verifier, state=None: {'access_token': 'a', 'refresh_token': 'r', 'expires_in': 3600},
    )

    token = charon_auth.login_oauth(
        'anthropic',
        status_cb=_status,
    )

    assert 'claude.ai/oauth/authorize' in captured['url']
    assert 'response_type=code' in captured['url']
    assert 'client_id=' in captured['url']
    assert 'code_challenge=' in captured['url']
    assert 'code_challenge_method=S256' in captured['url']
    assert 'code=true' in captured['url']  # Anthropic-specific param
    assert token.get('auth_url') == captured['url']
    assert token.get('access_token') == 'a'


def test_codex_oauth_url_shape(monkeypatch, tmp_path):
    monkeypatch.setattr(charon_auth, 'AUTH_DIR', tmp_path / 'auth')
    monkeypatch.setattr(charon_auth, 'AUTH_FILE', tmp_path / 'auth' / 'auth.json')

    captured = {'url': ''}

    def _status(msg: str):
        if msg.startswith('AUTH_URL::'):
            captured['url'] = msg.split('AUTH_URL::', 1)[1].strip()

    monkeypatch.setattr(
        charon_auth,
        '_run_callback_server',
        lambda host, port, timeout=180, callback_path='/callback': ('fake-code', 'fake-state'),
    )
    monkeypatch.setattr(
        charon_auth,
        '_exchange_code_form',
        lambda provider, code, verifier: {'access_token': 'b', 'refresh_token': 'r', 'expires_in': 3600},
    )

    token = charon_auth.login_oauth('openai-codex', status_cb=_status)

    assert 'auth.openai.com/oauth/authorize' in captured['url']
    assert 'client_id=app_EMoamEEZ73f0CkXaXp7hrann' in captured['url']
    assert 'codex_cli_simplified_flow=true' in captured['url']
    assert token.get('access_token') == 'b'
