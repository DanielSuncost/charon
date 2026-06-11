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
