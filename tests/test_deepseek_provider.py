from __future__ import annotations

import json

import httpx
import pytest

from charon.providers import Message, ModelInfo, ToolCall, deepseek
from charon.providers import model_catalog, provider_bridge
from charon.providers.effort import supported_efforts
from charon.providers.httpx_openai import HttpxOpenAIProvider

TOOLS = [{'name': 'Read', 'description': 'read', 'input_schema': {'type': 'object', 'properties': {}}}]

# Shapes from api-docs.deepseek.com: reasoning_content deltas, then content,
# then a usage chunk with DeepSeek's own cache-hit counters.
SSE = (
    b'data: {"choices":[{"index":0,"delta":{"role":"assistant","content":null,"reasoning_content":"Let me think."}}]}\n\n'
    b'data: {"choices":[{"index":0,"delta":{"content":"Hi."}}]}\n\n'
    b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":100,'
    b'"completion_tokens":20,"total_tokens":120,"prompt_cache_hit_tokens":64,"prompt_cache_miss_tokens":36,'
    b'"completion_tokens_details":{"reasoning_tokens":5}}}\n\n'
    b'data: [DONE]\n\n'
)


async def _send(messages, effort, tools=TOOLS):
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured['url'] = str(request.url)
        captured['body'] = json.loads(request.content)
        return httpx.Response(200, content=SSE)

    provider = HttpxOpenAIProvider(base_url=deepseek.BASE_URL, api_key='sk-test', provider_name='deepseek')
    provider._mock_handler = handler
    deltas = [d async for d in provider.stream(
        messages, ModelInfo(provider='openai', model_id='deepseek-flash'), 'sys', tools=tools, thinking_level=effort,
    )]
    return captured, deltas, provider


@pytest.mark.asyncio
async def test_off_disables_server_default_thinking():
    captured, _, _ = await _send([Message(role='user', content='hi')], 'off')
    assert captured['url'] == 'https://api.deepseek.com/chat/completions'
    assert captured['body']['thinking'] == {'type': 'disabled'}
    assert 'reasoning_effort' not in captured['body']


@pytest.mark.asyncio
@pytest.mark.parametrize(('level', 'wire'), [('minimal', 'low'), ('medium', 'low'), ('high', 'high'), ('xhigh', 'high'), ('ultra', 'max')])
async def test_effort_clamps_onto_low_high_max(level, wire):
    captured, _, provider = await _send([Message(role='user', content='hi')], level)
    assert captured['body']['thinking'] == {'type': 'enabled'}
    assert captured['body']['reasoning_effort'] == wire
    assert provider.last_request_metrics['reasoning_effort_emitted'] == wire


@pytest.mark.asyncio
async def test_reasoning_streams_as_thinking_and_cache_hits_are_counted():
    _, deltas, _ = await _send([Message(role='user', content='hi')], 'high')
    assert [d.text for d in deltas if d.type == 'thinking'] == ['Let me think.']
    assert ''.join(d.text for d in deltas if d.type == 'text') == 'Hi.'
    usage = json.loads(deltas[-1].text)['usage']
    assert usage['input_tokens'] == 100 and usage['cache_read_tokens'] == 64


@pytest.mark.asyncio
async def test_prior_reasoning_is_passed_back_in_its_own_field():
    history = [
        Message(role='user', content='go'),
        Message(role='assistant', content='', thinking='plan', tool_calls=[ToolCall(id='c1', name='Read', arguments={})]),
        Message(role='tool_result', content='ok', tool_call_id='c1'),
    ]
    captured, _, _ = await _send(history, 'high')
    assistant = captured['body']['messages'][2]
    assert assistant['reasoning_content'] == 'plan'
    assert assistant['content'] is None
    assert captured['body']['thinking'] == {'type': 'enabled'}


@pytest.mark.asyncio
async def test_tool_history_without_reasoning_runs_non_thinking():
    history = [
        Message(role='user', content='go'),
        Message(role='assistant', content='', tool_calls=[ToolCall(id='c1', name='Read', arguments={})]),
        Message(role='tool_result', content='ok', tool_call_id='c1'),
    ]
    captured, _, provider = await _send(history, 'high')
    assert captured['body']['thinking'] == {'type': 'disabled'}
    assert 'reasoning_content' not in captured['body']['messages'][2]
    assert provider.last_request_metrics['reasoning_downgraded']


def test_discovery_learns_efforts_and_drops_retired_models():
    listing = {'object': 'list', 'data': [
        {'id': 'deepseek-flash', 'name': 'DeepSeek-V4.1-Flash', 'context_window': 1048576,
         'effort': {'supported_levels': ['low', 'high', 'max'], 'default_level': 'high'}},
        {'id': 'deepseek-reasoner', 'name': 'legacy'},
    ]}
    seen = {}

    def get(url, headers, params):
        seen.update(url=url, auth=headers.get('Authorization'))
        return listing

    models = model_catalog.discover('deepseek', 'sk-test', '', get)
    assert seen == {'url': 'https://api.deepseek.com/models', 'auth': 'Bearer sk-test'}
    assert [m['id'] for m in models] == ['deepseek-flash']
    assert models[0]['efforts'] == ['low', 'high', 'max'] and models[0]['context_window'] == 1048576
    assert supported_efforts('deepseek-v4-pro') == ('low', 'high', 'max')


def test_key_comes_from_deepseek_sign_in_never_openai(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY', 'sk-openai')
    monkeypatch.delenv('DEEPSEEK_API_KEY', raising=False)
    monkeypatch.delenv('DEEPSEEK_API_KEY_FILE', raising=False)
    resolve = provider_bridge._resolve_api_key
    assert resolve('openai', 'deepseek', {'api_key': 'generic'}, {}) == ''
    store = {'providers': {'deepseek': {'api_key': 'sk-deep'}}}
    assert resolve('openai', 'deepseek', {}, store) == 'sk-deep'


def test_saved_key_file_is_0600(tmp_path, monkeypatch):
    from charon.providers import charon_auth
    monkeypatch.setattr(charon_auth, 'AUTH_DIR', tmp_path / 'auth')
    monkeypatch.setattr(charon_auth, 'AUTH_FILE', tmp_path / 'auth' / 'auth.json')
    deepseek.save_api_key('sk-deep')
    assert (charon_auth.AUTH_FILE.stat().st_mode & 0o777) == 0o600
    monkeypatch.delenv('DEEPSEEK_API_KEY', raising=False)
    monkeypatch.delenv('DEEPSEEK_API_KEY_FILE', raising=False)
    assert deepseek.read_api_key(tmp_path) == 'sk-deep'
