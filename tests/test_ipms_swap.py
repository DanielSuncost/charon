"""IPMS swap primitive: full-fidelity checkpoint + silent cross-provider resume.

The core claim under test: run turn 1 on model A, checkpoint, resume on
model B, and model B sees the identical system prompt and the full turn-1
history — no [CONTEXT TRANSFER] block, no truncation, no message filtering.
"""
import asyncio
import json

from charon.context.context_transfer import (
    apply_checkpoint_to_engine,
    checkpoint_messages,
    create_checkpoint,
    create_checkpoint_from_engine,
    load_checkpoint,
)
from charon.conversation.conversation_engine import ConversationEngine
from charon.providers import Message, ModelInfo, StreamDelta, ToolCall

import pytest


def _run(coro):
    return asyncio.run(coro)


class ScriptedProvider:
    """Provider-protocol fake: replays scripted text, records what it saw."""

    def __init__(self, name: str, responses: list[str]):
        self.name = name
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def stream(self, messages, model, system_prompt, tools=None,
                     thinking_level='off', max_tokens=16384):
        self.calls.append({
            'messages': [(m.role, m.content) for m in messages],
            'thinking': [m.thinking for m in messages],
            'system_prompt': system_prompt,
            'model_id': model.model_id,
            'tools': tools,
            'thinking_level': thinking_level,
            'max_tokens': max_tokens,
        })
        text = self._responses.pop(0) if self._responses else '(no scripted response)'
        yield StreamDelta(type='text', text=text)
        yield StreamDelta(type='done', text=json.dumps({
            'usage': {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2},
            'stop_reason': 'end_turn',
        }))


MODEL_A = ModelInfo(provider='mock-a', model_id='model-a', context_window=200000)
MODEL_B = ModelInfo(provider='mock-b', model_id='model-b', context_window=200000)
SYSTEM = 'You are Nyx, a release engineer. Charter: never ship on Fridays.'


def _engine(provider, model, **kwargs):
    kwargs.setdefault('system_prompt', SYSTEM)
    kwargs.setdefault('thinking_level', 'off')
    kwargs.setdefault('auto_compact', False)
    return ConversationEngine(provider, model, **kwargs)


def test_two_turn_swap_preserves_turn1_facts(tmp_path):
    provider_a = ScriptedProvider('mock-a', ['Noted: the deploy token is amaranth-7.'])
    engine_a = _engine(provider_a, MODEL_A)
    final_a, _ = _run(engine_a.submit_and_collect(
        'Remember this: the deploy token is amaranth-7.'))
    assert 'amaranth-7' in final_a

    ckpt = create_checkpoint_from_engine(engine_a, state_dir=tmp_path, label='turn-1')

    provider_b = ScriptedProvider('mock-b', ['The deploy token is amaranth-7.'])
    engine_b = _engine(provider_b, MODEL_B, system_prompt='(placeholder)')
    restored = apply_checkpoint_to_engine(engine_b, ckpt)
    assert restored == 2  # turn-1 user + assistant

    final_b, _ = _run(engine_b.submit_and_collect('What is the deploy token?'))
    assert 'amaranth-7' in final_b

    call = provider_b.calls[0]
    assert call['model_id'] == 'model-b'
    # Full turn-1 history is visible to model B, in order.
    roles = [r for r, _ in call['messages']]
    assert roles == ['user', 'assistant', 'user']
    assert 'amaranth-7' in call['messages'][0][1]
    assert 'amaranth-7' in call['messages'][1][1]
    # The swap is silent: identical system prompt, no transfer block.
    assert call['system_prompt'] == SYSTEM
    assert '[CONTEXT TRANSFER]' not in call['system_prompt']


def test_checkpoint_round_trip_fidelity(tmp_path):
    messages = [
        Message(role='user', content='Check the build status.'),
        Message(role='assistant', content='Running the check.',
                thinking='I should look at CI.',
                tool_calls=[ToolCall(id='tc-1', name='Bash',
                                     arguments={'command': 'ci status'})]),
        Message(role='tool_result', content='green', tool_call_id='tc-1',
                tool_name='Bash', is_error=False),
        Message(role='assistant', content='Build is green.'),
    ]
    ckpt = create_checkpoint(
        state_dir=tmp_path,
        messages=messages,
        system_prompt=SYSTEM,
        source_provider='mock-a',
        source_model='model-a',
        agent={'id': 'AG-IPMS', 'charter': 'never ship on Fridays'},
    )

    loaded = load_checkpoint(tmp_path, ckpt['id'])
    assert loaded is not None
    assert loaded['system_prompt'] == SYSTEM
    assert loaded['source']['model'] == 'model-a'
    assert loaded['agent']['charter'] == 'never ship on Fridays'

    full = checkpoint_messages(loaded, strip_thinking=False)
    assert [m.role for m in full] == ['user', 'assistant', 'tool_result', 'assistant']
    assert full[1].thinking == 'I should look at CI.'
    assert full[1].tool_calls[0].id == 'tc-1'
    assert full[1].tool_calls[0].arguments == {'command': 'ci status'}
    assert full[2].tool_call_id == 'tc-1'
    assert full[2].is_error is False

    stripped = checkpoint_messages(loaded, strip_thinking=True)
    assert stripped[1].thinking == ''
    assert stripped[1].content == 'Running the check.'


