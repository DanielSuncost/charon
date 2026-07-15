#!/usr/bin/env python3
"""IPMS switch-matrix experiment.

Runs the v0 probe battery (release-engineer decision stream + persona) over
every directed pair of the given models, all five conditions per pair, and
scores C / Cons / DC / IS with paired bootstrap CIs.

Results JSON goes to charon-research/results/ by convention (the script
lives here; analysis artifacts live in the research repo).
"""
import argparse
import itertools
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from charon.ipms.battery import BATTERY_VERSION, build_spec  # noqa: E402
from charon.ipms.harness import Backbone, run_pair  # noqa: E402
from charon.ipms.metrics import bootstrap_summary  # noqa: E402
from charon.providers import ModelInfo  # noqa: E402
from charon.providers.provider_bridge import create_provider_and_model  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--models', default='gpt-5.4,gpt-5.5',
                    help='comma-separated model ids reachable via the authenticated provider')
    ap.add_argument('--auth-state-dir', default=str(ROOT / '.charon_state'))
    ap.add_argument('--run-dir', default='',
                    help='where raw pair records + checkpoints go (default .ipms_runs/<ts>)')
    ap.add_argument('--out', default=str(ROOT.parent / 'charon-research' / 'results' / 'exp_ipms_switch_matrix.json'))
    ap.add_argument('--n-boot', type=int, default=2000)
    args = ap.parse_args()

    stamp = time.strftime('%Y%m%d-%H%M%S')
    run_dir = Path(args.run_dir) if args.run_dir else ROOT / '.ipms_runs' / stamp
    run_dir.mkdir(parents=True, exist_ok=True)

    provider, base_model, ready = create_provider_and_model(Path(args.auth_state_dir))
    if not ready:
        print('no ready provider (check onboarding.json / auth)', file=sys.stderr)
        return 2

    model_ids = [m.strip() for m in args.models.split(',') if m.strip()]
    backbones = {
        mid: Backbone(provider, ModelInfo(provider=base_model.provider, model_id=mid,
                                          context_window=base_model.context_window))
        for mid in model_ids
    }

    spec = build_spec()
    matrix: dict[str, dict] = {}
    for a, b in itertools.permutations(model_ids, 2):
        key = f'{a}->{b}'
        print(f'[{time.strftime("%H:%M:%S")}] running pair {key} '
              f'({len(spec.turns)} turns, {len(spec.probes)} probes x 5 conditions)...',
              flush=True)
        t0 = time.time()
        pair = run_pair(backbones[a], backbones[b], spec,
                        run_dir=run_dir, billing_state_dir=args.auth_state_dir)
        record_path = next(run_dir.glob(f'pair-{spec.id}-*{a}__*{b}.json'))
        record = json.loads(record_path.read_text())
        summary = bootstrap_summary(record, n_boot=args.n_boot)
        errors = {c: r.error for c, r in pair.conditions.items() if r.error}
        matrix[key] = {
            'summary': summary,
            'checkpoint_id': pair.checkpoint_id,
            'record_path': str(record_path),
            'billing_mode': pair.billing_mode,
            'condition_errors': errors,
            'wall_seconds': round(time.time() - t0, 1),
        }
        inv = summary['invariance']
        print(f'    IS={inv["IS"]} ci={inv["ci"]} '
              f'(raw: treat={inv["IS_raw_treatment"]:.3f} '
              f'ceil={inv["IS_raw_ceiling"]:.3f} floor={inv["IS_raw_floor"]:.3f})'
              + (f' ERRORS={errors}' if errors else ''), flush=True)

    out = {
        'experiment': 'ipms_switch_matrix',
        'battery_version': BATTERY_VERSION,
        'spec_id': spec.id,
        'created_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
        'models': model_ids,
        'provider': base_model.provider,
        'snapshot_pinned': False,  # subscription aliases; pin metered ids for camera-ready
        'n_probes': len(spec.probes),
        'n_trajectory_turns': len(spec.turns),
        'n_boot': args.n_boot,
        'run_dir': str(run_dir),
        'matrix': matrix,
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding='utf-8')
    print(f'wrote {out_path}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
