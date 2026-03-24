# F00 - Persistent Self-Builder Loop (First Feature)

Objective:
Create a persistent orchestration loop that continuously executes feature work until stopped.

Loop contract:
1. Read active goal + queue
2. Select one task
3. Plan minimal patch
4. Implement in branch
5. Run test gate
6. Evaluate result
7. Commit/rollback
8. Log outcome
9. Repeat

Hard safety rails:
- STOP file: `./CHARON_STOP`
- max_retries_per_task: 3
- max_consecutive_failures: 5
- dry_run mode support
- never merge to main without explicit approval flag

Definition of done:
- Loop survives process restarts
- Writes append-only run log with timestamps
- Demonstrates at least 3 successful task cycles on sample backlog
