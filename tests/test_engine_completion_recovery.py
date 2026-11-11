"""Engine behaviour when a completion or a request goes wrong without an error
the caller should see: a refused prompt, a cut-off reply, an empty completion,
a tool call whose arguments cannot be trusted.

Contract: docs/contracts/context-budget-and-recovery.md
"""
from __future__ import annotations

import asyncio
import json

import pytest

from charon.context.context_budget import (
    learned_context_window,
    reset_learned_context_windows,
)
from charon.conversation.conversation_engine import (
    ConversationEngine,
    compact_messages,
)
from charon.providers import Message, ModelInfo, StreamDelta, ToolCall

OVERFLOW_8K = (
    "HTTP 400: This model's maximum context length is 8192 tokens. However, "
    "you requested 9000 tokens (9000 in the messages, 0 in the completion)."
)
SUMMARIZER_PROMPT = 'You are a conversation summarizer'


def _done(stop_reason: str = 'end_turn', output_tokens: int = 50) -> StreamDelta:
    return StreamDelta(type='done', text=json.dumps({
        'usage': {'input_tokens': 100, 'output_tokens': output_tokens,
                  'total_tokens': 100 + output_tokens},
        'stop_reason': stop_reason,
    }))


def text(body: str, stop_reason: str = 'end_turn') -> list[StreamDelta]:
    return [StreamDelta(type='text', text=body), _done(stop_reason)]


def empty(stop_reason: str = 'end_turn') -> list[StreamDelta]:
    return [_done(stop_reason)]


def error(message: str, status_code: int | None = 400) -> list[StreamDelta]:
    return [StreamDelta(type='error', error=message, status_code=status_code,
                        error_code='http_error')]


def tool(call: ToolCall) -> list[StreamDelta]:
    return [StreamDelta(type='tool_call', tool_call=call), _done('tool_use')]


class ScriptedProvider:
    """Plays queued completions in order and records every request."""

    def __init__(self, *responses: list[StreamDelta], summary: list[StreamDelta] | None = None):
        self.responses = list(responses)
        self.summary = summary if summary is not None else text('## Goal\nsummarized')
        self.requests: list[dict] = []
        self.summary_requests: list[dict] = []

    async def stream(self, messages, model, system_prompt, tools=None,
                     thinking_level='off', max_tokens=16384):
        request = {'messages': list(messages), 'system_prompt': system_prompt,
                   'max_tokens': max_tokens}
        if system_prompt.startswith(SUMMARIZER_PROMPT):
            self.summary_requests.append(request)
            deltas = self.summary
        else:
            self.requests.append(request)
            index = min(len(self.requests), len(self.responses)) - 1
            deltas = self.responses[index]
        for delta in deltas:
            yield delta


def _model(name: str, window: int = 100000) -> ModelInfo:
    return ModelInfo(provider='mock', model_id=name, context_window=window)


def _engine(provider, model, tmp_path, **kwargs) -> ConversationEngine:
    return ConversationEngine(
        provider, model, project_root=tmp_path, system_prompt='You are a test agent.',
        **kwargs,
    )


def _submit(engine: ConversationEngine, message: str):
    async def collect():
        return [event async for event in engine.submit(message)]
    return asyncio.run(collect())


def _types(events) -> list[str]:
    return [event.type for event in events]


def _one(events, event_type: str):
    matches = [event for event in events if event.type == event_type]
    assert len(matches) == 1, _types(events)
    return matches[0]


@pytest.fixture(autouse=True)
def _clean_learned_windows():
    reset_learned_context_windows()
    yield
    reset_learned_context_windows()


# ── Refused prompt ──────────────────────────────────────────────────

