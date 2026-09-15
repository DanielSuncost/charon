"""Transport-level: the outgoing Codex body's reasoning.effort for every level.

The SSE client is faked at the AsyncClientPool seam so the exact JSON body
Charon would send is captured; no network.
"""
from __future__ import annotations

import asyncio
import base64
import json
import time

import pytest

from charon.providers import Message, ModelInfo
from charon.providers import httpx_codex
from charon.providers.httpx_codex import HttpxCodexProvider


def _jwt(account_id: str = 'acct-test') -> str:
    def enc(obj: dict) -> str:
        raw = json.dumps(obj, separators=(',', ':')).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b'=').decode()
    payload = {'exp': int(time.time()) + 3600, 'https://api.openai.com/auth': {'chatgpt_account_id': account_id}}
    return f'{enc({"alg": "none"})}.{enc(payload)}.sig'


class _Resp:
    status_code = 200
    headers: dict = {}

    async def aread(self):
        return b''

    async def aiter_lines(self):
        yield 'data: [DONE]'


class _Ctx:
    def __init__(self, resp):
        self.resp = resp

    async def __aenter__(self):
        return self.resp

    async def __aexit__(self, *exc):
        return False


class _Client:
    def __init__(self, captured: list):
        self.captured = captured

    def stream(self, method, url, json=None, headers=None):
        self.captured.append({'method': method, 'url': url, 'body': json})
        return _Ctx(_Resp())


async def _drain(agen):
    return [d async for d in agen]


def _send(monkeypatch, *, model_id: str, thinking_level: str) -> dict:
    captured: list = []
    provider = HttpxCodexProvider(api_key=_jwt())
    client = _Client(captured)

    async def fake_get(*args, **kwargs):
        return client, False

    monkeypatch.setattr(provider._clients, 'get', fake_get)
    monkeypatch.setattr(httpx_codex.config, 'codex_websocket', lambda: False)   # force the SSE body path
    asyncio.run(_drain(provider.stream(
        messages=[Message(role='user', content='hi')],
        model=ModelInfo(provider='codex', model_id=model_id, supports_thinking=True),
        system_prompt='',
        thinking_level=thinking_level,
    )))
    assert len(captured) == 1 and captured[0]['method'] == 'POST'
    return captured[0]['body']


@pytest.mark.parametrize('level', ['low', 'medium', 'high', 'xhigh', 'max'])
def test_codex_body_carries_each_astra_effort_level_unchanged(monkeypatch, level):
    body = _send(monkeypatch, model_id='gpt-6-astra', thinking_level=level)
    assert body['model'] == 'gpt-6-astra'
    assert body['reasoning'] == {'effort': level, 'summary': 'auto'}


def test_codex_body_has_no_reasoning_block_when_off(monkeypatch):
    body = _send(monkeypatch, model_id='gpt-6-astra', thinking_level='off')
    assert 'reasoning' not in body


def test_codex_body_sends_ultra_as_max_and_minimal_as_the_floor(monkeypatch):
    # Both ends pinned by live 400s: 'ultra' is a CLI mode, 'minimal' is not an astra level.
    assert _send(monkeypatch, model_id='gpt-6-astra', thinking_level='ultra')['reasoning']['effort'] == 'max'
    assert _send(monkeypatch, model_id='gpt-6-astra', thinking_level='minimal')['reasoning']['effort'] == 'low'


@pytest.mark.parametrize('model_id,level,expected', [
    ('gpt-5', 'xhigh', 'high'),          # unknown model keeps the pre-existing ceiling
    ('gpt-5', 'ultra', 'high'),
    ('gpt-5', 'minimal', 'low'),         # unknown model: the old minimal→low fold
    ('gpt-5.5', 'max', 'xhigh'),         # 5.5 advertises up to xhigh
    ('gpt-5.5', 'xhigh', 'xhigh'),       # …and xhigh itself is no longer folded
    ('gpt-5.6-luna', 'ultra', 'max'),
    ('gpt-5.6-sol', 'ultra', 'max'),
])
def test_codex_body_clamps_to_what_the_model_advertises(monkeypatch, model_id, level, expected):
    body = _send(monkeypatch, model_id=model_id, thinking_level=level)
    assert body['reasoning']['effort'] == expected
