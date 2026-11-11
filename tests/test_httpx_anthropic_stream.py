"""HttpxAnthropicProvider stream endings: the real stop reason reaches the
engine, and tool input that does not parse is never passed on as {}.

Contract: docs/contracts/context-budget-and-recovery.md
"""
from __future__ import annotations

import asyncio
import json

import httpx

from charon.providers import Message, ModelInfo
from charon.providers.httpx_anthropic import HttpxAnthropicProvider

MODEL = ModelInfo(provider='anthropic', model_id='claude-test')


class _OneClient:
    def __init__(self, events: list[dict]):
        body = ''.join(f'data: {json.dumps(event)}\n\n' for event in events).encode()
        self._client = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(200, content=body)))

    async def get(self):
        return self._client, False


def _stream(events: list[dict]):
    provider = HttpxAnthropicProvider(api_key='sk-ant-api-test')
    provider._clients = _OneClient(events)

    async def collect():
        return [delta async for delta in provider.stream(
            messages=[Message(role='user', content='go')], model=MODEL, system_prompt='sys')]
    return asyncio.run(collect())


def _tool_use(partial_json: str) -> list[dict]:
    return [
        {'type': 'message_start', 'message': {'usage': {'input_tokens': 10}}},
        {'type': 'content_block_start', 'index': 0,
         'content_block': {'type': 'tool_use', 'id': 'toolu_1', 'name': 'Write'}},
        {'type': 'content_block_delta', 'index': 0,
         'delta': {'type': 'input_json_delta', 'partial_json': partial_json}},
        {'type': 'content_block_stop', 'index': 0},
    ]


def test_max_tokens_stop_reason_is_reported():
    deltas = _stream([
        {'type': 'message_start', 'message': {'usage': {'input_tokens': 10}}},
        {'type': 'content_block_delta', 'index': 0,
         'delta': {'type': 'text_delta', 'text': 'partial'}},
        {'type': 'message_delta', 'delta': {'stop_reason': 'max_tokens'},
         'usage': {'output_tokens': 5}},
    ])

    done = json.loads(deltas[-1].text)
    assert done['stop_reason'] == 'max_tokens'
    assert done['usage']['output_tokens'] == 5


def test_stop_reason_defaults_to_end_turn_when_the_stream_names_none():
    deltas = _stream([
        {'type': 'message_start', 'message': {'usage': {'input_tokens': 10}}},
        {'type': 'content_block_delta', 'index': 0,
         'delta': {'type': 'text_delta', 'text': 'hello'}},
    ])

    assert json.loads(deltas[-1].text)['stop_reason'] == 'end_turn'


def test_complete_tool_input_is_passed_through():
    deltas = _stream(_tool_use('{"path": "a.txt", "content": "hi"}'))

    call = next(d.tool_call for d in deltas if d.type == 'tool_call')
    assert call.arguments == {'path': 'a.txt', 'content': 'hi'}
    assert call.invalid_reason == ''


def test_cut_off_tool_input_is_marked_invalid():
    deltas = _stream(_tool_use('{"path": "a.txt", "content": "hi'))

    call = next(d.tool_call for d in deltas if d.type == 'tool_call')
    assert call.arguments == {}
    assert 'did not run' in call.invalid_reason
