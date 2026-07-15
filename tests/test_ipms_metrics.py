"""IPMS battery, metrics, and distance components."""
import asyncio
import json

from charon.ipms import Backbone, run_pair
from charon.ipms.battery import (
    DECISIONS,
    FACTS,
    PERSONA_ITEMS,
    build_spec,
    extract_recorded_decisions,
    parse_decision,
    parse_score,
)
from charon.ipms.distance import judge_identity_pair, pairwise_distance
from charon.ipms.metrics import (
    bootstrap_summary,
    invariance_score,
    is_raw,
    probe_scores,
    submetrics,
)
from charon.providers import ModelInfo, StreamDelta

import pytest


# ── battery ──────────────────────────────────────────────────────────────────

def test_build_spec_structure():
    spec = build_spec()
    assert len(spec.turns) == 2 + len(DECISIONS)
    kinds = {}
    for p in spec.probes:
        kinds[p.kind] = kinds.get(p.kind, 0) + 1
    assert kinds == {'continuity': len(FACTS), 'decision': len(DECISIONS),
                     'persona': len(PERSONA_ITEMS)}
    for p in spec.probes:
        if p.kind == 'continuity':
            assert p.expected
            # anti-gaming: the answer never appears in the probe text
            assert p.expected.lower() not in p.text.lower()
        if p.kind == 'persona':
            assert p.pre_text and p.pre_text != p.text
        if p.kind == 'decision':
            assert 'DECISION:' in p.text


def test_parsers():
    assert parse_decision('DECISION: A — RATIONALE: safer.') == 'A'
    assert parse_decision('decision: b — rationale: speed.') == 'B'
    assert parse_decision('I would go with option A.') is None
    assert parse_score('SCORE: 6') == 6
    assert parse_score('SCORE: 9') is None
    assert parse_score('six out of seven') is None


def test_extract_recorded_decisions():
    transcript = [
        {'user': f'{DECISIONS[0]["scenario"]} fmt', 'assistant': 'DECISION: B — RATIONALE: soak.'},
        {'user': f'{DECISIONS[1]["scenario"]} fmt', 'assistant': 'no format here'},
    ]
    recorded = extract_recorded_decisions(transcript)
    assert recorded == {DECISIONS[0]['id']: 'B'}


# ── metrics on a synthetic record ────────────────────────────────────────────

def _cond(responses):
    return {'condition': 'x', 'prefix_model': 'a', 'suffix_model': 'b',
            'probe_responses': responses, 'usage': {}, 'error': ''}


def _record():
    transcript = [
        {'user': f'{DECISIONS[0]["scenario"]} fmt',
         'assistant': 'DECISION: A — RATIONALE: reversible.'},
        {'user': f'{DECISIONS[1]["scenario"]} fmt',
         'assistant': 'DECISION: B — RATIONALE: weekend.'},
    ]
    pre = [
        {'probe_id': 'p1', 'kind': 'persona', 'response': 'SCORE: 6'},
        {'probe_id': 'p2', 'kind': 'persona', 'response': 'SCORE: 2'},
    ]
    good = _cond([
        {'probe_id': 'f1', 'kind': 'continuity', 'expected': 'cobalt', 'response': 'cobalt'},
        {'probe_id': 'f2', 'kind': 'continuity', 'expected': '6442', 'response': 'port 6442.'},
        {'probe_id': DECISIONS[0]['id'], 'kind': 'decision', 'response': 'DECISION: A — RATIONALE: same.'},
        {'probe_id': DECISIONS[1]['id'], 'kind': 'decision', 'response': 'DECISION: B — RATIONALE: same.'},
        {'probe_id': 'p1', 'kind': 'persona', 'response': 'SCORE: 6'},
        {'probe_id': 'p2', 'kind': 'persona', 'response': 'SCORE: 2'},
    ])
    drifted = _cond([
        {'probe_id': 'f1', 'kind': 'continuity', 'expected': 'cobalt', 'response': 'no idea'},
        {'probe_id': 'f2', 'kind': 'continuity', 'expected': '6442', 'response': '6442'},
        {'probe_id': DECISIONS[0]['id'], 'kind': 'decision', 'response': 'DECISION: B — RATIONALE: flipped.'},
        {'probe_id': DECISIONS[1]['id'], 'kind': 'decision', 'response': 'DECISION: B — RATIONALE: same.'},
        {'probe_id': 'p1', 'kind': 'persona', 'response': 'SCORE: 3'},   # |6-3|/6 -> 0.5
        {'probe_id': 'p2', 'kind': 'persona', 'response': 'SCORE: 2'},   # 1.0
    ])
    floor = _cond([
        {'probe_id': 'f1', 'kind': 'continuity', 'expected': 'cobalt', 'response': 'unknown'},
        {'probe_id': 'f2', 'kind': 'continuity', 'expected': '6442', 'response': 'unknown'},
        {'probe_id': DECISIONS[0]['id'], 'kind': 'decision', 'response': 'DECISION: B — RATIONALE: guess.'},
        {'probe_id': DECISIONS[1]['id'], 'kind': 'decision', 'response': 'DECISION: A — RATIONALE: guess.'},
        {'probe_id': 'p1', 'kind': 'persona', 'response': 'SCORE: 1'},   # |6-1|/6
        {'probe_id': 'p2', 'kind': 'persona', 'response': 'SCORE: 5'},   # |2-5|/6
    ])
    return {
        'transcript': transcript,
        'pre_responses': pre,
        'conditions': {'swap-same': good, 'swap-diff': drifted, 'memory-off': floor},
    }


