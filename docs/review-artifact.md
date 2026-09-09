# The review artifact — one shape for every review

Every review in this repo produces the same document: **findings, severity, evidence,
test results, unresolved risks**. Human review, agent review, a `SpawnShade` reviewing one
dimension of a diff, an exploratory QA session — same five fields, so results compose
instead of each reviewer inventing a format.

Why fix the shape at all: in a repo where several agents review each other's branches,
the expensive failure is not a missed bug, it is an **unfalsifiable review** — "looks
good", "some concerns", "tests pass" — that cannot be checked, merged, or acted on. Each
of the five fields exists to make one kind of hand-waving impossible.

| Field | Makes this impossible |
|---|---|
| Findings | vague unease with no claim attached |
| Severity | a nit and a data-loss bug reading the same |
| Evidence | a claim nobody can verify or reproduce |
| Test results | "tests pass" asserted rather than run |
| Unresolved risks | silence standing in for completeness |

Produce one when: closing a branch, handing work back through `/devteam`, finishing a
`code-review`, or completing an exploratory QA charter (`skills/code-review/SKILL.md`,
`skills/exploratory-qa/SKILL.md`).

## The five fields

### 1. Findings

A numbered list. One line per finding, each a **claim about code at a location with a
consequence** — not a feeling, not a question.

```
- **F1** [major] Executor registered in ALL_TOOL_DEFS but missing from TOOL_EXECUTORS, so the tool is callable and unrunnable.
```

Format: `- **F<n>** [severity] claim`. If there is nothing to report, write `No findings.`
— an empty section is a broken artifact, not a clean review. A question you cannot turn
into a claim is not a finding; it belongs in unresolved risks.

### 2. Severity

Every finding carries exactly one level, and the section rolls them up and states the
decision.

| Level | Meaning | Disposition |
|---|---|---|
| `blocker` | Wrong or unsafe: data loss, corrupted protocol, secret exposure, suite fails, contract broken | Merge is blocked |
| `major` | A real defect with a plausible trigger, or changed behaviour with no test | Fix before merge |
| `minor` | Genuine but bounded: narrow edge case, misleading error text, dead code | Fix now or file it |
| `nit` | Preference or style, no defect | Non-blocking; may be ignored without argument |

The section ends with `Decision: request changes | approve with follow-ups | approve`. An
approval is a claim about the evidence you gathered — if the evidence is thin, the
decision is "approve with follow-ups" and the thinness goes in unresolved risks.

Do not smuggle taste into `major`. Do not soften a `blocker` because the branch is late.

### 3. Evidence

For each finding id, the proof: a `file:line` locator, the quoted hunk, or a command with
its real output. Enough that the next reader can confirm the finding without repeating
your investigation.

```
**F1** — `src/charon/tools/__init__.py:1465`

$ ./.venv/bin/python -c "from charon.tools import TOOL_EXECUTORS; print('Demo' in TOOL_EXECUTORS)"
False
```

Rules: no finding without evidence; evidence is what you observed, not what you expect
would happen; a mechanism you inferred but did not run is labelled as inference, in the
finding itself.

### 4. Test results

The commands you actually ran and their real tail — never the adjective. At minimum, for
a code change in this repo:

```
$ ./.venv/bin/python -m pytest -q
1450 passed, 1 skipped in 85.37s

$ ./.venv/bin/ruff check .
All checks passed!
```

The two gates are independent (CI runs `lint` and `python-tests` as separate jobs), so
both belong here. If you did not run them, write `Not run: <reason>` — an honest gap is
acceptable, an implied pass is not. Where a fix has a regression test, show it red before
and green after; that is the evidence the test tests anything.

### 5. Unresolved risks

What is still unknown after the review:

- paths, layers, or platforms you did not exercise (the Rust TUI needs `cargo`; a
  provider you have no key for);
- findings you could not reproduce, with what you tried;
- symptoms fixed without an identified mechanism;
- follow-ups the decision depends on.

`None` on its own is invalid. If there genuinely is no residual risk, say what you covered
that makes that true.

## Skeleton and mechanical check

```bash
python3 skills/code-review/review_artifact.py template > review.md   # the skeleton
python3 skills/code-review/review_artifact.py check review.md        # exit 1, problems listed
python3 skills/code-review/review_artifact.py selftest               # the checker's own tests
```

The checker enforces **shape**: the five sections exist, findings parse and carry a
severity from the ladder, every finding id appears in evidence, test results and
unresolved risks are neither empty nor placeholders. Use it as a gate where reviews are
produced automatically — e.g. as a `constraint_commands` entry on a `SpawnJudgeLoop`, or
as the `expected_outputs` contract of a review shade.

## Where the artifact goes

- **On a branch** — commit it next to the work or paste it into the handoff message; the
  branch name and diff range belong in the header.
- **In a workspace** — store it with `ws.put_artifact(text=..., kind='document', title='review: <subject>')`
  and attach it to the work item's acceptance criterion with `ws.attach_evidence(...)`. The
  work-item `pass` guard requires every required criterion to be `passed` with at least one
  evidence artifact, so a review artifact is what actually moves work to done
  (`src/charon/workspace/README.md`).
- **Across agents** — one artifact per review, not one per reviewer: merge shade findings
  into a single document, renumbering ids, so severity counts mean something.

## Honest limits

The checker validates form, never truth. It cannot tell whether a severity is right,
whether the evidence supports the claim, or whether the review looked in the right place.
A conforming artifact is a reviewable review — that is all it claims to be.
