# F00 v1 Implementation Spec - Persistent Self-Builder Runtime

## Scope
Define production-ready baseline for the persistent builder loop used by Charon.

## Runtime contracts

### Inputs
- Queue file / DB table containing tasks with statuses: pending, in_progress, completed, failed, blocked
- Config: polling interval, stop-file path, failure thresholds

### Outputs
- Updated task statuses
- Append-only event log (JSONL)
- Per-cycle summary metrics (success rate, idle ratio, retry count)

## Required behavior
1. On startup, emit loop_start.
2. If stop file exists, emit loop_stop_file_detected then loop_exit.
3. If no pending tasks, emit loop_idle and sleep.
4. For selected task:
   - mark in_progress
   - execute worker
   - evaluate result
   - emit success/failure event
5. On max failure threshold, emit loop_halt and exit.

## Failure handling
- Timeout => task failure with explicit error field
- Worker crash => task failure with captured stderr if available
- Corrupt queue => quarantine strategy (future) + immediate alert event

## Test plan
- Unit: queue load/save, event formatting, stop-file detection
- Integration: full loop over synthetic queue with 2 success tasks
- Failure: worker timeout path + threshold halt
- Recovery: restart loop and verify continuation from persisted queue state

## Non-goals for v1
- distributed workers
- speculative branching
- merge automation

## Exit criteria
- loop survives restart
- stop-file halt works consistently
- events are machine-parseable and complete
- tests pass in CI
