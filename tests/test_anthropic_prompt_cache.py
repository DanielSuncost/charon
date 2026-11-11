"""Prompt caching on the Anthropic providers.

A broken cache is silent in production: requests keep succeeding and only
the bill changes. These tests pin the two things that make caching work:
the breakpoints are on the request, and the cached prefix (tools, system,
earlier messages) is byte-identical from one request to the next. The live
test at the bottom checks the API actually reports cache reads.
"""
from __future__ import annotations

import asyncio
import json
import os

import httpx
import pytest

from charon.conversation.conversation_engine import ConversationEngine
from charon.providers import ModelInfo
from charon.providers.httpx_anthropic import (
    HttpxAnthropicProvider,
    apply_prompt_caching,
    usage_from_anthropic,
)

MODEL = ModelInfo(provider='anthropic', model_id='claude-haiku-5-5', context_window=200_000)


def _sse(events: list[dict]) -> bytes:
    return b''.join(f'data: {json.dumps(e)}\n\n'.encode() for e in events)


def _text_reply(text: str) -> bytes:
    return _sse([
        {'type': 'message_start', 'message': {'usage': {
            'input_tokens': 7, 'cache_read_input_tokens': 2000, 'cache_creation_input_tokens': 40,
        }}},
        {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}},
        {'type': 'content_block_delta', 'index': 0, 'delta': {'type': 'text_delta', 'text': text}},
        {'type': 'content_block_stop', 'index': 0},
        {'type': 'message_delta', 'delta': {'stop_reason': 'end_turn'}, 'usage': {'output_tokens': 3}},
        {'type': 'message_stop'},
    ])


def _tool_reply(calls: list[tuple[str, str, dict]]) -> bytes:
    events: list[dict] = [{'type': 'message_start', 'message': {'usage': {'input_tokens': 5}}}]
    for index, (call_id, name, args) in enumerate(calls):
        events += [
            {'type': 'content_block_start', 'index': index,
             'content_block': {'type': 'tool_use', 'id': call_id, 'name': name, 'input': {}}},
            {'type': 'content_block_delta', 'index': index,
             'delta': {'type': 'input_json_delta', 'partial_json': json.dumps(args)}},
            {'type': 'content_block_stop', 'index': index},
        ]
    events += [
        {'type': 'message_delta', 'delta': {'stop_reason': 'tool_use'}, 'usage': {'output_tokens': 9}},
        {'type': 'message_stop'},
    ]
    return _sse(events)


@pytest.fixture
def wire(monkeypatch):
    """Capture every request body and answer from a script of SSE replies."""
    bodies: list[dict] = []
    replies: list[bytes] = []

    async def send(self, request, *args, **kwargs):
        bodies.append(json.loads(request.content))
        return httpx.Response(200, content=replies.pop(0), request=request)

    monkeypatch.setattr(httpx.AsyncClient, 'send', send)
    return bodies, replies


def _engine(tmp_path) -> ConversationEngine:
    provider = HttpxAnthropicProvider(api_key='sk-ant-api-test')
    return ConversationEngine(provider, MODEL, project_root=tmp_path, interactive=False, thinking_level='off')


def _breakpoints(body: dict) -> int:
    count = 1 if 'cache_control' in body else 0
    count += sum('cache_control' in t for t in body.get('tools') or [])
    count += sum('cache_control' in b for b in body.get('system') or [] if isinstance(b, dict))
    for message in body.get('messages') or []:
        if isinstance(message.get('content'), list):
            count += sum('cache_control' in b for b in message['content'] if isinstance(b, dict))
    return count


def _strip(blocks):
    return [{k: v for k, v in b.items() if k != 'cache_control'} for b in blocks]


def test_request_carries_breakpoints_on_tools_system_and_tail(tmp_path, wire):
    bodies, replies = wire
    replies.append(_text_reply('hello'))
    asyncio.run(_engine(tmp_path).submit_and_collect('hi'))

    body = bodies[0]
    assert body['cache_control'] == {'type': 'ephemeral'}
    assert body['tools'][-1]['cache_control'] == {'type': 'ephemeral'}
    assert body['system'][-1]['cache_control'] == {'type': 'ephemeral'}
    assert all('cache_control' not in t for t in body['tools'][:-1])
    assert _breakpoints(body) <= 4


def test_cached_prefix_is_byte_identical_across_turns(tmp_path, wire):
    """The regression that zeroes the hit rate: anything per-call in the prefix."""
    bodies, replies = wire
    (tmp_path / 'a.txt').write_text('alpha\n')
    (tmp_path / 'b.txt').write_text('beta\n')
    replies += [
        _text_reply('first'),
        _tool_reply([('t1', 'Read', {'path': 'a.txt'}), ('t2', 'Read', {'path': 'b.txt'})]),
        _text_reply('read both'),
    ]
    engine = _engine(tmp_path)
    asyncio.run(engine.submit_and_collect('hello'))
    # "txt" once loaded the X tool mid-session, rewriting the cached prefix.
    asyncio.run(engine.submit_and_collect('read a.txt and b.txt, then fix and explain'))

    assert len(bodies) == 3
    for earlier, later in zip(bodies, bodies[1:], strict=False):
        assert _strip(later['tools']) == _strip(earlier['tools'])
        assert _strip(later['system']) == _strip(earlier['system'])
        assert later['messages'][:len(earlier['messages'])] == earlier['messages']
        assert _breakpoints(later) <= 4

    # Parallel tool results travel as one user message (one lookback position).
    results = [m for m in bodies[2]['messages'] if isinstance(m['content'], list)
               and m['content'] and m['content'][0].get('type') == 'tool_result']
    assert len(results) == 1 and len(results[0]['content']) == 2


