"""Helpers to launch external agent sessions wrapped in Charon's Boat."""
from __future__ import annotations

import json
import os
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
    'claude-code': {
        'command': ['claude'],
        'display_name': 'Claude Code',
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


# ── Claude Code integration ────────────────────────────────���────────

# Charon's own install root (src/charon/fleet/<this file> -> parents[3]), not the
# target project — the hook bridge ships with Charon, not with the project it wraps.
_HOOKS_DIR = Path(__file__).resolve().parents[3] / 'tools' / 'claude-code-hooks'


def _sessions_dir(state_dir: Path) -> Path:
    d = state_dir / 'claude_code_sessions'
    d.mkdir(parents=True, exist_ok=True)
    return d


def launch_claude_code_session(
    *,
    state_dir: Path | str,
    project_root: Path | str,
    task_instruction: str,
    room_id: str | None = None,
    session_name: str | None = None,
    judge_eval_command: str = '',
    judge_target_score: float = 1.0,
    interactive: bool = False,
    max_turns: int = 25,
) -> dict[str, Any]:
    """Launch a Claude Code session orchestrated by Charon via hooks.

    Creates a room, writes hook settings, and spawns Claude Code.
    """
    state_dir = Path(state_dir).resolve()
    project_root = Path(project_root).resolve()

    if shutil.which('claude') is None:
        return {'ok': False, 'error': 'Claude Code CLI not found in PATH'}

    bridge = _HOOKS_DIR / 'charon_hook_bridge.py'
    if not bridge.exists():
        return {'ok': False, 'error': f'Hook bridge not found at {bridge}'}

    # Lazy import — only needed when actually launching.
    from charon.agents.inter_agent_rooms import append_event, create_room, load_room, update_room_meta, set_room_status

    name = session_name or f"cc-{_slug(task_instruction[:40])}-{int(time.time())}"

    # Build room meta.
    meta: dict[str, Any] = {
        'task_instruction': task_instruction,
        'project': str(project_root),
        'session_name': name,
        'launcher': 'charon',
        'launch_mode': 'interactive' if interactive else 'batch',
        'hooks_configured': False,
    }
    if judge_eval_command:
        meta['judge_on_stop'] = True
        meta['judge_eval_command'] = judge_eval_command
        meta['judge_target_score'] = judge_target_score

    # Create room if not resuming an existing one.
    if room_id:
        room = load_room(state_dir, room_id)
        if not room:
            return {'ok': False, 'error': f'Room {room_id} not found'}
        room_id = room['id']
        update_room_meta(state_dir, room_id, **meta)
    else:
        room = create_room(
            state_dir,
            kind='claude-code',
            title=task_instruction[:60],
            project=str(project_root),
            participants=[
                {'name': 'claude-code', 'role': 'implementer', 'session': name},
                {'name': 'charon', 'role': 'orchestrator'},
            ],
            meta=meta,
        )
        room_id = room['id']

    # Write/merge hook settings into the target project.
    sys.path.insert(0, str(_HOOKS_DIR))
    from generate_settings import write_settings
    settings_path = write_settings(project_root, bridge)
    update_room_meta(
        state_dir,
        room_id,
        hooks_configured=True,
        settings_path=str(settings_path),
    )

    # Write session mapping.
    created_at = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
    mapping = {
        'session_name': name,
        'room_id': room_id,
        'project': str(project_root),
        'task_instruction': task_instruction,
        'created_at': created_at,
        'interactive': interactive,
    }
    mapping_path = _sessions_dir(state_dir) / f'{name}.json'
    mapping_path.write_text(json.dumps(mapping, indent=2) + '\n')

    # Build environment.
    env = dict(os.environ)
    env['CHARON_STATE_DIR'] = str(state_dir)
    env['CHARON_ROOM_ID'] = room_id

    if interactive:
        # Wrap in charons-boat for PTY tracking.
        boat = _boat_script(project_root)
        if not boat.exists():
            boat = _boat_script(Path(__file__).resolve().parent.parent.parent)
        cmd = [str(boat), 'wrap', '--name', name, '--', 'claude']
    else:
        # Batch mode: -p with structured output.
        cmd = [
            'claude', '-p', task_instruction,
            '--output-format', 'json',
            '--max-turns', str(max_turns),
        ]

    append_event(state_dir, room_id, {
        'type': 'launcher_started',
        'launcher': 'claude-code',
        'interactive': interactive,
        'command': cmd,
        'session_name': name,
        'mapping_path': str(mapping_path),
    })

    try:
        if interactive:
            proc = subprocess.Popen(
                cmd,
                cwd=str(project_root),
                env=env,
            )
            update_room_meta(
                state_dir,
                room_id,
                pid=proc.pid,
                command=cmd,
                mapping_path=str(mapping_path),
            )
            return {
                'ok': True,
                'room_id': room_id,
                'session_name': name,
                'settings_path': str(settings_path),
                'pid': proc.pid,
                'interactive': interactive,
                'command': cmd,
                'message': f'Claude Code session launched. Room: {room_id}',
            }

        result = subprocess.run(
            cmd,
            cwd=str(project_root),
            env=env,
            capture_output=True,
            text=True,
        )
    except Exception as e:
        append_event(state_dir, room_id, {
            'type': 'launcher_error',
            'message': str(e),
        })
        set_room_status(state_dir, room_id, 'failed', summary=f'Launch failed: {e}')
        return {'ok': False, 'error': str(e), 'room_id': room_id}

    stdout = result.stdout or ''
    stderr = result.stderr or ''
    stdout_preview = stdout[:4000]
    stderr_preview = stderr[:4000]
    parsed_output = None
    parse_error = ''
    if stdout.strip():
        try:
            parsed_output = json.loads(stdout)
        except Exception as e:
            parse_error = str(e)

    append_event(state_dir, room_id, {
        'type': 'batch_process_completed',
        'returncode': result.returncode,
        'stdout_preview': stdout_preview,
        'stderr_preview': stderr_preview,
        'parsed_json': isinstance(parsed_output, dict),
    })

    summary = 'Claude Code batch session completed.'
    status = 'completed' if result.returncode == 0 else 'failed'
    output_payload: dict[str, Any] = {
        'returncode': result.returncode,
        'stdout_preview': stdout_preview,
        'stderr_preview': stderr_preview,
        'output_parsed': isinstance(parsed_output, dict),
    }
    if isinstance(parsed_output, dict):
        output_payload['result'] = parsed_output.get('result')
        output_payload['stop_reason'] = parsed_output.get('stop_reason')
        output_payload['session_id'] = parsed_output.get('session_id')
        summary = str(parsed_output.get('result') or summary)[:240]
    elif parse_error:
        output_payload['parse_error'] = parse_error

    update_room_meta(
        state_dir,
        room_id,
        batch_output=output_payload,
        mapping_path=str(mapping_path),
    )
    set_room_status(state_dir, room_id, status, summary=summary)

    return {
        'ok': result.returncode == 0,
        'room_id': room_id,
        'session_name': name,
        'settings_path': str(settings_path),
        'interactive': interactive,
        'command': cmd,
        'returncode': result.returncode,
        'stdout': stdout_preview,
        'stderr': stderr_preview,
        'parsed_output': parsed_output,
        'message': f'Claude Code batch session finished. Room: {room_id}',
    }
