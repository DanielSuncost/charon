# charon-gym

A Gymnasium-style environment over Charon's own machinery, so an external policy
can drive Charon as an episodic, verifier-rewarded environment.

```python
import charon_gym
env = charon_gym.make_env("fix_failing_test", state_dir)
obs = env.reset()
obs, reward, done, info = env.step({"tool": "Edit",
    "params": {"path": "calc.py", "oldText": "return a + b  # BUG", "newText": "return a * b"}})
```

It is an **adapter** (`apps/core-daemon/charon_gym.py`), not a new execution
engine — it reuses the existing pieces:

| Gym concept | Charon machinery |
|---|---|
| `action` | a tool call in Charon's tool vocabulary, dispatched via `tools.execute_tool` |
| `observation` | tool output + task context (goal, scope file contents, step, current reward) |
| `reward` | a `judge_engine` score, signed so higher is better |
| `reset` | re-materialize the fixture + shadow-git baseline (`CheckpointManager`) — deterministic |
| episode end | reward threshold, step budget, or `done` |

## The five tasks (mixed reward types)

| Task | Reward (judge) | Baseline → solved | Gate |
|---|---|---|---|
| `fix_failing_test` | Correctness (pass rate) | 0.5 → **1.0** | `frozen=[check.py]` |
| `maximize_output` | Quantitative (maximize) | 10 → **250** | — |
| `minimize_latency` | Quantitative (minimize) | −100 → **−40** | — |
| `pass_lint` | Correctness (pass rate) | 0 → **1.0** | `frozen=[lint.py]` |
| `improve_quality` | Aesthetic (LLM rubric) | 4 → **8** | — |

The `frozen` gates mean a policy can't "win" `fix_failing_test`/`pass_lint` by
editing the checker (see `docs/reward-hacking-demo.md`).

## Reference runner / proof the loop closes

`scripts/charon_gym_runner.py` drives each task with a trivial scripted (correct)
policy and records the episode traces to `results/charon_gym_episodes.json`. A
real run (codex as the aesthetic judge) closes all five:

```
fix_failing_test   baseline=     0.5  final=     1.0
maximize_output    baseline=    10.0  final=   250.0
minimize_latency   baseline=  -100.0  final=   -40.0
pass_lint          baseline=     0.0  final=     1.0
improve_quality    baseline=     4.0  final=     8.0
```

Example trace (`fix_failing_test`):

```json
[ {"step": 0, "reward": 0.5, "raw": 0.5},
  {"step": 1, "tool": "Edit", "reward": 1.0, "raw": 1.0, "done": true, "reached": true} ]
```

`--episodes K` runs K rollouts of one task and reports the reward distribution —
the multi-rollout mental model, even without distributed infra. The deterministic
tasks are covered by `tests/test_charon_gym.py` (no provider needed).

## What this is, and is NOT — honest scope

**Is:** a real, verifier-guided episodic environment over real coding tasks. The
reward apparatus (judges), the reset (shadow-git), and the action space (the tool
layer) are the same code Charon uses in production. An external policy gets real,
checkable rewards on every task.

**Is NOT:**
- distributed RL training infrastructure (no parallel workers, no replay, no
  scheduler);
- a learning algorithm — there are **no weight updates**; the consumer is an
  external policy (here a scripted one);
- a sandbox — tool execution is **not** OS-isolated. Scope is enforced for
  file tools and `frozen` is enforced at the engine level, but a policy could
  still reach outside the work tree via `Bash`. Real isolation needs the
  containerization workstream; until then, run untrusted policies with care.
- a large benchmark — it is exactly **five** hand-built tasks, by design.
