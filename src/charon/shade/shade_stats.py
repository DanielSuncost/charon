"""Shade usage stats — tracks token consumption and shade count per agent.

Stored in SQLite user_model table under the key 'shade_stats'.
Separate from chat token tracking.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

try:
    from charon.infra.diagnostics import record as _diag
except Exception:  # diagnostics is best-effort and must never block import
    def _diag(*args, **kwargs):
        return None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_stats(state_dir: Path) -> dict:
    """Load shade stats from SQLite."""
    try:
        from charon.infra.store_adapter import get_db, user_model_get
        db = get_db(state_dir)
        model = user_model_get(db)
        raw = model.get('shade_stats', {})
        if isinstance(raw, str):
            raw = json.loads(raw)
        if isinstance(raw, dict) and 'value' in raw:
            raw = raw['value']
            if isinstance(raw, str):
                raw = json.loads(raw)
        return raw if isinstance(raw, dict) else {}
    except Exception as e:
        _diag('shade_stats', 'shade stats load failed; treating as empty', error=e)
        return {}


def _save_stats(state_dir: Path, stats: dict) -> None:
    """Save shade stats to SQLite."""
    try:
        from charon.infra.store_adapter import get_db, user_model_set
        db = get_db(state_dir)
        user_model_set(db, 'shade_stats', stats)
    except Exception as e:
        _diag('shade_stats', 'shade stats save failed; usage not recorded', error=e)


def record_shade_usage(
    state_dir: Path,
    *,
    parent_agent_id: str,
    shade_agent_id: str,
    model_id: str,
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> None:
    """Record token usage from a shade execution."""
    stats = _load_stats(state_dir)

    # Global totals
    stats['total_shades'] = (stats.get('total_shades') or 0) + 1
    stats['total_input_tokens'] = (stats.get('total_input_tokens') or 0) + input_tokens
    stats['total_output_tokens'] = (stats.get('total_output_tokens') or 0) + output_tokens
    stats['last_updated'] = _now()

    # Per-agent totals
    agents = stats.setdefault('by_agent', {})
    ag = agents.setdefault(parent_agent_id, {
        'shades_spawned': 0, 'input_tokens': 0, 'output_tokens': 0,
    })
    ag['shades_spawned'] = (ag.get('shades_spawned') or 0) + 1
    ag['input_tokens'] = (ag.get('input_tokens') or 0) + input_tokens
    ag['output_tokens'] = (ag.get('output_tokens') or 0) + output_tokens

    # Per-model totals
    models = stats.setdefault('by_model', {})
    md = models.setdefault(model_id, {
        'shades': 0, 'input_tokens': 0, 'output_tokens': 0,
    })
    md['shades'] = (md.get('shades') or 0) + 1
    md['input_tokens'] = (md.get('input_tokens') or 0) + input_tokens
    md['output_tokens'] = (md.get('output_tokens') or 0) + output_tokens

    _save_stats(state_dir, stats)


def get_shade_stats(state_dir: Path) -> dict:
    """Get shade usage stats."""
    return _load_stats(state_dir)


def get_agent_shade_stats(state_dir: Path, agent_id: str) -> dict:
    """Get shade stats for a specific parent agent."""
    stats = _load_stats(state_dir)
    by_agent = stats.get('by_agent', {})
    agent_stats = by_agent.get(agent_id, {})
    return {
        'shades_spawned': agent_stats.get('shades_spawned', 0),
        'input_tokens': agent_stats.get('input_tokens', 0),
        'output_tokens': agent_stats.get('output_tokens', 0),
        'total_tokens': agent_stats.get('input_tokens', 0) + agent_stats.get('output_tokens', 0),
    }


def format_stats(stats: dict) -> str:
    """Format stats for display."""
    total = stats.get('total_shades', 0)
    inp = stats.get('total_input_tokens', 0)
    out = stats.get('total_output_tokens', 0)

    lines = [f'Shade usage: {total} shades spawned, {_fmt_tokens(inp)} in / {_fmt_tokens(out)} out']

    by_model = stats.get('by_model', {})
    if by_model:
        lines.append('By model:')
        for model_id, md in sorted(by_model.items()):
            lines.append(f'  {model_id}: {md.get("shades", 0)} shades, {_fmt_tokens(md.get("input_tokens", 0))} in / {_fmt_tokens(md.get("output_tokens", 0))} out')

    by_agent = stats.get('by_agent', {})
    if by_agent:
        lines.append('By agent:')
        for agent_id, ag in sorted(by_agent.items()):
            lines.append(f'  {agent_id}: {ag.get("shades_spawned", 0)} shades, {_fmt_tokens(ag.get("input_tokens", 0))} in / {_fmt_tokens(ag.get("output_tokens", 0))} out')

    return '\n'.join(lines)


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f'{n / 1_000_000:.1f}M'
    if n >= 1_000:
        return f'{n / 1_000:.1f}k'
    return str(n)
