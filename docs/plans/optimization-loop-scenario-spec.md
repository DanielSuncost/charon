# Optimization Loop — Concrete Scenario Spec

> What happens when a user says "run an Autoresearch-style optimization on my RL training code"

**Status**: Draft  
**Date**: 2026-03-24  
**Parent plan**: `procedure-learning-and-optimization-loops.md`

---

## The Scenario

The user has an RL training codebase at `/home/user/Projects/rl-trainer/`:

```
rl-trainer/
├── train.py          # PPO training loop — the file to optimize
├── evaluate.py       # Runs eval, prints reward metrics
├── environment.py    # Gym environment wrapper (frozen)
├── model.py          # Policy network (fair game)
├── config.yaml       # Hyperparameters (fair game)
└── tests/
    └── test_train.py # Sanity checks (must keep passing)
```

The user says:

> "I want to optimize my RL trainer. The metric is mean episode reward from
> evaluate.py — higher is better. Each training run takes about 3 minutes.
> Only touch train.py, model.py, and config.yaml. Run overnight — 100
> iterations max. Don't break the tests."

---

## Step-by-Step Flow

### Step 1: User Creates the Goal

The user can do this conversationally or via command. The agent (charon-01)
parses the intent and creates an optimization goal.

**What the agent does:**

```python
goal = goal_runtime.create_optimization_goal(
    state_dir=state_dir,
    agent_id="AG-0005",
    project="/home/user/Projects/rl-trainer",
    title="Optimize RL trainer reward",
    mode="optimize",
    metric={
        "name": "mean_episode_reward",
        "direction": "higher",             # "higher" or "lower"
        "extract_cmd": "python evaluate.py 2>&1 | grep 'mean_reward:' | awk '{print $2}'",
    },
    run_cmd="python train.py",             # how to run one iteration
    run_timeout=600,                        # 10 min max per run (3 min expected + buffer)
    scope=["train.py", "model.py", "config.yaml"],
    frozen=["environment.py", "evaluate.py", "tests/"],
    budget={
        "max_iterations": 100,
        "max_consecutive_failures": 5,     # stop if 5 in a row fail
        "max_wall_hours": 12,              # hard time limit
    },
    constraints=[
        "All tests must pass: pytest tests/ -x",
        "Don't add new dependencies",
        "Don't modify environment.py or evaluate.py",
        "Keep changes minimal and focused — one idea per iteration",
    ],
    program="""
    You are optimizing a PPO reinforcement learning trainer.
    The goal is to maximize mean episode reward.
    
    Ideas to explore (in rough priority order):
    - Learning rate schedules (cosine, warmup+decay, cyclical)
    - GAE lambda tuning
    - Entropy coefficient scheduling
    - Network architecture (layer sizes, activation functions)
    - Batch size and minibatch size
    - Number of PPO epochs per update
    - Gradient clipping thresholds
    - Reward normalization/clipping
    - Orthogonal initialization
    - Value function loss coefficient
    
    After ~20 iterations, review what's working and shift focus.
    Combine successful changes. Try removing things that don't help.
    """,
)
```

**What gets stored** (in goal_runtime, persisted to SQLite + JSON):

```json
{
    "goal_id": "goal-opt-a1b2c3",
    "title": "Optimize RL trainer reward",
    "mode": "optimize",
    "status": "confirmed",
    "metric": {
        "name": "mean_episode_reward",
        "direction": "higher",
        "extract_cmd": "python evaluate.py 2>&1 | grep 'mean_reward:' | awk '{print $2}'"
    },
    "run_cmd": "python train.py",
    "run_timeout": 600,
    "scope": ["train.py", "model.py", "config.yaml"],
    "frozen": ["environment.py", "evaluate.py", "tests/"],
    "budget": {
        "max_iterations": 100,
        "max_consecutive_failures": 5,
        "max_wall_hours": 12
    },
    "constraints": ["All tests must pass: pytest tests/ -x", "..."],
    "program": "You are optimizing a PPO reinforcement...",
    "baseline": null,
    "best_metric": null,
    "best_checkpoint": null,
    "current_iteration": 0,
    "consecutive_failures": 0,
    "iteration_history": [],
    "started_at": null,
    "project": "/home/user/Projects/rl-trainer"
}
```

### Step 2: Agent Confirms and Hands Off

The agent responds:

> "Created optimization goal `goal-opt-a1b2c3`. Here's the plan:
> - **Metric**: mean_episode_reward (higher is better)
> - **Run command**: `python train.py` (~3 min per run)
> - **Eval command**: `python evaluate.py` (extracts mean_reward)
> - **Scope**: train.py, model.py, config.yaml
> - **Budget**: 100 iterations, 12 hour wall limit
> - **Safety**: tests must pass, checkpoints before each change
> 
> I'll establish a baseline first, then start iterating. You can check
> progress anytime with `/goal status goal-opt-a1b2c3` or just ask me.
> Starting now."

