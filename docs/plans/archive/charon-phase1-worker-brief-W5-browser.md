# Worker Brief W5 — Browser and Web Operations

Parent plan: `docs/plans/charon-autonomous-implementation-master-plan.md`
Status: Ready for autonomous implementation

## Objective
Enable reliable browser-based research and real web workflow execution.

## Scope
Implement or strengthen:
- persistent browser session abstraction
- DOM/accessibility-style page inspection
- screenshot capture
- vision-assisted page analysis
- interaction primitives:
  - navigate
  - click
  - type
  - scroll
  - upload
  - download
- login and multi-step flow support
- browser-safe policy controls
- browser activity summarized into task memory/compaction

## Must not break
- existing Browser tool usage unless intentionally versioned
- existing browser cleanup behavior
- TUI/session runtime

## Constraints
- local backend first; remote/cloud backend may be additive
- cleanup must be reliable
- errors must expose enough state for debugging
- preserve backward compatibility where feasible

## Required tests
- navigate and inspect page
- fill and submit multi-step form
- preserve session state across steps
- screenshot + vision path works
- cleanup path does not leak stuck sessions

## Acceptance benchmark
- complete a realistic browser workflow reliably enough for routine use

## Deliverables
1. implementation
2. tests
3. short design note
4. explicit list of unchanged public interfaces
5. known limitations
