"""Read-only projection of this chat's shade tree for the input-bar work list."""
from __future__ import annotations

import time
from pathlib import Path

from charon.shade.shade_orchestrator import is_contract_stale, load_contracts

ACTIVE = {'running', 'pending', 'queued', 'blocked', 'paused', 'waiting'}


def _compact(value, limit=100):
    return ' '.join(str(value or '').split())[:limit]


def collect_subagent_activity(state_dir: Path, owner_id: str) -> dict:
    snapshot = {'owner_id': owner_id, 'collected_at': time.time(), 'items': [], 'total': 0}
    if not owner_id:
        return snapshot
    contracts = load_contracts(state_dir)
    # Follow lineage, including through completed parents with still-running
    # children. Never show unrelated agents just because they share a project.
    owners = {owner_id}
    while True:
        children = {str(c.get('shade_agent_id')) for c in contracts
                    if c.get('parent_agent_id') in owners and c.get('shade_agent_id')}
        if children <= owners:
            break
        owners.update(children)
    rows = []
    for contract in contracts:
        if contract.get('parent_agent_id') not in owners or contract.get('status') not in ACTIVE:
            continue
        phases = contract.get('phases') or []
        phase = next((p for p in phases if p.get('phase_id') == contract.get('current_phase_id')), {})
        # Between phases current_phase_id can still point at the completed one.
        if phase.get('status') == 'completed':
            phase = next((p for p in phases if p.get('status') in {'running', 'queued', 'pending'}), {})
        stale = is_contract_stale(contract)
        rows.append({
            'contract_id': _compact(contract.get('id'), 80),
            'agent_id': _compact(contract.get('shade_agent_id'), 80),
            'title': _compact(contract.get('goal')) or 'Subagent task',
            'phase': _compact(phase.get('name'), 30),
            'status': 'stalled' if stale else _compact(contract.get('status'), 20),
        })
    rows.sort(key=lambda r: (r['status'] != 'stalled', r['contract_id']))
    snapshot.update(items=rows[:50], total=len(rows))
    return snapshot
