"""Regression-hardening for the Libris revision loop:

1. Incumbent-preferring tie-break — a new round must strictly BEAT the best to
   take over; an equal-scoring rewrite never displaces the proven report.
2. Depth-collapse veto — a later round that sheds a large fraction of its content
   for only a marginal score gain is kept out (a hidden regression the coarse
   judge score misses); a decisive score jump overrides the veto.
3. In-loop pairwise gate — blind head-to-head of the newest checkpoint vs the
   running best demotes a higher-scoring-but-actually-worse round.
"""
import asyncio
from pathlib import Path

import charon.libris.libris_runtime as lr
from charon.libris import libris_pairwise as lp


def _ck(cid, it, score, chars, *, rejected=False):
    return {'checkpoint_id': cid, 'iteration': it, 'score': score,
            'report_chars': chars, 'report_path': f'/nope/{cid}.md',
            'pairwise_rejected': rejected}


def _patch_ckpts(monkeypatch, ckpts):
    monkeypatch.setattr(lr, 'list_checkpoints', lambda *a, **k: ckpts)


# ── Fix 1: incumbent-preferring tie-break ────────────────────────────────────

def test_tie_keeps_incumbent(monkeypatch):
    # Two rounds tie at 7.8; the EARLIER one keeps the crown (this is the exact
    # gut-microbiome case: a shorter round tied the score and was wrongly promoted
    # by recency under the old (score, iteration) sort).
    _patch_ckpts(monkeypatch, [_ck('ckp_002', 2, 7.8, 15000),
                               _ck('ckp_003', 3, 7.8, 8800)])
    best = lr.select_best_checkpoint(Path('/x'), Path('/y'), 'op', 't')
    assert best['checkpoint_id'] == 'ckp_002'


def test_strict_improvement_takes_over(monkeypatch):
    # A round that genuinely beats the best (and keeps its length) wins.
    _patch_ckpts(monkeypatch, [_ck('ckp_001', 1, 7.8, 15000),
                               _ck('ckp_002', 2, 8.3, 15200)])
    best = lr.select_best_checkpoint(Path('/x'), Path('/y'), 'op', 't')
    assert best['checkpoint_id'] == 'ckp_002'


# ── Fix 2: depth-collapse veto ───────────────────────────────────────────────

def test_depth_collapse_marginal_gain_is_vetoed(monkeypatch):
    # Later round nudges the score up (+0.1) but sheds ~45% of the content ->
    # veto: keep the fuller incumbent.
    _patch_ckpts(monkeypatch, [_ck('ckp_002', 2, 7.8, 15000),
                               _ck('ckp_003', 3, 7.9, 8200)])
    best = lr.select_best_checkpoint(Path('/x'), Path('/y'), 'op', 't')
    assert best['checkpoint_id'] == 'ckp_002'


def test_large_score_jump_overrides_collapse(monkeypatch):
    # A decisive score jump (7.8 -> 9.6) overrides the depth-collapse veto even
    # though the report got shorter — we trust a decisive judge.
    _patch_ckpts(monkeypatch, [_ck('ckp_002', 2, 7.8, 15000),
                               _ck('ckp_003', 3, 9.6, 8200)])
    best = lr.select_best_checkpoint(Path('/x'), Path('/y'), 'op', 't')
    assert best['checkpoint_id'] == 'ckp_003'


def test_shorter_but_similar_length_is_not_vetoed(monkeypatch):
    # An 8% length trim with a real score gain is fine (above collapse_ratio).
    _patch_ckpts(monkeypatch, [_ck('ckp_002', 2, 7.8, 16000),
                               _ck('ckp_003', 3, 8.3, 14700)])
    best = lr.select_best_checkpoint(Path('/x'), Path('/y'), 'op', 't')
    assert best['checkpoint_id'] == 'ckp_003'


# ── Fix 3 filter: pairwise-rejected checkpoints are excluded ──────────────────

def test_pairwise_rejected_is_excluded(monkeypatch):
    # ckp_003 scored highest but was demoted by the pairwise gate -> not selected.
    _patch_ckpts(monkeypatch, [_ck('ckp_002', 2, 8.1, 15000),
                               _ck('ckp_003', 3, 8.4, 15100, rejected=True)])
    best = lr.select_best_checkpoint(Path('/x'), Path('/y'), 'op', 't')
    assert best['checkpoint_id'] == 'ckp_002'


