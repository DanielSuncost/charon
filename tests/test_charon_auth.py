

import urllib.parse

import pytest

from charon.providers import charon_auth


def test_anthropic_oauth_url_shape_and_local_callback_flow(monkeypatch, tmp_path):
    monkeypatch.setattr(charon_auth, 'AUTH_DIR', tmp_path / 'auth')
    monkeypatch.setattr(charon_auth, 'AUTH_FILE', tmp_path / 'auth' / 'auth.json')

    captured = {'url': ''}

    def _status(msg: str):
        if msg.startswith('AUTH_URL::'):
            captured['url'] = msg.split('AUTH_URL::', 1)[1].strip()

    # Echo back the state actually issued, as a real provider does. A fixed
    # 'fake-state' passed only because the state was never compared.
    def _callback(host, port, timeout=180, callback_path='/callback'):
        qs = urllib.parse.urlparse(captured['url']).query
        return ('fake-code', urllib.parse.parse_qs(qs).get('state', [''])[0])

    monkeypatch.setattr(charon_auth, '_run_callback_server', _callback)

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

    def _callback(host, port, timeout=180, callback_path='/callback'):
        qs = urllib.parse.urlparse(captured['url']).query
        return ('fake-code', urllib.parse.parse_qs(qs).get('state', [''])[0])

    monkeypatch.setattr(charon_auth, '_run_callback_server', _callback)
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


def test_login_refuses_a_callback_with_the_wrong_state(monkeypatch, tmp_path):
    """End-to-end CSRF check: a callback from another request must not be exchanged."""
    monkeypatch.setattr(charon_auth, 'AUTH_DIR', tmp_path / 'auth')
    monkeypatch.setattr(charon_auth, 'AUTH_FILE', tmp_path / 'auth' / 'auth.json')

    monkeypatch.setattr(
        charon_auth,
        '_run_callback_server',
        lambda host, port, timeout=180, callback_path='/callback': ('attacker-code', 'attacker-state'),
    )

    exchanged = []
    monkeypatch.setattr(
        charon_auth,
        '_exchange_code_json',
        lambda provider, code, verifier, state=None: exchanged.append(code) or {'access_token': 'a'},
    )

    with pytest.raises(RuntimeError, match='state mismatch'):
        charon_auth.login_oauth('anthropic', status_cb=lambda m: None)

    assert exchanged == [], 'the code was exchanged despite a bad state'


def test_issued_state_is_present_and_distinct_from_the_verifier(monkeypatch, tmp_path):
    """Regression: state was the PKCE verifier, publishing it in the redirect URL."""
    monkeypatch.setattr(charon_auth, 'AUTH_DIR', tmp_path / 'auth')
    monkeypatch.setattr(charon_auth, 'AUTH_FILE', tmp_path / 'auth' / 'auth.json')

    captured = {'url': ''}
    seen = {}

    def _status(msg: str):
        if msg.startswith('AUTH_URL::'):
            captured['url'] = msg.split('AUTH_URL::', 1)[1].strip()

    def _callback(host, port, timeout=180, callback_path='/callback'):
        qs = urllib.parse.urlparse(captured['url']).query
        return ('fake-code', urllib.parse.parse_qs(qs).get('state', [''])[0])

    monkeypatch.setattr(charon_auth, '_run_callback_server', _callback)
    monkeypatch.setattr(
        charon_auth,
        '_exchange_code_json',
        lambda provider, code, verifier, state=None: seen.update(verifier=verifier, state=state)
        or {'access_token': 'a'},
    )

    charon_auth.login_oauth('anthropic', status_cb=_status)

    assert seen['state'], 'no state reached the token exchange'
    assert seen['state'] != seen['verifier'], 'state is the PKCE verifier again'