The goal transitions to `status: "executing"`.

### Step 3: Daemon Picks Up the Optimization Goal

In `charon_loop.py`, the autonomous tick detects an `optimize` mode goal in
`executing` state. Instead of creating a single task, it enters the
optimization loop:

```python
# In charon_loop.py autonomous tick
if goal.get('mode') == 'optimize' and goal.get('status') == 'executing':
    from optimization_loop import OptimizationLoop
    loop = OptimizationLoop(goal, state_dir, agent_id)
    # This runs the next iteration (non-blocking — one iteration per tick)
    loop.tick()
```

The loop is **tick-driven**, not a blocking while-loop. Each daemon heartbeat
advances the optimization by one step. This keeps the daemon responsive and
lets the user interrupt at any time.

### Step 4: Baseline Run (Iteration 0)

**4a. Checkpoint the starting state:**

```python
checkpoint_mgr.snapshot(
    working_dir="/home/user/Projects/rl-trainer",
    label="optimization-baseline",
    scope=["train.py", "model.py", "config.yaml"]
)
```

**4b. Run the training + evaluation:**

```bash
cd /home/user/Projects/rl-trainer
python train.py > .charon_run.log 2>&1        # 3 min
python evaluate.py 2>&1 | grep 'mean_reward:' | awk '{print $2}'
# Output: 142.7
```

**4c. Record baseline:**

```json
{
    "baseline": 142.7,
    "best_metric": 142.7,
    "best_checkpoint": "cp-baseline-abc123",
    "current_iteration": 0,
    "iteration_history": [{
        "iteration": 0,
        "type": "baseline",
        "metric": 142.7,
        "kept": true,
        "checkpoint_id": "cp-baseline-abc123",
        "timestamp": "2026-03-24T22:00:00Z"
    }]
}
```

### Step 5: Iteration Loop (Iterations 1..N)

Each iteration follows a 6-step cycle. One iteration per daemon tick.

#### 5a. Pre-flight Checks

```python
def _preflight(self) -> bool:
    # Budget exhausted?
    if self.goal['current_iteration'] >= self.budget['max_iterations']:
        return self._finish("budget_exhausted")
    
    # Too many consecutive failures?
    if self.goal['consecutive_failures'] >= self.budget['max_consecutive_failures']:
        return self._finish("consecutive_failures")
    
    # Wall time exceeded?
    elapsed_hours = (now - self.goal['started_at']).total_seconds() / 3600
    if elapsed_hours >= self.budget['max_wall_hours']:
        return self._finish("time_limit")
    
    return True
```

#### 5b. Spawn Iteration Shade

A shade agent is created for this single iteration. It gets a focused contract:

```python
contract = shade_orchestrator.create_contract(
    state_dir=state_dir,
    parent_agent_id=agent_id,
    shade_agent_id=shade.id,
    project=goal['project'],
    goal=f"Optimization iteration {iteration}",
    scope=goal['scope'],
    constraints=goal['constraints'],
    phase_specs=[
        {
            'name': 'analyze',
            'objective': _build_analyze_prompt(goal),
        },
        {
            'name': 'implement',
            'objective': _build_implement_prompt(goal),
        },
    ],
)
```

**The analyze prompt includes:**

```markdown
# Optimization Iteration {N}

## Program
{goal.program}

## Current State
- Baseline: 142.7
- Current best: 187.3 (iteration 12)
- Last 5 iterations:
  - #15: entropy_coeff 0.005→0.003, reward=185.1, DISCARDED
  - #14: added cosine LR decay, reward=187.3, KEPT ✓
  - #13: batch_size 64→128, reward=182.9, DISCARDED
  - #12: GAE lambda 0.95→0.98, reward=187.3, KEPT ✓  
  - #11: gradient clip 0.5→0.3, reward=180.2, DISCARDED

## Relevant Procedures (auto-retrieved)
{procedures from memory_engine.recall()}

## Your Task
Propose ONE focused change. Explain your reasoning.
Do NOT repeat changes that were already tried and discarded.
Consider combining successful past changes with a new idea.
```

**The implement prompt:**

```markdown
Make exactly ONE focused change to files in scope: {scope}.
After making the change, verify syntax: python -c "import train"
Keep the change minimal. Commit with a descriptive message.
```

#### 5c. Shade Executes

The shade runs through its two phases:

