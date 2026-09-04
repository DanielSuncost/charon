from __future__ import annotations

import asyncio
import base64
import json
import time
from types import SimpleNamespace

import httpx

from charon.providers import Message, ModelInfo
from charon.providers.httpx_codex import HttpxCodexProvider


MODEL = ModelInfo(provider='codex', model_id='gpt-5.3-codex')


def _token() -> str:
    def encode(value):
        return base64.urlsafe_b64encode(
            json.dumps(value, separators=(',', ':')).encode()
        ).rstrip(b'=').decode()

    return '.'.join((
        encode({'alg': 'none'}),
        encode({
            'exp': int(time.time()) + 3600,
            'https://api.openai.com/auth': {'chatgpt_account_id': 'acct-test'},
        }),
        'sig',
    ))


def _response_events(text: str, response_id: str):
    return [
        json.dumps({'type': 'response.created', 'response': {'id': response_id}}),
        json.dumps({'type': 'response.output_text.delta', 'delta': text}),
        json.dumps({
            'type': 'response.completed',
            'response': {
                'id': response_id,
                'usage': {
                    'input_tokens': 12,
                    'output_tokens': 3,
                    'input_tokens_details': {'cached_tokens': 5},
                },
            },
        }),
    ]


class FakeSocket:
    def __init__(self, responses):
        self.state = SimpleNamespace(name='OPEN')
        self.close_code = None
        self.responses = list(responses)
        self.queue = []
        self.sent = []

    async def send(self, payload):
        self.sent.append(json.loads(payload))
        self.queue.extend(self.responses.pop(0))

    async def recv(self):
        await asyncio.sleep(0)
        return self.queue.pop(0)

    async def close(self, code=1000, reason=''):
        self.state = SimpleNamespace(name='CLOSED')
        self.close_code = code


async def _collect(provider, message='hello'):
    return [delta async for delta in provider.stream(
        messages=[Message(role='user', content=message)],
        model=MODEL,
        system_prompt='system',
        tools=None,
    )]


def test_codex_websocket_reuses_connection_and_streams(monkeypatch):
    monkeypatch.setenv('CHARON_CODEX_WEBSOCKET', '1')
    socket = FakeSocket([
        _response_events('first', 'resp-1'),
        _response_events('second', 'resp-2'),
    ])
    connects = []

    async def factory(url, headers):
        connects.append((url, headers))
        return socket

    provider = HttpxCodexProvider(api_key=_token())
    provider.configure_session('session-1', 'cache-1')
    provider._websocket_factory = factory

    async def run():
        first = await _collect(provider, 'one')
        first_metrics = dict(provider.last_request_metrics)
        second = await _collect(provider, 'two')
        second_metrics = dict(provider.last_request_metrics)
        await provider.aclose()
        return first, first_metrics, second, second_metrics

    first, first_metrics, second, second_metrics = asyncio.run(run())

    assert len(connects) == 1
    assert connects[0][0].startswith('wss://')
    assert connects[0][1]['OpenAI-Beta'] == 'responses_websockets=2026-02-06'
    assert [delta.text for delta in first if delta.type == 'text'] == ['first']
    assert [delta.text for delta in second if delta.type == 'text'] == ['second']
    assert first_metrics['transport'] == 'websocket'
    assert first_metrics['transport_reused'] is False
    assert second_metrics['transport_reused'] is True
    assert all(frame['type'] == 'response.create' for frame in socket.sent)
    assert all('previous_response_id' not in frame for frame in socket.sent)


class FakePool:
    def __init__(self, handler):
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def get(self, **_kwargs):
        return self.client, False

    async def aclose(self):
        await self.client.aclose()


def test_codex_websocket_connect_failure_falls_back_to_sse(monkeypatch):
    monkeypatch.setenv('CHARON_CODEX_WEBSOCKET', '1')

    async def failing_factory(_url, _headers):
        raise OSError('upgrade rejected')

    def sse_handler(_request):
        events = _response_events('sse answer', 'resp-sse')
        body = ''.join(f'data: {event}\n\n' for event in events) + 'data: [DONE]\n\n'
        return httpx.Response(200, text=body)

    provider = HttpxCodexProvider(api_key=_token())
    provider._websocket_factory = failing_factory
    provider._clients = FakePool(sse_handler)

    async def run():
        deltas = await _collect(provider)
        metrics = dict(provider.last_request_metrics)
        await provider.aclose()
        return deltas, metrics

    deltas, metrics = asyncio.run(run())

    assert [delta.text for delta in deltas if delta.type == 'text'] == ['sse answer']
    assert metrics['transport'] == 'sse'
    assert metrics['websocket_fallback'] is True
    assert 'upgrade rejected' in metrics['websocket_error']


def test_codex_websocket_prewarm_is_reused_by_first_turn(monkeypatch):
    monkeypatch.setenv('CHARON_CODEX_WEBSOCKET', '1')
    socket = FakeSocket([_response_events('ready', 'resp-ready')])
    connects = 0

    async def factory(_url, _headers):
        nonlocal connects
        connects += 1
        return socket

    provider = HttpxCodexProvider(api_key=_token())
    provider._websocket_factory = factory

    async def run():
        assert await provider.prewarm() is True
        deltas = await _collect(provider)
        metrics = dict(provider.last_request_metrics)
        await provider.aclose()
        return deltas, metrics

    deltas, metrics = asyncio.run(run())
    assert connects == 1
    assert [delta.text for delta in deltas if delta.type == 'text'] == ['ready']
    assert metrics['transport_reused'] is True
