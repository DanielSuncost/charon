# Procedure Learning & Optimization Loops

> Unified system for procedural memory and autonomous optimization.

**Status**: Draft  
**Author**: charon-01  
**Date**: 2026-03-24  

---

## Motivation

Charon agents today have strong **declarative memory** (facts, preferences, knowledge)
but no **procedural memory** (how to do things). Every time an agent encounters a
familiar task type, it figures out the approach from scratch.

Meanwhile, Karpathy's Autoresearch demonstrates a tight optimization loop:
modify → run → measure → keep/discard → repeat. This is procedural learning
with a quantitative feedback signal.

These aren't two separate problems. Optimization loops consume procedures
(the "program" that governs what to try) and produce procedures (successful
experiments become reusable knowledge). We should build one system that handles both.

---

## Design Principles

1. **Build on what exists** — MemoryEngine, goal_runtime, shade_orchestrator,
   task_summarizer, context_assembler are all integration points, not rewrites.
2. **Procedures are memories** — stored in MemoryEngine with `category="procedure"`,
   retrieved by semantic similarity, versioned automatically.
3. **Optimization is a goal mode** — not a separate system. A goal with a metric,
   budget, and program becomes an optimization loop executed via shades.
4. **Gradual sophistication** — three phases, each independently useful.

---

## Phase 1: Procedural Memory

**Goal**: Agents learn and reuse multi-step approaches automatically.  
**Effort**: ~2-3 days  
**Dependencies**: None (all infrastructure exists)

### 1.1 Procedure Data Model

Add to MemoryEngine — no schema changes needed, just conventions on the
`category="procedure"` tier:

```python
# Stored as structured content in MemoryEngine
{
    "type": "procedure",
    "trigger": "deploy a FastAPI app to production",   # when to use this
    "steps": [                                          # what to do
        "Check for Dockerfile, create if missing",
        "Run tests with pytest",
        "Build docker image with tag from git sha",
        "Push to registry",
        "Update deployment manifest",
        "Apply with kubectl and wait for rollout"
    ],
    "tools_used": ["Bash", "Read", "Write", "Edit"],
    "pitfalls": ["Must run tests before build — broken builds waste 10 min"],
    "outcome": "success",
    "source_task": "task-abc123",
    "times_used": 0,
    "times_succeeded": 0,
    "project_scope": "my-api"                          # optional: project-specific
}
```

Stored via `memory_engine.add(json.dumps(procedure), category="procedure")`.
The vector embedding captures the semantic meaning of `trigger` + `steps` for retrieval.

### 1.2 Auto-Capture Hook

**File**: `apps/core-daemon/task_summarizer.py`

After `summarize_fast()` runs, check if the task had ≥5 tool calls and succeeded.
If so, extract a procedure:

```python
def maybe_extract_procedure(
    *,
    instruction: str,
    tool_calls: list[dict],
    response_text: str,
    total_turns: int,
) -> dict | None:
    """Extract a procedure from a successful complex task."""
    if total_turns < 5:
        return None

    # Group tool calls into logical steps
    steps = _extract_steps(tool_calls)
    tools = list({tc.get('tool', '') for tc in tool_calls})
    pitfalls = _extract_errors_recovered(tool_calls)  # errors that were overcome

    return {
        "type": "procedure",
        "trigger": instruction[:200],
        "steps": steps,
        "tools_used": tools,
        "pitfalls": pitfalls,
        "outcome": "success",
        "times_used": 0,
        "times_succeeded": 0,
    }
```

The calling code in the daemon stores this via MemoryEngine if it's not a
near-duplicate (MemoryEngine already deduplicates at `DEDUP_THRESHOLD=0.95`).

### 1.3 Auto-Retrieve and Inject

**File**: `apps/core-daemon/context_assembler.py`

Before assembling context for a new task, recall matching procedures:

```python
def _recall_procedures(memory_engine, task_text: str, limit: int = 3) -> list[str]:
    """Retrieve relevant procedures for the current task."""
    result = memory_engine.recall(
        task_text,
        category="procedure",
        limit=limit,
    )
    return [m.memory.content for m in result.memories if m.score > 0.45]
```

Inject into the system prompt as a new layer (between Layer 4 working memory
and Layer 5 goal context):

```
═══ RELEVANT PROCEDURES ═══
These approaches worked for similar tasks before. Use as guidance, not gospel.

[Procedure 1: deploy a FastAPI app...]
[Procedure 2: ...]
```

### 1.4 Procedure Tool

