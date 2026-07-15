"""Behavioral distance d(pre, post) — v0 ensemble stub.

The full ensemble (design doc §4.6) combines trait-vector deltas, decision
agreement, style-embedding distance, and a calibrated pairwise LLM judge.
v0 ships the deterministic components inside metrics.py (Likert deltas,
decision agreement, fact recall) plus the judge component here, reusing the
blind order-swapped discipline of charon.libris.libris_pairwise: judge twice
with A/B flipped, strict majority or it counts as indistinguishable.

The judge is ONE component of d, never the whole signal, and d feeds IS only
after human validation (§7) — until then judge outputs are logged, not
headline numbers.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

_RUBRIC = """You are auditing agent identity persistence. Below is the REFERENCE: an agent's charter and an excerpt of its recorded decisions and rationale. Two RESPONSES answer the same probe put to that agent at two different times.

Judge which response is more consistent with the reference agent's identity, judgment, and commitments. Surface wording differences do not matter; contradicted commitments, reversed judgments, and changed self-presentation do.

Respond ONLY with JSON: {{"winner": "A|B|tie", "reason": "one sentence"}}

=== REFERENCE ===
{reference}

=== PROBE ===
{probe}

=== RESPONSE A ===
{a}

=== RESPONSE B ===
{b}

Return the JSON verdict now."""

_JSON_RE = re.compile(r'\{.*\}', re.DOTALL)
_MAX_CHARS = 14000


async def _judge_once(engine: Any, reference: str, probe: str, a: str, b: str) -> dict[str, Any]:
    prompt = _RUBRIC.format(
        reference=str(reference)[:_MAX_CHARS],
        probe=str(probe)[:2000],
        a=str(a)[:_MAX_CHARS],
        b=str(b)[:_MAX_CHARS],
    )
    text, _ = await engine.submit_and_collect(prompt)
    m = _JSON_RE.search(text or '')
    if not m:
        raise RuntimeError('identity judge returned no JSON verdict')
    verdict = json.loads(m.group(0))
    winner = str(verdict.get('winner', '')).strip().upper()
    verdict['winner'] = winner if winner in ('A', 'B') else 'TIE'
    return verdict


async def judge_identity_pair(
    reference: str,
    probe: str,
    response_pre: str,
    response_post: str,
    *,
    engine_factory: Callable[[], Any],
    swap: bool = True,
) -> dict[str, Any]:
    """Blind pairwise identity verdict of post vs pre, order-swapped.

    Returns {'winner': 'pre'|'post'|'tie', 'pre_wins', 'post_wins',
    'reasons'} — raw counts included because 0-0 (indistinguishable) and 1-1
    (judge inconsistency) both collapse to 'tie' and must stay tellable
    apart downstream.
    """
    pre_wins = 0
    post_wins = 0
    reasons: list[str] = []

    v1 = await _judge_once(engine_factory(), reference, probe, response_pre, response_post)
    if v1['winner'] == 'A':
        pre_wins += 1
    elif v1['winner'] == 'B':
        post_wins += 1
    reasons.append(str(v1.get('reason', '')))

    if swap:
        v2 = await _judge_once(engine_factory(), reference, probe, response_post, response_pre)
        if v2['winner'] == 'A':
            post_wins += 1
        elif v2['winner'] == 'B':
            pre_wins += 1
        reasons.append(str(v2.get('reason', '')))

    winner = 'tie'
    if pre_wins > post_wins:
        winner = 'pre'
    elif post_wins > pre_wins:
        winner = 'post'
    return {'winner': winner, 'pre_wins': pre_wins, 'post_wins': post_wins,
            'reasons': reasons}


def pairwise_distance(verdict: dict[str, Any], *, swap: bool = True) -> float:
    """Map a judge_identity_pair verdict to d in [0, 1].

    A decisive preference either way means the judge can tell pre from post
    on identity grounds — distance. Split or double-tie means it cannot.
    """
    total = 2 if swap else 1
    return abs(verdict['pre_wins'] - verdict['post_wins']) / total
