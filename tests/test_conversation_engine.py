"""Tests for Charon conversation engine.

These tests use a mock provider to avoid real API calls.
They verify the agent loop, tool execution, compaction, and event flow.
"""
import asyncio
import json
import sys
from pathlib import Path
from typing import AsyncIterator

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'apps' / 'core-daemon'))

from providers import Message, ModelInfo, StreamDelta, ToolCall, Usage
from conversation_engine import (
    ConversationEngine, build_system_prompt, estimate_tokens,
    should_compact, EngineEvent,
)


# ============================================================================
# Mock provider
# ============================================================================

class MockProvider:
    """Mock LLM provider for testing."""

    def __init__(self):
        self.responses: list[list[StreamDelta]] = []
        self._call_count = 0
        self.received_messages: list[list[Message]] = []

    def queue_text_response(self, text: str):
        """Queue a simple text response."""
        self.responses.append([
            StreamDelta(type='text', text=text),
            StreamDelta(type='done', text=json.dumps({
                'usage': {'input_tokens': 100, 'output_tokens': 50, 'total_tokens': 150},
                'stop_reason': 'end_turn',
            })),
        ])

    def queue_tool_call(self, name: str, arguments: dict, call_id: str = 'tc-001'):
        """Queue a tool call response."""
        self.responses.append([
            StreamDelta(type='text', text=f'I will use {name}.'),
            StreamDelta(type='tool_call', tool_call=ToolCall(id=call_id, name=name, arguments=arguments)),
            StreamDelta(type='done', text=json.dumps({
                'usage': {'input_tokens': 100, 'output_tokens': 50, 'total_tokens': 150},
                'stop_reason': 'tool_use',
            })),
        ])

    def queue_error(self, error: str):
        """Queue an error response."""
        self.responses.append([
            StreamDelta(type='error', error=error),
        ])

    async def stream(self, messages, model, system_prompt, tools=None,
                     thinking_level='off', max_tokens=16384) -> AsyncIterator[StreamDelta]:
        self.received_messages.append(list(messages))
        idx = min(self._call_count, len(self.responses) - 1)
        self._call_count += 1
        for delta in self.responses[idx]:
            yield delta


MODEL = ModelInfo(provider='mock', model_id='mock-1', context_window=100000)


