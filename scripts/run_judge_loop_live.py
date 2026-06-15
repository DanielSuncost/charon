#!/usr/bin/env python3
"""Drive a REAL judge loop with the live LLM implementer (shade_implementer).

Optimization target: implement transform(n) = #primes <= n so check.py passes.
check.py is FROZEN (the loop must not edit it). Correctness judge, target 1.0.
This exercises the full live path: LLM reads/edits within scope -> run_iteration
checkpoints + scores + keeps/rolls back -> convergence.

  PYTHONPATH=apps/core-daemon CHARON_STATE_DIR=$PWD/.charon_state \
    python scripts/run_judge_loop_live.py
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "core-daemon"))

from judge_engine import create_loop, create_judge, run_baseline, run_iteration, check_convergence  # noqa
from checkpoint_manager import CheckpointManager  # noqa
from judge_loop_driver import shade_implementer  # noqa

CHECK = (
    "from solver import transform\n"
    "cases = [(10,4),(30,10),(100,25),(50,15)]\n"
    "p = sum(1 for n,e in cases if transform(n)==e); f=len(cases)-p\n"
    "print(f'{p} passed, {f} failed')\n"
)


def main():
    state = Path(__file__).resolve().parents[1] / ".charon_state"
    tmp = Path(tempfile.mkdtemp(prefix="jl_live_"))
    work = tmp / "project"; work.mkdir()
    cfgstate = tmp / "state"; cfgstate.mkdir()
    (work / "solver.py").write_text("def transform(n):\n    return 0  # TODO: count primes <= n\n")
    (work / "check.py").write_text(CHECK)

    cfg = create_loop(
        cfgstate, goal="Implement transform(n) = the number of primes <= n so check.py passes.",
        project=str(work), agent_id="live", judge_type="correctness",
        eval_command="python3 check.py", parse_mode="pass_rate", direction="maximize",
        target_score=1.0, scope=["solver.py"], frozen=["check.py"], max_iterations=6,
    )
    judge = create_judge(cfg)
    cp = CheckpointManager(cfgstate, work)   # whole-tree: full rollback + frozen detection
    cfg = run_baseline(cfg, judge, work, checkpoint_mgr=cp)
    print(f"baseline pass_rate = {cfg.baseline}")
    print(f"{'tick':<5}{'summary':<48}{'score':<8}{'kept':<6}{'best'}")
    print("-" * 75)

    for tick in range(1, cfg.max_iterations + 1):
        summary = shade_implementer(state, cfg, work)   # <-- the live LLM call
        if summary is None:
            print(f"{tick:<5}{'(implementer returned None — no provider/failed)':<48}")
            break
        cfg, it = run_iteration(cfg, judge, work, change_summary=str(summary)[:46],
                                checkpoint_mgr=cp)
        print(f"{tick:<5}{(str(summary)[:46]):<48}{str(it.score):<8}{str(it.kept):<6}{cfg.best_score}")
        conv = check_convergence(cfg)
        if conv:
            print("-" * 75)
            print(f"converged: {conv.reason}  best={cfg.best_score}")
            break

    print("\nfinal solver.py:")
    print((work / "solver.py").read_text())
    print("check.py untouched (frozen):", (work / "check.py").read_text() == CHECK)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
