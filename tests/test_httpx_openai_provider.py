"""HttpxOpenAIProvider — the real, always-used path for local models (LM
Studio/Ollama/vLLM), reached via httpx.MockTransport so nothing here needs a
real server.

Local models' native tool-calling is unreliable in a way cloud providers'
rarely is: a completion can get cut off mid-argument, or just emit JSON that
doesn't quite validate. Before this file, a malformed tool-call argument
silently became {} (see _finalize_tool_call_arguments's history) — a wrong,
silent result, potentially worse than an error. These tests exercise the
three-step recovery (parse, lenient truncation repair, grammar-constrained
retry against the same server) end to end, plus the plain streaming path.
"""
import json

import httpx
import pytest

from charon.providers import Message, ModelInfo
from charon.providers.httpx_openai import (
    HttpxOpenAIProvider,
    _finalize_tool_call_arguments,
    _tool_schema_map,
    _try_lenient_json_repair,
)

MODEL = ModelInfo(provider='local', model_id='qwen3-30b-a3b')
WRITE_FILE_TOOL = {
    'name': 'write_file',
    'description': 'write a file',
    'input_schema': {
        'type': 'object',
        'properties': {'path': {'type': 'string'}, 'content': {'type': 'string'}},
        'required': ['path', 'content'],
    },
}


def _sse(chunks: list[dict]) -> bytes:
    body = ''.join(f'data: {json.dumps(c)}\n\n' for c in chunks)
    return (body + 'data: [DONE]\n\n').encode('utf-8')


def _tool_call_chunks(tool_name: str, arguments: str) -> list[dict]:
    return [
        {'choices': [{'delta': {'tool_calls': [
            {'index': 0, 'id': 'call_1', 'function': {'name': tool_name, 'arguments': ''}},
        ]}, 'finish_reason': None}]},
        {'choices': [{'delta': {'tool_calls': [
            {'index': 0, 'function': {'arguments': arguments}},
        ]}, 'finish_reason': None}]},
        {'choices': [{'delta': {}, 'finish_reason': 'tool_calls'}]},
    ]


async def _run_stream(provider, tools=None, model=MODEL):
    deltas = []
    async for d in provider.stream(
        messages=[Message(role='user', content='do it')],
        model=model, system_prompt='sys', tools=tools,
    ):
        deltas.append(d)
    return deltas


def _tool_call_delta(deltas):
    matches = [d for d in deltas if d.type == 'tool_call']
    assert len(matches) == 1, deltas
    return matches[0].tool_call


@pytest.mark.asyncio
async def test_stream_plain_text_passthrough():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse([
            {'choices': [{'delta': {'content': 'hello '}, 'finish_reason': None}]},
            {'choices': [{'delta': {'content': 'world'}, 'finish_reason': 'stop'}]},
        ]))

    provider = HttpxOpenAIProvider(base_url='http://fake-local:1234/v1', api_key='x')
    provider._mock_handler = handler
    deltas = await _run_stream(provider)

    text = ''.join(d.text for d in deltas if d.type == 'text')
    assert text == 'hello world'
    assert deltas[-1].type == 'done'


