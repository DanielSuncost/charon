"""Helpers to launch external agent sessions wrapped in Charon's Boat."""
from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


SUPPORTED_EXTERNAL_AGENTS = {
    'hermes': {
        'command': ['hermes'],
        'display_name': 'Hermes',
    },
    'pi': {
        'command': ['pi'],
        'display_name': 'pi',
    },
    'charon': {
        'command': [sys.executable, '-m', 'charon_native_session'],
        'display_name': 'Charon',
    },
}


def _slug(text: str) -> str:
    out = []
    last_dash = False
    for ch in (text or '').strip().lower():
        if ch.isalnum():
            out.append(ch)
            last_dash = False
        elif not last_dash:
            out.append('-')
            last_dash = True
    return ''.join(out).strip('-') or 'session'


def _boat_script(project_root: Path) -> Path:
    return project_root / 'tools' / 'charons-boat' / 'charons-boat'


def launch_wrapped_session(
    *,
    state_dir: Path | str,
    project_root: Path | str,
    agent_kind: str,
    session_name: str | None = None,
) -> dict[str, Any]:
    del state_dir  # reserved for future richer registration paths
    project_root = Path(project_root).resolve()
    agent_kind = str(agent_kind or '').strip().lower()
    spec = SUPPORTED_EXTERNAL_AGENTS.get(agent_kind)
    if not spec:
        return {'ok': False, 'error': f'Unsupported external agent: {agent_kind}'}

    boat = _boat_script(project_root)
    if not boat.exists():
        return {'ok': False, 'error': f"charons-boat not found at {boat}"}

    executable = spec['command'][0]
    if shutil.which(executable) is None:
        return {'ok': False, 'error': f"{spec['display_name']} executable not found in PATH"}

    name = session_name or f"{_slug(agent_kind)}-{int(time.time())}"
    cmd = [str(boat), 'wrap', '--name', name, '--', *spec['command']]
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(project_root),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        return {'ok': False, 'error': str(e)}

    return {
        'ok': True,
        'agent_kind': agent_kind,
        'display_name': spec['display_name'],
        'session_name': name,
        'tmux_session': f'boat-{name}',
        'command': cmd,
        'pid': proc.pid,
        'message': f"{spec['display_name']} session created.",
    }
