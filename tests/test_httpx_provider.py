"""Tests for the httpx-based OpenAI-compatible provider.

Tests the SSE parsing, message conversion, and error handling
without requiring a running server (uses httpx mock transport).
"""
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'apps' / 'core-daemon'))

import httpx
from providers import Message, ModelInfo, StreamDelta, ToolCall
from providers.httpx_openai import HttpxOpenAIProvider, _convert_messages, _convert_tools


MODEL = ModelInfo(provider='local', model_id='test-model', context_window=32000)


def _run(coro):
    return asyncio.run(coro)


# ============================================================================
# Message conversion tests
# ============================================================================

class TestMessageConversion:
    def test_user_message(self):
        msgs = _convert_messages([Message(role='user', content='hello')])
        assert len(msgs) == 1
        assert msgs[0] == {'role': 'user', 'content': 'hello'}

    def test_assistant_message_text(self):
        msgs = _convert_messages([Message(role='assistant', content='hi there')])
        assert len(msgs) == 1
        assert msgs[0]['role'] == 'assistant'
        assert msgs[0]['content'] == 'hi there'

    def test_assistant_message_with_tool_calls(self):
        msgs = _convert_messages([Message(
            role='assistant',
            content='I will read the file.',
            tool_calls=[ToolCall(id='tc-1', name='Read', arguments={'path': 'x.py'})],
        )])
        assert len(msgs) == 1
        assert msgs[0]['role'] == 'assistant'
        assert msgs[0]['content'] == 'I will read the file.'
        assert len(msgs[0]['tool_calls']) == 1
        assert msgs[0]['tool_calls'][0]['function']['name'] == 'Read'

    def test_assistant_message_tool_calls_only(self):
        msgs = _convert_messages([Message(
            role='assistant',
            content='',
            tool_calls=[ToolCall(id='tc-1', name='Bash', arguments={'command': 'ls'})],
        )])
        assert msgs[0]['content'] is None  # OpenAI requires null when no text

    def test_tool_result(self):
        msgs = _convert_messages([Message(
            role='tool_result',
            content='file contents here',
            tool_call_id='tc-1',
        )])
        assert len(msgs) == 1
        assert msgs[0]['role'] == 'tool'
        assert msgs[0]['tool_call_id'] == 'tc-1'
        assert msgs[0]['content'] == 'file contents here'

    def test_full_conversation(self):
        msgs = _convert_messages([
            Message(role='user', content='read x.py'),
            Message(role='assistant', content='', tool_calls=[
                ToolCall(id='tc-1', name='Read', arguments={'path': 'x.py'}),
            ]),
            Message(role='tool_result', content='print("hi")', tool_call_id='tc-1'),
            Message(role='assistant', content='The file contains a print statement.'),
        ])
        assert len(msgs) == 4
        assert msgs[0]['role'] == 'user'
        assert msgs[1]['role'] == 'assistant'
        assert msgs[2]['role'] == 'tool'
        assert msgs[3]['role'] == 'assistant'


# ============================================================================
# Tool conversion tests
# ============================================================================

class TestToolConversion:
    def test_anthropic_format(self):
        tools = _convert_tools([{
            'name': 'Read',
            'description': 'Read a file',
            'input_schema': {
                'type': 'object',
                'properties': {'path': {'type': 'string'}},
                'required': ['path'],
            },
        }])
        assert len(tools) == 1
        assert tools[0]['type'] == 'function'
        assert tools[0]['function']['name'] == 'Read'
        assert 'parameters' in tools[0]['function']

    def test_openai_format_passthrough(self):
        tool = {
            'type': 'function',
            'function': {
                'name': 'Test',
                'description': 'test',
                'parameters': {'type': 'object'},
            },
        }
        tools = _convert_tools([tool])
        assert tools[0] == tool

    def test_simple_format(self):
        tools = _convert_tools([{
            'name': 'Bash',
            'description': 'Run command',
            'parameters': {
                'type': 'object',
                'properties': {'command': {'type': 'string'}},
                'required': ['command'],
            },
        }])
        assert tools[0]['function']['name'] == 'Bash'


