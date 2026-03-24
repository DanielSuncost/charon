# ADR-0002: F00 Persistent Loop State Machine

Status: accepted
Date: 2026-03-13

## Context
F00 is the foundational self-builder loop. It must run continuously with deterministic safety behavior.

## Decision
The loop is defined as a state machine:

INIT -> LOAD_QUEUE -> SELECT_TASK -> EXECUTE -> EVALUATE -> (COMMIT | RETRY | HALT) -> LOAD_QUEUE

Global interrupts:
- STOP file present => HALT
- max consecutive failures exceeded => HALT

Idle behavior:
- If no pending task, enter IDLE and poll at fixed interval.

## Safety rails
- Stop file: `CHARON_STOP` (configurable)
- max_consecutive_failures (default: 5)
- per-task retries (future: default 3)
- append-only JSONL run log

## Observability requirements
Each cycle emits events:
- loop_start, task_start, task_success/task_failure, loop_idle, loop_halt, loop_exit

## Consequences
Positive:
- deterministic stop behavior
- resumable execution
- easy replay/debug from logs

Costs:
- additional state and test complexity
