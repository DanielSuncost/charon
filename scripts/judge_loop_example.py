#!/usr/bin/env python3
"""Runnable judge-loop example — optimize a real program against a real metric.

This drives the production judge-loop machinery (judge_engine + judge_loop_driver
+ checkpoint_manager) end to end. The only piece stubbed out is the *implementer*:
in production that is a scoped LLM shade that edits files; here a scripted
implementer makes the same kind of real source edits so the example is
deterministic and needs no model or API.

What it demonstrates:
  - baseline measurement by running the real eval_command,
  - keep-on-improvement / rollback-on-regression against best_score,
  - shadow-git checkpoint + rollback of real files (added files included),
  - convergence detection (target_met).

Run:  PYTHONPATH=apps/core-daemon python scripts/judge_loop_example.py
"""
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "core-daemon"))

from judge_engine import create_loop, load_loop  # noqa: E402
from checkpoint_manager import CheckpointManager  # noqa: E402
from judge_loop_driver import advance_loop  # noqa: E402

SOLVER = '''\
BATCH = {batch}

def run():
    total = 0
    for i in range(BATCH):
        total += (i * 7) % 13
    return total + BATCH * 10

if __name__ == "__main__":
    print(run())
'''


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="judge_loop_example_"))
    work = tmp / "project"; work.mkdir()
    state = tmp / "state"; state.mkdir()
    (work / "solver.py").write_text(SOLVER.format(batch=1))

    def set_batch(n: int):
        src = (work / "solver.py").read_text()
        (work / "solver.py").write_text(re.sub(r"BATCH = \d+", f"BATCH = {n}", src))

    def current_batch() -> int:
        return int(re.search(r"BATCH = (\d+)", (work / "solver.py").read_text()).group(1))

    # Scripted implementer: improve -> regress (must roll back) -> hit target.
    plan = iter([5, 3, 20])

    def implementer(config, working_dir):
        n = next(plan, None)
        if n is None:
            return None
        set_batch(n)
        return f"set BATCH={n}"

    config = create_loop(
        state,
        goal="maximize solver.py throughput",
        project=str(work),
        agent_id="AG-example",
        judge_type="quantitative",
        direction="maximize",
        target_score=150.0,
        eval_command="python3 solver.py",
        metric_name="throughput",
        max_iterations=10,
    )
    cp = CheckpointManager(state, work)

    print(f"{'tick':<6}{'action':<14}{'score':<8}{'kept':<7}{'best':<7}{'BATCH on disk'}")
    print("-" * 60)
    tick = 0
    while config.status in ("created", "running"):
        tick += 1
        config, ev = advance_loop(state, config, implementer=implementer,
                                  working_dir=work, checkpoint_mgr=cp)
        print(f"{tick:<6}{ev.get('action',''):<14}{str(ev.get('score','-')):<8}"
              f"{str(ev.get('kept','-')):<7}{str(config.best_score):<7}{current_batch()}")
        if config.status == "completed":
            print("-" * 60)
            print(f"converged: reason={config.convergence.reason} "
                  f"best={config.best_score} rollbacks="
                  f"{sum(1 for it in config.iterations if it.status == 'discarded')}")
            break

    print(f"\nFinal solver.py BATCH (best kept): {current_batch()}  "
          f"(no .git in project: {not (work / '.git').exists()})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