# ============================================================================
# Provider error handling
# ============================================================================

class TestProviderErrors:
    def test_connection_refused(self):
        provider = HttpxOpenAIProvider(
            base_url='http://127.0.0.1:19999/v1',  # nothing running here
            api_key='test',
            timeout=3.0,
        )

        async def run():
            events = []
            async for delta in provider.stream(
                messages=[Message(role='user', content='hello')],
                model=MODEL,
                system_prompt='test',
            ):
                events.append(delta)
            return events

        events = _run(run())
        assert any(e.type == 'error' for e in events)
        error_event = [e for e in events if e.type == 'error'][0]
        assert 'Connection failed' in error_event.error or 'connection' in error_event.error.lower()


# ============================================================================
# SSE parsing (mock transport)
# ============================================================================

class TestSSEParsing:
    def _make_provider_with_response(self, sse_lines: list[str], status: int = 200):
        """Create a provider backed by a mock transport that returns fixed SSE lines."""
        sse_body = '\n'.join(sse_lines) + '\n'

        async def mock_handler(request: httpx.Request):
            return httpx.Response(
                status_code=status,
                content=sse_body.encode(),
                headers={'content-type': 'text/event-stream'},
            )

        # We'll manually patch the provider to use our handler
        provider = HttpxOpenAIProvider(base_url='http://mock/v1', api_key='test')
        provider._mock_handler = mock_handler
        return provider

    def test_parse_text_response(self):
        """Test parsing a simple text SSE stream."""
        # Simulate the SSE parsing directly
        sse_lines = [
            'data: {"choices":[{"delta":{"content":"Hello"},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{"content":" world"},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
            'data: [DONE]',
        ]

        # Parse manually to verify format
        for line in sse_lines:
            if line == 'data: [DONE]':
                continue
            json_str = line[6:]
            chunk = json.loads(json_str)
            assert 'choices' in chunk

    def test_parse_tool_call_chunks(self):
        """Test parsing streamed tool calls."""
        chunks = [
            {
                'choices': [{
                    'delta': {
                        'tool_calls': [{
                            'index': 0,
                            'id': 'call_abc',
                            'function': {'name': 'Read', 'arguments': ''},
                        }],
                    },
                    'finish_reason': None,
                }],
            },
            {
                'choices': [{
                    'delta': {
                        'tool_calls': [{
                            'index': 0,
                            'function': {'arguments': '{"path":'},
                        }],
                    },
                    'finish_reason': None,
                }],
            },
            {
                'choices': [{
                    'delta': {
                        'tool_calls': [{
                            'index': 0,
                            'function': {'arguments': '"x.py"}'},
                        }],
                    },
                    'finish_reason': None,
                }],
            },
            {
                'choices': [{'delta': {}, 'finish_reason': 'tool_calls'}],
            },
        ]

        # Simulate tool call accumulation (same logic as provider)
        current_tool_calls: dict[int, dict] = {}
        for chunk in chunks:
            delta = chunk['choices'][0]['delta']
            for tc in delta.get('tool_calls', []):
                idx = tc.get('index', 0)
                if idx not in current_tool_calls:
                    current_tool_calls[idx] = {'id': tc.get('id', ''), 'name': '', 'arguments': ''}
                entry = current_tool_calls[idx]
                if tc.get('id'):
                    entry['id'] = tc['id']
                fn = tc.get('function', {})
                if fn.get('name'):
                    entry['name'] = fn['name']
                if fn.get('arguments'):
                    entry['arguments'] += fn['arguments']

        assert len(current_tool_calls) == 1
        tc = current_tool_calls[0]
        assert tc['id'] == 'call_abc'
        assert tc['name'] == 'Read'
        args = json.loads(tc['arguments'])
        assert args == {'path': 'x.py'}

    def test_think_block_separation(self):
        """Test that inline <think> blocks are parsed into thinking deltas."""
        from providers.httpx_openai import _drain_think_buffer

        deltas, buf, in_think = _drain_think_buffer(
            '<think>Let me analyze this.</think>\nHere is my answer.',
            False,
        )

        assert buf == ''
        assert in_think is False
        assert ''.join(d.text for d in deltas if d.type == 'thinking') == 'Let me analyze this.'
        assert ''.join(d.text for d in deltas if d.type == 'text') == 'Here is my answer.'

    def test_think_block_streamed_across_chunks(self):
        """Test think block detection when <think> spans multiple SSE chunks."""
        from providers.httpx_openai import _drain_think_buffer

        chunks = ['<thi', 'nk>deep ', 'thought</thi', 'nk>\nAnswer here']
        in_think = False
        buf = ''
        deltas = []

        for chunk in chunks:
            buf += chunk
            parsed, buf, in_think = _drain_think_buffer(buf, in_think)
            deltas.extend(parsed)

        if buf:
            deltas.append(type('Delta', (), {'type': 'thinking' if in_think else 'text', 'text': buf})())

        assert ''.join(d.text for d in deltas if d.type == 'thinking') == 'deep thought'
        assert ''.join(d.text for d in deltas if d.type == 'text') == 'Answer here'

    def test_orphan_closing_think_tag_is_stripped(self):
        """Visible text should not leak lone </think> tags."""
        from providers.httpx_openai import _drain_think_buffer

        deltas, buf, in_think = _drain_think_buffer('</think>\n\nHello', False)

        assert buf == ''
        assert in_think is False
        assert ''.join(d.text for d in deltas if d.type == 'text') == 'Hello'

    def test_http_error_response(self):
        """Test that HTTP errors are handled gracefully."""
        provider = HttpxOpenAIProvider(
            base_url='http://127.0.0.1:19999/v1',
            timeout=2.0,
        )

        async def run():
            events = []
            async for delta in provider.stream(
                messages=[Message(role='user', content='hi')],
                model=MODEL,
                system_prompt='test',
            ):
                events.append(delta)
            return events

        events = _run(run())
        assert len(events) >= 1
        assert events[0].type == 'error'