def test_system_prompt_override_for_ablations(tmp_path):
    ckpt = create_checkpoint(
        state_dir=tmp_path,
        messages=[Message(role='user', content='hi')],
        system_prompt=SYSTEM,
        source_provider='mock-a',
        source_model='model-a',
    )
    engine_b = _engine(ScriptedProvider('mock-b', []), MODEL_B)
    apply_checkpoint_to_engine(engine_b, ckpt,
                               system_prompt_override='You are an assistant.')
    assert engine_b.system_prompt == 'You are an assistant.'
    assert engine_b.resume_checkpoint['id'] == ckpt['id']
    assert engine_b.resume_checkpoint['source']['provider'] == 'mock-a'


def test_resume_seeds_lossless_store(tmp_path):
    provider_a = ScriptedProvider('mock-a', ['Understood: canary first, always.'])
    engine_a = _engine(provider_a, MODEL_A)
    _run(engine_a.submit_and_collect('Policy: always deploy canary first.'))
    ckpt = create_checkpoint_from_engine(engine_a, state_dir=tmp_path)

    provider_b = ScriptedProvider('mock-b', ['Canary first, per your policy.'])
    engine_b = _engine(provider_b, MODEL_B,
                       state_dir=tmp_path, agent_id='ipms-resumed')
    if not engine_b.has_lossless_store:
        pytest.skip('lossless context store unavailable in this environment')
    restored = apply_checkpoint_to_engine(engine_b, ckpt)
    assert restored == 2

    # The store now holds the trajectory, so DB assembly sees it too.
    fresh = _engine(ScriptedProvider('mock-b', []), MODEL_B,
                    state_dir=tmp_path, agent_id='ipms-resumed')
    assert fresh.load_from_store() == 2

    _run(engine_b.submit_and_collect('What is the deploy policy?'))
    roles = [r for r, _ in provider_b.calls[0]['messages']]
    assert roles == ['user', 'assistant', 'user']
    assert 'canary' in provider_b.calls[0]['messages'][0][1]


def test_resume_onto_divergent_store_raises(tmp_path):
    ckpt = create_checkpoint(
        state_dir=tmp_path,
        messages=[
            Message(role='user', content='alpha'),
            Message(role='assistant', content='beta'),
        ],
        system_prompt=SYSTEM,
        source_provider='mock-a',
        source_model='model-a',
    )
    # Pre-seed the target agent_id with a different history.
    dirty = _engine(ScriptedProvider('mock-b', ['sure']), MODEL_B,
                    state_dir=tmp_path, agent_id='ipms-dirty')
    if not dirty.has_lossless_store:
        pytest.skip('lossless context store unavailable in this environment')
    _run(dirty.submit_and_collect('unrelated conversation'))
    _run(dirty.submit_and_collect('more unrelated turns'))

    engine_b = _engine(ScriptedProvider('mock-b', []), MODEL_B,
                       state_dir=tmp_path, agent_id='ipms-dirty')
    with pytest.raises(RuntimeError, match='divergent history'):
        apply_checkpoint_to_engine(engine_b, ckpt)


def test_checkpoint_from_engine_without_state_dir_requires_explicit_dir():
    engine = _engine(ScriptedProvider('mock-a', []), MODEL_A)
    with pytest.raises(ValueError, match='state_dir'):
        create_checkpoint_from_engine(engine)


def test_same_shape_different_content_store_raises(tmp_path):
    """A store holding a same-role-pattern but different trajectory must be
    rejected — DB assembly would silently serve the wrong history."""
    prov = ScriptedProvider('mock-a', ['noted X1'])
    seeded = _engine(prov, MODEL_A, state_dir=tmp_path, agent_id='ipms-shape')
    if not seeded.has_lossless_store:
        pytest.skip('lossless context store unavailable in this environment')
    _run(seeded.submit_and_collect('the fact is X1'))

    ckpt = create_checkpoint(
        state_dir=tmp_path,
        messages=[
            Message(role='user', content='the fact is X2'),
            Message(role='assistant', content='noted X2'),
        ],
        system_prompt=SYSTEM,
        source_provider='mock-a',
        source_model='model-a',
    )
    engine_b = _engine(ScriptedProvider('mock-b', []), MODEL_B,
                       state_dir=tmp_path, agent_id='ipms-shape')
    with pytest.raises(RuntimeError, match='divergent history'):
        apply_checkpoint_to_engine(engine_b, ckpt)


def test_empty_checkpoint_onto_dirty_store_raises(tmp_path):
    ckpt = create_checkpoint(
        state_dir=tmp_path, messages=[], system_prompt=SYSTEM,
        source_provider='mock-a', source_model='model-a',
    )
    dirty = _engine(ScriptedProvider('mock-b', ['ok']), MODEL_B,
                    state_dir=tmp_path, agent_id='ipms-turn0')
    if not dirty.has_lossless_store:
        pytest.skip('lossless context store unavailable in this environment')
    _run(dirty.submit_and_collect('pre-existing dirty turn'))

    engine_b = _engine(ScriptedProvider('mock-b', []), MODEL_B,
                       state_dir=tmp_path, agent_id='ipms-turn0')
    with pytest.raises(RuntimeError, match='divergent history'):
        apply_checkpoint_to_engine(engine_b, ckpt)


