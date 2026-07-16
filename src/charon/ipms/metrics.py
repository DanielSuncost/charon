"""IPMS metrics v0: Continuity, Decision-consistency, Consistency, and the
normalized Invariance Score with paired bootstrap CIs.

All functions score the persisted pair record (the dict written by
harness._persist_pair), so saved runs can be re-scored offline.

Per-probe scores are in [0, 1] or None (unscoreable — format noncompliance,
missing pre response, provider error). Unscoreable probes are excluded from
means; their counts are reported so silence never masquerades as coverage.

- Continuity  C    = mean(fact recalled) over continuity probes
- Decision    DC   = mean(re-decision == recorded choice) over decision probes
- Consistency Cons = mean(1 - |score_post - score_pre| / 6) over persona
                     paraphrase pairs (pre from the prefix model at the
                     checkpoint, post from the condition's engine)

IS_raw(condition) = weighted mean of [C, Cons, DC]
IS = (IS_raw(treatment) - IS_raw(floor)) / (IS_raw(ceiling) - IS_raw(floor)),
clipped to [0, 1]; ceiling = swap-same, floor = memory-off (design doc §4).

Statistics split (benchcommons 0.1.0): the per-condition C/Cons/DC grids and
their paired bootstrap CIs come from benchcommons.stats.switch_matrix when
the package is installed (it consumes paired_probe_table() output and ports
these exact semantics — see charon-research/docs/benchmark-commons-design.md
§3.6). The IS composite (weighted mean over shared sub-metrics, normalized
against ceiling/floor) is IPMS-specific by design, so its joint-resample
bootstrap stays here. The local per-metric resampler remains only as a
fallback for environments without benchcommons (it is not yet public);
point estimates are identical on both paths.
"""
from __future__ import annotations

import random
from typing import Any

from charon.ipms.battery import (
    extract_recorded_decisions,
    fact_recalled,
    parse_decision,
    parse_score,
)

try:
    from benchcommons.stats import switch_matrix as _bc_switch_matrix
except ImportError:  # benchcommons is optional until it goes public
    _bc_switch_matrix = None

DEFAULT_WEIGHTS = {'C': 1 / 3, 'Cons': 1 / 3, 'DC': 1 / 3}
KIND_TO_METRIC = {'continuity': 'C', 'decision': 'DC', 'persona': 'Cons'}


def _base_id(probe_id: str) -> str:
    """Strip the '#k' suffix bootstrap resampling appends to duplicates."""
    return probe_id.split('#', 1)[0]


def _pre_scores(record: dict[str, Any]) -> dict[str, int | None]:
    return {
        _base_id(r['probe_id']): parse_score(r.get('response', ''))
        for r in record.get('pre_responses', [])
    }


def probe_scores(record: dict[str, Any], condition: str) -> dict[str, dict[str, Any]]:
    """Per-probe scores for one condition: {probe_id: {kind, score}}."""
    cond = record['conditions'][condition]
    recorded = extract_recorded_decisions(record.get('transcript', []))
    pre = _pre_scores(record)

    out: dict[str, dict[str, Any]] = {}
    for resp in cond['probe_responses']:
        pid = resp['probe_id']
        base = _base_id(pid)
        kind = resp['kind']
        text = resp.get('response', '')
        score: float | None = None
        if resp.get('error'):
            score = None
        elif kind == 'continuity':
            exp = resp.get('expected')
            score = float(fact_recalled(text, exp)) if exp else None
        elif kind == 'decision':
            rec = recorded.get(base)
            post = parse_decision(text)
            score = float(rec == post) if rec and post else None
        elif kind == 'persona':
            pre_score = pre.get(base)
            post_score = parse_score(text)
            if pre_score is not None and post_score is not None:
                score = 1.0 - abs(post_score - pre_score) / 6.0
        out[pid] = {'kind': kind, 'score': score}
    return out


