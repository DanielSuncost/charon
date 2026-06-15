#!/usr/bin/env python3
"""Live aesthetic judge loop: polish a CLI --help screen against a rubric.

A stochastic LLM judge (AestheticJudge, σ≈0.22) scores help.txt 0–10; the loop
refines it. A frozen constraint (check_help.py) requires every documented command
and flag to remain present, so the loop can't "improve" the score by dropping
content. Exercises the noise-floor min_delta on a subjective metric.

  PYTHONPATH=apps/core-daemon CHARON_STATE_DIR=$PWD/.charon_state \
    python scripts/run_judge_loop_aesthetic_live.py
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
from provider_bridge import create_provider_and_model  # noqa: E402

# Deliberately terse/ugly baseline — lots of headroom for a judge to discriminate.
HELP = """\
usage: charon [cmd] [opts]
commands: chat, recall, remember, loops, status
opts: --help --json --state-dir DIR --verbose
run charon chat to start. recall searches memory. loops manages judge loops.
"""

# Required content the loop must NOT drop while polishing (frozen constraint).
REQUIRED = ["chat", "recall", "remember", "loops", "status",
            "--help", "--json", "--state-dir", "--verbose"]

CHECK = (
    "import sys\n"
    "text = open('help.txt').read()\n"
    f"required = {REQUIRED!r}\n"
    "missing = [r for r in required if r not in text]\n"
    "print(('missing: ' + ', '.join(missing)) if missing else 'all required content present')\n"
    "sys.exit(1 if missing else 0)\n"
)

RUBRIC = (
    "You are reviewing the --help output of a command-line tool. Score it 0-10 on:\n"
    "  1. Clarity & grammar — correct, precise, no ambiguity.\n"
    "  2. Scannability — a usage line, grouped sections, aligned columns.\n"
    "  3. Completeness — each command has a one-line description, each option is\n"
    "     explained, and there is at least one concrete usage example.\n"
    "  4. Polish — consistent style, no clutter, reads like a tool you'd trust.\n"
    "10 = exemplary, comparable to `gh` or `ripgrep` help. Be a discerning critic; "
    "reserve 9-10 for genuinely excellent output."
)


def main():
    state = ROOT / ".charon_state"
    provider, model, ready = create_provider_and_model(state)
    if not ready:
        print("no provider configured in .charon_state"); return 1

    tmp = Path(tempfile.mkdtemp(prefix="jl_aes_"))
    work = tmp / "project"; work.mkdir()
    cfgstate = tmp / "state"; cfgstate.mkdir()
    (work / "help.txt").write_text(HELP)
    (work / "check_help.py").write_text(CHECK)

    cfg = create_loop(
        cfgstate, goal="Polish this CLI --help text into clear, scannable, complete, polished help.",
        project=str(work), agent_id="aes", judge_type="aesthetic", rubric=RUBRIC,
        direction="maximize", target_score=9.0,
        scope=["help.txt"], frozen=["check_help.py"],
        constraint_commands=["python3 check_help.py"],
        max_iterations=8,   # aesthetic min_delta defaults to ≈2σ (0.5) automatically
    )
    judge = create_judge(cfg, provider=provider, model=model)
    cp = CheckpointManager(cfgstate, work)
    cfg = run_baseline(cfg, judge, work, checkpoint_mgr=cp)
    print(f"min_delta (noise floor) = {cfg.min_delta}")
    print(f"baseline aesthetic score = {cfg.baseline}/10")
    print(f"{'tick':<5}{'summary':<50}{'score':<7}{'kept':<6}{'best'}")
    print("-" * 78)

    for tick in range(1, cfg.max_iterations + 1):
        summary = shade_implementer(state, cfg, work)
        if summary is None:
            print(f"{tick:<5}(implementer returned None)"); break
        cfg, it = run_iteration(cfg, judge, work, change_summary=str(summary)[:48],
                                checkpoint_mgr=cp)
        sc = f"{it.score}" if it.score is not None else "n/a"
        flag = it.status if not it.kept else "kept"
        print(f"{tick:<5}{str(summary)[:48]:<50}{sc:<7}{str(it.kept):<6}{cfg.best_score}  [{flag}]")
        conv = check_convergence(cfg)
        if conv:
            print("-" * 78)
            print(f"converged: {conv.reason}  best={cfg.best_score}/10 (baseline {cfg.baseline})")
            break

    print("\n=== final help.txt ===\n" + (work / "help.txt").read_text())
    print("frozen check_help.py untouched:", (work / "check_help.py").read_text() == CHECK)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
