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
    cost_budget_usd: float = 0.0   # 0 = unlimited
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


def effective_budget(ctx: Any, *, preset: str = 'standard', **overrides: Any) -> dict[str, Any]:
    """Return ctx's inherited budget, or mint a fresh one if this call starts
    a new tree (ctx carries no budget of its own yet).

    A descendant's `preset`/`overrides` are ignored whenever ctx already
    carries a budget — only the root of a tree gets to pick its shape.
    """
    existing = getattr(ctx, 'topology_budget', None)
    if isinstance(existing, dict) and existing.get('root_id'):
        return existing
    root_id = str(getattr(ctx, 'agent_id', '') or '') or f'root-{uuid.uuid4().hex[:8]}'
    return mint_budget(root_id, preset=preset, **overrides)


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
                data.setdefault('total_tokens_used', 0)
                data.setdefault('reactivations_by_node', {})
                data.setdefault('total_cost_usd', 0.0)
                return data
    except Exception as e:
        _diag('topology_budget', 'topology state read failed; starting fresh', error=e, path=str(path))
    return {
        'total_agents': 0, 'children_by_node': {}, 'total_tokens_used': 0,
        'reactivations_by_node': {}, 'total_cost_usd': 0.0,
    }


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

        token_budget = int(budget.get('token_budget') or 0)
        tokens_used = int(state.get('total_tokens_used', 0))
        if token_budget > 0 and tokens_used >= token_budget:
            return False, (
                f'topology token budget exhausted (token_budget={token_budget}, '
                f'{tokens_used} used so far by this delegation tree)'
            )

        cost_budget = float(budget.get('cost_budget_usd') or 0)
        cost_used = float(state.get('total_cost_usd', 0.0))
        if cost_budget > 0 and cost_used >= cost_budget:
            return False, (
                f'topology cost budget exhausted (cost_budget_usd=${cost_budget:.4f}, '
                f'${cost_used:.4f} spent so far by this delegation tree)'
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


def record_usage(state_dir: Path, budget: dict[str, Any], tokens: int) -> None:
    """Add `tokens` to this tree's running total-tokens-used counter.

    Called after a call's real token cost is known (a turn's usage isn't
    knowable in advance, so this is a post-hoc accumulation, not a
    reservation) — try_reserve()'s token_budget check then gates further
    spawning once the tree's cumulative usage reaches the budget. Best
    effort: like try_reserve, failure to persist is logged and swallowed
    rather than raised, so a bookkeeping problem never blocks real work.
    """
    if tokens <= 0:
        return
    root_id = str(budget.get('root_id') or 'root')
    path = _state_path(Path(state_dir), root_id)
    lock_file = None
    try:
        if _HAS_FCNTL:
            path.parent.mkdir(parents=True, exist_ok=True)
            lock_file = open(path.with_suffix('.lock'), 'w')
            fcntl.flock(lock_file, fcntl.LOCK_EX)

        state = _load(path)
        state['total_tokens_used'] = int(state.get('total_tokens_used', 0)) + int(tokens)
        _save(path, state)
    except Exception as e:
        _diag('topology_budget', 'usage recording failed; token accounting may undercount', error=e, root_id=root_id)
    finally:
        if lock_file is not None:
            try:
                fcntl.flock(lock_file, fcntl.LOCK_UN)
            except Exception:
                pass
            lock_file.close()


def record_cost(state_dir: Path, budget: dict[str, Any], cost_usd: float) -> None:
    """Add `cost_usd` to this tree's running total-cost-spent counter.

    Same shape and same caveats as record_usage(): a call's real dollar cost
    isn't knowable until it finishes, so this is post-hoc accumulation, and
    try_reserve()'s cost_budget_usd check is what actually gates further
    spawning once the tree's cumulative spend reaches the budget.
    """
    if cost_usd <= 0:
        return
    root_id = str(budget.get('root_id') or 'root')
    path = _state_path(Path(state_dir), root_id)
    lock_file = None
    try:
        if _HAS_FCNTL:
            path.parent.mkdir(parents=True, exist_ok=True)
            lock_file = open(path.with_suffix('.lock'), 'w')
            fcntl.flock(lock_file, fcntl.LOCK_EX)

        state = _load(path)
        state['total_cost_usd'] = round(float(state.get('total_cost_usd', 0.0)) + float(cost_usd), 6)
        _save(path, state)
    except Exception as e:
        _diag('topology_budget', 'cost recording failed; cost accounting may undercount', error=e, root_id=root_id)
    finally:
        if lock_file is not None:
            try:
                fcntl.flock(lock_file, fcntl.LOCK_UN)
            except Exception:
                pass
            lock_file.close()


def try_reserve_reactivation(
    state_dir: Path,
    budget: dict[str, Any],
    *,
    shade_id: str,
    max_reactivations: int = 20,
) -> tuple[bool, str]:
    """Re-check a retained shade's reactivation against time/token budget and
    a reactivation-count cap — WITHOUT incrementing children_by_node/
    total_agents, since a reactivation is the same tree node, already
    counted once at its original spawn. Skipping this check entirely (as a
    naive reactivation path would) is the first concrete way this whole
    governance layer could be bypassed: a retained shade could otherwise be
    reactivated an unbounded number of times with no re-check of anything.

    Returns (ok, reason), same shape as try_reserve().
    """
    time_budget_min = int(budget.get('time_budget_minutes') or 0)
    started_at = float(budget.get('started_at') or 0)
    if time_budget_min > 0 and started_at and (time.time() - started_at) > time_budget_min * 60:
        return False, f'topology time budget exhausted ({time_budget_min}m since this delegation tree started)'

    shade_id = shade_id or 'root'
    root_id = str(budget.get('root_id') or 'root')
    path = _state_path(Path(state_dir), root_id)
    lock_file = None
    try:
        if _HAS_FCNTL:
            path.parent.mkdir(parents=True, exist_ok=True)
            lock_file = open(path.with_suffix('.lock'), 'w')
            fcntl.flock(lock_file, fcntl.LOCK_EX)

        state = _load(path)

        token_budget = int(budget.get('token_budget') or 0)
        tokens_used = int(state.get('total_tokens_used', 0))
        if token_budget > 0 and tokens_used >= token_budget:
            return False, (
                f'topology token budget exhausted (token_budget={token_budget}, '
                f'{tokens_used} used so far by this delegation tree)'
            )

        cost_budget = float(budget.get('cost_budget_usd') or 0)
        cost_used = float(state.get('total_cost_usd', 0.0))
        if cost_budget > 0 and cost_used >= cost_budget:
            return False, (
                f'topology cost budget exhausted (cost_budget_usd=${cost_budget:.4f}, '
                f'${cost_used:.4f} spent so far by this delegation tree)'
            )

        reactivations = state.setdefault('reactivations_by_node', {})
        count = int(reactivations.get(shade_id, 0))
        if max_reactivations > 0 and count >= max_reactivations:
            return False, f'max reactivations reached for this shade (max_reactivations={max_reactivations})'

        reactivations[shade_id] = count + 1
        _save(path, state)
        return True, ''
    except Exception as e:
        _diag('topology_budget', 'reactivation check failed open; allowing reactivation without accounting', error=e, root_id=root_id)
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


def token_budget_utilization(state_dir: Path, budget: dict[str, Any]) -> float | None:
    """Fraction (0-1) of this tree's token_budget already used, or None if
    the budget is unlimited (token_budget <= 0).

    Lets a caller downgrade to a cheaper model tier for the tree's remaining
    calls as its budget runs low, instead of only finding out it's gone via
    try_reserve()'s hard refusal once it's already exhausted.
    """
    token_budget = int(budget.get('token_budget') or 0)
    if token_budget <= 0:
        return None
    root_id = str(budget.get('root_id') or 'root')
    state = current_state(state_dir, root_id)
    return min(1.0, int(state.get('total_tokens_used', 0)) / token_budget)


def cost_budget_utilization(state_dir: Path, budget: dict[str, Any]) -> float | None:
    """Fraction (0-1) of this tree's cost_budget_usd already spent, or None
    if the budget is unlimited (cost_budget_usd <= 0). Mirrors
    token_budget_utilization() exactly, for the dollar-cost dimension.
    """
    cost_budget = float(budget.get('cost_budget_usd') or 0)
    if cost_budget <= 0:
        return None
    root_id = str(budget.get('root_id') or 'root')
    state = current_state(state_dir, root_id)
    return min(1.0, float(state.get('total_cost_usd', 0.0)) / cost_budget)


def _peer_message_state_path(state_dir: Path) -> Path:
    return Path(state_dir) / 'topology' / 'peer_messages.json'


def try_reserve_peer_message(
    state_dir: Path, *, sender_agent_id: str, peer_agent_id: str, max_messages: int = 50,
) -> tuple[bool, str]:
    """Cap how many messages one agent may send to a peer via
    charon.rlm(peer_agent_id=...), to prevent two agents messaging each
    other in an unbounded loop.

    Deliberately not a TopologyBudget: messaging a peer isn't spawning a
    child, so it has no place in a delegation tree's depth/breadth/total-
    agent accounting. This is a separate, simple counter keyed by the
    (sender, peer) pair, using the same atomic-file-lock pattern as
    try_reserve() rather than a new one.
    """
    key = f'{sender_agent_id or "?"}->{peer_agent_id or "?"}'
    path = _peer_message_state_path(state_dir)
    lock_file = None
    try:
        if _HAS_FCNTL:
            path.parent.mkdir(parents=True, exist_ok=True)
            lock_file = open(path.with_suffix('.lock'), 'w')
            fcntl.flock(lock_file, fcntl.LOCK_EX)

        try:
            state = json.loads(path.read_text()) if path.exists() else {}
        except Exception:
            state = {}
        if not isinstance(state, dict):
            state = {}

        count = int(state.get(key, 0))
        if max_messages > 0 and count >= max_messages:
            return False, (
                f'peer-message cap reached ({sender_agent_id!r} -> {peer_agent_id!r}, '
                f'max_messages={max_messages})'
            )

        state[key] = count + 1
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(state))
        return True, ''
    except Exception as e:
        _diag('topology_budget', 'peer-message cap check failed open; allowing message without accounting', error=e)
        return True, ''
    finally:
        if lock_file is not None:
            try:
                fcntl.flock(lock_file, fcntl.LOCK_UN)
            except Exception:
                pass
            lock_file.close()
