# Judge Loop — worked example

A judge loop optimizes code against a measurable signal: it changes the code,
scores it, **keeps improvements and rolls back regressions**, and repeats until
the target is met or a budget is exhausted. The engine primitives live in
`apps/core-daemon/judge_engine.py`; the driver that runs a loop one step per
daemon heartbeat is `apps/core-daemon/judge_loop_driver.py`; checkpoints use a
shadow git repo (`apps/core-daemon/checkpoint_manager.py`) kept separate from
the user's own `.git`.

## A reproducible run

`scripts/judge_loop_example.py` drives the **production** machinery end to end
against a real program (`solver.py`, whose printed number is the metric). The
only stubbed piece is the *implementer* — in production a scoped LLM shade edits
files; here a scripted implementer makes the same kind of real edits so the run
is deterministic and needs no model or API.

```
$ PYTHONPATH=apps/core-daemon python scripts/judge_loop_example.py
tick  action        score   kept   best   BATCH on disk
------------------------------------------------------------
1     baseline      10.0    -      10.0   1
2     iterated      68.0    True   68.0   5
3     iterated      38.0    False  68.0   5
4     iterated      308.0   True   308.0  20
------------------------------------------------------------
converged: reason=target_met best=308.0 rollbacks=1

Final solver.py BATCH (best kept): 20  (no .git in project: True)
```

## What this demonstrates

- **Baseline** is measured by running the real `eval_command` (`python3 solver.py`).
- **Keep on improvement:** tick 2 (`BATCH=5`, score 68) beats the baseline and is kept.
- **Rollback on regression — on real files:** tick 3 set `BATCH=3` (score 38 < 68);
  the loop ran the *actual* program, saw the regression, and **reverted the file via
  shadow git** — note "BATCH on disk" is back to `5`, not `3`. (Rollback also removes
  files an iteration *adds*, not just modifications — see
  `tests/test_judge_loop_driver.py::test_rollback_removes_files_added_in_discarded_iteration`.)
- **Convergence:** tick 4 (`BATCH=20`, score 308 ≥ target 150) is kept and the loop
  stops with `reason=target_met`, **1 rollback** recorded in the iteration history.
- **Hygiene:** no `.git` is created in the project — checkpoints live in a separate
  `GIT_DIR` under the state dir.

The orchestration (baseline → implement → score → keep/rollback → converge) is
covered by `tests/test_judge_loop_driver.py` and `tests/test_judge_loop_integration.py`.
The live path (implementer = real shade) additionally needs a configured provider.
