# Worktree Isolation for Shade Contracts

> Add `git worktree` isolation to shade orchestration so parallel shades work on independent branches without conflicts.
>
> Date: 2026-04-10
> Status: Proposed
> Related: `shade_orchestrator.py`, `checkpoint_manager.py`, `batch_orchestrator.py`, `optimization-loop-scenario-spec.md` (§3: Run isolation)

---

## Problem

Shade contracts currently scope agents to **file paths** within a single working copy. This means:

1. **Parallel shades conflict** — Two shades modifying overlapping files race on disk
2. **No branch isolation** — All shade work happens on the current branch; rollback is via shadow git repo (checkpoint_manager), not real branches
3. **No merge workflow** — Shade output lands directly in the working copy; there's no review/merge step

dmux solves this with per-task git worktrees. Charon's shade system already has `active_branch_id` and `branch_history` fields on contracts — worktree support was anticipated but never implemented.

---

## Design

### New module: `worktree_manager.py`

```python
class WorktreeManager:
    """Manages git worktrees for isolated shade execution."""

    def __init__(self, state_dir: Path, project_root: Path):
        # Worktrees live at ~/.charon/worktrees/{project_hash}/{branch}/
        ...

    def create(self, branch_name: str, base_ref: str = "HEAD") -> WorktreeInfo:
        """Create a new worktree + branch.

        Runs: git worktree add <path> -b <branch_name> <base_ref>
        Returns: WorktreeInfo(path, branch, base_ref, created_at)
        """

    def merge(self, worktree_path: Path, target: str = "main",
              strategy: str = "merge", auto_commit: bool = True) -> MergeResult:
        """Merge worktree branch back to target.

        1. Switch to target branch in main repo
        2. git merge <worktree_branch> --no-ff
        3. If conflict: return MergeResult(success=False, conflicts=[...])
        4. If clean: optionally auto-commit
        Returns: MergeResult(success, conflicts, diff_stat, commit_sha)
        """

    def cleanup(self, worktree_path: Path, delete_branch: bool = False) -> None:
        """Remove worktree. Optionally delete the branch if already merged."""

    def list(self) -> list[WorktreeInfo]:
        """List active worktrees for this project with branch, status, diff stats."""

    def diff_stat(self, worktree_path: Path) -> str:
        """Show diff --stat of worktree branch vs its base ref."""

    def diff_full(self, worktree_path: Path) -> str:
        """Full diff of worktree branch vs its base ref."""
```

**Storage**: `~/.charon/worktrees/{project_hash}/{branch_name}/`
- Project hash: `sha256(project_root)[:12]` (same pattern as checkpoint_manager)
- Each worktree is a full working copy with its own branch

**State file**: `~/.charon/state/worktrees.json`
```json
[
  {
    "id": "wt-a1b2c3",
    "project_root": "/Users/me/Projects/suiteswarm",
    "worktree_path": "/Users/me/.charon/worktrees/3f8a.../feature-auth",
    "branch": "shade/ctr-abc123/feature-auth",
    "base_ref": "abc1234",
    "contract_id": "ctr-abc123",
    "status": "active",
    "created_at": "2026-04-10T12:00:00Z",
    "merged_at": null
  }
]
```

### Integration with shade_orchestrator.py

**`create_contract()` changes:**

```python
def create_contract(
    state_dir: Path,
    *,
    # ... existing params ...
    isolation: str = 'scope',  # NEW: 'worktree' | 'scope' | 'none'
    auto_merge: bool = False,  # NEW: merge on contract completion?
) -> dict:
```

When `isolation="worktree"`:
1. Branch name: `shade/{contract_id}/{slugified_goal}`
2. Create worktree before first phase
3. Set contract's `worktree_path` and `active_branch_id` to the new branch
4. All phases execute with `ToolContext.project_root` = worktree path

**Contract completion:**
- If `auto_merge=True`: merge worktree → base branch, cleanup on success
- If `auto_merge=False`: mark contract as `completed_pending_merge`, keep worktree alive for review
- On failure: keep worktree for inspection, mark contract as `failed`

**Contract record additions:**
```python
rec = {
    # ... existing fields ...
    'isolation': isolation,          # NEW
    'worktree_path': None,           # NEW — set when worktree created
    'auto_merge': auto_merge,        # NEW
    'merge_status': None,            # NEW — 'pending' | 'merged' | 'conflict' | None
}
```

### Integration with batch_orchestrator.py

Parallel batch jobs benefit most from worktree isolation — N shades can work on N branches simultaneously with zero conflicts.

When a batch is created with `isolation="worktree"`:
- Each task in the batch gets its own worktree + branch
- Fan-in: after all tasks complete, merge sequentially (or present merge queue to user)
- Conflict between batch branches: flag for manual resolution

### Integration with checkpoint_manager.py

When operating in a worktree:
- **Skip shadow repo creation** — the worktree IS isolated, commits go to its own branch
- `snapshot()`: regular `git commit` on the worktree's branch
- `rollback()`: `git reset --hard <checkpoint_sha>` on the worktree's branch
- `diff()`: `git diff <checkpoint_sha> HEAD` on the worktree's branch

Detection: if `worktree_path` is set in contract, checkpoint_manager uses the worktree's native git instead of the shadow GIT_DIR approach.

### Integration with tools/__init__.py

Add to `ToolContext`:
```python
@dataclass
class ToolContext:
    # ... existing fields ...
    worktree_path: Path | None = None  # NEW — if set, tools operate here
```

Tool dispatch: when `worktree_path` is set, Bash/Git/Read/Write/Edit tools use it as cwd instead of `project_root`. This is transparent to the shade — it thinks it's working in the project root.

---

## Branch naming convention

```
shade/{contract_id}/{goal_slug}
```

Examples:
- `shade/ctr-a1b2c3d4ef/optimize-auth-middleware`
- `shade/ctr-f5e6d7c8ba/add-unit-tests`

This makes branches easy to identify and clean up. `git branch --list 'shade/*'` shows all shade branches.

---

## Path breakage mitigation

The optimization-loop spec (§3) flagged that worktree paths may break hardcoded references. Mitigations:

1. **Relative paths**: Tools should use relative paths within the worktree. The worktree is a full clone — all relative paths work identically.
2. **Symlinks**: If a project has absolute-path config (e.g. `.env` with absolute paths), WorktreeManager can optionally symlink specific files from the main repo.
3. **Scope still applies**: Even in worktree mode, the `scope` restriction on contracts limits which files the shade can modify. Worktree isolation prevents disk conflicts; scope prevents logical overreach.

---

## Rollout

1. **Phase 1**: `worktree_manager.py` — standalone module with create/merge/cleanup/list
2. **Phase 2**: Wire into `create_contract()` with `isolation="worktree"` param
3. **Phase 3**: Wire into `batch_orchestrator.py` for parallel worktree batches
4. **Phase 4**: Update checkpoint_manager to detect worktree mode and skip shadow repo

Backward compatible: `isolation` defaults to `"scope"`, existing contracts unchanged.
