"""Tests for conversation engine steering and follow-up queues."""
import sys
import asyncio
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'apps' / 'core-daemon'))

from conversation_engine import ConversationEngine, EngineEvent
from providers import ModelInfo, StreamDelta, ToolCall, Message


# ── Mock provider that returns tool calls then text ─────────────────

class MockToolProvider:
    """Provider that returns a tool call on first turn, then text."""

    def __init__(self, tool_name='Bash', tool_args=None, response_text='Done.'):
        self.tool_name = tool_name
        self.tool_args = tool_args or {'command': 'echo test'}
        self.response_text = response_text
        self.call_count = 0

    async def stream(self, messages, model, system_prompt, tools=None,
                     thinking_level='off', max_tokens=16384):
        self.call_count += 1

        # Check if last message is a user steer — just respond with text
        last_msg = messages[-1] if messages else None
        if last_msg and last_msg.role == 'user' and self.call_count > 1:
            yield StreamDelta(type='text', text=f'Acknowledged: {last_msg.content}')
            yield StreamDelta(type='done', text='{"stop_reason": "end_turn"}')
            return

        # First call or after tool result: return tool call
        if self.call_count == 1:
            yield StreamDelta(
                type='tool_call',
                tool_call=ToolCall(id='tc-1', name=self.tool_name, arguments=self.tool_args),
            )
            yield StreamDelta(type='done', text='{"stop_reason": "tool_use"}')
            return

        # After tool result: return text
        yield StreamDelta(type='text', text=self.response_text)
        yield StreamDelta(type='done', text='{"stop_reason": "end_turn"}')


class MockTextProvider:
    """Provider that just returns text, no tools."""

    def __init__(self, responses=None):
        self.responses = list(responses or ['Response 1', 'Response 2', 'Response 3'])
        self.call_count = 0

    async def stream(self, messages, model, system_prompt, tools=None,
                     thinking_level='off', max_tokens=16384):
        self.call_count += 1
        text = self.responses.pop(0) if self.responses else f'Response {self.call_count}'
        yield StreamDelta(type='text', text=text)
        yield StreamDelta(type='done', text='{"stop_reason": "end_turn"}')


MODEL = ModelInfo(provider='mock', model_id='test', context_window=100000)


# ── Tests ───────────────────────────────────────────────────────────

def _collect(engine, message):
    """Run submit() and collect all events."""
    events = []
    async def _run():
        async for ev in engine.submit(message):
            events.append(ev)
    asyncio.run(_run())
    return events


def test_steer_queue_methods():
    engine = ConversationEngine(MockTextProvider(), MODEL, project_root='/tmp')
    assert engine.pending_messages == 0

    engine.steer('Change approach')
    assert engine.pending_messages == 1

    engine.follow_up('Then do this')
    assert engine.pending_messages == 2

    engine.reset()
    assert engine.pending_messages == 0


def test_steer_interrupts_after_tool_call():
    """Steering message should be delivered after a tool finishes, skipping remaining tools."""
    provider = MockToolProvider()
    engine = ConversationEngine(provider, MODEL, project_root='/tmp')

    # Queue a steer before submitting — it will be picked up after the first tool
    engine.steer('Actually, stop and try a different approach')

    events = _collect(engine, 'Run some tests')

    event_types = [e.type for e in events]

    # Should have: tool_execution_start, tool_execution_end, steer_delivered
    assert 'tool_execution_start' in event_types
    assert 'tool_execution_end' in event_types
    assert 'steer_delivered' in event_types

    # The steer should be in the messages
    steer_msgs = [m for m in engine.messages if m.role == 'user' and 'different approach' in (m.content or '')]
    assert len(steer_msgs) == 1


def test_follow_up_delivered_after_agent_stops():
    """Follow-up should be delivered when agent would normally stop."""
    provider = MockTextProvider(responses=['First response', 'Second response'])
    engine = ConversationEngine(provider, MODEL, project_root='/tmp')

    engine.follow_up('Now do this second thing')

    events = _collect(engine, 'Do the first thing')

    event_types = [e.type for e in events]

    # Should have follow_up_delivered
    assert 'follow_up_delivered' in event_types

    # Provider should have been called twice (original + follow-up)
    assert provider.call_count == 2

    # Both user messages should be in history
    user_msgs = [m for m in engine.messages if m.role == 'user']
    assert len(user_msgs) == 2
    assert 'first thing' in user_msgs[0].content
    assert 'second thing' in user_msgs[1].content


def test_follow_up_not_delivered_if_empty():
    """No follow-up means agent stops normally."""
    provider = MockTextProvider(responses=['Done'])
    engine = ConversationEngine(provider, MODEL, project_root='/tmp')

    events = _collect(engine, 'Simple request')

    event_types = [e.type for e in events]
    assert 'follow_up_delivered' not in event_types
    assert provider.call_count == 1


def test_steer_empty_string_ignored():
    engine = ConversationEngine(MockTextProvider(), MODEL, project_root='/tmp')
    engine.steer('')
    engine.steer('   ')
    assert engine.pending_messages == 0


def test_follow_up_empty_string_ignored():
    engine = ConversationEngine(MockTextProvider(), MODEL, project_root='/tmp')
    engine.follow_up('')
    engine.follow_up('   ')
    assert engine.pending_messages == 0


def test_abort_still_works():
    """Abort should still stop the engine immediately."""
    provider = MockTextProvider(responses=['Long response'])
    engine = ConversationEngine(provider, MODEL, project_root='/tmp')
    engine.abort()

    events = _collect(engine, 'Do something')

    # Should finish quickly without processing
    done_events = [e for e in events if e.type == 'done']
    assert len(done_events) == 1


def test_multiple_follow_ups_chained():
    """Multiple follow-ups should be delivered one at a time."""
    provider = MockTextProvider(responses=['R1', 'R2', 'R3'])
    engine = ConversationEngine(provider, MODEL, project_root='/tmp')

    engine.follow_up('Second task')
    engine.follow_up('Third task')

    events = _collect(engine, 'First task')

    follow_up_events = [e for e in events if e.type == 'follow_up_delivered']
    assert len(follow_up_events) == 2
    assert provider.call_count == 3
