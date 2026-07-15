"""IPMS conditions harness: each condition must present exactly the right
(model, system prompt, history) tuple to the probe engine, with probes
isolated from one another and all conditions paired on one checkpoint.
"""
import json

from charon.ipms import Backbone, Probe, TrajectorySpec, run_pair
from charon.providers import ModelInfo, StreamDelta

import pytest


class ScriptedProvider:
    """Provider-protocol fake: replays scripted text, records what it saw."""

    def __init__(self, name: str, responses: list[str] | None = None):
        self.name = name
        self._responses = list(responses or [])
        self.calls: list[dict] = []

    async def stream(self, messages, model, system_prompt, tools=None,
                     thinking_level='off', max_tokens=16384):
        self.calls.append({
            'messages': [(m.role, m.content) for m in messages],
            'system_prompt': system_prompt,
            'model_id': model.model_id,
        })
        text = self._responses.pop(0) if self._responses else f'({self.name} reply)'
        yield StreamDelta(type='text', text=text)
        yield StreamDelta(type='done', text=json.dumps({
            'usage': {'input_tokens': 3, 'output_tokens': 2, 'total_tokens': 5},
            'stop_reason': 'end_turn',
        }))


SYSTEM = 'You are Nyx, a release engineer. Charter: never ship on Fridays.'
STRIPPED = 'You are an assistant.'


def _spec():
    return TrajectorySpec(
        id='t1',
        system_prompt=SYSTEM,
        stripped_system_prompt=STRIPPED,
        turns=['Decision: we ship canary first. Reason: blast radius.',
               'Also remember: the release codeword is opal-9.'],
        probes=[
            Probe(id='p1', kind='continuity', text='What is the release codeword?'),
            Probe(id='p2', kind='decision', text='Do we ship canary first?'),
        ],
    )


def _backbones():
    prov_a = ScriptedProvider('mock-a')
    prov_b = ScriptedProvider('mock-b')
    a = Backbone(prov_a, ModelInfo(provider='mock-a', model_id='model-a'))
    b = Backbone(prov_b, ModelInfo(provider='mock-b', model_id='model-b'))
    return a, b, prov_a, prov_b


def test_run_pair_produces_all_conditions(tmp_path):
    a, b, _, _ = _backbones()
    pair = run_pair(a, b, _spec(), run_dir=tmp_path)
    assert set(pair.conditions) == {
        'no-swap', 'swap-same', 'swap-diff', 'memory-off', 'scaffold-off'}
    assert len(pair.transcript) == 2
    assert pair.checkpoint_id.startswith('ckpt-')
    for cond in pair.conditions.values():
        assert len(cond.probe_responses) == 2
        assert cond.error == ''
    # Result record persisted for downstream scoring.
    files = list(tmp_path.glob('pair-t1-*.json'))
    assert len(files) == 1
    record = json.loads(files[0].read_text())
    assert record['conditions']['swap-diff']['suffix_model'] == 'mock-b/model-b'


def _probe_calls(provider, model_id):
    """Calls that carry probe text (skip the 2 trajectory turns on the prefix)."""
    return [c for c in provider.calls
            if c['model_id'] == model_id and 'codeword?' in str(c['messages'][-1])]


def test_condition_geometry(tmp_path):
    a, b, prov_a, prov_b = _backbones()
    run_pair(a, b, _spec(), run_dir=tmp_path)

    # Trajectory ran exactly once, on the prefix model.
    traj_calls = [c for c in prov_a.calls if 'canary first' in str(c['messages'][0])]
    assert all(c['model_id'] == 'model-a' for c in traj_calls)
    trajectory_user_turns = {str(c['messages'][-1][1]) for c in prov_a.calls}
    assert 'Also remember: the release codeword is opal-9.' in trajectory_user_turns

    # no-swap + swap-same probe on model A; both see full 4-message history
    # (2 turns x user+assistant) plus the probe, and the intact charter.
    a_probe_calls = _probe_calls(prov_a, 'model-a')
    assert len(a_probe_calls) == 2  # p1 for no-swap + p1 for swap-same
    for call in a_probe_calls:
        assert len(call['messages']) == 5
        assert call['system_prompt'] == SYSTEM
        assert '[CONTEXT TRANSFER]' not in call['system_prompt']

    # Suffix model sees three flavors for probe p1:
    b_probe_calls = _probe_calls(prov_b, 'model-b')
    assert len(b_probe_calls) == 3  # swap-diff, memory-off, scaffold-off
    by_history = {len(c['messages']): c for c in b_probe_calls}
    # memory-off: probe only, charter intact.
    assert by_history[1]['system_prompt'] == SYSTEM
    # swap-diff and scaffold-off: full history; prompts differ.
    full = [c for c in b_probe_calls if len(c['messages']) == 5]
    prompts = sorted(c['system_prompt'] for c in full)
    assert prompts == sorted([SYSTEM, STRIPPED])
    for c in full:
        assert 'opal-9' in str(c['messages'][2][1])  # turn-2 user text present


