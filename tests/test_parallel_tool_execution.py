from __future__ import annotations

import asyncio
import json
import threading
import time

from charon.conversation import conversation_engine as engine_module
from charon.conversation.conversation_engine import ConversationEngine
from charon.providers import ModelInfo, StreamDelta, ToolCall
from charon.tools import ToolResult


MODEL = ModelInfo(provider='mock', model_id='mock', context_window=100_000)


class BatchProvider:
    def __init__(self, calls):
        self.calls = calls
        self.count = 0

    async def stream(self, messages, model, system_prompt, tools=None,
                     thinking_level='off', max_tokens=16384):
        self.count += 1
        if self.count == 1:
            for call in self.calls:
                yield StreamDelta(type='tool_call', tool_call=call)
            yield StreamDelta(
                type='done',
                text=json.dumps({'stop_reason': 'tool_use', 'usage': {}}),
            )
        else:
            yield StreamDelta(type='text', text='done')
            yield StreamDelta(
                type='done',
                text=json.dumps({'stop_reason': 'end_turn', 'usage': {}}),
            )


def _collect(engine):
    async def run():
        return [event async for event in engine.submit('run tools')]
    return asyncio.run(run())


def test_shared_tools_overlap_and_results_stay_in_provider_order(tmp_path, monkeypatch):
    calls = [
        ToolCall(id='slow', name='Read', arguments={'path': 'slow'}),
        ToolCall(id='fast', name='Read', arguments={'path': 'fast'}),
    ]
    active = 0
    max_active = 0
    lock = threading.Lock()

    def fake_execute(name, arguments, ctx):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.10 if arguments['path'] == 'slow' else 0.03)
        with lock:
            active -= 1
        return ToolResult(content=arguments['path'])

    monkeypatch.setattr(engine_module, 'execute_tool', fake_execute)
    engine = ConversationEngine(BatchProvider(calls), MODEL, project_root=tmp_path)
    events = _collect(engine)

    assert max_active == 2
    results = [message for message in engine.messages if message.role == 'tool_result']
    assert [message.tool_call_id for message in results[:2]] == ['slow', 'fast']
    assert sum(event.type == 'tool_execution_end' for event in events) == 2


def test_exclusive_tools_form_serial_barriers(tmp_path, monkeypatch):
    calls = [
        ToolCall(id='one', name='Write', arguments={'path': 'a'}),
        ToolCall(id='two', name='Edit', arguments={'path': 'a'}),
    ]
    active = 0
    max_active = 0
    lock = threading.Lock()

    def fake_execute(name, arguments, ctx):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.03)
        with lock:
            active -= 1
        return ToolResult(content=name)

    monkeypatch.setattr(engine_module, 'execute_tool', fake_execute)
    engine = ConversationEngine(BatchProvider(calls), MODEL, project_root=tmp_path)
    _collect(engine)

    assert max_active == 1


def test_tool_limit_persists_synthetic_results_for_every_skipped_call(
    tmp_path, monkeypatch,
):
    calls = [
        ToolCall(id='one', name='Read', arguments={'path': 'a'}),
        ToolCall(id='two', name='Read', arguments={'path': 'b'}),
    ]
    monkeypatch.setattr(
        engine_module,
        'execute_tool',
        lambda name, arguments, ctx: ToolResult(content='ok'),
    )
    engine = ConversationEngine(
        BatchProvider(calls),
        MODEL,
        project_root=tmp_path,
        max_tool_calls_per_turn=1,
    )
    events = _collect(engine)

    results = [message for message in engine.messages if message.role == 'tool_result']
    assert [message.tool_call_id for message in results[:2]] == ['one', 'two']
    assert results[1].is_error is True
    assert 'limit 1' in results[1].content
    synthetic = [
        event for event in events
        if event.type == 'tool_execution_end' and event.data.get('synthetic')
    ]
    assert len(synthetic) == 1
