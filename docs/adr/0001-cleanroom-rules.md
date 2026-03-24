# ADR-0001: Cleanroom Rewrite Rules for Charon

Status: accepted
Date: 2026-03-13

## Context
Charon is a cleanroom project inspired by Hermes concepts but with a different mission:
persistent research and long-horizon task execution across multiple concurrent projects.

## Decision
We adopt strict cleanroom rules:
1. No direct copy-paste of Hermes implementation code into Charon runtime.
2. Behavior-level inspiration is allowed; implementation must be original.
3. Every major subsystem (orchestration, queueing, memory, UI) gets explicit contracts first.
4. First-class reliability requirements: resumability, stop switches, test gates, audit logs.
5. Local-first runtime support via opencode + LM Studio integration.

## Consequences
Positive:
- clear legal and maintenance boundaries
- architecture tuned for Charon mission
- easier future relicensing and contributor onboarding

Costs:
- slower than direct copy
- requires explicit design documents and tests before implementation

## Enforcement
- PR checklist requires "cleanroom compliance" attestation.
- Architectural PRs require ADR reference.
