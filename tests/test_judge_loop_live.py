"""Live end-to-end judge-loop test with a real LLM implementer.

Opt-in only: set CHARON_RUN_LIVE=1 (and have a provider configured in
.charon_state). It makes real provider calls, so it's skipped by default and in
CI. Proves the full path: a real model reads a FROZEN checker, edits within its
write-scope, the engine scores/keeps/rolls back, and the loop converges — with
the frozen file untouched.
"""
import os
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(
    os.environ.get('CHARON_RUN_LIVE') != '1',
    reason='live LLM test; set CHARON_RUN_LIVE=1 to run',
)

CHECK = (
    "from solver import transform\n"
    "cases = [(10,4),(30,10),(100,25),(50,15)]\n"
    "p = sum(1 for n,e in cases if transform(n)==e); f=len(cases)-p\n"
    "print(f'{p} passed, {f} failed')\n"
)


def test_live_loop_converges_with_real_implementer(tmp_path):
    from charon.judge.judge_engine import create_loop, create_judge, run_baseline, run_iteration, check_convergence
    from charon.automation.checkpoint_manager import CheckpointManager
    from charon.judge.judge_loop_driver import shade_implementer

    state = ROOT / '.charon_state'
    # Bail clearly if no provider is configured rather than failing opaquely.
    from charon.providers.model_registry import get_shade_provider_and_model
    provider, _model, ready = get_shade_provider_and_model(state)
    if not provider or not ready:
        pytest.skip('no shade provider configured in .charon_state')

    work = tmp_path / 'project'
    work.mkdir()
    cfgstate = tmp_path / 'state'
    cfgstate.mkdir()
    (work / 'solver.py').write_text("def transform(n):\n    return 0  # TODO\n")
    (work / 'check.py').write_text(CHECK)

    cfg = create_loop(
        cfgstate, goal='Implement transform(n) = the number of primes <= n so check.py passes.',
        project=str(work), agent_id='live', judge_type='correctness',
        eval_command='python3 check.py', parse_mode='pass_rate', direction='maximize',
        target_score=1.0, scope=['solver.py'], frozen=['check.py'], max_iterations=6,
    )
    judge = create_judge(cfg)
    cp = CheckpointManager(cfgstate, work)   # whole-tree: full rollback + frozen detection
    cfg = run_baseline(cfg, judge, work, checkpoint_mgr=cp)
    assert cfg.baseline == 0.0

    converged = None
    for _ in range(cfg.max_iterations):
        summary = shade_implementer(state, cfg, work)
        assert summary, 'live implementer returned no change'
        cfg, _it = run_iteration(cfg, judge, work, change_summary=str(summary)[:200],
                                 checkpoint_mgr=cp)
        converged = check_convergence(cfg)
        if converged:
            break

    assert converged is not None and converged.converged
    assert converged.reason == 'target_met'
    assert cfg.best_score == 1.0
    # The frozen checker was never modified.
    assert (work / 'check.py').read_text() == CHECK