def test_probe_isolation(tmp_path):
    a, b, prov_a, prov_b = _backbones()
    run_pair(a, b, _spec(), run_dir=tmp_path)
    # No probe engine ever sees another probe's text in its history.
    for prov in (prov_a, prov_b):
        for call in prov.calls:
            history = [str(content) for _, content in call['messages'][:-1]]
            assert not any('codeword?' in h or 'canary first?' in h
                           for h in history)


def test_provider_error_is_recorded_not_swallowed(tmp_path):
    class ErringProvider(ScriptedProvider):
        def __init__(self):
            super().__init__('mock-err')
            self.fail_when = 'codeword?'

        async def stream(self, messages, model, system_prompt, tools=None,
                         thinking_level='off', max_tokens=16384):
            if self.fail_when in str(messages[-1].content):
                yield StreamDelta(type='error', error='boom 500')
                return
            async for d in super().stream(messages, model, system_prompt,
                                          tools, thinking_level, max_tokens):
                yield d

    a = Backbone(ScriptedProvider('mock-a'),
                 ModelInfo(provider='mock-a', model_id='model-a'))
    b = Backbone(ErringProvider(),
                 ModelInfo(provider='mock-b', model_id='model-b'))
    pair = run_pair(a, b, _spec(), run_dir=tmp_path)
    swap_diff = pair.conditions['swap-diff']
    assert 'boom 500' in swap_diff.error
    p1 = next(r for r in swap_diff.probe_responses if r['probe_id'] == 'p1')
    assert p1['response'] == '' and 'boom 500' in p1['error']
    # The other probe still ran.
    p2 = next(r for r in swap_diff.probe_responses if r['probe_id'] == 'p2')
    assert p2['response']


def test_usage_accumulates(tmp_path):
    a, b, _, _ = _backbones()
    pair = run_pair(a, b, _spec(), run_dir=tmp_path)
    for cond in pair.conditions.values():
        assert cond.usage['input_tokens'] == 6   # 2 probes x 3
        assert cond.usage['output_tokens'] == 4  # 2 probes x 2


def test_transient_error_with_recovery_does_not_raise(tmp_path):
    """The engine retries 429/502/503 within one submit; a recovered turn is
    a success even though an 'error' event was emitted."""

    class FlakyProvider(ScriptedProvider):
        def __init__(self):
            super().__init__('mock-flaky')
            self.attempts = 0

        async def stream(self, messages, model, system_prompt, tools=None,
                         thinking_level='off', max_tokens=16384):
            self.attempts += 1
            if self.attempts == 1:
                yield StreamDelta(type='error', error='429 too many requests')
                return
            async for d in super().stream(messages, model, system_prompt,
                                          tools, thinking_level, max_tokens):
                yield d

    a = Backbone(FlakyProvider(), ModelInfo(provider='mock-a', model_id='model-a'))
    b = Backbone(ScriptedProvider('mock-b'), ModelInfo(provider='mock-b', model_id='model-b'))
    pair = run_pair(a, b, _spec(), run_dir=tmp_path)
    assert len(pair.transcript) == 2  # trajectory survived the transient error


def test_pair_result_carries_record_path(tmp_path):
    a, b, _, _ = _backbones()
    pair = run_pair(a, b, _spec(), run_dir=tmp_path)
    from pathlib import Path
    assert Path(pair.record_path).exists()
    assert json.loads(Path(pair.record_path).read_text())['spec_id'] == 't1'


def test_apply_strict_rejects_scaffold_drift(tmp_path):
    from charon.context.context_transfer import create_checkpoint_from_engine
    from charon.ipms.harness import IpmsRunError, _apply_strict, _make_engine

    a, b, _, _ = _backbones()
    source = _make_engine(a, SYSTEM, 1024)
    ckpt = create_checkpoint_from_engine(source, state_dir=tmp_path)
    drifted = _make_engine(b, '(pending)', 2048)
    with pytest.raises(IpmsRunError, match='scaffold drift'):
        _apply_strict(drifted, ckpt)


def test_trajectory_failure_raises(tmp_path):
    class DeadProvider(ScriptedProvider):
        async def stream(self, messages, model, system_prompt, tools=None,
                         thinking_level='off', max_tokens=16384):
            yield StreamDelta(type='error', error='dead on arrival')

    a = Backbone(DeadProvider('mock-a'),
                 ModelInfo(provider='mock-a', model_id='model-a'))
    b = Backbone(ScriptedProvider('mock-b'),
                 ModelInfo(provider='mock-b', model_id='model-b'))
    from charon.ipms.harness import IpmsRunError
    with pytest.raises(IpmsRunError, match='dead on arrival'):
        run_pair(a, b, _spec(), run_dir=tmp_path)