def test_caching_never_mutates_shared_tool_definitions():
    tools = [{'name': 'Read', 'input_schema': {}}, {'name': 'Bash', 'input_schema': {}}]
    body = apply_prompt_caching({'tools': tools, 'system': 'static prefix', 'messages': []})
    assert 'cache_control' not in tools[-1]
    assert body['tools'][-1]['cache_control'] == {'type': 'ephemeral'}


def test_usage_reports_the_whole_prompt_with_the_cache_split(tmp_path, wire):
    bodies, replies = wire
    replies.append(_text_reply('hello'))
    _, events = asyncio.run(_engine(tmp_path).submit_and_collect('hi'))
    usage = next(e.data['usage'] for e in events if e.type == 'message_end')
    assert usage['input_tokens'] == 2047
    assert usage['uncached_input_tokens'] == 7
    assert usage['cache_read_tokens'] == 2000
    assert usage['cache_write_tokens'] == 40
    assert usage_from_anthropic({})['input_tokens'] == 0


def test_incidental_x_does_not_change_the_tool_list(tmp_path, wire):
    """"fix", "explain", "txt" once loaded the X tool and rewrote the whole prefix."""
    bodies, replies = wire
    replies += [_text_reply('one'), _text_reply('two')]
    engine = _engine(tmp_path)
    asyncio.run(engine.submit_and_collect('hello'))
    asyncio.run(engine.submit_and_collect('fix the txt export and explain the next step'))
    assert [t['name'] for t in bodies[1]['tools']] == [t['name'] for t in bodies[0]['tools']]
    assert 'X' not in {t['name'] for t in bodies[1]['tools']}


def test_near_full_session_keeps_the_prefix_for_several_turns(tmp_path, wire):
    """Over budget, history is evicted in chunks, not one message per turn."""
    bodies, replies = wire
    turns = 40
    replies += [_text_reply(f'ack {i}') for i in range(turns)]
    small = ModelInfo(provider='anthropic', model_id='claude-haiku-5-5', context_window=4000)
    engine = ConversationEngine(
        HttpxAnthropicProvider(api_key='sk-ant-api-test'), small, project_root=tmp_path,
        agent_id='cache-test-agent', state_dir=tmp_path / 'state', interactive=False,
        thinking_level='off', auto_compact=False,  # isolate eviction from compaction
    )
    assert engine._lossless_enabled
    # A 4-message protected tail keeps this small window proportioned like a
    # real one, where the 20-message tail is a small share of 200k tokens.
    from charon.context.context_assembler import ContextAssembler
    engine._ctx_assembler = ContextAssembler(fresh_tail_count=4)
    for i in range(turns):
        asyncio.run(engine.submit_and_collect(f'note {i}: ' + 'lorem ipsum ' * 80))

    first_messages = [b['messages'][0] for b in bodies]
    assert first_messages[0] != first_messages[-1], 'the session never went over budget'
    moves = sum(1 for a, b in zip(first_messages, first_messages[1:], strict=False) if a != b)
    print(f'near-full: prefix moved on {moves} of {turns - 1} turns')
    assert moves <= turns // 8, f'prefix moved on {moves} of {turns - 1} turns'
    # Between moves, each request extends the previous one byte for byte.
    for earlier, later in zip(bodies, bodies[1:], strict=False):
        if later['messages'][0] == earlier['messages'][0]:
            assert later['messages'][:len(earlier['messages'])] == earlier['messages']
        assert later['system'] == earlier['system']
        assert later['tools'] == earlier['tools']


LIVE_KEY = os.environ.get('ANTHROPIC_API_KEY', '')


@pytest.mark.skipif(
    os.environ.get('CHARON_LIVE_ANTHROPIC') != '1' or not LIVE_KEY,
    reason='live API check: set CHARON_LIVE_ANTHROPIC=1 and ANTHROPIC_API_KEY',
)
def test_live_second_identical_request_reads_the_cache(tmp_path):
    """Two identical requests: the second must report cache_read_input_tokens > 0.

    Uses CHARON_LIVE_ANTHROPIC_MODEL (default claude-haiku-5-5, 512-token
    minimum prefix). On Opus 4.6/4.5 or Haiku 4.5 the minimum is 4096 and
    Charon's ~2-3.5k prefix legitimately caches nothing.
    """
    model = ModelInfo(
        provider='anthropic',
        model_id=os.environ.get('CHARON_LIVE_ANTHROPIC_MODEL', 'claude-haiku-5-5'),
        context_window=200_000,
    )

    async def one_request() -> dict:
        provider = HttpxAnthropicProvider(api_key=LIVE_KEY)
        engine = ConversationEngine(provider, model, project_root=tmp_path, interactive=False,
                                    thinking_level='off', max_tokens=16)
        _, events = await engine.submit_and_collect('Reply with the single word: ok')
        await provider.aclose()
        return next(e.data['usage'] for e in events if e.type == 'message_end')

    first = asyncio.run(one_request())
    second = asyncio.run(one_request())
    print('live usage', json.dumps({'first': first, 'second': second}))
    assert first['cache_write_tokens'] + first['cache_read_tokens'] > 0, first
    assert second['cache_read_tokens'] > 0, second