@pytest.mark.asyncio
async def test_stream_tool_call_with_valid_json_needs_no_repair():
    calls = {'n': 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls['n'] += 1
        return httpx.Response(200, content=_sse(_tool_call_chunks(
            'write_file', json.dumps({'path': 'a.txt', 'content': 'hi'}),
        )))

    provider = HttpxOpenAIProvider(base_url='http://fake-local:1234/v1', api_key='x')
    provider._mock_handler = handler
    deltas = await _run_stream(provider, tools=[WRITE_FILE_TOOL])

    tc = _tool_call_delta(deltas)
    assert tc.name == 'write_file'
    assert tc.arguments == {'path': 'a.txt', 'content': 'hi'}
    assert calls['n'] == 1, 'valid JSON must not trigger any repair round-trip'


@pytest.mark.asyncio
async def test_stream_tool_call_truncated_json_recovered_by_lenient_repair():
    """Cut off mid-value (the model ran out of tokens) — recoverable by
    closing the open string/brace, no second HTTP call needed."""
    calls = {'n': 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls['n'] += 1
        return httpx.Response(200, content=_sse(_tool_call_chunks(
            'write_file', '{"path": "a.txt", "content": "hi',
        )))

    provider = HttpxOpenAIProvider(base_url='http://fake-local:1234/v1', api_key='x')
    provider._mock_handler = handler
    deltas = await _run_stream(provider, tools=[WRITE_FILE_TOOL])

    tc = _tool_call_delta(deltas)
    assert tc.arguments == {'path': 'a.txt', 'content': 'hi'}
    assert calls['n'] == 1, 'lenient repair must not need a network round-trip'


@pytest.mark.asyncio
async def test_stream_tool_call_malformed_json_recovered_via_constrained_decoding():
    """Missing comma — bracket-balanced, so lenient repair can't fix it —
    but a schema is known, so the grammar-constrained retry kicks in."""
    request_bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        request_bodies.append(body)
        if body.get('stream') is False:
            assert body['response_format']['json_schema']['schema'] == WRITE_FILE_TOOL['input_schema']
            return httpx.Response(200, json={
                'choices': [{'message': {'content': json.dumps({'path': 'a.txt', 'content': 'hi'})}}],
            })
        return httpx.Response(200, content=_sse(_tool_call_chunks(
            'write_file', '{"path": "a.txt" "content": "hi"}',
        )))

    provider = HttpxOpenAIProvider(base_url='http://fake-local:1234/v1', api_key='x')
    provider._mock_handler = handler
    deltas = await _run_stream(provider, tools=[WRITE_FILE_TOOL])

    tc = _tool_call_delta(deltas)
    assert tc.arguments == {'path': 'a.txt', 'content': 'hi'}
    assert len(request_bodies) == 2, 'expected exactly one repair round-trip'


@pytest.mark.asyncio
async def test_stream_tool_call_falls_back_to_empty_dict_when_repair_impossible():
    """No schema available (tool wasn't in the request's tool list) — the
    constrained-decoding step can't run, so this stays the old {} fallback
    rather than raising."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_sse(_tool_call_chunks(
            'mystery_tool', '{"path": "a.txt" "content": "hi"}',
        )))

    provider = HttpxOpenAIProvider(base_url='http://fake-local:1234/v1', api_key='x')
    provider._mock_handler = handler
    deltas = await _run_stream(provider, tools=None)

    tc = _tool_call_delta(deltas)
    assert tc.arguments == {}


@pytest.mark.asyncio
async def test_stream_tool_call_falls_back_to_empty_dict_when_server_rejects_constrained_retry():
    """Server doesn't support response_format (older llama.cpp/Ollama) —
    the retry POST 400s, and the call still degrades to {} instead of
    raising through the whole stream."""
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get('stream') is False:
            return httpx.Response(400, json={'error': {'message': 'response_format not supported'}})
        return httpx.Response(200, content=_sse(_tool_call_chunks(
            'write_file', '{"path": "a.txt" "content": "hi"}',
        )))

    provider = HttpxOpenAIProvider(base_url='http://fake-local:1234/v1', api_key='x')
    provider._mock_handler = handler
    deltas = await _run_stream(provider, tools=[WRITE_FILE_TOOL])

    tc = _tool_call_delta(deltas)
    assert tc.arguments == {}
    assert deltas[-1].type == 'done', 'a failed repair must not abort the stream'


# ---------------------------------------------------------------------------
# Unit-level: the repair helpers directly, no transport involved.
# ---------------------------------------------------------------------------

def test_lenient_repair_closes_truncated_string_and_object():
    assert _try_lenient_json_repair('{"path": "a.txt", "content": "hi') == {'path': 'a.txt', 'content': 'hi'}


def test_lenient_repair_closes_truncated_nested_structure():
    assert _try_lenient_json_repair('{"items": ["a", "b"') == {'items': ['a', 'b']}


def test_lenient_repair_returns_none_when_still_invalid():
    assert _try_lenient_json_repair('{"path": "a.txt" "content": "hi"}') is None


def test_lenient_repair_returns_none_for_empty_or_non_object():
    assert _try_lenient_json_repair('') is None
    assert _try_lenient_json_repair('[1, 2, 3]') is None


def test_tool_schema_map_handles_anthropic_style():
    assert _tool_schema_map([WRITE_FILE_TOOL]) == {'write_file': WRITE_FILE_TOOL['input_schema']}


def test_tool_schema_map_handles_openai_function_style():
    tools = [{'type': 'function', 'function': {'name': 'x', 'parameters': {'type': 'object'}}}]
    assert _tool_schema_map(tools) == {'x': {'type': 'object'}}


def test_tool_schema_map_handles_bare_parameters_style():
    tools = [{'name': 'y', 'parameters': {'type': 'object'}}]
    assert _tool_schema_map(tools) == {'y': {'type': 'object'}}


def test_tool_schema_map_empty_for_none_or_empty_list():
    assert _tool_schema_map(None) == {}
    assert _tool_schema_map([]) == {}


@pytest.mark.asyncio
async def test_finalize_tool_call_arguments_empty_string_is_empty_dict_no_repair():
    args = await _finalize_tool_call_arguments(
        '', 'write_file', WRITE_FILE_TOOL['input_schema'],
        client=None, base_url='http://x', headers={}, model_id='m',
    )
    assert args == {}
