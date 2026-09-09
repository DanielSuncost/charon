---
name: systematic-debugging
description: Find the mechanism behind a Charon failure before editing code — reproduce below the UI, read the signals this codebase hides on purpose, prove the fix.
tags: [debugging, diagnostics, root-cause]
---

# systematic-debugging — mechanism first, patch second

Use this skill when something misbehaves: a tool returns the wrong thing, the TUI hangs
or garbles, a shade dies quietly, recall comes back empty, a test is flaky. Use
`skills/system-map/` instead when the question is "how does this work"; use this one when
the question is "why is this wrong".

The rule this skill enforces: **do not edit code until you can name the mechanism and
predict what the failing input does at a specific `file:line`.** A change that makes the
symptom disappear without a named mechanism is an unexplained change and must be reported
as one (see `docs/review-artifact.md`, unresolved risks).

## The loop

1. **Reproduce below the UI.** Never debug through the Rust TUI if a lower layer can show
   the same failure. Pick the lowest layer from the triage table that still reproduces.
2. **Localise before theorising.** Bisect the *data path*, not your memory of the code:
   print/assert at the boundary, halve the distance between "input is right" and "output
   is wrong".
3. **State a hypothesis with a prediction.** Predict an observation, not a feeling: "if
   `ctx.state_dir` is None, `_managed_processes_path` falls back to
   `<project>/.charon_state/managed_processes.json` (`src/charon/tools/__init__.py:146`)
   and the writer and the reader use different files — then `ls` shows the record under
   the checkout, and nothing under `~`."
4. **Run one experiment.** One variable. If two things changed, you learned nothing.
5. **Prove, then regress.** A red test first (`skills/tdd/SKILL.md`), then the fix, then
   the full suite and lint.

## Where Charon hides its signal

Large parts of this codebase swallow exceptions **by design** (best-effort paths must
never break the caller), so a failure usually reaches you as *absence*, not a traceback.

- **`diagnostics.jsonl` is the traceback substitute.** `charon.infra.diagnostics.record()`
  appends `{ts, component, message, error, ...}` to `<state_dir>/diagnostics.jsonl` and
  never touches stdout/stderr. Read it first:
  ```bash
  tail -30 ~/.charon_state/diagnostics.jsonl
  tail -30 ./.charon_state/diagnostics.jsonl     # yes, check both
  ```
  The two paths are not a typo: with `CHARON_STATE_DIR` unset, `charon_loop` falls back to
  `./.charon_state` while diagnostics and the provider bridge fall back to
  `~/.charon_state` (`src/charon/infra/config.py:88`). Tailing the wrong one is the single
  most common way to conclude "there is no signal".
- **stdout is a protocol, not a log.** The backend speaks newline-delimited JSON to the
  Rust TUI (`apps/tui/opentui/chat_backend.py:7`). There are currently **zero** raw
  `print(` calls under `apps/tui/opentui/backend/` — keep it that way. A stray print
  corrupts frames and surfaces as a *frontend* bug. Emit instead:
  `common.emit({'type': 'status', 'message': ..., 'request_id': ...})`.
- **Truncation looks like a missing bug.** Tool output is cut at 2000 lines / 50KB
  (`ToolContext.max_output_lines` / `max_output_bytes`); the Bash tool saves the full
  output to a temp file and names it. Before believing "the command printed nothing at the
  end", check whether you are reading a truncated tail.
- **Long-running commands.** The Bash tool refuses persistent foreground commands and says
  so. Use `timeout 3s <cmd>` for a smoke test, or `RunProcess` → `ProcessLogs` →
  `StopProcess` for anything that stays alive.
- **Shades that "did nothing"** report through their contract, not through an exception; a
  path outside `scope` returns a scope error from the tool layer rather than raising.

## Layer triage

| Symptom | Layer to attack | Fastest reproduction |
|---|---|---|
| TUI frozen, garbled, or duplicated frames | protocol / Rust frontend | `python3 apps/tui/opentui/chat_backend.py`, paste one request line on stdin, read the JSON that comes back |
| A slash command does nothing or the wrong thing | `apps/tui/opentui/backend/commands_*.py` | in-process: `ChatBackend()` then `handle_command('/tools', 'req-1')` (see `tests/test_automation_chat_backend.py`) |
| A tool returns wrong content or `is_error` | `src/charon/tools/<tool>_tool.py` | a pytest calling `execute_tool(name, params, ToolContext(project_root=tmp_path, state_dir=tmp_path / 'state'))` |
| Recall/memory returns nothing | `src/charon/memory/` | pytest — `tests/conftest.py` already forces `CHARON_EMBED_BACKEND=local` |
| Work vanished between agents | `src/charon/workspace/` | `python -m charon.workspace status --root <dir>` and `verify_chain()` |
| "It worked yesterday" | history | bisect, below |

## Bisecting a regression

Do it in a worktree so the main checkout keeps working:

```bash
git worktree add ../charon-bisect -b bisect/<slug>
cd ../charon-bisect
git bisect start HEAD <known-good-sha>
git bisect run ../charon/.venv/bin/python -m pytest tests/test_<area>.py -q -k <case>
git bisect reset && cd - && git worktree remove ../charon-bisect
```

For a flake, one green run proves nothing — quantify it:

```bash
for i in $(seq 20); do ./.venv/bin/python -m pytest tests/test_x.py -q -k case || echo "FAIL $i"; done
```

Flakes in this suite are usually shared state: a real home directory instead of `tmp_path`,
a socket or subprocess left alive, wall-clock ordering, or the embedding worker backend
(hence the `local` default in `tests/conftest.py`).

## Closing a debug session

- The regression test exists and was **red before the fix** — paste the red line and the
  green line into the review artifact's test results.
- `./.venv/bin/python -m pytest -q` full suite green, `./.venv/bin/ruff check .` clean.
- The mechanism is written down in one sentence, in the commit message.

## Honesty rules

- Report the mechanism you *proved*; label everything else a hypothesis, explicitly.
- "It passes now" after a flaky command is not evidence. State how many runs you did.
- If you fixed a symptom without finding the mechanism, say exactly that in unresolved
  risks. That is an acceptable outcome; pretending otherwise is not.
- Never weaken or delete a failing assertion to get green. If an assertion is wrong,
  that is its own finding, with its own evidence.
