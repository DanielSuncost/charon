import asyncio
import base64
import json
import time
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'apps' / 'core-daemon'))

from providers.httpx_codex import HttpxCodexProvider


def _jwt_with_exp(exp: int, account_id: str = 'acct-test') -> str:
    def enc(obj: dict) -> str:
        raw = json.dumps(obj, separators=(',', ':')).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b'=').decode()

    payload = {
        'exp': exp,
        'https://api.openai.com/auth': {'chatgpt_account_id': account_id},
    }
    return f'{enc({"alg": "none"})}.{enc(payload)}.sig'


def test_codex_token_expiry_detection():
    expired = HttpxCodexProvider(api_key=_jwt_with_exp(int(time.time()) - 10))
    fresh = HttpxCodexProvider(api_key=_jwt_with_exp(int(time.time()) + 3600))

    assert expired._token_expires_soon()
    assert not fresh._token_expires_soon()


def test_codex_ensure_fresh_token_refreshes_expired_token(monkeypatch):
    provider = HttpxCodexProvider(
        api_key=_jwt_with_exp(int(time.time()) - 10),
        refresh_token='refresh-old',
    )

    async def fake_refresh():
        provider._api_key = _jwt_with_exp(int(time.time()) + 3600)
        provider._refresh_token = 'refresh-new'
        return True

    monkeypatch.setattr(provider, '_refresh_access_token', fake_refresh)

    assert asyncio.run(provider._ensure_fresh_token()) is True
    assert provider._refresh_token == 'refresh-new'
    assert not provider._token_expires_soon()


def test_codex_save_token_data_preserves_refresh_token(tmp_path):
    auth_path = tmp_path / 'auth.json'
    auth_path.write_text(json.dumps({
        'providers': {
            'openai-codex': {
                'tokens': {
                    'access_token': 'old-access',
                    'refresh_token': 'old-refresh',
                },
                'auth_type': 'oauth',
            },
        },
    }))
    provider = HttpxCodexProvider(api_key='old-access', refresh_token='old-refresh', auth_store_path=str(auth_path))

    provider._save_token_data({'access_token': 'new-access', 'expires_in': 3600})

    saved = json.loads(auth_path.read_text())
    tokens = saved['providers']['openai-codex']['tokens']
    assert tokens['access_token'] == 'new-access'
    assert tokens['refresh_token'] == 'old-refresh'
    assert tokens['expires_in'] == 3600


def _write_auth_store(path, access_token, refresh_token):
    path.write_text(json.dumps({
        'providers': {
            'openai-codex': {
                'tokens': {'access_token': access_token, 'refresh_token': refresh_token},
                'auth_type': 'oauth',
            },
        },
    }))


def test_codex_read_tokens_from_disk_picks_up_newer_token(tmp_path):
    auth_path = tmp_path / 'auth.json'
    fresh = _jwt_with_exp(int(time.time()) + 3600)
    _write_auth_store(auth_path, fresh, 'refresh-disk')

    provider = HttpxCodexProvider(api_key='old-access', refresh_token='refresh-old', auth_store_path=str(auth_path))
    provider._read_tokens_from_disk()

    assert provider._api_key == fresh
    assert provider._refresh_token == 'refresh-disk'


def test_codex_locked_refresh_uses_disk_token_without_refetch(tmp_path, monkeypatch):
    """If another process already refreshed (fresh token on disk), the locked
    path must use it and NOT spend our single-use refresh token on the network."""
    auth_path = tmp_path / 'auth.json'
    fresh = _jwt_with_exp(int(time.time()) + 3600)
    _write_auth_store(auth_path, fresh, 'refresh-disk')

    provider = HttpxCodexProvider(
        api_key=_jwt_with_exp(int(time.time()) - 10),
        refresh_token='refresh-old',
        auth_store_path=str(auth_path),
    )
    calls = {'n': 0}

    async def fake_refresh():
        calls['n'] += 1
        return True

    monkeypatch.setattr(provider, '_refresh_access_token', fake_refresh)

    assert asyncio.run(provider._ensure_fresh_token()) is True
    assert calls['n'] == 0  # no network refresh — reused the disk token
    assert provider._api_key == fresh


def test_codex_locked_refresh_refreshes_when_disk_also_expired(tmp_path, monkeypatch):
    auth_path = tmp_path / 'auth.json'
    expired = _jwt_with_exp(int(time.time()) - 10)
    _write_auth_store(auth_path, expired, 'refresh-disk')

    provider = HttpxCodexProvider(api_key=expired, refresh_token='refresh-old', auth_store_path=str(auth_path))
    calls = {'n': 0}

    async def fake_refresh():
        calls['n'] += 1
        provider._api_key = _jwt_with_exp(int(time.time()) + 3600)
        return True

    monkeypatch.setattr(provider, '_refresh_access_token', fake_refresh)

    assert asyncio.run(provider._ensure_fresh_token()) is True
    assert calls['n'] == 1
    assert not provider._token_expires_soon()
