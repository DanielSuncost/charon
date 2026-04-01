from __future__ import annotations

import re
from pathlib import Path
from typing import Any


def _read_text(path_str: str) -> str:
    try:
        p = Path(str(path_str or ''))
        if p.exists():
            return p.read_text(encoding='utf-8', errors='replace')
    except Exception:
        pass
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



def get_topic_checkpoint_metrics(state_dir: Path, project_root: Path, operation_id: str, topic_slug: str) -> dict[str, Any]:
    from libris_runtime import list_checkpoints

    items = list_checkpoints(state_dir, project_root, operation_id, topic_slug)
    latest = items[-1] if items else {}
    prev = items[-2] if len(items) >= 2 else {}
    latest_score = float(latest.get('score') or 0.0) if latest else 0.0
    prev_score = float(prev.get('score') or 0.0) if prev else 0.0
    score_delta = round(latest_score - prev_score, 4) if latest and prev else latest_score
    critique_md = _read_text(str(latest.get('critique_path') or '')) if latest else ''
    return {
        'checkpoint_count': len(items),
        'latest_score': latest_score,
        'prev_score': prev_score,
        'score_delta': score_delta,
        'latest_checkpoint_id': latest.get('checkpoint_id') or '',
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
        from libris_runtime import get_operation_state
        op = get_operation_state(state_dir, project_root, operation_id)
        max_ckp = int(((op.get('budget_status') or {}).get('budget') or {}).get('max_checkpoints_per_topic') or 0) if op else 0
    if not max_ckp:
        max_ckp = 3

    remaining_checkpoint_slots = max(0, max_ckp - checkpoint_count)
    remaining_revision_capacity = max(0, min(2, max_ckp - 1) - revision_round)

    latest_score = float(metrics.get('latest_score') or 0.0)
    score_delta = float(metrics.get('score_delta') or 0.0)
    issue_count = int(metrics.get('issue_count') or 0)

    enough_quality = latest_score >= 0.84 and issue_count <= 1
    plateau = checkpoint_count >= 2 and score_delta < 0.04
    serious_deficits = issue_count >= 2 or latest_score < 0.78

    should_revise = (
        checkpoint_count >= 1 and
        remaining_checkpoint_slots > 0 and
        remaining_revision_capacity > 0 and
        serious_deficits and
        not enough_quality and
        not plateau
    )

    reasons = []
    if serious_deficits:
        reasons.append('serious_deficits')
    if enough_quality:
        reasons.append('quality_good_enough')
    if plateau:
        reasons.append('score_plateau')
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