Let agents manually create/edit procedures (like Hermes's `skill_manage`):

**File**: `apps/core-daemon/tools/procedure_tool.py`

```
Tool: Procedure
Actions: create, update, list, recall
```

- `create`: Agent writes a procedure after a complex task (prompted by system prompt guidance)
- `update`: Agent patches a procedure when it finds a step is wrong/outdated
- `list`: Show all procedures (name + trigger only, progressive disclosure)
- `recall`: Semantic search for procedures matching a query

This supplements auto-capture — agents can also explicitly codify knowledge.

### 1.5 Outcome Tracking

When a procedure is retrieved and the task completes, update the procedure:

```python
procedure["times_used"] += 1
if task_succeeded:
    procedure["times_succeeded"] += 1
```

This uses MemoryEngine's existing versioning (`version`, `parent_id`). The
updated procedure replaces the old one, creating a version chain.

Procedures with low success rates (< 50% after 3+ uses) get a warning
annotation. Procedures unused for 30+ days get `forget_after` set.

### Deliverables
- [ ] `procedure_extractor.py` — extracts procedures from tool call traces
- [ ] Hook in `task_summarizer.py` → auto-capture after complex tasks
- [ ] Hook in `context_assembler.py` → auto-retrieve before each task
- [ ] `tools/procedure_tool.py` — manual CRUD tool for agents
- [ ] System prompt guidance in `system_prompt_builder.py` Layer 4.5
- [ ] Outcome tracking on procedure retrieval → usage

---

## Phase 2: Checkpoint/Rollback

**Goal**: Safe filesystem snapshots so optimization iterations can be cleanly reverted.  
**Effort**: ~1-2 days  
**Dependencies**: None (standalone)

This is a prerequisite for optimization loops. When an iteration makes things
worse, we need to revert cleanly.

### 2.1 Shadow Git Manager

**File**: `apps/core-daemon/checkpoint_manager.py`

Direct port of the Hermes approach (proven design):

```python
class CheckpointManager:
    """Shadow git repo for transparent filesystem snapshots.
    
    Creates a bare git repo at .charon_state/checkpoints/{hash(dir)}/
    that tracks changes without polluting the user's project.
    Uses GIT_DIR + GIT_WORK_TREE separation.
    """
    
    def snapshot(self, label: str = "") -> str:
        """Create a checkpoint. Returns checkpoint ID (commit sha)."""
        
    def rollback(self, checkpoint_id: str) -> bool:
        """Restore working directory to a previous checkpoint."""
        
    def list_checkpoints(self, limit: int = 20) -> list[dict]:
        """List recent checkpoints with labels and timestamps."""
        
    def diff(self, checkpoint_id: str) -> str:
        """Show diff between current state and a checkpoint."""
```

### 2.2 Integration Points

- **Shade orchestrator**: Auto-snapshot before each shade phase starts
- **Optimization loop** (Phase 3): Snapshot before each iteration, rollback on regression
- **Agent tool**: Optional `Checkpoint` tool for manual save/restore

### Deliverables
- [ ] `checkpoint_manager.py` — shadow git implementation
- [ ] Auto-snapshot hook in shade phase transitions
- [ ] `tools/checkpoint_tool.py` — optional agent-facing tool

---

## Phase 3: Optimization Loops

**Goal**: Autonomous modify→run→measure→keep/discard cycles for any measurable goal.  
**Effort**: ~3-4 days  
**Dependencies**: Phase 1 (procedures), Phase 2 (checkpoints)

### 3.1 Metric-Aware Goals

Extend the goal data model in `goal_runtime.py`:

```python
def _goal_node(..., mode: str = 'standard', **kwargs) -> dict:
    goal = {
        # ... existing fields ...
        
        # New fields for optimization mode
        'mode': mode,                    # 'standard' | 'optimize' | 'explore'
        'metric': None,                  # e.g. "pytest tests/ pass rate"
        'metric_cmd': None,              # e.g. "pytest tests/ --tb=no -q"
        'budget': None,                  # e.g. {"max_iterations": 15, "max_minutes": 60}
        'scope': [],                     # files the agent can modify
        'program': None,                 # instructions governing the loop (like program.md)
        'baseline': None,               # initial metric value
        'best': None,                    # best metric seen so far
        'iteration_history': [],          # [{iteration, change_summary, metric, kept, checkpoint_id}]
    }
    return goal
```

### 3.2 Optimization Executor

**File**: `apps/core-daemon/optimization_loop.py`

The core loop, executed by the daemon when it picks up an `optimize` mode goal:

```python
class OptimizationLoop:
    """Autoresearch-style optimization via shade iterations."""
    
    def __init__(self, goal: dict, checkpoint_mgr: CheckpointManager,
                 memory_engine: MemoryEngine):
        self.goal = goal
        self.checkpoints = checkpoint_mgr
        self.memory = memory_engine
        self.iteration = 0
        
    async def run(self):
        """Main optimization loop."""
        # 1. Measure baseline
        self.goal['baseline'] = await self._measure()
        self.goal['best'] = self.goal['baseline']
        
        budget = self.goal.get('budget', {})
        max_iters = budget.get('max_iterations', 20)
        
        while self.iteration < max_iters:
            self.iteration += 1
            
            # 2. Snapshot current state
            cp_id = self.checkpoints.snapshot(
                label=f"iter-{self.iteration}-before"
            )
            
            # 3. Spawn shade to make one modification
            result = await self._run_iteration_shade()
            
            # 4. Measure new metric
            new_metric = await self._measure()
            
            # 5. Keep or discard
            kept = self._is_improvement(new_metric)
            if not kept:
                self.checkpoints.rollback(cp_id)
            else:
                self.goal['best'] = new_metric
            
            # 6. Record history
            self.goal['iteration_history'].append({
                'iteration': self.iteration,
                'change_summary': result.get('summary', ''),
                'metric_before': self.goal['best'] if not kept else None,
                'metric_after': new_metric,
                'kept': kept,
                'checkpoint_id': cp_id,
            })
            
            # 7. Update procedures from successful iterations
            if kept:
                self._capture_procedure(result)
    
    async def _run_iteration_shade(self) -> dict:
        """Spawn a shade for one optimization iteration.
        
        The shade gets:
        - The program (instructions for what to try)
        - The current metric value and history of what's been tried
        - Scope restrictions (which files to modify)
        - Procedures from similar past optimizations
        """
        procedures = self.memory.recall(
            self.goal['program'],
            category="procedure",
            limit=3,
        )
        
        # Build shade instruction with full context
        instruction = self._build_iteration_prompt(procedures)
        
        # Delegate to shade_orchestrator.create_contract()
        # with custom phase plan: [analyze, modify, report]
        ...
    
    async def _measure(self) -> dict:
        """Run the metric command and parse the result."""
        # Execute metric_cmd, parse output for the metric value
        ...
    
    def _is_improvement(self, new_metric) -> bool:
        """Compare new metric to current best."""
        # Supports: lower_is_better (loss), higher_is_better (accuracy)
        ...
```

### 3.3 Iteration Shade Contract

Each iteration gets a specialized shade contract:

```python
def optimization_iteration_phases(goal: dict, iteration: int) -> list[dict]:
    return [
        {
            'name': 'analyze',
            'objective': (
                f'Iteration {iteration}. Review the program, current metric '
                f'({goal["best"]}), and history of past attempts. '
                f'Decide what single change to try next. '
                f'Do NOT repeat changes that were already tried and discarded.'
            ),
        },
        {
            'name': 'implement',
            'objective': (
                f'Make exactly ONE focused change to the files in scope: '
                f'{goal["scope"]}. Keep the change minimal and testable.'
            ),
        },
        {
            'name': 'report',
            'objective': (
                f'Summarize what you changed and why. The metric will be '
                f'measured automatically after this phase.'
            ),
        },
    ]
```

### 3.4 Program Authoring

The `program` field is the human's lever — equivalent to Autoresearch's `program.md`.
It can be:

- **Inline text**: passed directly when creating the goal
- **A procedure reference**: "use procedure X as the program"
- **A file path**: point to a markdown file in the project

Example programs:

```markdown
# Optimize context compactor speed

## Metric
Wall-clock time for `pytest tests/test_context_compactor.py -x`
Lower is better.

## Scope  
Only modify: apps/core-daemon/context_compactor.py

## Constraints
- All existing tests must keep passing
- Don't change the public API (CompactionConfig, compact() signature)
- Don't add new dependencies

## Ideas to explore
- Batch multiple summarization calls into one
- Cache summaries that haven't changed
- Use cheaper/faster model for leaf summaries
- Reduce fresh_tail_count if tests still pass
- Parallelize independent summarization calls
```

### 3.5 Goal Creation Interface

From the agent (via a tool or slash command):

```
/goal create --mode optimize \
  --metric "pytest tests/test_compactor.py execution time" \
  --metric-cmd "pytest tests/test_compactor.py --tb=no -q 2>&1 | tail -1" \
  --scope apps/core-daemon/context_compactor.py \
  --budget '{"max_iterations": 15}' \
  --program docs/programs/optimize-compactor.md
```

Or conversationally:

> "Optimize the context compactor. Metric is test execution time, lower is
> better. Only touch context_compactor.py. Run 15 iterations max."

The agent creates the goal via goal_runtime with mode=optimize.

### 3.6 Explore Mode

A lighter variant — no single metric, just structured exploration:

```python
# mode='explore' goals run a fixed number of shade iterations
# Each shade tries a different approach to the goal
# All results are recorded but nothing is kept/discarded
# Output: a procedure summarizing what was learned
```

Useful for: "figure out the best way to deploy this app", "research how to
implement feature X", "compare approaches to Y".

### Deliverables
- [ ] Extend `goal_runtime._goal_node()` with optimization fields
- [ ] `optimization_loop.py` — core modify→run→measure→keep/discard loop
- [ ] `optimization_iteration_phases()` — shade contract template for iterations
- [ ] Metric parsing utilities (extract numbers from command output)
- [ ] Program file support (load program from .md file)
- [ ] Goal creation via tool/command with mode=optimize
- [ ] Explore mode variant
- [ ] Procedure auto-capture from successful optimization iterations

---

## Phase 4: Refinements (Future)

These are stretch goals that become valuable once the core loop is proven:

### 4.1 Procedure Composition
Procedures reference sub-procedures. "Deploy to production" links to
"Run tests" + "Build image" + "Push to registry" as sub-steps, each a
separate procedure that can be independently improved.

### 4.2 Conditional Procedures
Branching logic: "If Python project → pytest; if JS → vitest; if Rust → cargo test".
Matched by project context, not just semantic similarity.

### 4.3 Cross-Project Transfer
When a new project is onboarded, relevant procedures from other projects are
suggested. Uses MemoryEngine's `container_tag` to scope, but recall can
optionally search across containers.

### 4.4 Prompt Caching for Optimization
Anthropic-style cache breakpoints on the stable prefix (system prompt +
program + history) to reduce cost of repeated iterations. The program and
history grow monotonically, making them ideal cache targets.

### 4.5 Cost Tracking
Per-iteration and per-goal cost estimation. Set budget limits in dollars,
not just iteration count. Uses model pricing data to estimate cost before
each iteration.

---

## Integration Map

```
┌─────────────────────────────────────────────────────────┐
│                    User / Agent                         │
│  "optimize X" / "learn how to Y" / auto after tasks    │
└──────────────────┬──────────────────────────────────────┘
                   │
         ┌─────────▼──────────┐
         │   goal_runtime.py  │  mode: standard|optimize|explore
         │   (extended)       │  metric, budget, program, history
         └─────────┬──────────┘
                   │
        ┌──────────▼───────────┐
        │ optimization_loop.py │  modify→run→measure→keep/discard
        │ (new, Phase 3)       │  drives the iteration cycle
        └──────────┬───────────┘
                   │
     ┌─────────────▼──────────────┐
     │   shade_orchestrator.py    │  each iteration = shade contract
     │   (existing)               │  analyze → implement → report
     └─────────────┬──────────────┘
                   │
    ┌──────────────▼──────────────────┐
    │     checkpoint_manager.py       │  snapshot before, rollback on fail
    │     (new, Phase 2)              │
    └──────────────┬──────────────────┘
                   │
    ┌──────────────▼──────────────────┐
    │       memory_engine.py          │  procedures stored & retrieved
    │       (existing, new category)  │  auto-capture ← task_summarizer
    │                                 │  auto-inject  → context_assembler
    └─────────────────────────────────┘
```

---

## Implementation Order

| Order | Component | Effort | Depends On |
|-------|-----------|--------|------------|
| 1a | `procedure_extractor.py` | 1 day | — |
| 1b | Auto-capture hook in `task_summarizer.py` | 0.5 day | 1a |
| 1c | Auto-retrieve in `context_assembler.py` | 0.5 day | 1a |
| 1d | `tools/procedure_tool.py` | 0.5 day | 1a |
| 1e | Outcome tracking | 0.5 day | 1a, 1c |
| 2a | `checkpoint_manager.py` | 1 day | — |
| 2b | Shade auto-snapshot hook | 0.5 day | 2a |
| 3a | Goal model extensions | 0.5 day | — |
| 3b | `optimization_loop.py` | 2 days | 1a, 2a, 3a |
| 3c | Metric parsing utilities | 0.5 day | — |
| 3d | Program file support | 0.5 day | — |
| 3e | Explore mode | 1 day | 3b |

**Critical path**: 1a → 1b+1c → 2a → 3b (total ~5 days)  
**Full plan**: ~9 days with parallelization

---

## Risks & Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| Procedure extraction produces low-quality steps | Noise in retrieval | Conservative threshold (≥5 turns, success only). User can delete bad procedures via tool. Dedup prevents flooding. |
| Optimization loop burns tokens on bad iterations | Cost | Budget field caps iterations. Checkpoint rollback prevents accumulating damage. |
| Metric parsing is fragile | Loop breaks | Structured metric output format. Fallback to "did the command exit 0". |
| Shade model (local Qwen 27B) may be too weak for optimization reasoning | Poor iteration quality | Tier system — optimization iterations use `strong` tier. Auto-set tiers from model registry. |
| Procedures go stale | Bad guidance | `forget_after` field + success rate tracking. Low-success procedures get deprioritized. |
