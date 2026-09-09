---
name: tdd
description: Write the failing test first in Charon's suite — where the test goes, how imports resolve, how to keep it hermetic, and what "green" is allowed to mean.
tags: [testing, pytest, workflow]
---

# tdd — the failing test is the specification

Use this skill for any change to behaviour: a bug fix, a new tool, a new slash command, a
protocol or schema change, a refactor that must preserve behaviour. Skip it only for
changes with no observable behaviour (docs, comments, this file).

The cycle here is cheap enough that skipping it is never a time argument: one test file
runs in ~4s (most of it import), the whole suite in ~85s.

## The cycle, with the real commands

```bash
# 1. RED — write the test, watch it fail for the reason you expect
./.venv/bin/python -m pytest tests/test_<area>.py -q -k <case>

# 2. GREEN — smallest change that passes
./.venv/bin/python -m pytest tests/test_<area>.py -q

# 3. REFACTOR — then the gates, both of them
./.venv/bin/python -m pytest -q            # baseline: 1450 passed, 1 skipped
./.venv/bin/ruff check .                   # CI's lint job fails independently of pytest
```

Read the red output before writing any implementation. A test that fails with
`ImportError` or `AttributeError` has not yet specified anything — make it fail on the
assertion you actually care about.

## Where the test goes

Tests are one flat directory, `tests/test_<area>.py`; there is no package structure to
mirror. `pyproject.toml` puts `src`, `apps/tui`, `apps/tui/opentui` and `.` on
`pythonpath`, so all three of these import styles work from any test:

```python
from charon.tools import ToolContext, execute_tool     # src/charon/**
from chat_backend import ChatBackend                    # apps/tui/opentui/chat_backend.py
from backend.common import emit                         # apps/tui/opentui/backend/common.py
```

Contract/schema fixtures live under `tests/contracts/` and validate against
`docs/contracts/*.schema.json`; multi-process fixtures live in `tests/fixtures/`.

## Patterns to copy, not invent

**A tool test** — build a `ToolContext` on `tmp_path` and call the executor directly
(`tests/test_tools_refine.py:6`):

```python
def _ctx(tmp_path):
    proj = tmp_path / 'proj'
    proj.mkdir(parents=True, exist_ok=True)
    return tools_mod.ToolContext(project_root=proj, agent_id='AG-1', state_dir=tmp_path / 'state')
```

**A command/backend test** — instantiate `ChatBackend()` in-process and monkeypatch the
seam, instead of launching the TUI (`tests/test_automation_chat_backend.py:23`):

```python
monkeypatch.setattr(backend, 'handle_command', fake_handle_command)
backend.handle_chat('every hour check https://example.com', 'req-2')
assert captured['command'] == '/monitor every hour check https://example.com'
```

**Error paths are behaviour.** Tools return `ToolResult(content=..., is_error=True)`
rather than raising, so assert on both fields — `assert r.is_error` and a substring of
`r.content`. A tool that raises instead of returning is itself a bug.

## Hermetic rules (non-negotiable)

- **Never touch `~/.charon` or `~/.charon_state`.** Every state dir is `tmp_path`. A test
  that reads the developer's real home passes on your machine and fails in CI, or worse,
  passes in CI and eats local state.
- No network, no sleeps longer than a tick, no dependence on wall-clock ordering.
- `tests/conftest.py` already forces `CHARON_EMBED_BACKEND=local` and skips the
  embedding-dependent modules when `sentence-transformers` is absent — do not re-set the
  env var per test, and if your new module needs real embeddings, add it to
  `_EMBEDDING_MODULES` there rather than letting it fail in a minimal environment.
- Subprocesses and managed processes must be stopped in the test that started them.

## Adding a tool, test-first

1. A test that calls `execute_tool('<Name>', {...}, ctx)` and asserts `Unknown tool` is
   *not* returned — red, because nothing is registered.
2. Add `<NAME>_TOOL_DEF` (with `input_schema` and `required`) and `execute_<name>` in
   `src/charon/tools/<name>_tool.py`.
3. Register in **both** places in `src/charon/tools/__init__.py`: `ALL_TOOL_DEFS` and
   `TOOL_EXECUTORS`. A def without an executor is a tool the model can call and the
   runtime cannot run; an executor without a def is dead code.
4. Tests for: the happy path, each required-field rejection, and the state-dir fallback
   (`ctx.state_dir or (ctx.project_root / '.charon_state')`).

## Honesty rules

- A test that also passes before your change is not a regression test. Show it red first;
  if you cannot make it red, say so and explain why the change is still safe.
- Do not assert on incidental strings (exact log wording, dict ordering) — that is a test
  that will fail for the next person for no reason.
- Report the suite result you actually ran. "Tests pass" means the full suite plus ruff,
  and the review artifact wants the command and its tail, not the adjective.