def submetrics(record: dict[str, Any], condition: str) -> dict[str, Any]:
    """C / Cons / DC for one condition, with scoreable counts."""
    scores = probe_scores(record, condition)
    out: dict[str, Any] = {}
    for kind, metric in KIND_TO_METRIC.items():
        vals = [s['score'] for s in scores.values()
                if s['kind'] == kind and s['score'] is not None]
        total = sum(1 for s in scores.values() if s['kind'] == kind)
        out[metric] = sum(vals) / len(vals) if vals else None
        out[f'n_{metric}'] = len(vals)
        out[f'n_{metric}_total'] = total
    return out


def is_raw(record: dict[str, Any], condition: str,
           weights: dict[str, float] | None = None) -> float | None:
    """Weighted mean of the available sub-metrics for one condition."""
    weights = weights or DEFAULT_WEIGHTS
    sub = submetrics(record, condition)
    acc = 0.0
    wsum = 0.0
    for metric, w in weights.items():
        v = sub.get(metric)
        if v is not None:
            acc += w * v
            wsum += w
    return acc / wsum if wsum > 0 else None


def invariance_score(
    record: dict[str, Any],
    *,
    treatment: str = 'swap-diff',
    ceiling: str = 'swap-same',
    floor: str = 'memory-off',
    weights: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Normalized IS for one pair record (None when degenerate).

    The three raws entering the normalization use only sub-metrics that are
    scoreable in ALL of treatment/ceiling/floor — otherwise a metric dropping
    out on one side (e.g. format noncompliance under one condition) would
    silently compare different constructs across the ratio.
    """
    weights = weights or DEFAULT_WEIGHTS
    subs = {c: submetrics(record, c) for c in (treatment, ceiling, floor)}
    shared = [m for m in weights
              if all(subs[c].get(m) is not None for c in subs)]

    def wmean(cond: str) -> float:
        wsum = sum(weights[m] for m in shared)
        return sum(weights[m] * subs[cond][m] for m in shared) / wsum

    is_norm: float | None = None
    degenerate = ''
    raw_t = raw_c = raw_f = None
    if not shared:
        degenerate = 'missing sub-metrics'
    else:
        raw_t, raw_c, raw_f = wmean(treatment), wmean(ceiling), wmean(floor)
        if abs(raw_c - raw_f) < 1e-9:
            degenerate = 'ceiling == floor'
        else:
            is_norm = max(0.0, min(1.0, (raw_t - raw_f) / (raw_c - raw_f)))
    return {
        'IS': is_norm,
        'IS_raw_treatment': raw_t,
        'IS_raw_ceiling': raw_c,
        'IS_raw_floor': raw_f,
        'metrics_used': shared,
        'degenerate': degenerate,
    }


def paired_probe_table(record: dict[str, Any]) -> dict[str, dict[str, float | None]]:
    """Per-probe paired scores across all conditions: {probe_id: {condition: score}}.

    This is the exchange format for benchcommons' switch_matrix /
    paired_bootstrap_ci once 0.1.0 lands: one row per probe, one column per
    condition, None where unscoreable.
    """
    table: dict[str, dict[str, float | None]] = {}
    for condition in record['conditions']:
        for pid, s in probe_scores(record, condition).items():
            table.setdefault(pid, {})[condition] = s['score']
    return table


def _resample_record(record: dict[str, Any], rng: random.Random) -> dict[str, Any]:
    """Resample probes WITH replacement, paired across all conditions.

    The same resampled probe multiset applies to the pre-pass, every
    condition, and the transcript-derived recorded decisions, preserving the
    pairing structure the CIs rely on.
    """
    any_cond = next(iter(record['conditions'].values()))
    probe_ids = [r['probe_id'] for r in any_cond['probe_responses']]
    sampled = [rng.choice(probe_ids) for _ in probe_ids]
    # Duplicated draws get unique synthetic ids ('pid#k') so downstream
    # scoring keeps the MULTISET; collapsing duplicates would shrink every
    # CI by ~20-30% (the resample would cover only ~63% unique probes).
    sampled_unique = [
        pid if k == 0 else f'{pid}#{k}'
        for k, pid in ((sampled[:i].count(p), p) for i, p in enumerate(sampled))
    ]

    def take(responses: list[dict[str, Any]]) -> list[dict[str, Any]]:
        by_id = {r['probe_id']: r for r in responses}
        return [
            {**by_id[_base_id(uid)], 'probe_id': uid}
            for uid in sampled_unique if _base_id(uid) in by_id
        ]

    resampled = dict(record)
    resampled['pre_responses'] = take(record.get('pre_responses', []))
    resampled['conditions'] = {
        name: {**cond, 'probe_responses': take(cond['probe_responses'])}
        for name, cond in record['conditions'].items()
    }
    return resampled


def bootstrap_summary(
    record: dict[str, Any],
    *,
    n_boot: int = 2000,
    seed: int = 17,
    weights: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Point estimates + paired bootstrap 95% CIs for every condition's
    sub-metrics and for IS.

    C/Cons/DC grids come from benchcommons.stats.switch_matrix when
    installed; the IS/IS_raw composites always use the local joint-resample
    loop (IPMS-specific — see the module docstring).
    """
    rng = random.Random(seed)
    conditions = list(record['conditions'])

    point: dict[str, Any] = {
        cond: submetrics(record, cond) for cond in conditions
    }
    for cond in conditions:
        point[cond]['IS_raw'] = is_raw(record, cond, weights)
    is_point = invariance_score(record, weights=weights)

    # Per-kind CIs via the shared stats package when available.
    metric_ci: dict[str, dict[str, list[float] | None]] = {
        cond: {} for cond in conditions
    }
    use_benchcommons = _bc_switch_matrix is not None
    if use_benchcommons:
        kind_of: dict[str, str] = {}
        for cond in conditions:
            for pid, s in probe_scores(record, cond).items():
                kind_of[pid] = s['kind']
        table = paired_probe_table(record)
        for kind, metric in KIND_TO_METRIC.items():
            kind_table = {pid: row for pid, row in table.items()
                          if kind_of.get(pid) == kind}
            if not kind_table:
                for cond in conditions:
                    metric_ci[cond][metric] = None
                continue
            grid = _bc_switch_matrix(kind_table, conditions=conditions,
                                     n_boot=n_boot, seed=seed)
            for cond in conditions:
                cell = grid.cells[cond]
                metric_ci[cond][metric] = (
                    [cell.lo, cell.hi] if cell.lo is not None else None)

    # Local joint-resample loop: always for IS/IS_raw, and for C/Cons/DC
    # only when benchcommons is absent.
    samples: dict[str, dict[str, list[float]]] = {
        cond: {'C': [], 'Cons': [], 'DC': [], 'IS_raw': []}
        for cond in conditions
    }
    is_samples: list[float] = []
    for _ in range(n_boot):
        rs = _resample_record(record, rng)
        for cond in conditions:
            if not use_benchcommons:
                sub = submetrics(rs, cond)
                for metric in ('C', 'Cons', 'DC'):
                    if sub[metric] is not None:
                        samples[cond][metric].append(sub[metric])
            raw = is_raw(rs, cond, weights)
            if raw is not None:
                samples[cond]['IS_raw'].append(raw)
        boot_is = invariance_score(rs, weights=weights)['IS']
        if boot_is is not None:
            is_samples.append(boot_is)

    def ci(vals: list[float]) -> list[float] | None:
        if len(vals) < 50:
            return None
        ordered = sorted(vals)
        lo = ordered[int(0.025 * (len(ordered) - 1))]
        hi = ordered[int(0.975 * (len(ordered) - 1))]
        return [lo, hi]

    if not use_benchcommons:
        for cond in conditions:
            for metric in ('C', 'Cons', 'DC'):
                metric_ci[cond][metric] = ci(samples[cond][metric])

    out: dict[str, Any] = {'conditions': {}, 'n_boot': n_boot, 'seed': seed,
                           'stats_backend': 'benchcommons' if use_benchcommons
                           else 'interim-local'}
    for cond in conditions:
        out['conditions'][cond] = {
            **point[cond],
            'ci': {**metric_ci[cond], 'IS_raw': ci(samples[cond]['IS_raw'])},
        }
    out['invariance'] = {**is_point, 'ci': ci(is_samples),
                         'n_boot_valid': len(is_samples)}
    return out
