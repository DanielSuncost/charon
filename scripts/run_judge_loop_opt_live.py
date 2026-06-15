#!/usr/bin/env python3
"""Live judge loop on a HARD optimization task: speed up count_close_pairs.

A continuous metric (wall-clock, minimize) with no closed-form answer and a
FROZEN correctness gate — so the loop must genuinely hill-climb over many ticks,
banking each real speedup and rolling back regressions / wrong answers.

  PYTHONPATH=apps/core-daemon CHARON_STATE_DIR=$PWD/.charon_state \
    python scripts/run_judge_loop_opt_live.py
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "core-daemon"))

from judge_engine import (  # noqa: E402
    create_loop, create_judge, run_baseline, run_iteration, check_convergence,
)
from checkpoint_manager import CheckpointManager  # noqa: E402
from judge_loop_driver import shade_implementer  # noqa: E402

SOLVER = '''import math

def count_close_pairs(points, r):
    """Number of unordered point pairs within Euclidean distance r."""
    n = len(points)
    count = 0
    for i in range(n):
        for j in range(i + 1, n):
            dx = points[i][0] - points[j][0]
            dy = points[i][1] - points[j][1]
            if math.sqrt(dx * dx + dy * dy) <= r:
                count += 1
    return count
'''

BENCH = '''import random, time
from solver import count_close_pairs

def make(n, seed):
    rng = random.Random(seed)
    return [(rng.random(), rng.random()) for _ in range(n)]

pts, r = make(4000, 12345), 0.02
best = float("inf")
for _ in range(3):
    t = time.perf_counter()
    count_close_pairs(pts, r)
    best = min(best, time.perf_counter() - t)
print(f"{best:.6f}")
'''

TEST = '''import math, random, sys
from solver import count_close_pairs

def oracle(points, r):
    n, c = len(points), 0
    for i in range(n):
        for j in range(i + 1, n):
            if math.hypot(points[i][0]-points[j][0], points[i][1]-points[j][1]) <= r:
                c += 1
    return c

def rnd(n, seed):
    g = random.Random(seed); return [(g.random(), g.random()) for _ in range(n)]

cases = [rnd(0,1), rnd(1,2), rnd(50,3), rnd(200,4), [(0.0,0.0),(0.0,0.0)], rnd(300,5)]
radii = [0.0, 0.05, 0.1, 0.3, 1.5]
ok = total = 0
for pts in cases:
    for r in radii:
        total += 1
        ok += (count_close_pairs(pts, r) == oracle(pts, r))
print(f"{ok} passed, {total-ok} failed")
sys.exit(0 if ok == total else 1)
'''


def main():
    state = ROOT / ".charon_state"
    tmp = Path(tempfile.mkdtemp(prefix="jl_opt_"))
    work = tmp / "project"; work.mkdir()
    cfgstate = tmp / "state"; cfgstate.mkdir()
    (work / "solver.py").write_text(SOLVER)
    (work / "bench.py").write_text(BENCH)
    (work / "test_correct.py").write_text(TEST)

    cfg = create_loop(
        cfgstate, goal="Make count_close_pairs faster without changing its output.",
        project=str(work), agent_id="opt", judge_type="quantitative",
        metric_name="seconds", direction="minimize",
        eval_command="python3 bench.py", parse_mode="last_float",
        scope=["solver.py"], frozen=["bench.py", "test_correct.py"],
        constraint_commands=["python3 test_correct.py"],
        target_score=0.0, max_iterations=12,
        # Wall-clock spans orders of magnitude (1.8s -> ms): use a RELATIVE floor
        # (keep a change that's >3% faster) instead of an absolute one that goes
        # blind once the metric shrinks below it.
        min_delta=0.0, min_delta_rel=0.03,
    )
    judge = create_judge(cfg)
    cp = CheckpointManager(cfgstate, work)
    cfg = run_baseline(cfg, judge, work, checkpoint_mgr=cp)
    print(f"baseline = {cfg.baseline:.6f}s")
    print(f"{'tick':<5}{'summary':<54}{'sec':<11}{'kept':<6}{'best'}")
    print("-" * 88)

    for tick in range(1, cfg.max_iterations + 1):
        summary = shade_implementer(state, cfg, work)
        if summary is None:
            print(f"{tick:<5}(implementer returned None — no provider/failed)")
            break
        cfg, it = run_iteration(cfg, judge, work, change_summary=str(summary)[:52],
                                checkpoint_mgr=cp)
        sec = f"{it.score:.6f}" if it.score is not None else "n/a"
        best = f"{cfg.best_score:.6f}" if cfg.best_score is not None else "n/a"
        flag = it.status if not it.kept else "kept"
        print(f"{tick:<5}{str(summary)[:52]:<54}{sec:<11}{str(it.kept):<6}{best}  [{flag}]")
        conv = check_convergence(cfg)
        if conv:
            print("-" * 88)
            print(f"converged: {conv.reason}  best={cfg.best_score:.6f}s "
                  f"(speedup {cfg.baseline / cfg.best_score:.1f}x over baseline)")
            break

    print("\nfinal solver.py:\n" + (work / "solver.py").read_text())
    frozen_ok = ((work / "bench.py").read_text() == BENCH and
                 (work / "test_correct.py").read_text() == TEST)
    print("frozen files untouched:", frozen_ok)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
