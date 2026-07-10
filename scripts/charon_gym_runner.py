#!/usr/bin/env python3
"""Reference runner for charon-gym: drive each task with a trivial scripted
policy to prove the loop closes (reset -> N steps -> scored reward).

  PYTHONPATH=src CHARON_STATE_DIR=$PWD/.charon_state \
    python scripts/charon_gym_runner.py            # all 5 tasks, once
  ... python scripts/charon_gym_runner.py --episodes 10 --task maximize_output

The "policy" here is a hand-written correct action sequence per task — enough to
demonstrate the environment is real and returns real rewards. An RL policy would
replace this scripted policy; the env/reward/reset are unchanged.
"""
import argparse
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from charon import charon_gym  # noqa: E402

BETTER_SUMMARIZE = (
    "def mean(numbers):\n"
    "    \"\"\"Return the arithmetic mean of a non-empty sequence of numbers.\"\"\"\n"
    "    if not numbers:\n"
    "        raise ValueError('mean() requires at least one value')\n"
    "    return sum(numbers) / len(numbers)\n"
)

# A trivial, correct policy per task: a list of tool-call actions.
POLICIES = {
    "fix_failing_test": [
        {"tool": "Edit", "params": {"path": "calc.py",
                                    "oldText": "return a + b  # BUG", "newText": "return a * b"}},
    ],
    "maximize_output": [
        {"tool": "Edit", "params": {"path": "solver.py",
                                    "oldText": "FACTOR = 1", "newText": "FACTOR = 25"}},
    ],
    "minimize_latency": [
        {"tool": "Write", "params": {"path": "latency.txt", "content": "40\n"}},
    ],
    "pass_lint": [
        {"tool": "Edit", "params": {"path": "module.py",
                                    "oldText": "\treturn 1", "newText": "    return 1"}},
    ],
    "improve_quality": [
        {"tool": "Write", "params": {"path": "summarize.py", "content": BETTER_SUMMARIZE}},
    ],
}


def _provider():
    try:
        from charon.providers.provider_bridge import create_provider_and_model
        p, m, ready = create_provider_and_model(Path(".charon_state"))
        return (p, m) if ready else (None, None)
    except Exception:
        return (None, None)


def run_episode(task_id, state_dir, provider, model):
    env = charon_gym.make_env(task_id, state_dir, provider=provider, model=model)
    obs = env.reset()
    trace = [{"step": 0, "reward": obs["reward"], "raw": obs["raw_score"]}]
    final = obs
    for action in POLICIES[task_id]:
        obs, reward, done, info = env.step(action)
        trace.append({"step": info["step"], "tool": info["tool"], "reward": reward,
                      "raw": obs["raw_score"], "done": done, "reached": info["reached_threshold"]})
        final = obs
        if done:
            break
    return {"task": task_id, "baseline_reward": trace[0]["reward"],
            "final_reward": final["reward"], "trace": trace}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", default=None)
    ap.add_argument("--episodes", type=int, default=1)
    ap.add_argument("--out", default="results/charon_gym_episodes.json")
    args = ap.parse_args()

    provider, model = _provider()
    state = Path(tempfile.mkdtemp(prefix="charon_gym_"))
    tasks = [args.task] if args.task else charon_gym.list_tasks()

    report = {"provider_available": provider is not None, "episodes": {}}
    for t in tasks:
        runs = [run_episode(t, state, provider, model) for _ in range(args.episodes)]
        finals = [r["final_reward"] for r in runs]
        report["episodes"][t] = {
            "n": len(runs),
            "baseline_reward": runs[0]["baseline_reward"],
            "final_reward_mean": round(sum(finals) / len(finals), 4),
            "example_trace": runs[0]["trace"],
        }
        print(f"{t:18} baseline={runs[0]['baseline_reward']:>8}  final={runs[-1]['final_reward']:>8}  "
              f"(steps={len(runs[-1]['trace'])-1})")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2))
    print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
