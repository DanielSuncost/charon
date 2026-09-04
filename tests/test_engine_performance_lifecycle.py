from __future__ import annotations

import asyncio
import json
import time

from charon.conversation.conversation_engine import ConversationEngine
from charon.providers import ModelInfo, StreamDelta


MODEL = ModelInfo(provider='mock', model_id='mock', context_window=100_000)


class BlockingThenAnswerProvider:
    def __init__(self):
        self.calls = 0
        self.cancelled = False

    async def stream(self, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                self.cancelled = True
                raise
            return
        yield StreamDelta(type='text', text='current status')
        yield StreamDelta(
            type='done',
            text=json.dumps({'usage': {}, 'stop_reason': 'end_turn'}),
        )


def test_steer_cancels_blocked_provider_read_promptly(tmp_path):
    provider = BlockingThenAnswerProvider()
    engine = ConversationEngine(provider, MODEL, project_root=tmp_path)

    async def run():
        events = []

        async def consume():
            async for event in engine.submit('long request'):
                events.append(event)

        task = asyncio.create_task(consume())
        while provider.calls == 0:
            await asyncio.sleep(0.01)
        started = time.monotonic()
        engine.steer('status?')
        await asyncio.wait_for(task, timeout=1.0)
        return events, time.monotonic() - started

    events, elapsed = asyncio.run(run())
    assert elapsed < 0.5
    assert provider.cancelled
    assert provider.calls == 2
    assert any(event.type == 'steer_delivered' for event in events)
    assert any(
        event.type == 'text_delta' and event.data['text'] == 'current status'
        for event in events
    )


class RetryProvider:
    def __init__(self, retryable=True):
        self.calls = 0
        self.retryable = retryable

    async def stream(self, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            yield StreamDelta(
                type='error',
                error='HTTP 503: busy',
                status_code=503,
                retryable=self.retryable,
                retry_after_seconds=0,
            )
            return
        yield StreamDelta(type='text', text='recovered')
        yield StreamDelta(
            type='done',
            text=json.dumps({'usage': {}, 'stop_reason': 'end_turn'}),
        )


def test_structured_retry_has_one_owner_and_emits_metrics(tmp_path):
    provider = RetryProvider()
    engine = ConversationEngine(provider, MODEL, project_root=tmp_path)

    _, events = asyncio.run(engine.submit_and_collect('hello'))

    assert provider.calls == 2
    retries = [event for event in events if event.type == 'retry']
    assert len(retries) == 1
    assert retries[0].data['wait_seconds'] == 0
    performance = next(event for event in events if event.type == 'performance')
    assert performance.data['provider'] == 'mock'
    assert performance.data['turns'] == 2
    assert performance.data['elapsed_ms'] >= 0


def test_non_retryable_structured_error_is_not_retried(tmp_path):
    provider = RetryProvider(retryable=False)
    engine = ConversationEngine(provider, MODEL, project_root=tmp_path)

    asyncio.run(engine.submit_and_collect('hello'))

    assert provider.calls == 1