class TestContextOverflow:
    def test_refused_prompt_is_resent_and_the_caller_sees_no_error(self, tmp_path):
        provider = ScriptedProvider(error(OVERFLOW_8K), text('done'))
        engine = _engine(provider, _model('overflow-once', 65536), tmp_path)

        events = _submit(engine, 'do the task')

        assert 'error' not in _types(events)
        recovery = _one(events, 'context_overflow_recovery')
        assert recovery.data['previous_window'] == 65536
        assert recovery.data['window'] == 8192
        assert recovery.data['reported_window'] == 8192
        assert len(provider.requests) == 2
        assert [m.role for m in engine.messages] == ['user', 'assistant']
        assert engine.messages[-1].content == 'done'
        assert _one(events, 'done').data['total_turns'] == 1

    def test_the_window_is_remembered_for_the_next_engine(self, tmp_path):
        model = _model('overflow-remembered', 65536)
        _submit(_engine(ScriptedProvider(error(OVERFLOW_8K), text('ok')), model, tmp_path), 'go')

        assert learned_context_window('mock', 'overflow-remembered') == 8192
        fresh = _engine(ScriptedProvider(text('ok')), model, tmp_path)
        assert fresh._context_window() == 8192
        assert model.context_window == 65536, 'the model record itself is not rewritten'

    def test_the_retry_asks_for_no_more_output_than_the_window_allows(self, tmp_path):
        provider = ScriptedProvider(error(OVERFLOW_8K), text('done'))
        engine = _engine(provider, _model('overflow-cap', 65536), tmp_path)

        _submit(engine, 'do the task')

        assert provider.requests[0]['max_tokens'] == 32768
        assert provider.requests[1]['max_tokens'] < 8192

    def test_a_refusal_naming_no_window_still_shrinks_the_plan(self, tmp_path):
        provider = ScriptedProvider(
            error('HTTP 400: context_length_exceeded'), text('done'))
        engine = _engine(provider, _model('overflow-unnamed', 10000), tmp_path)

        events = _submit(engine, 'go')

        assert _one(events, 'context_overflow_recovery').data['window'] == 8000
        assert 'error' not in _types(events)

    def test_history_is_compacted_before_the_retry(self, tmp_path):
        provider = ScriptedProvider(error(OVERFLOW_8K), text('done'))
        engine = _engine(provider, _model('overflow-compact', 65536), tmp_path)
        for index in range(4):
            engine.messages.append(Message(role='user', content=f'question {index}'))
            engine.messages.append(Message(role='assistant', content=f'answer {index}'))

        events = _submit(engine, 'next question')

        assert _one(events, 'context_overflow_recovery').data['compacted'] is True
        assert len(provider.summary_requests) == 1
        retried = provider.requests[1]['messages']
        assert retried[0].content.startswith('[Conversation summary')
        assert retried[-1].content == 'next question'
        assert len(retried) < 9

    def test_recovery_is_bounded_and_then_the_error_is_reported(self, tmp_path):
        provider = ScriptedProvider(error('HTTP 400: context_length_exceeded'))
        engine = _engine(provider, _model('overflow-forever', 65536), tmp_path)

        events = _submit(engine, 'go')

        assert _types(events).count('context_overflow_recovery') == 2
        assert 'context_length_exceeded' in _one(events, 'error').data['error']
        assert len(provider.requests) == 3

    def test_other_errors_are_reported_as_before(self, tmp_path):
        provider = ScriptedProvider(error('HTTP 400: invalid request body'))
        engine = _engine(provider, _model('plain-error'), tmp_path)

        events = _submit(engine, 'go')

        assert 'context_overflow_recovery' not in _types(events)
        assert 'invalid request body' in _one(events, 'error').data['error']
        assert len(provider.requests) == 1


# ── Budget applied to requests ──────────────────────────────────────

class TestRequestBudget:
    def test_oversized_tool_result_is_clipped_in_the_request_only(self, tmp_path):
        provider = ScriptedProvider(text('done'))
        engine = _engine(provider, _model('clip', 8192), tmp_path)
        big = 'HEAD' + 'x' * 60000 + 'TAIL'
        engine.messages.extend([
            Message(role='user', content='run it'),
            Message(role='assistant', content='', tool_calls=[
                ToolCall(id='tc-1', name='Bash', arguments={'command': 'make'})]),
            Message(role='tool_result', content=big, tool_call_id='tc-1', tool_name='Bash'),
        ])

        _submit(engine, 'what happened?')

        sent = provider.requests[0]['messages'][2]
        assert sent.role == 'tool_result'
        assert sent.content.startswith('HEAD') and sent.content.endswith('TAIL')
        assert 'characters omitted' in sent.content
        assert len(sent.content) < 16000
        assert engine.messages[2].content == big

    def test_small_window_compacts_at_its_history_budget(self, tmp_path):
        provider = ScriptedProvider(text('done'))
        engine = _engine(provider, _model('small-compact', 8192), tmp_path)
        # About 5000 tokens: under 70% of the window, over what an 8k window
        # leaves for history once the reply reserve is set aside.
        for index in range(5):
            engine.messages.append(Message(role='user', content=f'q{index} ' + 'x' * 2000))
            engine.messages.append(Message(role='assistant', content=f'a{index} ' + 'y' * 2000))

        events = _submit(engine, 'continue')

        assert 'compaction_end' in _types(events)
        assert len(provider.summary_requests) == 1

    def test_large_window_is_left_alone(self, tmp_path):
        provider = ScriptedProvider(text('done'))
        engine = _engine(provider, _model('large'), tmp_path)
        for index in range(5):
            engine.messages.append(Message(role='user', content=f'q{index} ' + 'x' * 2000))
            engine.messages.append(Message(role='assistant', content=f'a{index} ' + 'y' * 2000))

        events = _submit(engine, 'continue')

        assert 'compaction_end' not in _types(events)
        assert provider.requests[0]['max_tokens'] == 32768
        assert len(provider.requests[0]['messages']) == 11

    def test_unknown_window_does_no_budgeting(self, tmp_path):
        provider = ScriptedProvider(text('done'))
        engine = _engine(provider, _model('unknown-window', 0), tmp_path)
        engine.messages.extend([
            Message(role='user', content='x' * 400000),
            Message(role='assistant', content='ok'),
        ] * 3)

        events = _submit(engine, 'continue')

        assert 'compaction_end' not in _types(events)
        assert provider.requests[0]['max_tokens'] == 32768