def _run(coro):
    """Helper to run async functions in tests."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is None:
        return asyncio.run(coro)
    return loop.run_until_complete(coro)


async def _collect_events(engine: ConversationEngine, message: str) -> list[EngineEvent]:
    events = []
    async for event in engine.submit(message):
        events.append(event)
    return events


# ============================================================================
# System prompt tests
# ============================================================================

class TestSystemPrompt:
    def test_default_prompt_has_tools(self):
        prompt = build_system_prompt(cwd='/tmp/proj')
        assert 'Read' in prompt
        assert 'Bash' in prompt
        assert 'Edit' in prompt
        assert 'Write' in prompt

    def test_default_prompt_has_cwd(self):
        prompt = build_system_prompt(cwd='/my/project')
        assert '/my/project' in prompt

    def test_default_prompt_has_guidelines(self):
        prompt = build_system_prompt(cwd='/tmp')
        assert 'concise' in prompt.lower()
        assert 'file paths' in prompt.lower()

    def test_custom_prompt(self):
        prompt = build_system_prompt(cwd='/tmp', custom_prompt='You are a pirate.')
        assert 'You are a pirate' in prompt
        assert '/tmp' in prompt

    def test_project_context(self):
        prompt = build_system_prompt(cwd='/tmp', project_context='This is a Django app.')
        assert 'Django app' in prompt


# ============================================================================
# Token estimation
# ============================================================================

class TestTokenEstimation:
    def test_basic_estimate(self):
        msgs = [Message(role='user', content='Hello world')]
        tokens = estimate_tokens(msgs)
        assert tokens > 0
        assert tokens < 100

    def test_empty_messages(self):
        assert estimate_tokens([]) == 0

    def test_should_compact(self):
        # Create messages that exceed threshold
        big_content = 'x' * 400000  # ~100k tokens
        msgs = [Message(role='user', content=big_content)]
        assert should_compact(msgs, context_window=50000, threshold=0.7)

    def test_should_not_compact_small(self):
        msgs = [Message(role='user', content='hello')]
        assert not should_compact(msgs, context_window=100000, threshold=0.7)


# ============================================================================
# Engine - basic flow
# ============================================================================

class TestEngineBasicFlow:
    def test_simple_text_response(self, tmp_path):
        provider = MockProvider()
        provider.queue_text_response('Hello! How can I help?')

        engine = ConversationEngine(provider, MODEL, project_root=tmp_path)
        events = _run(_collect_events(engine, 'Hi'))

        types = [e.type for e in events]
        assert 'message_start' in types  # user message
        assert 'turn_start' in types
        assert 'text_delta' in types
        assert 'message_end' in types  # assistant message
        assert 'done' in types

        # Check text content
        text_events = [e for e in events if e.type == 'text_delta']
        assert any('Hello' in e.data.get('text', '') for e in text_events)

    def test_messages_accumulated(self, tmp_path):
        provider = MockProvider()
        provider.queue_text_response('First response')
        provider.queue_text_response('Second response')

        engine = ConversationEngine(provider, MODEL, project_root=tmp_path)
        _run(_collect_events(engine, 'First'))
        assert len(engine.messages) == 2  # user + assistant

        _run(_collect_events(engine, 'Second'))
        assert len(engine.messages) == 4  # 2 users + 2 assistants

    def test_error_response(self, tmp_path):
        provider = MockProvider()
        provider.queue_error('API overloaded')

        engine = ConversationEngine(provider, MODEL, project_root=tmp_path)
        events = _run(_collect_events(engine, 'Hi'))

        error_events = [e for e in events if e.type == 'error']
        assert len(error_events) >= 1
        assert 'overloaded' in error_events[0].data.get('error', '').lower()


# ============================================================================
# Engine - tool use
# ============================================================================

class TestEngineToolUse:
    def test_read_tool_call(self, tmp_path):
        (tmp_path / 'test.txt').write_text('file contents here')

        provider = MockProvider()
        provider.queue_tool_call('Read', {'path': 'test.txt'})
        provider.queue_text_response('The file contains "file contents here".')

        engine = ConversationEngine(provider, MODEL, project_root=tmp_path)
        events = _run(_collect_events(engine, 'Read test.txt'))

        types = [e.type for e in events]
        assert 'tool_call' in types
        assert 'tool_execution_start' in types
        assert 'tool_execution_end' in types

        # Check tool result
        tool_end = [e for e in events if e.type == 'tool_execution_end'][0]
        assert 'file contents here' in tool_end.data.get('content', '')
        assert not tool_end.data.get('is_error', True)

        # Should have looped back for second LLM call
        assert provider._call_count == 2

    def test_write_tool_call(self, tmp_path):
        provider = MockProvider()
        provider.queue_tool_call('Write', {'path': 'new.txt', 'content': 'hello world'})
        provider.queue_text_response('File written successfully.')

        engine = ConversationEngine(provider, MODEL, project_root=tmp_path)
        _run(_collect_events(engine, 'Create new.txt'))

        assert (tmp_path / 'new.txt').read_text() == 'hello world'

    def test_edit_tool_call(self, tmp_path):
        (tmp_path / 'code.py').write_text('x = 1\ny = 2\n')

        provider = MockProvider()
        provider.queue_tool_call('Edit', {
            'path': 'code.py',
            'oldText': 'x = 1',
            'newText': 'x = 42',
        })
        provider.queue_text_response('Updated x to 42.')

        engine = ConversationEngine(provider, MODEL, project_root=tmp_path)
        _run(_collect_events(engine, 'Change x to 42'))

        assert 'x = 42' in (tmp_path / 'code.py').read_text()

    def test_bash_tool_call(self, tmp_path):
        provider = MockProvider()
        provider.queue_tool_call('Bash', {'command': 'echo hello'})
        provider.queue_text_response('The command output "hello".')

        engine = ConversationEngine(provider, MODEL, project_root=tmp_path)
        events = _run(_collect_events(engine, 'Run echo'))

        tool_end = [e for e in events if e.type == 'tool_execution_end'][0]
        assert 'hello' in tool_end.data.get('content', '')

    def test_tool_error_reported(self, tmp_path):
        provider = MockProvider()
        provider.queue_tool_call('Read', {'path': 'nonexistent.txt'})
        provider.queue_text_response('File not found.')

        engine = ConversationEngine(provider, MODEL, project_root=tmp_path)
        events = _run(_collect_events(engine, 'Read missing file'))

        tool_end = [e for e in events if e.type == 'tool_execution_end'][0]
        assert tool_end.data.get('is_error') is True

    def test_unknown_tool_handled(self, tmp_path):
        provider = MockProvider()
        provider.queue_tool_call('NotARealTool', {'x': 1})
        provider.queue_text_response('Tool not found.')

        engine = ConversationEngine(provider, MODEL, project_root=tmp_path)
        events = _run(_collect_events(engine, 'Use fake tool'))

        tool_end = [e for e in events if e.type == 'tool_execution_end'][0]
        assert tool_end.data.get('is_error') is True
        assert 'Unknown tool' in tool_end.data.get('content', '')

    def test_tool_results_in_messages(self, tmp_path):
        (tmp_path / 'f.txt').write_text('data')

        provider = MockProvider()
        provider.queue_tool_call('Read', {'path': 'f.txt'})
        provider.queue_text_response('Done.')

        engine = ConversationEngine(provider, MODEL, project_root=tmp_path)
        _run(_collect_events(engine, 'Read f.txt'))

        # Messages: user, assistant(tool_call), tool_result, assistant(text)
        assert len(engine.messages) == 4
        assert engine.messages[0].role == 'user'
        assert engine.messages[1].role == 'assistant'
        assert len(engine.messages[1].tool_calls) == 1
        assert engine.messages[2].role == 'tool_result'
        assert engine.messages[3].role == 'assistant'


# ============================================================================
# Engine - abort
# ============================================================================

class TestEngineAbort:
    def test_abort_stops_loop(self, tmp_path):
        provider = MockProvider()
        # Queue many tool calls - engine should stop after abort
        for i in range(10):
            provider.queue_tool_call('Bash', {'command': f'echo {i}'})

        engine = ConversationEngine(provider, MODEL, project_root=tmp_path)
        engine.abort()  # abort before even starting
        events = _run(_collect_events(engine, 'Do many things'))

        # Should complete quickly
        assert any(e.type == 'done' for e in events)


# ============================================================================
# Engine - multi-turn
# ============================================================================

class TestEngineMultiTurn:
    def test_context_preserved_across_turns(self, tmp_path):
        provider = MockProvider()
        provider.queue_text_response('I see you said hello.')
        provider.queue_text_response('Yes, you asked about x before.')

        engine = ConversationEngine(provider, MODEL, project_root=tmp_path)
        _run(_collect_events(engine, 'hello'))
        _run(_collect_events(engine, 'what did I say before?'))

        # Second call receives messages before the new assistant response:
        # user1, asst1, user2 = 3 messages
        assert len(provider.received_messages[1]) == 3


# ============================================================================
# Engine - reset
# ============================================================================

class TestEngineReset:
    def test_reset_clears_messages(self, tmp_path):
        provider = MockProvider()
        provider.queue_text_response('hello')

        engine = ConversationEngine(provider, MODEL, project_root=tmp_path)
        _run(_collect_events(engine, 'hi'))
        assert len(engine.messages) > 0

        engine.reset()
        assert len(engine.messages) == 0
