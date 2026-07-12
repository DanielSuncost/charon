"""Libris convergence: judge scores are 0-10 but thresholds are 0-1 (normalize),
and weak citation_quality must trigger a revision even when overall looks fine."""
from pathlib import Path


from charon.libris import libris_convergence as conv


def test_norm01_handles_both_scales():
    assert conv._norm01(7.8) == 0.78   # 0-10 judge score -> 0-1
    assert conv._norm01(9) == 0.9
    assert conv._norm01(0.84) == 0.84  # already 0-1 left alone
    assert conv._norm01(0) == 0.0
    assert conv._norm01(None) == 0.0


def _decide(monkeypatch, *, latest_score, citation_quality, issue_count=0,
            checkpoint_count=1, revision_round=0, max_ckp=3,
            improved_over_best=True, best_score=None):
    ls = conv._norm01(latest_score)
    metrics = {
        'checkpoint_count': checkpoint_count,
        'latest_score': ls,
        'prev_score': 0.0,
        'score_delta': ls,
        'best_score': conv._norm01(best_score) if best_score is not None else ls,
        'improved_over_best': improved_over_best,
        'citation_quality': conv._norm01(citation_quality),
        'issue_count': issue_count,
    }
    monkeypatch.setattr(conv, 'get_topic_checkpoint_metrics', lambda *a, **k: metrics)
    topic = {'slug': 't', 'revision_round': revision_round,
             'budget': {'max_checkpoints_per_topic': max_ckp}}
    return conv.should_request_additional_revision(Path('/x'), Path('/y'), 'op', topic)


def test_weak_citations_trigger_revision_despite_good_overall(monkeypatch):
    # overall 8.4/10 (good) but citation_quality 6/10 (weak) -> must revise
    r = _decide(monkeypatch, latest_score=8.4, citation_quality=6.0)
    assert r['should_revise'] is True
    assert 'weak_citations' in r['reasons']


def test_strong_citations_and_score_converge(monkeypatch):
    # overall 8.8/10 and citation_quality 9/10 -> good enough, no revision
    r = _decide(monkeypatch, latest_score=8.8, citation_quality=9.0)
    assert r['should_revise'] is False
    assert 'quality_good_enough' in r['reasons']


def test_low_overall_score_triggers_revision(monkeypatch):
    # 7.0/10 normalizes to 0.70 < 0.78 -> serious deficit (was masked pre-fix,
    # since 7.0 >= 0.84 read as good enough on the un-normalized scale)
    r = _decide(monkeypatch, latest_score=7.0, citation_quality=8.5)
    assert r['should_revise'] is True
    assert 'serious_deficits' in r['reasons']


def test_no_improvement_stops_revision_despite_weak_citations(monkeypatch):
    # A revision round ran (checkpoint 2) but did NOT beat the best; even though
    # citations are still weak, stop — this is the hill-climb guard that prevents
    # chasing a subscore into a regression / wasted rounds.
    r = _decide(monkeypatch, latest_score=7.3, citation_quality=6.5,
                checkpoint_count=2, improved_over_best=False, best_score=8.1)
    assert r['should_revise'] is False
    assert 'no_improvement' in r['reasons']


def test_improving_round_keeps_revising(monkeypatch):
    # Latest round set a new best and citations are still weak -> keep revising.
    r = _decide(monkeypatch, latest_score=8.1, citation_quality=7.6,
                checkpoint_count=2, improved_over_best=True, best_score=8.1)
    assert r['should_revise'] is True


def test_regressed_round_is_flagged(monkeypatch):
    r = _decide(monkeypatch, latest_score=7.3, citation_quality=6.5,
                checkpoint_count=3, improved_over_best=False, best_score=8.1)
    assert 'regressed' in r['reasons'] and r['should_revise'] is False


def test_metrics_compute_best_and_improvement(tmp_path, monkeypatch):
    # get_topic_checkpoint_metrics should surface best_score / improved_over_best
    # from the real checkpoint list.
    ckpts = [{'score': 7.5, 'iteration': 1, 'checkpoint_id': 'ckp_001', 'metrics': {}, 'critique_path': ''},
             {'score': 8.1, 'iteration': 2, 'checkpoint_id': 'ckp_002', 'metrics': {}, 'critique_path': ''},
             {'score': 7.3, 'iteration': 3, 'checkpoint_id': 'ckp_003', 'metrics': {}, 'critique_path': ''}]
    monkeypatch.setattr(conv, 'list_checkpoints', lambda *a, **k: ckpts, raising=False)
    import charon.libris.libris_runtime as lrmod
    monkeypatch.setattr(lrmod, 'list_checkpoints', lambda *a, **k: ckpts)
    m = conv.get_topic_checkpoint_metrics(tmp_path, tmp_path, 'op', 't')
    assert m['best_score'] == 0.81            # 8.1 normalized
    assert m['latest_score'] == 0.73          # regressed round
    assert m['improved_over_best'] is False   # 0.73 did not beat prev best 0.81
    assert m['best_checkpoint_id'] == 'ckp_002'