# ── Compaction that must fit the window it relieves ─────────────────

class TestCompaction:
    def _history(self) -> list[Message]:
        history = [Message(role='user', content='Port the parser to Rust and keep the CLI flags.')]
        for index in range(6):
            history.append(Message(role='assistant', content=f'step {index} ' + 'y' * 8000))
            history.append(Message(role='user', content=f'ok {index}'))
        return history

    def test_summarizer_input_is_bounded(self):
        provider = ScriptedProvider(summary=text('## Goal\nport the parser'))

        asyncio.run(compact_messages(
            self._history(), provider, _model('summarize'), 'sys', max_input_tokens=2000))

        prompt = provider.summary_requests[0]['messages'][0].content
        assert len(prompt) <= 2000 * 4
        assert 'Port the parser to Rust' in prompt, 'the opening of the transcript is kept'
        assert 'characters omitted' in prompt

    def test_failed_summarizer_keeps_the_opening_request(self):
        provider = ScriptedProvider(summary=error(OVERFLOW_8K))

        compacted, summary = asyncio.run(compact_messages(
            self._history(), provider, _model('summarize-fails'), 'sys'))

        assert 'Port the parser to Rust and keep the CLI flags.' in summary
        assert 'Port the parser to Rust' in compacted[0].content

    def test_empty_summary_is_treated_as_a_failure(self):
        provider = ScriptedProvider(summary=empty())

        _, summary = asyncio.run(compact_messages(
            self._history(), provider, _model('summarize-empty'), 'sys'))

        assert 'Port the parser to Rust' in summary


# ── Cut-off reply ───────────────────────────────────────────────────

class TestTruncation:
    def test_cut_off_reply_is_continued(self, tmp_path):
        provider = ScriptedProvider(text('The answer is', 'length'), text('42.'))
        engine = _engine(provider, _model('cut'), tmp_path)

        events = _submit(engine, 'what is the answer?')

        assert _one(events, 'completion_recovery').data['kind'] == 'truncated'
        assert 'error' not in _types(events)
        assert [m.role for m in engine.messages] == ['user', 'assistant', 'user', 'assistant']
        assert engine.messages[1].content == 'The answer is'
        assert 'cut off at the output limit' in engine.messages[2].content
        assert engine.messages[3].content == '42.'
        assert provider.requests[1]['messages'][-1].content == engine.messages[2].content

    def test_anthropic_style_stop_reason_is_recognised(self, tmp_path):
        provider = ScriptedProvider(text('partial', 'max_tokens'), text('rest'))
        engine = _engine(provider, _model('cut-anthropic'), tmp_path)

        events = _submit(engine, 'go')

        assert _one(events, 'completion_recovery').data['kind'] == 'truncated'

    def test_reasoning_that_used_the_whole_budget_is_not_stored(self, tmp_path):
        thinking_only = [StreamDelta(type='thinking', text='hmm ' * 100), _done('length')]
        provider = ScriptedProvider(thinking_only, text('answer'))
        engine = _engine(provider, _model('cut-thinking'), tmp_path)

        _submit(engine, 'go')

        assert [m.role for m in engine.messages] == ['user', 'user', 'assistant']
        assert 'before it produced any answer or tool call' in engine.messages[1].content

    def test_repeated_cut_offs_stop_after_two_continuations(self, tmp_path):
        provider = ScriptedProvider(text('more', 'length'))
        engine = _engine(provider, _model('cut-forever'), tmp_path)

        events = _submit(engine, 'go')

        assert _types(events).count('completion_recovery') == 2
        assert len(provider.requests) == 3
        assert [e.data['stop_reason'] for e in events if e.type == 'turn_end'][-1] == 'length'

    def test_a_clean_reply_restores_the_continuation_budget(self, tmp_path):
        provider = ScriptedProvider(
            text('a', 'length'), text('b', 'length'),
            tool(ToolCall(id='tc-1', name='Read', arguments={'path': 'missing.txt'})),
            text('c', 'length'), text('d'),
        )
        engine = _engine(provider, _model('cut-reset'), tmp_path)

        events = _submit(engine, 'go')

        assert _types(events).count('completion_recovery') == 3
        assert engine.messages[-1].content == 'd'


