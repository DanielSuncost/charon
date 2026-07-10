"""charon-gym: an external consumer imports it, drives reset/step on every task,
and receives real rewards. The four deterministic tasks are asserted fully; the
aesthetic task needs a provider for a real reward, so we only assert its
reset/step plumbing here."""

from charon import charon_gym


# Correct scripted action for each deterministic task.
DETERMINISTIC = {
    "fix_failing_test": ({"tool": "Edit", "params": {"path": "calc.py",
                          "oldText": "return a + b  # BUG", "newText": "return a * b"}}, 1.0),
    "maximize_output": ({"tool": "Edit", "params": {"path": "solver.py",
                         "oldText": "FACTOR = 1", "newText": "FACTOR = 25"}}, 250.0),
    "minimize_latency": ({"tool": "Write", "params": {"path": "latency.txt", "content": "40\n"}}, -40.0),
    "pass_lint": ({"tool": "Edit", "params": {"path": "module.py",
                   "oldText": "\treturn 1", "newText": "    return 1"}}, 1.0),
}


def test_registry_has_five_tasks():
    assert len(charon_gym.list_tasks()) == 5
    assert set(charon_gym.list_tasks()) >= set(DETERMINISTIC) | {"improve_quality"}


def test_deterministic_tasks_close_the_loop(tmp_path):
    for task_id, (action, expected_reward) in DETERMINISTIC.items():
        env = charon_gym.make_env(task_id, tmp_path / task_id)
        obs = env.reset()
        assert obs["task"] == task_id and "goal" in obs and obs["step"] == 0
        baseline = obs["reward"]
        obs, reward, done, info = env.step(action)
        assert not info["tool_error"], f"{task_id}: tool failed: {obs['last_tool_output']}"
        assert reward >= baseline, f"{task_id}: reward did not improve ({baseline} -> {reward})"
        assert reward == expected_reward, f"{task_id}: reward {reward} != {expected_reward}"
        assert done and info["reached_threshold"], f"{task_id}: did not reach threshold"


def test_reset_is_deterministic(tmp_path):
    env = charon_gym.make_env("maximize_output", tmp_path / "det")
    env.reset()
    env.step({"tool": "Edit", "params": {"path": "solver.py",
              "oldText": "FACTOR = 1", "newText": "FACTOR = 25"}})
    # reset must return to baseline byte-for-byte, regardless of prior steps.
    obs2 = env.reset()
    assert obs2["reward"] == 10.0
    assert "FACTOR = 1" in obs2["files"]["solver.py"]


def test_aesthetic_task_plumbing_without_provider(tmp_path):
    # No provider in CI -> the aesthetic reward is degenerate, but reset/step
    # must still run and return a well-formed (obs, reward, done, info).
    env = charon_gym.make_env("improve_quality", tmp_path / "aes")
    obs = env.reset()
    assert obs["task"] == "improve_quality"
    obs, reward, done, info = env.step(
        {"tool": "Write", "params": {"path": "summarize.py", "content": "def mean(xs):\n    return sum(xs)/len(xs)\n"}})
    assert isinstance(reward, float)
    assert set(info) >= {"step", "tool", "tool_error", "reached_threshold"}