def test_submetrics_math():
    r = _record()
    good = submetrics(r, 'swap-same')
    assert good['C'] == 1.0 and good['DC'] == 1.0 and good['Cons'] == 1.0
    drift = submetrics(r, 'swap-diff')
    assert drift['C'] == 0.5
    assert drift['DC'] == 0.5
    assert drift['Cons'] == pytest.approx(0.75)
    assert drift['n_C'] == 2 and drift['n_C_total'] == 2


def test_unscoreable_probes_excluded_not_zeroed():
    r = _record()
    r['conditions']['swap-diff']['probe_responses'][2]['response'] = 'no format'
    drift = submetrics(r, 'swap-diff')
    assert drift['n_DC'] == 1 and drift['n_DC_total'] == 2
    assert drift['DC'] == 1.0  # the one scoreable probe matched


def test_invariance_score_normalization():
    r = _record()
    raw_c = is_raw(r, 'swap-same')
    raw_t = is_raw(r, 'swap-diff')
    raw_f = is_raw(r, 'memory-off')
    assert raw_c == pytest.approx(1.0)
    assert raw_t == pytest.approx((0.5 + 0.5 + 0.75) / 3)
    inv = invariance_score(r)
    expected = (raw_t - raw_f) / (raw_c - raw_f)
    assert inv['IS'] == pytest.approx(expected)
    assert inv['degenerate'] == ''


def test_invariance_degenerate_cases():
    r = _record()
    r['conditions']['memory-off'] = json.loads(
        json.dumps(r['conditions']['swap-same']))
    inv = invariance_score(r)
    assert inv['IS'] is None and inv['degenerate'] == 'ceiling == floor'


def test_bootstrap_summary_shapes():
    r = _record()
    out = bootstrap_summary(r, n_boot=200, seed=3)
    assert set(out['conditions']) == {'swap-same', 'swap-diff', 'memory-off'}
    ci = out['conditions']['swap-diff']['ci']['C']
    assert ci is not None and 0.0 <= ci[0] <= ci[1] <= 1.0
    inv = out['invariance']
    assert inv['IS'] is not None
    assert inv['ci'] is not None and inv['ci'][0] <= inv['IS'] + 1e-9


def test_probe_scores_error_marked_unscoreable():
    r = _record()
    r['conditions']['swap-diff']['probe_responses'][0]['error'] = 'boom'
    scores = probe_scores(r, 'swap-diff')
    assert scores['f1']['score'] is None


# ── end-to-end: harness + battery + metrics with a history-aware fake ────────

