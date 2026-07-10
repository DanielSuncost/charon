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
            checkpoint_count=1, revision_round=0, max_ckp=3):
    metrics = {
        'checkpoint_count': checkpoint_count,
        'latest_score': conv._norm01(latest_score),
        'prev_score': 0.0,
        'score_delta': conv._norm01(latest_score),
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
