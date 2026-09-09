# skills/ — Charon's own engineering practice

One directory per skill, each with a `SKILL.md`. These are not documentation about the
project (that is `docs/`); they are the procedures an agent working *in this repo* is
expected to follow, written against real Charon commands, paths and invariants.

| Skill | Use it when |
|---|---|
| [system-map](system-map/SKILL.md) | You need to know how a subsystem works or who owns a file, before changing code you do not own |
| [systematic-debugging](systematic-debugging/SKILL.md) | Something is wrong and you do not yet know the mechanism |
| [tdd](tdd/SKILL.md) | You are about to change observable behaviour |
| [spikes](spikes/SKILL.md) | The blocker is knowledge, not effort — timeboxed, throwaway |
| [code-review](code-review/SKILL.md) | Before committing a branch, or reviewing another agent's |
| [exploratory-qa](exploratory-qa/SKILL.md) | The suite is green and it still feels wrong |

`code-review` and `exploratory-qa` both end in the same deliverable: the five-field review
artifact specified in [`docs/review-artifact.md`](../docs/review-artifact.md), checkable
with `python3 skills/code-review/review_artifact.py check <file>`.

## Format

```markdown
---
name: <directory name>
description: <one line — what the skill is for; this is what a reader sees before opening it>
tags: [three, or, four]
---

# <name> — <tagline>

<when to use it, and when not to>
...
## Honesty rules
<what this skill may not claim>
```

The frontmatter is the contract Charon's own scanner reads
(`charon.memory.assimilation._scan_hermes_skills` walks `skills/**/SKILL.md`, requires
`---` frontmatter, and takes `name`, `description`, `tags`); a `SKILL.md` without it is
skipped entirely. Every skill here ends with an **Honesty rules** section, because a
procedure that produces confident output is only as good as the claims it forbids.

Known wart: `Skills(action='list')` previews the *first line* of a `SKILL.md`
(`src/charon/tools/skills_tool.py`), which for a file with frontmatter is `---`. The
frontmatter is still the right convention — the scanner and this directory depend on it —
but the listing preview is unhelpful until that tool learns to parse it.

Skills that ship executable helpers keep them in the same directory, stdlib-only and
runnable from anywhere (`skills/system-map/*.py`, `skills/code-review/review_artifact.py`).
Helpers carry their own `selftest` subcommand where they have no home in `tests/`.

## Writing a new one

1. It must be **specific to this repo**. Generic advice an agent already knows is noise;
   the value is in the paths, commands, and invariants it would otherwise have to
   rediscover.
2. Show the real command, with the real flags, from the real root.
3. Say what the skill may not claim. Every skill here can be misused to produce confident
   nonsense — name that failure mode.
4. If a skill would be hollow, do not write it.