def test_all_rejected_falls_back(monkeypatch):
    # Degenerate: if every checkpoint is rejected, still return the best of them.
    _patch_ckpts(monkeypatch, [_ck('ckp_001', 1, 7.5, 15000, rejected=True),
                               _ck('ckp_002', 2, 8.1, 15000, rejected=True)])
    best = lr.select_best_checkpoint(Path('/x'), Path('/y'), 'op', 't')
    assert best['checkpoint_id'] == 'ckp_002'


# ── Fix 3: pairwise judge aggregation + gate ─────────────────────────────────

class _FakeEngine:
    """Always prefers whichever report contains the marker 'BETTER', regardless of
    A/B position — simulates a consistent verdict under order-swapping."""
    async def submit_and_collect(self, prompt: str):
        b_marker = prompt.index('=== REPORT B ===')
        better_at = prompt.find('BETTER')
        winner = 'A' if 0 <= better_at < b_marker else 'B'
        return ('{"winner":"%s","reason":"x"}' % winner, None)


def test_judge_pair_new_wins_both_orderings():
    v = asyncio.run(lp.judge_pair('Q?', 'OLD worse report', 'NEW BETTER report',
                                  engine_factory=_FakeEngine, swap=True))
    assert v['winner'] == 'new' and v['new_wins'] == 2 and v['old_wins'] == 0


def test_judge_pair_old_wins_both_orderings():
    v = asyncio.run(lp.judge_pair('Q?', 'OLD BETTER report', 'NEW worse report',
                                  engine_factory=_FakeEngine, swap=True))
    assert v['winner'] == 'old' and v['old_wins'] == 2


def test_gate_cheap_skips_when_latest_not_selected(monkeypatch):
    # If the deterministic selector already prefers an earlier checkpoint, the gate
    # must NOT spend an LLM call.
    cks = [_ck('ckp_002', 2, 7.8, 15000), _ck('ckp_003', 3, 7.8, 8800)]  # tie -> incumbent
    _patch_ckpts(monkeypatch, cks)
    called = {'judge': False}
    def _boom(*a, **k):
        called['judge'] = True
        raise AssertionError('should not judge')
    monkeypatch.setattr(lp, 'judge_pair', _boom)
    out = asyncio.run(lp.agate_latest_checkpoint(Path('/x'), Path('/y'), 'op', 't',
                                                 question='Q?'))
    assert out['ran'] is False and out['reason'] == 'latest_not_selected'
    assert called['judge'] is False


def test_gate_demotes_latest_when_it_loses(monkeypatch, tmp_path):
    # Latest scored highest (so it WOULD be delivered) but loses the pairwise ->
    # mark rejected + revert draft.
    old = tmp_path / 'old.md'
    old.write_text('OLD BETTER full report')
    new = tmp_path / 'new.md'
    new.write_text('NEW worse thin report')
    cks = [{'checkpoint_id': 'ckp_002', 'iteration': 2, 'score': 8.1, 'report_chars': 15000,
            'report_path': str(old)},
           {'checkpoint_id': 'ckp_003', 'iteration': 3, 'score': 8.4, 'report_chars': 15100,
            'report_path': str(new)}]
    _patch_ckpts(monkeypatch, cks)
    marked = {}
    monkeypatch.setattr(lr, 'mark_checkpoint_pairwise_rejected',
                        lambda *a, **k: marked.update({'id': a[4], 'winner': k.get('winner')}) or True)
    monkeypatch.setattr(lr, 'revert_topic_draft_to_best', lambda *a, **k: True)
    monkeypatch.setattr(lp, 'default_engine_factory', lambda *a, **k: _FakeEngine())
    out = asyncio.run(lp.agate_latest_checkpoint(Path('/x'), Path('/y'), 'op', 't',
                                                 question='Q?'))
    assert out['ran'] is True and out['rejected_latest'] is True
    assert marked['id'] == 'ckp_003' and marked['winner'] == 'old'
