"""Image content through the three OpenAI-family adapters, plus a text-only
regression guard.

The browser vision fallback sends Anthropic-style image blocks. These tests pin
the wire shape each OpenAI-family adapter now produces for them — a Responses
API input_image item on codex, a Chat Completions image_url part on httpx_openai
and openai_compat — and pin that text-only payloads serialize exactly as they
did before images were carried. The GOLDEN_* payloads were generated from the
pre-change converters and are compared as serialized JSON, key order included.

codex and httpx_openai are driven end to end over httpx.MockTransport. The
compat adapter talks through the optional openai SDK, which this environment
does not install, so its stream() runs against a stand-in SDK that records the
exact kwargs the adapter hands to chat.completions.create.
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
from types import SimpleNamespace

import httpx

from charon.providers import Message, ModelInfo, ToolCall
from charon.providers import openai_compat
from charon.providers.httpx_codex import HttpxCodexProvider, _convert_messages_to_input
from charon.providers.httpx_openai import HttpxOpenAIProvider
from charon.providers.httpx_openai import _convert_messages as openai_convert_messages
from charon.providers.image_content import to_chat_completions_parts, to_responses_input_parts

CODEX_MODEL = ModelInfo(provider='codex', model_id='gpt-5.3-codex')
OPENAI_MODEL = ModelInfo(provider='openai', model_id='gpt-4o')

PNG = b'\x89PNG\r\n\x1a\n' + bytes(range(16))
PNG_B64 = base64.b64encode(PNG).decode('ascii')
DATA_URL = f'data:image/png;base64,{PNG_B64}'
IMAGE_BLOCK = {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': PNG_B64}}
QUESTION = 'What is in this image?'
REPLY = 'a small image'


def _image_message() -> Message:
    return Message(role='user', content=[IMAGE_BLOCK, {'type': 'text', 'text': QUESTION}])


def _text_conversation() -> list[Message]:
    return [
        Message(role='user', content='read x.py'),
        Message(role='assistant', content='', tool_calls=[
            ToolCall(id='call-1', name='Read', arguments={'path': 'x.py'}),
        ]),
        Message(role='tool_result', content='print("hi")', tool_call_id='call-1', tool_name='Read'),
        Message(role='assistant', content='It prints hi.'),
        Message(role='user', content=[{'type': 'text', 'text': 'follow-up'}]),
        Message(role='user', content='héllo ✓'),
    ]


GOLDEN_CODEX = [
    {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'read x.py'}]},
    {'type': 'function_call', 'id': 'fc_call-1', 'call_id': 'fc_call-1', 'name': 'Read',
     'arguments': '{"path": "x.py"}'},
    {'type': 'function_call_output', 'call_id': 'fc_call-1', 'output': 'print("hi")'},
    {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'It prints hi.'}]},
    {'type': 'message', 'role': 'user',
     'content': [{'type': 'input_text', 'text': '[{"type": "text", "text": "follow-up"}]'}]},
    {'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'héllo ✓'}]},
]

GOLDEN_OPENAI = [
    {'role': 'user', 'content': 'read x.py'},
    {'role': 'assistant', 'tool_calls': [{'id': 'call-1', 'type': 'function',
                                          'function': {'name': 'Read', 'arguments': '{"path": "x.py"}'}}],
     'content': None},
    {'role': 'tool', 'tool_call_id': 'call-1', 'content': 'print("hi")'},
    {'role': 'assistant', 'content': 'It prints hi.'},
    {'role': 'user', 'content': '[{"type": "text", "text": "follow-up"}]'},
    {'role': 'user', 'content': 'héllo ✓'},
]

GOLDEN_COMPAT = [
    {'role': 'user', 'content': 'read x.py'},
    {'role': 'assistant', 'content': '', 'tool_calls': [{'id': 'call-1', 'type': 'function',
                                                         'function': {'name': 'Read', 'arguments': '{"path": "x.py"}'}}]},
    {'role': 'tool', 'tool_call_id': 'call-1', 'content': 'print("hi")'},
    {'role': 'assistant', 'content': 'It prints hi.'},
    {'role': 'user', 'content': '[{"type": "text", "text": "follow-up"}]'},
    {'role': 'user', 'content': 'héllo ✓'},
]


def _same_json(actual, expected) -> bool:
    return json.dumps(actual) == json.dumps(expected)


# ── capture harnesses ────────────────────────────────────────────────────────

def _codex_token() -> str:
    def enc(obj):
        return base64.urlsafe_b64encode(json.dumps(obj, separators=(',', ':')).encode()).rstrip(b'=').decode()
    claims = {'exp': int(time.time()) + 3600, 'https://api.openai.com/auth': {'chatgpt_account_id': 'acct-test'}}
    return '.'.join((enc({'alg': 'none'}), enc(claims), 'sig'))


class _Pool:
    def __init__(self, handler):
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def get(self, **_kwargs):
        return self.client, False

    async def aclose(self):
        await self.client.aclose()


def _codex_request_body(monkeypatch, messages: list[Message]) -> dict:
    monkeypatch.setenv('CHARON_CODEX_WEBSOCKET', '0')  # the SSE POST carries the body we inspect
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        events = [
            {'type': 'response.created', 'response': {'id': 'resp-1'}},
            {'type': 'response.output_text.delta', 'delta': REPLY},
            {'type': 'response.completed', 'response': {'id': 'resp-1', 'usage': {
                'input_tokens': 1, 'output_tokens': 1, 'input_tokens_details': {'cached_tokens': 0}}}},
        ]
        return httpx.Response(200, text=''.join(f'data: {json.dumps(e)}\n\n' for e in events) + 'data: [DONE]\n\n')

    provider = HttpxCodexProvider(api_key=_codex_token())
    provider._clients = _Pool(handler)

    async def run():
        deltas = [d async for d in provider.stream(messages=messages, model=CODEX_MODEL, system_prompt='sys')]
        await provider.aclose()
        return deltas

    deltas = asyncio.run(run())
    assert len(bodies) == 1, deltas
    assert [d.text for d in deltas if d.type == 'text'] == [REPLY]
    return bodies[0]


def _openai_request_body(messages: list[Message]) -> dict:
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        chunk = {'choices': [{'delta': {'content': REPLY}, 'finish_reason': 'stop'}]}
        return httpx.Response(200, content=f'data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n'.encode())

    provider = HttpxOpenAIProvider(base_url='http://fake-openai:1234/v1', api_key='x')
    provider._mock_handler = handler

    async def run():
        return [d async for d in provider.stream(messages=messages, model=OPENAI_MODEL, system_prompt='sys')]

    deltas = asyncio.run(run())
    assert len(bodies) == 1, deltas
    assert [d.text for d in deltas if d.type == 'text'] == [REPLY]
    return bodies[0]


class _FakeCompletions:
    def __init__(self):
        self.kwargs: dict | None = None

    async def create(self, **kwargs):
        self.kwargs = kwargs

        async def chunks():
            delta = SimpleNamespace(content=REPLY, tool_calls=None)
            yield SimpleNamespace(choices=[SimpleNamespace(delta=delta, finish_reason='stop')], usage=None)
        return chunks()


def _compat_request_kwargs(monkeypatch, messages: list[Message]) -> dict:
    completions = _FakeCompletions()
    fake_sdk = SimpleNamespace(
        AsyncOpenAI=lambda **_kw: SimpleNamespace(chat=SimpleNamespace(completions=completions)),
        APIError=RuntimeError,
    )
    monkeypatch.setattr(openai_compat, 'openai', fake_sdk, raising=False)
    monkeypatch.setattr(openai_compat, 'HAS_OPENAI', True)
    provider = openai_compat.OpenAICompatProvider(base_url='http://fake-openai/v1', api_key='x')

    async def run():
        return [d async for d in provider.stream(messages=messages, model=OPENAI_MODEL, system_prompt='sys')]

    deltas = asyncio.run(run())
    assert completions.kwargs is not None, deltas
    assert [d.text for d in deltas if d.type == 'text'] == [REPLY]
    return completions.kwargs


def _text_parts(parts: list[dict]) -> list[str]:
    return [p.get('text', '') for p in parts if p.get('type') in ('text', 'input_text')]


# ── image payload shape ──────────────────────────────────────────────────────

def test_codex_image_block_is_sent_as_input_image_item(monkeypatch):
    body = _codex_request_body(monkeypatch, [_image_message()])
    assert body['input'] == [{
        'type': 'message',
        'role': 'user',
        'content': [
            {'type': 'input_image', 'image_url': DATA_URL},
            {'type': 'input_text', 'text': QUESTION},
        ],
    }]
    assert not any(PNG_B64 in text for text in _text_parts(body['input'][0]['content']))


def test_openai_image_block_is_sent_as_image_url_part():
    body = _openai_request_body([_image_message()])
    assert body['messages'] == [
        {'role': 'system', 'content': 'sys'},
        {'role': 'user', 'content': [
            {'type': 'image_url', 'image_url': {'url': DATA_URL}},
            {'type': 'text', 'text': QUESTION},
        ]},
    ]
    assert not any(PNG_B64 in text for text in _text_parts(body['messages'][1]['content']))


def test_compat_image_block_is_sent_as_image_url_part(monkeypatch):
    kwargs = _compat_request_kwargs(monkeypatch, [_image_message()])
    assert kwargs['messages'] == [
        {'role': 'system', 'content': 'sys'},
        {'role': 'user', 'content': [
            {'type': 'image_url', 'image_url': {'url': DATA_URL}},
            {'type': 'text', 'text': QUESTION},
        ]},
    ]


def test_image_translation_for_codex_and_openai_keeps_url_sources_and_unknown_blocks():
    url_block = {'type': 'image', 'source': {'type': 'url', 'url': 'https://example.test/a.png'}}
    odd_block = {'type': 'document', 'title': 'kept, not dropped'}
    blocks = [url_block, odd_block, IMAGE_BLOCK]
    assert to_responses_input_parts(blocks) == [
        {'type': 'input_image', 'image_url': 'https://example.test/a.png'},
        {'type': 'input_text', 'text': json.dumps(odd_block)},
        {'type': 'input_image', 'image_url': DATA_URL},
    ]
    assert to_chat_completions_parts(blocks) == [
        {'type': 'image_url', 'image_url': {'url': 'https://example.test/a.png'}},
        {'type': 'text', 'text': json.dumps(odd_block)},
        {'type': 'image_url', 'image_url': {'url': DATA_URL}},
    ]


# ── text-only regression: serialization unchanged ────────────────────────────

def test_text_only_codex_payload_unchanged(monkeypatch):
    assert _same_json(_convert_messages_to_input(_text_conversation()), GOLDEN_CODEX)
    body = _codex_request_body(monkeypatch, _text_conversation())
    assert _same_json(body['input'], GOLDEN_CODEX)


def test_text_only_openai_payload_unchanged():
    assert _same_json(openai_convert_messages(_text_conversation()), GOLDEN_OPENAI)
    body = _openai_request_body(_text_conversation())
    assert _same_json(body['messages'], [{'role': 'system', 'content': 'sys'}, *GOLDEN_OPENAI])


def test_text_only_compat_payload_unchanged(monkeypatch):
    assert _same_json(openai_compat._convert_messages(_text_conversation()), GOLDEN_COMPAT)
    kwargs = _compat_request_kwargs(monkeypatch, _text_conversation())
    assert _same_json(kwargs['messages'], [{'role': 'system', 'content': 'sys'}, *GOLDEN_COMPAT])
