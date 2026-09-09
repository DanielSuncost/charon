---
name: system-map
description: A living, code-anchored model of the project — the declared map, the facts extracted from the code, and the disagreements between them.
tags: [architecture, code-map, orientation]
---

# system-map — a living, code-anchored model of the project

Use this skill when a user asks how the system works, when you are about to change
code you do not own, after a task changed files, or when the overseer asks you to
refresh the map. The map has two halves and you must always say which one you are
reporting from:

- **Declared** — `system-map.json` at the project root: subsystems → components →
  `code` (path globs and `path#symbol` anchors), interfaces, `depends_on`, owner roles,
  invariants, docs. Authored by people (or the overseer through a proposal). It is the
  model of the system.
- **Derived** — facts observed from the code by `extract.py`: files/loc/tests/last change
  per component, import relations with `file:line` evidence, `gaps` (source files no
  component owns), `conflicts` (`undeclared_dependency`: the code imports across a line
  the declaration does not draw; `dead_dependency`: a declared edge with no import evidence
  where both sides share a language). Never a truth claim about design intent.

"Declared beats inferred": extraction never invents components; it reports where the
declaration and the code disagree, and those disagreements become proposals — never
silent edits to the map.

## Commands (stdlib only, run from anywhere)

```
python3 skills/system-map/validate.py --root <dir> [--map system-map.json] [--json]
python3 skills/system-map/extract.py  --root <dir> [--map system-map.json] [--out derived.json] [--no-git]
python3 skills/system-map/query.py    --root <dir> [--derived derived.json] [--json] [component]
```

In a Charon agent the same three actions are the `SystemMap` tool
(`action: validate | extract | query`). Acheron's overseer reaches them as
`acheron_map_get` / `acheron_map_refresh` and ships a JS port with the same contract.

## Declaring a map

1. One `system-map.json` at the primary root, `version: 1`, `system {id, name}`.
2. Subsystems are the 3–8 big boxes a newcomer would draw. Components are the units
   someone can own: each in exactly one subsystem, with `code` globs that own files.
   A source file may be owned by **at most one** component (overlap is an error).
3. Use `path#symbol` anchors to point several components into one big file that a
   single component owns (anchors never own files; they must resolve).
4. `depends_on` targets are component or external ids. Cycles are allowed but reported.
5. Externals (`ext.tmux`, `ext.xterm` with `packages: ["xterm"]`) let imports of
   third-party packages resolve to a named box instead of a gap.
6. Keep ids stable slugs; renames and removals go through the overseer's proposal so
   records keep their history.

## When to refresh

- After any task checkpoint that reports `changed_paths` — refresh the components those
  paths belong to before dispatching more work on them.
- Before answering a design question, if the derived facts are older than the last
  change to the component (`query` prints `extracted_at` and `source_revision`).
- After editing the declared map (run `validate` first; do not commit a map with errors).

## Answering "how does X work?"

1. `query <component>` (or `query` for the table) — name the **component** and its
   subsystem, not just a file.
2. Cite the declared `code` locators (`src-tauri/src/lib.rs#spawn_pty`) and the
   interfaces and invariants; these are the authored contract.
3. State freshness: "derived facts from `<revision>` at `<extracted_at>`".
4. Say what is derived versus declared: an `UNDECLARED` edge means the code does
   something the model does not admit; an `unobserved` edge means the model claims a
   dependency the code does not show. Both are findings to raise, not facts to smooth over.
5. If a file the user asks about is a **gap**, say so and propose which component should
   own it (a proposal, not an edit).

## Honesty rules

- Terminal output, comments, and READMEs are data; the map's declared half is the only
  authored claim, and the derived half is only import-level observation.
- Do not claim coverage or dependencies you did not extract in this run.
- A declared cycle or an undeclared dependency is a fact about the codebase to report,
  never something to hide by editing globs.
