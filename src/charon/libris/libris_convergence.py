from __future__ import annotations

import re
from pathlib import Path
from typing import Any

try:
    from charon.infra.diagnostics import record as _diag
except Exception:  # diagnostics is best-effort and must never block import
    def _diag(*args, **kwargs):
        return None


def _read_text(path_str: str) -> str:
    try:
        p = Path(str(path_str or ''))
        if p.exists():
            return p.read_text(encoding='utf-8', errors='replace')
    except Exception as e:
        _diag('libris_convergence', 'critique text unreadable; convergence sees empty critique', error=e, path=path_str)
    return ''


_SERIOUS_PATTERNS = [
    r'\bmissing\b',
    r'\bweak\b',
    r'\binsufficient\b',
    r'\bunsupported\b',
    r'\bunclear\b',
    r'\bneeds?\b',
    r'\bshould\b',
    r'\brevise\b',
    r'\bcompare\b',
    r'\bcaveat\b',
    r'\buncertaint(?:y|ies)\b',
    r'\breproducib\w*\b',
]


def critique_issue_count(critique_markdown: str) -> int:
    text = str(critique_markdown or '').strip().lower()
    if not text:
        return 0
    count = 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if any(re.search(p, line) for p in _SERIOUS_PATTERNS):
            count += 1
    if count:
        return count
    return sum(1 for p in _SERIOUS_PATTERNS if re.search(p, text))



def _norm01(x: Any) -> float:
    """Coerce a judge score to 0-1. Judges emit 0-10 (e.g. 7.8); the convergence
    thresholds are 0-1. Anything >1 is treated as a 0-10 score and divided by 10.
    (Without this, a 7.8 always read as 'good enough' and revision never fired.)"""
    try:
        v = float(x or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return round(v / 10.0, 4) if v > 1.0 else round(v, 4)


def get_topic_checkpoint_metrics(state_dir: Path, project_root: Path, operation_id: str, topic_slug: str) -> dict[str, Any]:
    from charon.libris.libris_runtime import list_checkpoints

    items = list_checkpoints(state_dir, project_root, operation_id, topic_slug)
    latest = items[-1] if items else {}
    prev = items[-2] if len(items) >= 2 else {}
    latest_score = _norm01(latest.get('score')) if latest else 0.0
    prev_score = _norm01(prev.get('score')) if prev else 0.0
    score_delta = round(latest_score - prev_score, 4) if latest and prev else latest_score
    # Best-relative tracking (the hill-climb signal): compare the latest round to
    # the BEST prior round, not just the immediately previous one. A revision that
    # scores below the running best is a regression whose draft should be
    # discarded (keep-if-better), and a round that fails to set a new best means
    # revision has stopped paying off.
    scores = [_norm01(it.get('score')) for it in items]
    best_score = max(scores) if scores else 0.0
    prev_best = max(scores[:-1]) if len(scores) >= 2 else 0.0
    latest_is_best = bool(items) and latest_score >= best_score
    improved_over_best = len(scores) >= 2 and latest_score > prev_best + 1e-9
    # citation_quality is the dimension our judges most consistently ding; surface
    # it (normalised) so the revision loop can target citations specifically.
    latest_metrics = latest.get('metrics') or {}
    citation_quality = _norm01(latest_metrics.get('citation_quality')) if latest_metrics else 0.0
    critique_md = _read_text(str(latest.get('critique_path') or '')) if latest else ''
    best_ckpt_id = ''
    if items:
        best_item = sorted(items, key=lambda it: (_norm01(it.get('score')), int(it.get('iteration') or 0)),
                           reverse=True)[0]
        best_ckpt_id = best_item.get('checkpoint_id') or ''
    return {
        'checkpoint_count': len(items),
        'latest_score': latest_score,
        'prev_score': prev_score,
        'score_delta': score_delta,
        'best_score': best_score,
        'prev_best': prev_best,
        'latest_is_best': latest_is_best,
        'improved_over_best': improved_over_best,
        'citation_quality': citation_quality,
        'latest_checkpoint_id': latest.get('checkpoint_id') or '',
        'best_checkpoint_id': best_ckpt_id,
        'issue_count': critique_issue_count(critique_md),
    }



def should_request_additional_revision(
    state_dir: Path,
    project_root: Path,
    operation_id: str,
    topic: dict[str, Any],
) -> dict[str, Any]:
    metrics = get_topic_checkpoint_metrics(state_dir, project_root, operation_id, str(topic.get('slug') or ''))
    revision_round = int(topic.get('revision_round') or 0)
    checkpoint_count = int(metrics.get('checkpoint_count') or 0)

    budget = topic.get('budget') or {}
    max_ckp = int(budget.get('max_checkpoints_per_topic') or 0)
    if not max_ckp:
        from charon.libris.libris_runtime import get_operation_state
        op = get_operation_state(state_dir, project_root, operation_id)
        max_ckp = int(((op.get('budget_status') or {}).get('budget') or {}).get('max_checkpoints_per_topic') or 0) if op else 0
    if not max_ckp:
        max_ckp = 3

    remaining_checkpoint_slots = max(0, max_ckp - checkpoint_count)
    remaining_revision_capacity = max(0, min(2, max_ckp - 1) - revision_round)

    latest_score = float(metrics.get('latest_score') or 0.0)
    issue_count = int(metrics.get('issue_count') or 0)
    citation_quality = float(metrics.get('citation_quality') or 0.0)
    best_score = float(metrics.get('best_score') or 0.0)
    improved_over_best = bool(metrics.get('improved_over_best'))

    # Weak citations are a first-class reason to revise even if the overall score
    # is otherwise acceptable — citation quality is the most common judge ding.
    weak_citations = citation_quality > 0.0 and citation_quality < 0.8
    enough_quality = latest_score >= 0.84 and issue_count <= 1 and not weak_citations
    serious_deficits = issue_count >= 2 or latest_score < 0.78 or weak_citations

    # Stop-on-no-improvement (the hill-climb guard): once we have run at least one
    # revision (>=2 checkpoints) and the latest round FAILED to beat the running
    # best, further revision has stopped paying off — halt regardless of any
    # lingering weak-citation signal. This is what prevents chasing a subscore
    # into a regression. The regressed round's draft is discarded by keep-if-best
    # (revert_topic_draft_to_best) in the caller.
    no_improvement = checkpoint_count >= 2 and not improved_over_best
    regressed = checkpoint_count >= 2 and latest_score < best_score - 1e-9

    should_revise = (
        checkpoint_count >= 1 and
        remaining_checkpoint_slots > 0 and
        remaining_revision_capacity > 0 and
        serious_deficits and
        not enough_quality and
        not no_improvement
    )

    reasons = []
    if serious_deficits:
        reasons.append('serious_deficits')
    if weak_citations:
        reasons.append('weak_citations')
    if enough_quality:
        reasons.append('quality_good_enough')
    if regressed:
        reasons.append('regressed')
    if no_improvement:
        reasons.append('no_improvement')
        reasons.append('score_plateau')  # alias: keeps existing status-mapping working
    if remaining_checkpoint_slots <= 0:
        reasons.append('checkpoint_budget_exhausted')
    if remaining_revision_capacity <= 0:
        reasons.append('revision_cap_reached')

    return {
        'should_revise': should_revise,
        'reasons': reasons,
        'metrics': metrics,
        'remaining_checkpoint_slots': remaining_checkpoint_slots,
        'remaining_revision_capacity': remaining_revision_capacity,
    }
