# Worker Brief W1 — Transparent Checkpoints and Undo

Parent plan: `docs/plans/charon-autonomous-implementation-master-plan.md`
Status: Ready for autonomous implementation

## Objective
Make file/code mutations safe by default and easy to reverse without altering user repo git state.

## Scope
Implement:
- automatic checkpoint creation before file-mutating operations
- checkpoint metadata:
  - agent id/name
  - task/goal summary
  - timestamp
  - working directory
- restore path
- diff inspection path
- “undo last agent action” path
- integration hooks for shades and judge loops

## Must not break
- existing git workflows in user repos
- shade orchestration
- judge loop behavior
- TUI session grid
- Charon’s Boat

## Constraints
- use isolated checkpoint storage (shadow git or equivalent)
- do not leak `.git` state into user project directories
- fail safe on large repos or unsupported repo states
- preserve current behavior when checkpoints are disabled/unavailable

## Required tests
- checkpoint created before write/edit mutation
- restore returns project to pre-mutation state
- diff inspection works on created checkpoints
- metadata is attached and queryable
- repeated mutations do not corrupt checkpoint history
- checkpointing does not alter user repo git config/state

## Acceptance benchmark
- make a change, inspect it, undo it immediately and safely

## Deliverables
1. implementation
2. tests
3. short design note
4. explicit list of unchanged public interfaces
5. known limitations
