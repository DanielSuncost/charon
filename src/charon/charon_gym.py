"""charon-gym — a Gymnasium-style environment over Charon's own machinery.

This is an ADAPTER, not a new execution engine. It exposes Charon as an
episodic environment an external policy can drive:

    env = make_env("fix_failing_test", state_dir)
    obs = env.reset()
    obs, reward, done, info = env.step({"tool": "Edit", "params": {...}})

- action     = a tool call in Charon's existing tool vocabulary (Read/Write/
               Edit/Bash/...), dispatched through tools.execute_tool.
- observation = the resulting tool output plus task context (goal, scope files,
               step index, current reward).
- reward     = a judge score from judge_engine, signed so higher is better
               (CorrectnessJudge / QuantitativeJudge / AestheticJudge).
- reset      = re-materialize the task fixture deterministically and snapshot a
               shadow-git baseline (CheckpointManager), so every reset returns
               to the same byte-for-byte state.

It is NOT distributed RL infra and does NOT update any weights — the reward
apparatus and environment are real; the consumer is an external policy.
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from charon.tools import ToolContext, execute_tool
from charon.judge.judge_engine import create_loop, create_judge
from charon.automation.checkpoint_manager import CheckpointManager


@dataclass
class GymTask:
    id: str
    goal: str
    setup: Callable[[Path], None]          # materialize the fixture into a dir
    judge_kwargs: dict                     # passed to create_loop (judge config)
    direction: str = "maximize"            # maximize | minimize (reward sign)
    reward_threshold: float | None = None  # episode succeeds at/over (or under) this
    step_budget: int = 12
    scope: list[str] = field(default_factory=list)
    frozen: list[str] = field(default_factory=list)
    observed_files: list[str] = field(default_factory=list)  # shown in observation


class CharonGymEnv:
    """Episodic environment for one task. Reset/step/reward, like Gymnasium."""

    def __init__(self, task: GymTask, state_dir: Path, provider=None, model=None):
        self.task = task
        self.state_dir = Path(state_dir)
        self._provider = provider
        self._model = model
        self._work = self.state_dir / "gym" / task.id / "work"
        self._cfg_state = self.state_dir / "gym" / task.id / "state"
        self._step = 0
        self._cp = None
        self._config = None
        self._judge = None
        self._last_output = ""

    # ── core API ────────────────────────────────────────────────────────
    def reset(self) -> dict:
        if self._work.exists():
            shutil.rmtree(self._work)
        self._work.mkdir(parents=True)
        self._cfg_state.mkdir(parents=True, exist_ok=True)
        self.task.setup(self._work)

        self._config = create_loop(
            self._cfg_state, goal=self.task.goal, project=str(self._work),
            agent_id=f"gym-{self.task.id}", direction=self.task.direction,
            scope=self.task.scope, frozen=self.task.frozen, **self.task.judge_kwargs,
        )
        self._judge = create_judge(self._config, provider=self._provider, model=self._model)
        self._cp = CheckpointManager(self._cfg_state, self._work, scope=self.task.scope or None)
        self._cp.snapshot("gym-baseline")  # shadow-git baseline for this episode
        self._step = 0
        self._last_output = ""
        return self._observation(self._reward())

    def step(self, action: dict) -> tuple[dict, float, bool, dict]:
        if self._config is None:
            raise RuntimeError("call reset() before step()")
        self._step += 1
        ctx = ToolContext(
            project_root=self._work, agent_id=f"gym-{self.task.id}",
            state_dir=self._cfg_state, scope=self.task.scope or None,
            frozen=self.task.frozen or None,
        )
        tool = str(action.get("tool", ""))
        params = action.get("params", {}) or {}
        result = execute_tool(tool, params, ctx)
        self._last_output = (result.content or "")[:1000]
        tool_error = bool(result.is_error)

        reward = self._reward()
        done = self._step >= self.task.step_budget or self._reached(reward)
        info = {
            "step": self._step, "tool": tool, "tool_error": tool_error,
            "reached_threshold": self._reached(reward),
        }
        return self._observation(reward), reward, done, info

    # ── internals ───────────────────────────────────────────────────────
    def _raw_score(self) -> float | None:
        v = self._judge.evaluate(self._config, self._work)
        return None if v.error else v.score

    def _reward(self) -> float:
        s = self._raw_score()
        if s is None:
            return float("-inf")  # unscoreable state (e.g. broken eval)
        return s if self.task.direction == "maximize" else -s

    def _reached(self, reward: float) -> bool:
        t = self.task.reward_threshold
        if t is None:
            return False
        # threshold is in raw-score units; compare against the signed reward.
        signed_t = t if self.task.direction == "maximize" else -t
        return reward >= signed_t

    def _observation(self, reward: float) -> dict:
        files = {}
        for rel in (self.task.observed_files or self.task.scope):
            p = self._work / rel
            if p.is_file():
                files[rel] = p.read_text()[:4000]
        return {
            "task": self.task.id,
            "goal": self.task.goal,
            "step": self._step,
            "reward": reward,
            "raw_score": self._raw_score(),
            "files": files,
            "last_tool_output": self._last_output,
        }


# ── Task registry (5 real tasks, mixed reward types) ────────────────────────

def _w(work: Path, name: str, text: str):
    (work / name).write_text(text)


def _setup_fix_test(work: Path):
    _w(work, "calc.py", "def add(a, b):\n    return a + b\n\n\ndef mul(a, b):\n    return a + b  # BUG\n")
    _w(work, "check.py",
       "from calc import add, mul\n"
       "checks = [add(2,3)==5, mul(2,3)==6]\n"
       "p = sum(1 for c in checks if c); f = len(checks)-p\n"
       "print(f'{p} passed, {f} failed')\n")


def _setup_maximize(work: Path):
    _w(work, "solver.py", "FACTOR = 1\nif __name__ == '__main__':\n    print(FACTOR * 10)\n")


def _setup_minimize(work: Path):
    _w(work, "latency.txt", "100\n")


def _setup_lint(work: Path):
    # "lint" = a self-contained convention check: no tabs, ends with newline.
    _w(work, "module.py", "def f():\n\treturn 1\n")  # uses a tab -> fails
    _w(work, "lint.py",
       "src = open('module.py').read()\n"
       "ok = ('\\t' not in src) and src.endswith('\\n')\n"
       "print('1 passed, 0 failed' if ok else '0 passed, 1 failed')\n")


def _setup_quality(work: Path):
    _w(work, "summarize.py",
       "def summarize(nums):\n    t=0\n    for n in nums: t=t+n\n    return t/len(nums)\n")


TASKS: dict[str, GymTask] = {
    "fix_failing_test": GymTask(
        id="fix_failing_test",
        goal="Fix mul() in calc.py so all checks pass (do not edit check.py).",
        setup=_setup_fix_test,
        judge_kwargs={"judge_type": "correctness", "eval_command": "python3 check.py",
                      "parse_mode": "pass_rate", "target_score": 1.0},
        direction="maximize", reward_threshold=1.0, step_budget=8,
        scope=["calc.py"], frozen=["check.py"], observed_files=["calc.py", "check.py"],
    ),
    "maximize_output": GymTask(
        id="maximize_output",
        goal="Increase the number printed by solver.py to at least 200 by raising FACTOR.",
        setup=_setup_maximize,
        judge_kwargs={"judge_type": "quantitative", "eval_command": "python3 solver.py",
                      "target_score": 200.0},
        direction="maximize", reward_threshold=200.0, step_budget=8,
        scope=["solver.py"], observed_files=["solver.py"],
    ),
    "minimize_latency": GymTask(
        id="minimize_latency",
        goal="Lower the value in latency.txt to 50 or below.",
        setup=_setup_minimize,
        judge_kwargs={"judge_type": "quantitative", "eval_command": "cat latency.txt",
                      "target_score": 50.0},
        direction="minimize", reward_threshold=50.0, step_budget=8,
        scope=["latency.txt"], observed_files=["latency.txt"],
    ),
    "pass_lint": GymTask(
        id="pass_lint",
        goal="Make lint.py report a pass by removing the tab in module.py (do not edit lint.py).",
        setup=_setup_lint,
        judge_kwargs={"judge_type": "correctness", "eval_command": "python3 lint.py",
                      "parse_mode": "pass_rate", "target_score": 1.0},
        direction="maximize", reward_threshold=1.0, step_budget=8,
        scope=["module.py"], frozen=["lint.py"], observed_files=["module.py", "lint.py"],
    ),
    "improve_quality": GymTask(
        id="improve_quality",
        goal="Improve the code quality of summarize.py (readability, edge cases, naming).",
        setup=_setup_quality,
        judge_kwargs={"judge_type": "aesthetic",
                      "rubric": "Rate 1-10 for readability, idiomatic Python, edge-case handling. "
                                "Return JSON {\"score\": N, \"feedback\": \"...\"}."},
        direction="maximize", reward_threshold=8.0, step_budget=4,
        scope=["summarize.py"], observed_files=["summarize.py"],
    ),
}


def list_tasks() -> list[str]:
    return list(TASKS.keys())


def make_env(task_id: str, state_dir: Path, provider=None, model=None) -> CharonGymEnv:
    if task_id not in TASKS:
        raise KeyError(f"unknown task: {task_id} (have: {list_tasks()})")
    return CharonGymEnv(TASKS[task_id], Path(state_dir), provider=provider, model=model)


__all__ = ["CharonGymEnv", "GymTask", "TASKS", "make_env", "list_tasks"]