1. **Analyze**: Reads the current code, reviews history, proposes a change
2. **Implement**: Makes the edit, verifies it doesn't crash on import

The shade completes and reports what it changed.

#### 5d. Checkpoint + Run + Measure

After the shade completes, the optimization loop (running in the daemon) takes over:

```python
# Checkpoint before we test (so we can rollback)
cp_id = checkpoint_mgr.snapshot(
    label=f"iter-{iteration}-pre-test"
)

# Run tests first (constraint check)
test_result = run_command("pytest tests/ -x", timeout=120)
if test_result.exit_code != 0:
    # Tests broke — rollback immediately
    checkpoint_mgr.rollback(cp_id)
    record_iteration(status="test_failure", kept=False)
    goal['consecutive_failures'] += 1
    return

# Run training
train_result = run_command(goal['run_cmd'], timeout=goal['run_timeout'])
if train_result.exit_code != 0:
    checkpoint_mgr.rollback(cp_id)
    record_iteration(status="crash", kept=False)
    goal['consecutive_failures'] += 1
    return

# Extract metric
metric_output = run_command(goal['metric']['extract_cmd'], timeout=60)
new_metric = parse_metric(metric_output.stdout)
```

#### 5e. Keep or Discard

```python
if goal['metric']['direction'] == 'higher':
    improved = new_metric > goal['best_metric']
else:
    improved = new_metric < goal['best_metric']

if improved:
    goal['best_metric'] = new_metric
    goal['best_checkpoint'] = cp_id
    goal['consecutive_failures'] = 0
    status = "kept"
else:
    checkpoint_mgr.rollback(cp_id)
    status = "discarded"
```

#### 5f. Record and Continue

```python
goal['iteration_history'].append({
    "iteration": iteration,
    "change_summary": shade_report.summary,     # from shade's report
    "metric_before": goal['best_metric'] if status == "discarded" else prev_best,
    "metric_after": new_metric,
    "status": status,                            # kept|discarded|crash|test_failure
    "kept": status == "kept",
    "checkpoint_id": cp_id,
    "shade_contract_id": contract.id,
    "duration_seconds": elapsed,
    "timestamp": now_iso(),
})

goal['current_iteration'] = iteration

# Auto-capture procedure from kept iterations
if status == "kept":
    procedure_extractor.capture_from_optimization(
        memory_engine=memory_engine,
        change_summary=shade_report.summary,
        metric_improvement=new_metric - prev_best,
        goal_context=goal['program'],
    )
```

### Step 6: User Checks Progress

At any point, the user can ask:

> "How's the optimization going?"

The agent queries the goal and responds:

> **Optimization: RL trainer reward** (`goal-opt-a1b2c3`)
> 
> | | Value |
> |---|---|
> | Iterations | 23 / 100 |
> | Baseline | 142.7 |
> | Current best | 194.2 (iteration 19) |
> | Improvement | +36.1% |
> | Kept | 8 / 23 |
> | Crashes | 2 |
> | Wall time | 2h 14m / 12h |
> 
> **Last 5 iterations:**
> | # | Change | Reward | Status |
> |---|--------|--------|--------|
> | 23 | Reduce entropy coeff to 0.001 | 189.4 | discarded |
> | 22 | Orthogonal init for policy net | 191.0 | discarded |
> | 21 | Increase PPO epochs 4→6 | 190.8 | discarded |
> | 20 | Add reward normalization | 193.1 | discarded |
> | 19 | Cosine LR + GAE 0.98 + larger net | 194.2 | **kept** ✓ |
> 
> Looks like we're plateauing — last 4 attempts haven't beaten iteration 19.
> The big wins came from LR scheduling and GAE tuning. Want me to adjust
> the program to focus on architecture changes?

### Step 7: User Steers (Optional)

The user can:

- **Update the program**: "Focus on architecture changes now. Try transformer-based policies."
- **Pause**: "Pause the optimization, I need to change evaluate.py"
- **Resume**: "Ok, resume"
- **Stop**: "Stop the optimization, iteration 19 is good enough"
- **Extend budget**: "Give it 50 more iterations"

These modify the goal in-place. The next daemon tick picks up the changes.

### Step 8: Completion

The loop ends when any budget is exhausted. The goal transitions to `completed`:

```python
def _finish(self, reason: str):
    goal['status'] = 'completed'
    goal['completion_reason'] = reason  # budget_exhausted|time_limit|user_stopped|...
    goal['completed_at'] = now_iso()
    
    # Restore best checkpoint as the final state
    if goal['best_checkpoint']:
        checkpoint_mgr.rollback(goal['best_checkpoint'])
    
    # Capture final procedure
    memory_engine.add(
        json.dumps({
            "type": "procedure",
            "trigger": f"optimize {goal['title']}",
            "summary": f"Best result: {goal['best_metric']} (iteration {best_iter})",
            "key_changes": [h for h in goal['iteration_history'] if h['kept']],
            "program_used": goal['program'],
        }),
        category="procedure",
        container_tag=project_tag,
    )
```

