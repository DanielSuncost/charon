---
name: spikes
description: Timeboxed throwaway investigation — one question, an isolated worktree, an answer backed by evidence, and code that gets deleted rather than merged.
tags: [research, prototyping, timebox]
---

# spikes — buy information, then throw the code away

Use this skill when the blocker is **knowledge, not effort**: can this library do X, is
this design fast enough, does the provider actually return that field, what shape does
this API return, would this refactor be a two-hour job or a two-day one. A spike is a
purchase — you spend a fixed amount of time to buy an answer.

Do **not** use it when you already know how to do the work; that is just implementation
without tests, and it will be reviewed as such.

## The contract

Write these four lines *before* you start. If you cannot fill them in, you do not have a
spike, you have a mood.

```
QUESTION:  the single question, phrased so the answer is yes/no or a number
BUDGET:    wall-clock timebox (30m / 2h / a day — pick one and hold it)
EVIDENCE:  what observation would count as an answer
DISPOSAL:  what happens to the code (default: deleted)
```

One question per spike. A spike that answers three questions answered none of them well.

## Where a spike runs

Never on `master`, never in the main checkout — an abandoned spike that leaves debris in
the working tree costs more than it bought.

```bash
git worktree add ../charon-spike-<slug> -b spike/<slug>
cd ../charon-spike-<slug>
# ... buy the answer ...
cd - && git worktree remove ../charon-spike-<slug> && git branch -D spike/<slug>
```

Lighter-weight options, in order of preference — reach for the smallest one that can
answer the question:

| Question shape | Instrument |
|---|---|
| "What does this object/API actually return?" | `PyKernel` — the kernel is persistent, so hold the response in a variable and poke at it across several calls instead of re-fetching |
| "Does this snippet behave the way I think?" | `ExecuteCode` — isolated subprocess, one shot, no files touched |
| "How fast is this path?" | `ExecuteCode`/`PyKernel` with `time.perf_counter()` around the real function, not a mock of it |
| "Does this design survive contact with the codebase?" | worktree spike, above |
| "Is this feasible at all, and how far off are we?" | worktree spike, plus a written comparison |

Anything long-running goes through `RunProcess`/`ProcessLogs`, not foreground `Bash` —
the Bash tool will refuse it anyway.

## Rules while the clock runs

- **Hack freely.** Hardcode, skip error handling, comment out the parts that get in the
  way. Spike code is not reviewed for quality — the only sin is a *misleading* spike,
  e.g. one that stubs out the very thing that was in question.
- **Do not test-drive a spike.** Tests are for code you keep. (If you find yourself
  wanting tests, the spike is over and implementation has started — see
  `skills/tdd/SKILL.md`.)
- **Stop at the budget.** "Nearly there" at the timebox is a data point, not an extension.
  Either book a second, differently-framed spike or report the question as open.

## Landing a spike

The deliverable is the answer, never the branch:

1. Write the answer in one paragraph: the question, the observation, the number, the
   confidence, and what you did *not* check.
2. Record the durable part where the next agent will find it —
   `Timeline log_decision {what, why, alternatives, topic}` for a decision the spike
   settled, `ProjectKnowledge add` for a project fact that outlives the task
   (build commands, conventions, known dead ends).
3. Delete the worktree and the branch. If some fragment is genuinely worth keeping,
   re-implement it test-first on a real branch — do not merge spike code because it
   "already works".
4. Exploratory write-ups do not belong in this repo. `docs/` carries shipped design and
   finished documentation; keep the notebook elsewhere.

## Honesty rules

- A spike proves feasibility, never correctness. "The spike works" is not evidence a
  feature works, and it must never be reported as a passing test.
- Report the budget you actually spent and what remains unknown — an honest "unresolved
  after 2h" is a useful result; a quiet overrun is not.
- If the spike answered a *different* question than the one you wrote down, say both:
  the question you asked, and the one you ended up answering.
