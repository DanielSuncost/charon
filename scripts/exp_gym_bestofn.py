#!/usr/bin/env python3
"""Best-of-N over charon-gym with a REAL LLM policy: measure pass@k per task.

Drives each gym task with a gpt-5.5 policy that, given the observation, proposes
a tool call (JSON). Runs N independent rollouts per task and reports pass@k —
the fraction of tasks solved within k samples — which shows the env discriminates
and the reward is real signal under a non-scripted policy.

  PYTHONPATH=apps/core-daemon CHARON_STATE_DIR=$PWD/.charon_state \
    python scripts/exp_gym_bestofn.py --n 6 --max-steps 4
"""
import argparse
import asyncio
import json
import re
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "core-daemon"))

import charon_gym  # noqa: E402
from providers import Message  # noqa: E402

SYS = ("You are a coding policy in an environment. Each step, return ONE tool call as JSON: "
       '{"tool": "Edit"|"Write", "params": {...}}. '
       'Edit params: {path, oldText, newText}. Write params: {path, content}. Return ONLY the JSON.')


def _policy(provider, model, obs):
    files = "\n".join(f"--- {n} ---\n{c}" for n, c in obs["files"].items())
    prompt = (f"Goal: {obs['goal']}\nCurrent reward: {obs['reward']} (higher is better).\n"
              f"Files:\n{files}\nLast tool output: {obs.get('last_tool_output','')[:200]}\n"
              "Return ONE tool call as JSON to increase the reward.")

    async def go():
        out = []
        async for d in provider.stream(messages=[Message(role="user", content=prompt, timestamp=0.0)],
                                       model=model, system_prompt=SYS, max_tokens=600):
            if d.type == "text":
                out.append(d.text)
            if d.type == "error":
                return ""
        return "".join(out)
    resp = asyncio.run(go())
    m = re.search(r"\{.*\}", resp, re.DOTALL)
    try:
        return json.loads(m.group(0)) if m else None
    except Exception:
        return None


def rollout(provider, model, task_id, state_dir, max_steps):
    env = charon_gym.make_env(task_id, state_dir, provider=provider, model=model)
    obs = env.reset()
    for _ in range(max_steps):
        action = _policy(provider, model, obs)
        if not action or "tool" not in action:
            break
        obs, reward, done, info = env.step(action)
        if done and info["reached_threshold"]:
            return True
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=6)
    ap.add_argument("--max-steps", type=int, default=4)
    ap.add_argument("--out", default="results/exp_gym_bestofn.json")
    args = ap.parse_args()
    from provider_bridge import create_provider_and_model
    provider, model, ready = create_provider_and_model(Path(".charon_state"))
    assert ready
    # deterministic tasks (aesthetic has no hard threshold to "pass")
    tasks = [t for t in charon_gym.list_tasks() if t != "improve_quality"]
    report = {"model": getattr(model, "model_id", str(model)), "n": args.n, "tasks": {}}
    state = Path(tempfile.mkdtemp(prefix="gym_bon_"))
    for t in tasks:
        successes = [rollout(provider, model, t, state / f"{t}_{i}", args.max_steps) for i in range(args.n)]
        s = sum(successes)
        report["tasks"][t] = {"n": args.n, "successes": s,
                              "pass@1": round(s / args.n, 3),
                              "pass@n": 1.0 if s > 0 else 0.0}
        print(f"{t:18} {s}/{args.n} solved  pass@1={s/args.n:.2f}  pass@{args.n}={'1.0' if s else '0.0'}")
    out = Path(args.out); out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2))
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
