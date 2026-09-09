---
name: exploratory-qa
description: Charter-driven manual probing of Charon's real surfaces — headless backend, slash commands, shades, state — with oracles that work when there is no expected output.
tags: [qa, testing, charter]
---

# exploratory-qa — go looking for what the tests cannot fail on

Use this skill when the suite is green and the thing still feels wrong, after a feature
lands, before a release or a demo, or when a bug report is too vague to reproduce. Its job
is to find failures nobody wrote an assertion for: garbled output, a command that silently
no-ops, state written where nothing reads it, a shade that reports success having done
nothing.

This is not "click around". It is timeboxed, chartered, and it produces evidence.

## The charter

One charter, one sitting (30–45 min). Write it before you start:

```
EXPLORE:  <area — e.g. /devteam handoff, provider switch mid-session>
WITH:     <resources — headless backend, tmp state dir, two shades>
TO FIND:  <risk — e.g. work reported complete that never reached disk>
```

Keep a running log as you go: what you did, what you saw, what surprised you. Surprise is
the signal — every "huh" is a candidate finding. Anything you cannot explain by the end of
the session becomes an unresolved risk, not a shrug.

## Driving Charon without the TUI

The Rust TUI needs `cargo` and a terminal; almost nothing worth exploring needs the TUI.

**In-process** (fastest, and how the tests do it — `tests/test_automation_chat_backend.py`):

```python
from chat_backend import ChatBackend           # apps/tui/opentui is on pythonpath
backend = ChatBackend()
backend.handle_command('/tools', 'req-1')
backend.handle_chat('every hour check https://example.com', 'req-2')
```

**Out-of-process, over the real protocol** — the one that catches protocol bugs:

```bash
echo '{"type":"command","command":"/tools","request_id":"req-1"}' | python3 apps/tui/opentui/chat_backend.py
```

No venv and no install needed: the backend bootstraps `src/` onto `sys.path` itself
(`apps/tui/opentui/backend/common.py:26`), which is why the Rust TUI can spawn it with a
bare `python3` (`crates/charon-tui/src/chat.rs:36`). stdin takes `chat` / `command` /
`refresh` / `abort`; stdout answers with one JSON object per line — a `refresh` frame at
startup, then `chat_delta`, `tool_call`, `tool_result`, `turn_complete`, `error`, `status`
(full table at the top of `apps/tui/opentui/chat_backend.py`). For an interactive session
start it under `RunProcess` so you can `ProcessLogs` and `StopProcess` it cleanly.

**Mind the state dir.** The backend's state is `<repo root>/.charon_state`, resolved at
import (`backend/common.py:28`); `CHARON_STATE_DIR` does *not* move it. An exploratory
session therefore mutates the checkout you launch it from — it creates agents, contracts
and cron entries. Run from a throwaway worktree (`git worktree add ../charon-qa-<slug>`),
or in-process point `common.STATE_DIR` at a temp dir before you touch anything, the way
the tests do.

## Oracles — how you know it is wrong with no expected output

| Oracle | What violates it |
|---|---|
| **Protocol shape** | anything on backend stdout that is not one JSON object per line; a `request_id` that never gets a `turn_complete`; deltas after completion |
| **Silent degradation** | new lines in `<state_dir>/diagnostics.jsonl` during the session — read them, they are the failures nobody raised |
| **Liveness** | the process is still alive and responsive after the run (`ProcessStatus`); no orphan subprocess or socket left behind |
| **Persistence** | what the UI claimed happened is on disk: agent, contract, cron, memory row. Claimed-but-absent is the highest-value bug class here |
| **Idempotence** | the same command twice — second run must not duplicate records or half-apply |
| **Reversibility** | `/abort` and `/interrupt` mid-turn leave a consistent state, not a wedged one |
| **Consistency** | two paths to the same fact agree (a command's summary vs the state file it claims to describe) |

## Heuristics that pay off in this codebase

- **Interrupt everything.** Abort mid-tool-call, mid-stream, mid-shade.
- **Boundaries of output.** Tool results truncate at 2000 lines / 50KB — run something that
  produces more and check what the UI and the transcript do with the tail.
- **Empty, huge, and hostile inputs.** Empty message, 10k-character message, emoji and RTL
  text, a path with spaces, a command with a missing argument (`/devteam` with no goal).
- **Missing preconditions.** No state dir, no network, no provider configured, no `cargo`.
  Charon is full of best-effort fallbacks; make each one actually fall back.
- **Concurrency.** Two shades on overlapping scopes, a `/devteam` while another run is
  live, a second backend on the same state dir.
- **Fresh vs resumed.** A brand-new state dir behaves differently from a resumed session;
  explore both, they are different products.

## What a session produces

Every finding gets: repro steps someone else can follow, the observation (paste the JSON
line, the log excerpt, the diagnostics record), and the state after. Then fold them into
the standard artifact — `docs/review-artifact.md`, checked with
`python3 skills/code-review/review_artifact.py check <file>`. A charter that found nothing
is still reported: it is coverage, and coverage is the only thing this activity can honestly
claim.

Anything reproducible that survives the session should leave a **failing test** behind
(`skills/tdd/SKILL.md`); exploratory testing that never turns into an assertion will find
the same bug again next quarter.

## Honesty rules

- Exploration shows the presence of bugs, never their absence. Never write "QA passed" —
  write which charters ran and what they covered.
- Report unreproducible observations as unreproducible, with what you tried. Do not delete
  them and do not upgrade them.
- Distinguish "the feature is wrong" from "I held it wrong": if you cannot tell, that is an
  unresolved risk with the exact steps attached.
- Say which layers you could not exercise this session, and why.
