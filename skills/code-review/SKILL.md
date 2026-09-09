---
name: code-review
description: Review a Charon diff against the invariants this codebase actually holds, and emit the standard five-field review artifact with evidence for every claim.
tags: [review, quality, evidence]
---

# code-review — review the diff, not the intention

Use this skill before committing your own branch, when reviewing another agent's worktree
branch, at a `/devteam` handoff, and whenever someone asks "is this ready". The output is
always the same artifact, specified in `docs/review-artifact.md`: findings, severity,
evidence, test results, unresolved risks.

Reviewing is not reading. A review that produces no artifact did not happen.

## Get the diff, and its claim

```bash
git log master..HEAD --oneline            # what the branch says it did
git diff --stat master...HEAD             # how big, and where
git diff master...HEAD                    # the actual change
git diff --name-only master...HEAD        # for the scope check, below
```

The `Git` tool (`action: diff | log | status`) does the same thing with truncation
handled. Read the claim first — commit message, task brief, PR body — then the diff, so
you can tell "does what it claims" from "does something else too".

**Two passes, in this order:**
1. *Does it do what it claims?* Walk the stated behaviour through the new code.
2. *What else does it do?* Every hunk that pass 1 did not explain is a question. Renames,
   default changes, widened excepts, and "while I was in here" edits live in pass 2.

**Scope check (multi-agent repo).** Branches here are produced by agents with a declared
write scope. Diff the touched paths against that scope; a file outside it is a finding
before you even read the change.

## Charon invariants worth failing a review over

| # | Invariant | How to check |
|---|---|---|
| 1 | The backend's stdout is a protocol (NDJSON to the Rust TUI), not a log | `git diff master...HEAD -- apps/tui \| grep -n "^+.*print("` — there are currently **zero** raw prints under `apps/tui/opentui/backend/`; a new one corrupts frames and looks like a frontend bug. Use `common.emit({'type': 'status', ...})` |
| 2 | Best-effort `except Exception` must leave a trace | every new swallow calls `charon.infra.diagnostics.record(component, message, error=e, **fields)`; a silent except is a **major** finding, not a nit |
| 3 | A tool is registered in two places | `<NAME>_TOOL_DEF` in `ALL_TOOL_DEFS` **and** the executor in `TOOL_EXECUTORS` (`src/charon/tools/__init__.py:1465`). One without the other is a callable-but-dead tool |
| 4 | Tools return errors, they do not raise | `ToolResult(content=..., is_error=True)`; `input_schema.required` matches the guards in the executor |
| 5 | State dir resolution is uniform | `ctx.state_dir or (ctx.project_root / '.charon_state')` — a hardcoded `~/.charon_state` or `Path('.charon_state')` splits state between callers |
| 6 | Writes to state are atomic | `charon.infra.fileio.write_json_atomic` / `read_json_or_quarantine`; a bare `json.dump` over a live file is a data-loss finding |
| 7 | Shade-facing code honours `ctx.scope` and `ctx.frozen` | new file-writing paths go through the checked helpers, not raw `open(..., 'w')` |
| 8 | Tests are hermetic | `tmp_path` only; no `~/.charon`, `~/.charon_state`, network, or sleeps. New behaviour has a test that was red first (`skills/tdd/SKILL.md`) |
| 9 | Long-running work is managed | `RunProcess`/`ProcessLogs`/`StopProcess`, never a foreground `Bash` daemon |
| 10 | Contract changes move together | `docs/contracts/*.schema.json` + `tests/contracts/`; cross-kernel code (`src/charon/workspace/`, `skills/system-map/`) must keep its parity fixtures passing |
| 11 | The two CI gates are independent | `./.venv/bin/python -m pytest -q` **and** `./.venv/bin/ruff check .` (rules `E4,E7,E9,F,B,PLE` — bugbear catches mutable defaults and unused-except patterns) |
| 12 | User-visible change is documented | `CHANGELOG.md`, and `docs/README.md` if a doc was added |

## Severity and evidence

The ladder and the artifact format are specified in `docs/review-artifact.md`. Two rules
matter more than the rest:

- **Every finding cites evidence**: a `file:line`, a quoted hunk, or a command with its
  output. A claim you cannot evidence is not a finding — move it to unresolved risks as a
  question.
- **Nits are labelled `[nit]`** so they can be ignored without an argument. Do not smuggle
  taste into `[major]`.

Write the artifact, then check it mechanically:

```bash
python3 skills/code-review/review_artifact.py template > review.md   # skeleton
python3 skills/code-review/review_artifact.py check review.md        # exit 1 lists problems
```

## Scaling a review

- **Wide diff** — one `SpawnShade` per dimension, each with a narrow `scope` and an
  `expected_outputs` entry naming the artifact section it owns (e.g. "Findings + Evidence
  for tests/"). Merge their findings into one artifact; do not paste four artifacts.
- **A finding with a number attached** (slow path, flaky test, failing suite) — hand it to
  `SpawnJudgeLoop` with `judge_type: 'correctness'`, `eval_command` the test command, and
  `constraint_commands: ['./.venv/bin/ruff check .']` so a fix that lints dirty is rolled
  back automatically.
- **"How does this subsystem work" questions** during review go to `skills/system-map/`,
  not to guesswork.

## Honesty rules

- Say what you did **not** review — paths you skipped, tests you did not run, layers you
  cannot exercise (the Rust TUI needs `cargo`). That belongs in unresolved risks, always.
- Report the test command you actually ran and its real tail. "Tests pass" without the
  output is not a test result.
- An approval is a claim about evidence you gathered, not a courtesy. If the evidence is
  thin, the decision is "approve with follow-ups" and the thinness is written down.
- Do not review the author. Findings describe code, at a location, with a consequence.