The agent reports final results:

> **Optimization complete** — stopped after 100 iterations (budget exhausted).
> 
> - **Baseline**: 142.7 → **Best**: 194.2 (+36.1%)
> - **Winning changes**: cosine LR schedule, GAE lambda 0.98, 256→512 hidden units
> - **Working directory restored to best checkpoint** (iteration 19)
> - **Procedure saved** — next time you optimize an RL trainer, I'll start with these insights

---

## State Machine

```
                    ┌──────────┐
         user says  │          │
        "optimize"  │ created  │
                    │          │
                    └────┬─────┘
                         │ agent confirms
                    ┌────▼─────┐
                    │confirmed │
                    └────┬─────┘
                         │ daemon picks up
                    ┌────▼─────┐
                    │executing │◄──────────────────┐
                    └────┬─────┘                   │
                         │                         │
                    ┌────▼──────┐    metric ok?     │
                    │ iteration │───yes──► keep ────┤
                    │  (shade)  │                   │
                    └────┬──────┘───no───► discard ─┤
                         │                         │
                         │ crash ──► rollback ──────┘
                         │
                    budget exhausted / user stopped / failures
                         │
                    ┌────▼─────┐
                    │completed │  ← restore best checkpoint
                    └──────────┘    capture procedure
```

---

## What Runs Where

| Component | Runs on | Model |
|-----------|---------|-------|
| User conversation (goal creation, status checks, steering) | Main agent (charon-01) | Claude (Anthropic API) |
| Optimization loop tick (preflight, checkpoint, run, measure, keep/discard) | Daemon (`charon_loop.py`) | No LLM — pure orchestration code |
| Iteration shade (analyze + implement) | Shade agent | Configured shade model (currently local Qwen 27B) |
| Training run (`python train.py`) | Subprocess | N/A — user's training code |
| Evaluation (`python evaluate.py`) | Subprocess | N/A — user's eval code |

**Cost profile**: The LLM cost is one shade invocation per iteration (~2 phases,
analyze + implement). With a local model (Qwen 27B on LM Studio), the LLM cost
is zero. The main cost is compute time for the training runs.

---

## Differences from Raw Autoresearch

| Aspect | Autoresearch | Charon Optimization |
|--------|-------------|---------------------|
| Agent model | Claude Code (one long session) | Shade per iteration (fresh context) |
| Context management | Single conversation grows forever | Each shade gets focused context: program + history summary + relevant procedures |
| Checkpoint | Git on the user's repo | Shadow git (doesn't pollute user's repo) |
| Steering | User interrupts and re-prompts | Goal fields updated in-place, next tick picks up |
| Learning | None — each run is independent | Procedures captured, retrieved for future optimizations |
| Multi-project | Single repo | Goal system spans projects, procedures transfer |
| Orchestration | Agent runs `train.py` itself | Daemon orchestrates: shade modifies code, daemon runs/measures |
| Crash recovery | Agent tries to fix | Rollback + next iteration. Daemon survives shade crashes |
| Visibility | Scroll through conversation | Structured iteration history, status queries |

---

## Open Questions

1. **Shade model quality**: Is local Qwen 27B good enough to propose meaningful
   RL optimizations? May need the tier system working — optimization iterations
   should use `strong` tier. This connects to the "tiers should auto-set" issue.

2. **Metric parsing robustness**: Grepping for numbers in command output is
   fragile. Should we require a structured output format (JSON line)?
   e.g. `{"metric": "mean_reward", "value": 194.2}`

3. **Run isolation**: Should training runs happen in the user's actual directory,
   or in a git worktree / temp copy? Worktree would give better isolation but
   adds complexity (paths may break).

4. **Parallelism**: Autoresearch is serial (one experiment at a time, one GPU).
   Should we support parallel iterations on multi-GPU setups? (e.g. 3 shades
   propose 3 changes, run 3 experiments on 3 GPUs, keep the best). This maps
   to batch_orchestrator, but is a stretch goal.

5. **How much history in the shade prompt**: Full iteration history gets long.
   Summary of last N + "key wins" list? Or use memory_engine.recall() to
   find the most relevant past iterations?

6. **Procedure granularity**: Should each kept iteration become its own procedure,
   or should the whole optimization run be one procedure? Probably: individual
   kept changes are "technique" procedures, the full run is a "program" procedure.