# ── Empty completion ────────────────────────────────────────────────

class TestEmptyCompletion:
    def test_empty_completion_is_retried_once_and_not_stored(self, tmp_path):
        provider = ScriptedProvider(empty(), text('here is the answer'))
        engine = _engine(provider, _model('empty-once'), tmp_path)

        events = _submit(engine, 'go')

        assert _one(events, 'completion_recovery').data['kind'] == 'empty'
        assert 'error' not in _types(events)
        assert [m.role for m in engine.messages] == ['user', 'user', 'assistant']
        assert 'was empty' in engine.messages[1].content
        assert all(m.content for m in provider.requests[1]['messages'])

    def test_second_empty_completion_ends_the_submission(self, tmp_path):
        provider = ScriptedProvider(empty())
        engine = _engine(provider, _model('empty-twice'), tmp_path)

        events = _submit(engine, 'go')

        assert _types(events).count('completion_recovery') == 1
        assert len(provider.requests) == 2
        assert 'done' in _types(events)

    def test_a_reply_after_tool_use_is_not_mistaken_for_empty(self, tmp_path):
        (tmp_path / 'a.txt').write_text('hello')
        provider = ScriptedProvider(
            tool(ToolCall(id='tc-1', name='Read', arguments={'path': 'a.txt'})),
            text('it says hello'),
        )
        engine = _engine(provider, _model('not-empty'), tmp_path)

        events = _submit(engine, 'read a.txt')

        assert 'completion_recovery' not in _types(events)


# ── Untrusted tool-call arguments ───────────────────────────────────

class TestInvalidToolCall:
    REASON = 'the arguments of this call were cut off before they were complete'

    def test_invalid_call_is_not_executed_and_the_model_is_told_why(self, tmp_path):
        provider = ScriptedProvider(
            tool(ToolCall(id='tc-1', name='Write',
                          arguments={'path': 'half.txt', 'content': 'half of'},
                          invalid_reason=self.REASON)),
            text('retrying'),
        )
        engine = _engine(provider, _model('invalid-call'), tmp_path)

        events = _submit(engine, 'write the file')

        assert not (tmp_path / 'half.txt').exists()
        assert 'tool_execution_start' not in _types(events)
        result = engine.messages[2]
        assert result.role == 'tool_result' and result.is_error
        assert result.tool_call_id == 'tc-1'
        assert self.REASON in result.content
        assert provider.requests[1]['messages'][-1].content == result.content

    def test_valid_calls_beside_an_invalid_one_still_run(self, tmp_path):
        calls = [
            StreamDelta(type='tool_call', tool_call=ToolCall(
                id='tc-1', name='Write', arguments={'path': 'ok.txt', 'content': 'whole'})),
            StreamDelta(type='tool_call', tool_call=ToolCall(
                id='tc-2', name='Write', arguments={}, invalid_reason=self.REASON)),
            _done('length'),
        ]
        provider = ScriptedProvider(calls, text('done'))
        engine = _engine(provider, _model('mixed-calls'), tmp_path)

        _submit(engine, 'write both')

        assert (tmp_path / 'ok.txt').read_text() == 'whole'
        results = {m.tool_call_id: m for m in engine.messages if m.role == 'tool_result'}
        assert not results['tc-1'].is_error
        assert results['tc-2'].is_error and self.REASON in results['tc-2'].content
