#!/usr/bin/env python3
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
STATE_DIR = ROOT / '.charon_state'
AGENTS_FILE = STATE_DIR / 'agents_runtime.json'

DEFAULT_FIELDS = {
    'id': '',
    'name': '',
    'type': 'unknown',
    'source': 'local',
    'mode': '',
    'goal': '',
    'status': 'idle',
    'current_task': '',
    'pid': None,
    'command': '',
    'args': '',
    'provider': '',
    'provider_usage_pct': None,
    'total_tokens': None,
    'prompt_tokens': None,
    'completion_tokens': None,
    'cost_estimate_usd': None,
    'project_context_summary': '',
    'preferences_model_summary': '',
    'last_heartbeat': '',
    'updated_at': '',
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_agents() -> list[dict]:
    if not AGENTS_FILE.exists():
        return []
    try:
        return json.loads(AGENTS_FILE.read_text())
    except Exception:
        return []


def _save_agents(agents: list[dict]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    AGENTS_FILE.write_text(json.dumps(agents, indent=2))


def _normalize(payload: dict) -> dict:
    data = {**DEFAULT_FIELDS, **payload}
    if not data.get('id'):
        raise ValueError('agent id required')
    return data


def list_agents(include_stopped: bool = True) -> list[dict]:
    agents = _load_agents()
    if include_stopped:
        return agents
    return [a for a in agents if a.get('status') not in ('stopped',)]


def get_agent(agent_id: str) -> dict | None:
    for agent in _load_agents():
        if agent.get('id') == agent_id:
            return agent
    return None


def upsert_agent(payload: dict) -> dict:
    agents = _load_agents()
    data = _normalize(payload)
    now = _now()
    data['updated_at'] = now
    data['last_heartbeat'] = data.get('last_heartbeat') or now

    for idx, agent in enumerate(agents):
        if agent.get('id') == data['id']:
            merged = {**agent, **data}
            agents[idx] = merged
            _save_agents(agents)
            return merged

    agents.append(data)
    _save_agents(agents)
    return data


def update_agent(agent_id: str, **fields) -> dict | None:
    agents = _load_agents()
    for idx, agent in enumerate(agents):
        if agent.get('id') == agent_id:
            agent = {**agent, **fields}
            agent['updated_at'] = _now()
            agents[idx] = agent
            _save_agents(agents)
            return agent
    return None


def prune_stale(ttl_seconds: int = 3600) -> int:
    agents = _load_agents()
    now = datetime.now(timezone.utc)
    keep: list[dict] = []
    removed = 0
    for agent in agents:
        ts = agent.get('updated_at') or agent.get('last_heartbeat')
        if not ts:
            keep.append(agent)
            continue
        try:
            parsed = datetime.fromisoformat(ts)
        except Exception:
            keep.append(agent)
            continue
        age = (now - parsed).total_seconds()
        if age > ttl_seconds:
            removed += 1
        else:
            keep.append(agent)
    if removed:
        _save_agents(keep)
    return removed


__all__ = [
    'list_agents',
    'get_agent',
    'upsert_agent',
    'update_agent',
    'prune_stale',
]