# ============================================================================
# End-to-end streaming via MockTransport
# ============================================================================

class TestStreamEndToEnd:
    def _provider_serving(self, sse_lines: list[str], status: int = 200):
        sse_body = '\n'.join(sse_lines) + '\n'

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                status_code=status,
                content=sse_body.encode(),
                headers={'content-type': 'text/event-stream'},
            )

        provider = HttpxOpenAIProvider(base_url='http://mock/v1', api_key='test')
        provider._mock_handler = handler
        return provider

    def _collect(self, provider):
        async def run():
            return [d async for d in provider.stream(
                messages=[Message(role='user', content='hi')],
                model=MODEL,
                system_prompt='test',
            )]
        return _run(run())

    def test_usage_reported_in_done_delta(self):
        """Regression: usage from the final chunk must reach the done delta
        (previously it was reset to {} right before emit, always reporting 0)."""
        sse = [
            'data: {"choices":[{"delta":{"content":"Hi"},"finish_reason":null}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
            'data: {"choices":[],"usage":{"prompt_tokens":11,"completion_tokens":7,"total_tokens":18}}',
            'data: [DONE]',
        ]
        events = self._collect(self._provider_serving(sse))
        done = [e for e in events if e.type == 'done']
        assert len(done) == 1
        usage = json.loads(done[0].text)['usage']
        assert usage['input_tokens'] == 11
        assert usage['output_tokens'] == 7
        assert usage['total_tokens'] == 18

    def test_tool_call_fallback_id_is_nonempty(self):
        """When a provider omits the tool-call id, we must synthesize a stable
        non-empty id (previously used id() of a dict)."""
        sse = [
            'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
            '"function":{"name":"Read","arguments":"{\\"path\\":\\"x.py\\"}"}}]},'
            '"finish_reason":null}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
            'data: [DONE]',
        ]
        events = self._collect(self._provider_serving(sse))
        calls = [e for e in events if e.type == 'tool_call']
        assert len(calls) == 1
        assert calls[0].tool_call.id and calls[0].tool_call.id.startswith('call_')
        assert calls[0].tool_call.name == 'Read'
        assert calls[0].tool_call.arguments == {'path': 'x.py'}
