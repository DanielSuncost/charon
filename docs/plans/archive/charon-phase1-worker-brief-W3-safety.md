# Worker Brief W3 — Approval and Safety Controls

Parent plan: `docs/plans/charon-autonomous-implementation-master-plan.md`
Status: Ready for autonomous implementation

## Objective
Make risky actions visible and controllable without crippling normal workflows.

## Scope
Implement:
- risk classifier for tool actions
- approval gate for destructive/high-risk actions
- configurable approval modes by agent/project
- policy controls for:
  - shell/file mutation
  - network use
  - secret-sensitive actions/paths
- audit log for gated actions

## Must not break
- low-friction behavior for harmless actions
- existing shade scope enforcement
- existing tool contracts unless version-gated

## Constraints
- persistent agents must be covered, not only shades
- policy behavior must degrade gracefully when unset
- approval UX must be minimal and clear
- reuse shared policy logic; do not create duplicate approval systems

## Required tests
- destructive action is gated in approval mode
- low-risk action remains ungated in normal conditions
- per-project policy override works
- audit log captures enough detail for review

## Acceptance benchmark
- destructive action requires approval while harmless actions remain smooth

## Deliverables
1. implementation
2. tests
3. short design note
4. explicit list of unchanged public interfaces
5. known limitations
