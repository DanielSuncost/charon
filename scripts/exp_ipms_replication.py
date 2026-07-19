#!/usr/bin/env python3
"""IPMS replication run: repeated base trajectories on selected pairs.

The 5x5 matrix produced one CI-significant cell (luna->gpt-5.4, IS 0.78)
out of 20 — consistent with a multiple-comparisons false positive at 95%.
This script re-runs chosen directed pairs with N independent base
trajectories each, giving trajectory-level variance the probe-level
bootstrap cannot see. Per-rep records are saved for offline re-scoring;
the summary reports the per-rep IS distribution and which recorded
decisions flipped under swap-diff (a recurring flip is systematic
disagreement, not sampling noise).
"""
import argparse
import json
import random
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from charon.ipms.battery import BATTERY_VERSION, build_spec  # noqa: E402
from charon.ipms.harness import backbone_from_entry, run_pair  # noqa: E402
from charon.ipms.metrics import (  # noqa: E402
    bootstrap_summary, probe_scores, submetrics,
)
from charon.providers.provider_bridge import create_provider_and_model  # noqa: E402

DEFAULT_PAIRS = 'gpt-5.6-luna>gpt-5.4,gpt-5.6-sol>gpt-5.4,gpt-5.6-sol>gpt-5.6-terra'


def _rep_ci(values: list[float], n_boot: int = 2000, seed: int = 17) -> list[float] | None:
    """Percentile bootstrap CI over per-trajectory IS values."""
    if len(values) < 3:
        return None
    rng = random.Random(seed)
    means = sorted(
        sum(rng.choice(values) for _ in values) / len(values)
        for _ in range(n_boot)
    )
    return [means[int(0.025 * (n_boot - 1))], means[int(0.975 * (n_boot - 1))]]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--pairs', default=DEFAULT_PAIRS,
                    help="comma-separated 'prefix>suffix' model-id pairs")
    ap.add_argument('--reps', type=int, default=10)
    ap.add_argument('--variant', default='standard',
                    choices=['standard', 'long', 'v1'],
                    help='battery variant (see charon.ipms.battery.build_spec)')
    ap.add_argument('--auth-state-dir', default=str(ROOT / '.charon_state'))
    ap.add_argument('--run-dir', default='')
    ap.add_argument('--out', default=str(ROOT.parent / 'charon-research' / 'results' / 'exp_ipms_replication.json'))
    ap.add_argument('--n-boot', type=int, default=2000)
    args = ap.parse_args()

    stamp = time.strftime('%Y%m%d-%H%M%S')
    base_dir = Path(args.run_dir) if args.run_dir else ROOT / '.ipms_runs' / f'repl-{stamp}'
    base_dir.mkdir(parents=True, exist_ok=True)

    provider, base_model, ready = create_provider_and_model(Path(args.auth_state_dir))
    if not ready:
        print('no ready provider', file=sys.stderr)
        return 2

    def backbone(mid: str):
        return backbone_from_entry(mid, codex_provider=provider,
                                   codex_provider_name=base_model.provider)

    pairs = [tuple(p.split('>')) for p in args.pairs.split(',') if p.strip()]
    spec = build_spec(variant=args.variant)

    results: dict[str, dict] = {}
    for a, b in pairs:
        key = f'{a}->{b}'
        reps = []
        for i in range(args.reps):
            rep_dir = base_dir / f'{a}__{b}'.replace('/', '-') / f'rep{i:02d}'
            print(f'[{time.strftime("%H:%M:%S")}] {key} rep {i + 1}/{args.reps}...', flush=True)
            try:
                pair = run_pair(backbone(a), backbone(b), spec, run_dir=rep_dir,
                                billing_state_dir=args.auth_state_dir)
            except Exception as e:
                print(f'    rep failed: {e}', flush=True)
                reps.append({'error': str(e)})
                continue
            record = json.loads(Path(pair.record_path).read_text())
            summary = bootstrap_summary(record, n_boot=args.n_boot)
            treat = submetrics(record, 'swap-diff')
            flipped = sorted(
                pid for pid, s in probe_scores(record, 'swap-diff').items()
                if s['kind'] == 'decision' and s['score'] == 0.0)
            reps.append({
                'IS': summary['invariance']['IS'],
                'IS_ci': summary['invariance']['ci'],
                'treat_C': treat['C'], 'treat_DC': treat['DC'],
                'treat_Cons': treat['Cons'],
                'flipped_decisions': flipped,
                'condition_errors': {c: v['error'] for c, v in record['conditions'].items()
                                     if v.get('error')},
                'record_path': pair.record_path,
            })
            print(f"    IS={summary['invariance']['IS']} flipped={flipped}", flush=True)

        vals = [r['IS'] for r in reps if r.get('IS') is not None]
        flip_hist: dict[str, int] = {}
        for r in reps:
            for pid in r.get('flipped_decisions', []):
                flip_hist[pid] = flip_hist.get(pid, 0) + 1
        results[key] = {
            'reps': reps,
            'n_ok': len(vals),
            'IS_mean': sum(vals) / len(vals) if vals else None,
            'IS_values': vals,
            'IS_rep_ci': _rep_ci(vals),
            'n_below_ceiling': sum(1 for v in vals if v < 0.999),
            'flip_histogram': flip_hist,
        }
        agg = results[key]
        print(f'== {key}: mean IS={agg["IS_mean"]:.3f} over {agg["n_ok"]} reps, '
              f'rep-CI={agg["IS_rep_ci"]}, flips={flip_hist}', flush=True)

    out = {
        'experiment': 'ipms_replication',
        'battery_version': BATTERY_VERSION,
        'created_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'reps_per_pair': args.reps,
        'provider': base_model.provider,
        'snapshot_pinned': False,
        'run_dir': str(base_dir),
        'pairs': results,
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding='utf-8')
    print(f'wrote {out_path}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
