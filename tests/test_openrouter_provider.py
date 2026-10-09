from __future__ import annotations

import json

import httpx
import pytest

from charon.providers import Message, ModelInfo, get_provider
from charon.providers.httpx_openai import HttpxOpenAIProvider
from charon.providers.worker_provider import get_worker_provider_status


def _sse() -> bytes:
    return b'data: {"choices":[],"usage":{"prompt_tokens":2,"completion_tokens":1}}\n\ndata: [DONE]\n\n'


async def _send(provider: HttpxOpenAIProvider, effort: str) -> dict:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content))
        return httpx.Response(200, content=_sse())

    provider._mock_handler = handler
    model = ModelInfo(provider='openrouter', model_id='vendor/model', supports_thinking=True)
    async for _ in provider.stream(
        [Message(role='user', content='hello')],
        model,
        '',
        thinking_level=effort,
    ):
        pass
    return captured


def test_openrouter_provider_reads_key_file_without_sdk(tmp_path, monkeypatch):
    key_file = tmp_path / 'openrouter.key'
    key_file.write_text('test-secret\n')
    monkeypatch.delenv('OPENROUTER_API_KEY', raising=False)
    monkeypatch.setenv('OPENROUTER_API_KEY_FILE', str(key_file))
    monkeypatch.setenv('OPENAI_BASE_URL', 'https://router.example/v1')

    provider = get_provider('openrouter')

    assert isinstance(provider, HttpxOpenAIProvider)
    assert provider._api_key == 'test-secret'
    assert provider._base_url == 'https://router.example/v1'


@pytest.mark.asyncio
@pytest.mark.parametrize(('level', 'wire'), [
    ('off', 'none'),
    ('minimal', 'minimal'),
    ('xhigh', 'xhigh'),
    ('ultra', 'max'),
])
async def test_openrouter_emits_explicit_reasoning_effort(level, wire):
    provider = HttpxOpenAIProvider(
        base_url='https://router.example/v1',
        api_key='test-secret',
        provider_name='openrouter',
    )

    body = await _send(provider, level)

    assert body['reasoning_effort'] == wire
    assert provider.last_request_metrics['reasoning_effort_requested'] == level
    assert provider.last_request_metrics['reasoning_effort_emitted'] == wire


@pytest.mark.asyncio
async def test_verification_event_is_sanitized_json(monkeypatch, capsys):
    monkeypatch.setenv('CHARON_REQUIRE_REASONING_EFFORT', '1')
    monkeypatch.setenv('CHARON_EMIT_REQUEST_CONFIG', '1')
    provider = HttpxOpenAIProvider(
        base_url='https://router.example/v1',
        api_key='do-not-log-this',
        provider_name='openrouter',
    )

    await _send(provider, 'high')

    event = json.loads(capsys.readouterr().err.strip())['provider_request']
    assert event == {
        'provider': 'openrouter',
        'model': 'vendor/model',
        'reasoning_effort_requested': 'high',
        'reasoning_field': 'reasoning_effort',
        'reasoning_effort_emitted': 'high',
        'reasoning_field_emitted': True,
    }


def test_openrouter_shade_is_ready_with_file_backed_key(tmp_path, monkeypatch):
    state = tmp_path / 'state'
    state.mkdir()
    (state / 'model_registry.json').write_text(json.dumps({
        'shade_model_mode': 'fixed',
        'shade_provider': 'openrouter',
        'shade_model': 'vendor/cheap-model',
        'shade_base_url': 'https://openrouter.ai/api/v1',
        'shade_api_key': None,
    }))
    key_file = tmp_path / 'openrouter.key'
    key_file.write_text('test-secret')
    monkeypatch.delenv('OPENROUTER_API_KEY', raising=False)
    monkeypatch.setenv('OPENROUTER_API_KEY_FILE', str(key_file))

    status = get_worker_provider_status(state)

    assert status['ok'] is True
    assert status['configured_provider'] == 'openrouter'