class HistoryAwareProvider:
    """Answers correctly only when the answer is actually in visible state."""

    def __init__(self, name):
        self.name = name

    async def stream(self, messages, model, system_prompt, tools=None,
                     thinking_level='off', max_tokens=16384):
        last = str(messages[-1].content)
        haystack = system_prompt + ' '.join(str(m.content) for m in messages[:-1])
        if 'DECISION:' in last:
            text = 'DECISION: A — RATIONALE: reversible wins.'
        elif 'SCORE:' in last:
            text = 'SCORE: 4'
        elif 'briefing' in last:
            text = 'Acknowledged.'
        else:  # continuity probe: recall only from history
            answer = 'unknown'
            for f in FACTS:
                if f['ask'].split('?')[0] in last:
                    if f['value'].lower() in haystack.lower():
                        answer = f['value']
                    break
            text = answer
        yield StreamDelta(type='text', text=text)
        yield StreamDelta(type='done', text=json.dumps({
            'usage': {'input_tokens': 1, 'output_tokens': 1, 'total_tokens': 2},
            'stop_reason': 'end_turn'}))


def test_full_pipeline_with_history_aware_fake(tmp_path):
    spec = build_spec('pipeline-test')
    a = Backbone(HistoryAwareProvider('a'), ModelInfo(provider='mock-a', model_id='model-a'))
    b = Backbone(HistoryAwareProvider('b'), ModelInfo(provider='mock-b', model_id='model-b'))
    pair = run_pair(a, b, spec, run_dir=tmp_path)

    record_path = next(tmp_path.glob('pair-pipeline-test-*.json'))
    record = json.loads(record_path.read_text())

    # pre-pass ran for every persona probe
    assert len(record['pre_responses']) == len(PERSONA_ITEMS)

    out = bootstrap_summary(record, n_boot=100, seed=7)
    # history-carrying conditions recall all facts; the floor recalls none
    for cond in ('no-swap', 'swap-same', 'swap-diff', 'scaffold-off'):
        assert out['conditions'][cond]['C'] == 1.0, cond
    assert out['conditions']['memory-off']['C'] == 0.0
    # deterministic fake: decisions and persona are stable everywhere
    for cond in out['conditions'].values():
        assert cond['DC'] == 1.0 and cond['Cons'] == 1.0
    inv = out['invariance']
    assert inv['IS'] == 1.0  # treatment == ceiling with this fake
    assert pair.conditions['memory-off'].error == ''


# ── distance: pairwise identity judge ────────────────────────────────────────

class FakeJudgeEngine:
    def __init__(self, verdicts):
        self._verdicts = verdicts
        self.prompts = []

    async def submit_and_collect(self, prompt):
        self.prompts.append(prompt)
        return json.dumps({'winner': self._verdicts.pop(0), 'reason': 'r'}), []


def _judge(verdicts):
    engines = []

    def factory():
        e = FakeJudgeEngine(verdicts)
        engines.append(e)
        return e
    return factory


def test_judge_identity_pair_order_swap():
    # Pass 1: B wins (post). Pass 2 (flipped): A wins (post again) -> decisive.
    verdicts = ['B', 'A']
    result = asyncio.run(judge_identity_pair(
        'ref', 'probe', 'pre-resp', 'post-resp', engine_factory=_judge(verdicts)))
    assert result == {'winner': 'post', 'pre_wins': 0, 'post_wins': 2,
                      'reasons': ['r', 'r']}
    assert pairwise_distance(result) == 1.0


def test_judge_split_collapses_to_tie_but_counts_survive():
    # Pass 1: A (pre). Pass 2 flipped: A (post) -> 1-1 split.
    verdicts = ['A', 'A']
    result = asyncio.run(judge_identity_pair(
        'ref', 'probe', 'x', 'y', engine_factory=_judge(verdicts)))
    assert result['winner'] == 'tie'
    assert result['pre_wins'] == 1 and result['post_wins'] == 1
    assert pairwise_distance(result) == 0.0


def test_judge_no_json_raises():
    class BadEngine:
        async def submit_and_collect(self, prompt):
            return 'no json here', []

    with pytest.raises(RuntimeError, match='no JSON'):
        asyncio.run(judge_identity_pair(
            'ref', 'probe', 'x', 'y', engine_factory=lambda: BadEngine(), swap=False))