def test_reset_window_is_rejected(tmp_path):
    """reset() clears the context window but keeps raw rows; assembly would
    serve an empty history while the raw-row comparison passes."""
    prov = ScriptedProvider('mock-a', ['noted'])
    seeded = _engine(prov, MODEL_A, state_dir=tmp_path, agent_id='ipms-reset')
    if not seeded.has_lossless_store:
        pytest.skip('lossless context store unavailable in this environment')
    _run(seeded.submit_and_collect('the fact is X1'))
    ckpt = create_checkpoint_from_engine(seeded, state_dir=tmp_path)
    seeded.reset()

    engine_b = _engine(ScriptedProvider('mock-b', []), MODEL_B,
                       state_dir=tmp_path, agent_id='ipms-reset')
    with pytest.raises(RuntimeError, match='reset or compacted'):
        apply_checkpoint_to_engine(engine_b, ckpt)


def test_failed_guard_leaves_engine_unmutated(tmp_path):
    dirty = _engine(ScriptedProvider('mock-b', ['ok', 'ok']), MODEL_B,
                    state_dir=tmp_path, agent_id='ipms-clean-fail')
    if not dirty.has_lossless_store:
        pytest.skip('lossless context store unavailable in this environment')
    _run(dirty.submit_and_collect('unrelated one'))
    _run(dirty.submit_and_collect('unrelated two'))

    ckpt = create_checkpoint(
        state_dir=tmp_path,
        messages=[Message(role='user', content='alpha'),
                  Message(role='assistant', content='beta')],
        system_prompt='CHECKPOINT-PROMPT',
        source_provider='mock-a', source_model='model-a',
    )
    engine_b = _engine(ScriptedProvider('mock-b', []), MODEL_B,
                       state_dir=tmp_path, agent_id='ipms-clean-fail',
                       system_prompt='(pre-apply prompt)')
    with pytest.raises(RuntimeError, match='divergent history'):
        apply_checkpoint_to_engine(engine_b, ckpt)
    assert engine_b.system_prompt == '(pre-apply prompt)'
    assert engine_b.messages == []
    assert not hasattr(engine_b, 'resume_checkpoint')


def test_scaffold_mismatches_detected(tmp_path):
    source = _engine(ScriptedProvider('mock-a', []), MODEL_A, max_tokens=1024)
    source.tools = []
    source.messages = [Message(role='user', content='hi')]
    ckpt = create_checkpoint_from_engine(source, state_dir=tmp_path)
    assert ckpt['source']['max_tokens'] == 1024
    assert ckpt['source']['tools'] == []
    assert ckpt['source']['thinking_level'] == 'off'

    target = _engine(ScriptedProvider('mock-b', []), MODEL_B,
                     max_tokens=2048, thinking_level='high')
    target.tools = [{'name': 'Bash'}]
    apply_checkpoint_to_engine(target, ckpt)
    mismatches = target.resume_checkpoint['scaffold_mismatches']
    assert any('max_tokens' in m for m in mismatches)
    assert any('thinking_level' in m for m in mismatches)
    assert any('tools' in m for m in mismatches)

    matched = _engine(ScriptedProvider('mock-b', []), MODEL_B, max_tokens=1024)
    matched.tools = []
    apply_checkpoint_to_engine(matched, ckpt)
    assert matched.resume_checkpoint['scaffold_mismatches'] == []


def test_resume_disables_auto_compact(tmp_path):
    ckpt = create_checkpoint(
        state_dir=tmp_path,
        messages=[Message(role='user', content='hi')],
        system_prompt=SYSTEM,
        source_provider='mock-a', source_model='model-a',
    )
    engine = ConversationEngine(ScriptedProvider('mock-b', []), MODEL_B,
                                system_prompt=SYSTEM, thinking_level='off')
    engine.tools = []
    assert engine.auto_compact is True
    apply_checkpoint_to_engine(engine, ckpt)
    assert engine.auto_compact is False


def test_thinking_never_reaches_wire_after_store_seeded_resume(tmp_path):
    ckpt = create_checkpoint(
        state_dir=tmp_path,
        messages=[
            Message(role='user', content='check ci'),
            Message(role='assistant', content='done',
                    thinking='private chain of thought'),
        ],
        system_prompt=SYSTEM,
        source_provider='mock-a', source_model='model-a',
    )
    prov = ScriptedProvider('mock-b', ['reply'])
    engine = _engine(prov, MODEL_B, state_dir=tmp_path, agent_id='ipms-think')
    if not engine.has_lossless_store:
        pytest.skip('lossless context store unavailable in this environment')
    apply_checkpoint_to_engine(engine, ckpt)
    _run(engine.submit_and_collect('probe'))
    assert all(t == '' for t in prov.calls[0]['thinking'])
