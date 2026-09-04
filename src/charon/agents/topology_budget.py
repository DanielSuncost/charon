"""Topology budget — breadth/depth/total-agent/time governance for delegation trees.

Every SpawnShade / SpawnBatch call — including PyKernel's `charon.spawn_shade`,
which re-enters SpawnShade directly — sits somewhere in a tree of delegation:
an agent spawns shades, and a shade can spawn further shades of its own. Left
alone that tree has no ceiling: SpawnBatch caps a single call at 50 tasks, but
nothing stops a node from calling it repeatedly, or a grandchild from doing
the same, so the *tree* can still grow unboundedly even though each call is
individually capped.

A TopologyBudget is minted once, at the root of a tree (the first spawn call
made by an agent with no budget of its own), and is then inherited unchanged
by every descendant: it rides on ToolContext.topology_budget and, for a
shade's own ConversationEngine, on engine.topology_budget — set post-hoc the
same way `engine.scope` already is in shade_tool._run_shade. Limits are fixed
at the root; a descendant cannot mint itself a fresh, more permissive budget
to escape governance — effective_budget() always returns the inherited
budget when one is present, ignoring any preset the descendant passes.

Call try_reserve() before actually spawning `count` children under a given
parent. It atomically checks depth/breadth/total-agent/time limits and, on
success, commits the reservation in the same step so concurrent spawns from
different threads can't both slip past the same limit.

This is a soft governor, not a security boundary: if the state file can't be
read or written, try_reserve() fails open (logs via diagnostics and allows
the spawn) rather than blocking legitimate work on infrastructure trouble —
consistent with the rest of the codebase's best-effort persistence style.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

try:
    from charon.infra.diagnostics import record as _diag
except Exception:  # diagnostics is best-effort and must never block import
    def _diag(*args, **kwargs):
        return None

try:
    import fcntl
    _HAS_FCNTL = True
except Exception:  # non-POSIX: best-effort, no cross-process lock
    fcntl = None  # type: ignore
    _HAS_FCNTL = False


@dataclass
class TopologyBudget:
    """Limits governing one delegation tree, fixed at the root."""
    root_id: str
    max_depth: int = 3
    max_breadth_per_level: int = 50
    max_total_agents: int = 200
    token_budget: int = 0          # 0 = unlimited
    time_budget_minutes: int = 0   # 0 = unlimited
    started_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Named presets so a caller (a model deciding how to spawn, or a future
# overseer picking a shape per task) can dial breadth/depth without having to
# reason about raw numbers. "standard" is the default for any spawn call that
# doesn't specify one.
PRESETS: dict[str, dict[str, int]] = {
    'narrow':   {'max_depth': 1, 'max_breadth_per_level': 3,  'max_total_agents': 5},
    'standard': {'max_depth': 3, 'max_breadth_per_level': 50, 'max_total_agents': 200},
    'wide':     {'max_depth': 4, 'max_breadth_per_level': 50, 'max_total_agents': 500},
}


def mint_budget(root_id: str, preset: str = 'standard', **overrides: Any) -> dict[str, Any]:
    """Create a fresh budget dict for a new delegation tree rooted at root_id."""
    limits = dict(PRESETS.get(preset) or PRESETS['standard'])
    limits.update({k: v for k, v in overrides.items() if v is not None})
    return TopologyBudget(root_id=root_id, **limits).to_dict()


def effective_budget(ctx: Any, *, preset: str = 'standard') -> dict[str, Any]:
    """Return ctx's inherited budget, or mint a fresh one if this call starts
    a new tree (ctx carries no budget of its own yet).

    A descendant's `preset` argument is ignored whenever ctx already carries
    a budget — only the root of a tree gets to pick its shape.
    """
    existing = getattr(ctx, 'topology_budget', None)
    if isinstance(existing, dict) and existing.get('root_id'):
        return existing
    root_id = str(getattr(ctx, 'agent_id', '') or '') or f'root-{uuid.uuid4().hex[:8]}'
    return mint_budget(root_id, preset=preset)


def _state_path(state_dir: Path, root_id: str) -> Path:
    safe = ''.join(c if (c.isalnum() or c in '-_') else '_' for c in root_id) or 'root'
    return Path(state_dir) / 'topology' / f'{safe}.json'


def _load(path: Path) -> dict[str, Any]:
    try:
        if path.exists():
            data = json.loads(path.read_text())
            if isinstance(data, dict):
                data.setdefault('total_agents', 0)
                data.setdefault('children_by_node', {})
                return data
    except Exception as e:
        _diag('topology_budget', 'topology state read failed; starting fresh', error=e, path=str(path))
    return {'total_agents': 0, 'children_by_node': {}}


def _save(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state))


def try_reserve(
    state_dir: Path,
    budget: dict[str, Any],
    *,
    parent_agent_id: str,
    depth: int,
    count: int = 1,
) -> tuple[bool, str]:
    """Check depth/breadth/total-agent/time limits for `count` new children
    of `parent_agent_id` at `depth`, and atomically commit if they pass.

    Returns (ok, reason). reason is a human-readable refusal when ok is
    False, and an empty string when ok is True.
    """
    max_depth = int(budget.get('max_depth') or 0)
    if max_depth > 0 and depth > max_depth:
        return False, f'max topology depth reached (max_depth={max_depth}, this spawn would land at depth {depth})'

    time_budget_min = int(budget.get('time_budget_minutes') or 0)
    started_at = float(budget.get('started_at') or 0)
    if time_budget_min > 0 and started_at and (time.time() - started_at) > time_budget_min * 60:
        return False, f'topology time budget exhausted ({time_budget_min}m since this delegation tree started)'

    root_id = str(budget.get('root_id') or 'root')
    parent_agent_id = parent_agent_id or 'root'
    path = _state_path(Path(state_dir), root_id)
    lock_file = None
    try:
        if _HAS_FCNTL:
            path.parent.mkdir(parents=True, exist_ok=True)
            lock_file = open(path.with_suffix('.lock'), 'w')
            fcntl.flock(lock_file, fcntl.LOCK_EX)

        state = _load(path)
        children_by_node = state.setdefault('children_by_node', {})
        existing_children = int(children_by_node.get(parent_agent_id, 0))
        total = int(state.get('total_agents', 0))

        max_breadth = int(budget.get('max_breadth_per_level') or 0)
        if max_breadth > 0 and existing_children + count > max_breadth:
            return False, (
                f'max topology breadth reached (max_breadth_per_level={max_breadth}, '
                f'this node already has {existing_children} children, {count} more requested)'
            )

        max_total = int(budget.get('max_total_agents') or 0)
        if max_total > 0 and total + count > max_total:
            return False, (
                f'max total agents reached for this delegation tree (max_total_agents={max_total}, '
                f'{total} already spawned, {count} more requested)'
            )

        children_by_node[parent_agent_id] = existing_children + count
        state['total_agents'] = total + count
        _save(path, state)
        return True, ''
    except Exception as e:
        _diag('topology_budget', 'reservation failed open; allowing spawn without accounting', error=e, root_id=root_id)
        return True, ''
    finally:
        if lock_file is not None:
            try:
                fcntl.flock(lock_file, fcntl.LOCK_UN)
            except Exception:
                pass
            lock_file.close()


def current_state(state_dir: Path, root_id: str) -> dict[str, Any]:
    """Read-only snapshot of a tree's accounting, for status/debugging."""
    return _load(_state_path(Path(state_dir), root_id))
