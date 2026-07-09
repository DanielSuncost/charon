# Worker Brief W2 — Runtime Reliability and Model Fallback

Parent plan: `docs/plans/charon-autonomous-implementation-master-plan.md`
Status: Ready for autonomous implementation

## Objective
Reduce brittle task failures and preserve continuity when providers/models fail.

## Scope
Implement:
- retry/backoff policy
- failure classification: transient vs terminal
- provider/model fallback path
- degraded-mode behavior with clear status/logging
- health-aware routing defaults

## Must not break
- existing successful provider flows
- shade provider separation
- current non-fallback behavior when fallback is disabled

## Constraints
- fallback decisions must be inspectable in logs/status
- do not silently change model behavior without surfacing it
- preserve task state where fallback is possible
- keep changes local to runtime/provider handling

## Required tests
- transient provider failure retries correctly
- terminal failure triggers fallback when configured
- disabled fallback preserves prior behavior
- fallback path returns intelligible task outcome/state

## Acceptance benchmark
- simulate provider outage and recover without routine task collapse

## Deliverables
1. implementation
2. tests
3. short design note
4. explicit list of unchanged public interfaces
5. known limitations
