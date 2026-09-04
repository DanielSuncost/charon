"""Tests for judge_engine.score_text — one-shot LLM-judge scoring of a
standalone piece of text (no file scope, no iterate/checkpoint loop),
reusing the same _call_llm_judge/JudgeVerdict machinery as AestheticJudge."""
from charon.judge.judge_engine import score_text
from charon.providers import StreamDelta


class _FakeProvider:
    def __init__(self, response_text):
        self._response_text = response_text

    async def stream(self, *, messages, model, system_prompt, max_tokens):
        yield StreamDelta(type='text', text=self._response_text)


def test_score_text_parses_high_score():
    provider = _FakeProvider('{"score": 9, "feedback": "Specific, reusable finding."}')
    verdict = score_text('a real finding', rubric='rate it', provider=provider, model='fake-model')
    assert verdict.score == 9
    assert verdict.error is None


def test_score_text_parses_low_score():
    provider = _FakeProvider('{"score": 2, "feedback": "Routine progress, not worth keeping."}')
    verdict = score_text('did some stuff', rubric='rate it', provider=provider, model='fake-model')
    assert verdict.score == 2


def test_score_text_no_provider_returns_error_verdict():
    verdict = score_text('anything', rubric='rate it')
    assert verdict.error == 'no_provider'
    assert verdict.score == 0.0


def test_score_text_prompt_includes_rubric_context_and_text():
    captured = {}

    class _CapturingProvider:
        async def stream(self, *, messages, model, system_prompt, max_tokens):
            captured['prompt'] = messages[0].content
            yield StreamDelta(type='text', text='{"score": 5, "feedback": "ok"}')

    score_text(
        'the output text', rubric='RUBRIC TEXT', context='THE OBJECTIVE',
        provider=_CapturingProvider(), model='fake-model',
    )
    assert 'RUBRIC TEXT' in captured['prompt']
    assert 'THE OBJECTIVE' in captured['prompt']
    assert 'the output text' in captured['prompt']
