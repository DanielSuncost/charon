"""Fleet registry — loads ~/.charon/fleet.json to discover remote servers and agents."""
from __future__ import annotations

import json
from pathlib import Path

try:
    from charon.infra.diagnostics import record as _diag
except Exception:  # diagnostics is best-effort and must never block import
    def _diag(*args, **kwargs):
        return None

FLEET_PATH = Path.home() / '.charon' / 'fleet.json'

_DEFAULTS = {
    'ssh_options': [
        '-o', 'ControlMaster=auto',
        '-o', 'ControlPath=~/.ssh/charon-%r@%h:%p',
        '-o', 'ControlPersist=600',
    ],
    'boat_command': 'charons-boat stream',
}


def load_fleet(path: Path | None = None) -> dict:
    """Read fleet config, filling in defaults for missing fields."""
    p = path or FLEET_PATH
    if not p.exists():
        return {'version': 1, 'servers': []}
    try:
        data = json.loads(p.read_text())
    except Exception as e:
        _diag('fleet_registry', 'unreadable fleet.json; fleet treated as empty', error=e)
        return {'version': 1, 'servers': []}

    for server in data.get('servers', []):
        if not server.get('id'):
            server['id'] = server.get('host', 'unknown')
        if not server.get('ssh_options'):
            server['ssh_options'] = list(_DEFAULTS['ssh_options'])
        if not server.get('boat_command'):
            server['boat_command'] = _DEFAULTS['boat_command']
        for agent in server.get('agents', []):
            agent.setdefault('type', 'unknown')
            agent.setdefault('specialization', '')
            agent.setdefault('project', '')
            agent.setdefault('auto_start', False)
    return data


def list_remote_servers(path: Path | None = None) -> list[dict]:
    """Return all server entries from fleet config."""
    return load_fleet(path).get('servers', [])


def list_remote_agents(path: Path | None = None) -> list[dict]:
    """Flatten all agents across servers, each annotated with server info."""
    agents = []
    for server in list_remote_servers(path):
        for agent in server.get('agents', []):
            agents.append({
                **agent,
                'server_id': server['id'],
                'host': server['host'],
                'user': server.get('user', ''),
                'ssh_options': server.get('ssh_options', []),
                'boat_command': server.get('boat_command', ''),
            })
    return agents


def save_fleet(data: dict, path: Path | None = None) -> None:
    """Write fleet config back to disk."""
    p = path or FLEET_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(data, indent=2) + '\n')
    tmp.replace(p)
