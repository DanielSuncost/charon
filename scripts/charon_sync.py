#!/usr/bin/env python3
"""Charon Sync — secure state sync between machines via SSH + rsync.

Usage:
    charon sync register <host> [--user <user>] [--name <label>]
    charon sync push [--to <name>] [--project <project>]
    charon sync pull [--from <name>] [--project <project>]
    charon sync status
    charon sync list
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE_DIR = ROOT / '.charon_state'
SYNC_CONFIG = Path.home() / '.charon' / 'sync_remotes.json'

# Files/dirs that should never be synced
EXCLUDE = [
    'browser/',
    'hermes-conversation-runtime/',
    'deleted_*/',
    'process_logs/',
    'live_sessions/',
    '*.db-wal',
    '*.db-shm',
    'managed_processes.json',
    'debug.log',
    'run.log',
    'ui_errors.log',
]

# Always sync these regardless of project filter
GLOBAL_ALWAYS = [
    'user_model.json',
    'USER.md',
    'agents.json',
    'onboarding.json',
    'model_registry.json',
    'conversation_index.json',
    'x_bookmarks.json',
    'session_providers/',
    'auth/',
]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Config ────────────────────────────────────────────────────────────

def load_remotes() -> dict:
    if SYNC_CONFIG.exists():
        return json.loads(SYNC_CONFIG.read_text())
    return {'remotes': {}, 'last_sync': {}}


def save_remotes(config: dict):
    SYNC_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    SYNC_CONFIG.write_text(json.dumps(config, indent=2))


# ── Register ──────────────────────────────────────────────────────────

def cmd_register(args):
    host = args.host
    user = args.user or os.environ.get('USER', 'root')
    name = args.name or host.split('.')[0]

    # Test SSH connectivity
    print(f'Testing SSH connection to {user}@{host}...')
    target = f'{user}@{host}'
    result = subprocess.run(
        ['ssh', '-o', 'ConnectTimeout=10', '-o', 'BatchMode=yes', target, 'echo ok'],
        capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        print(f'SSH connection failed: {result.stderr.strip()}')
        print('Make sure your SSH key is set up: ssh-copy-id', target)
        sys.exit(1)
    print(f'SSH OK.')

    # Check if remote has charon installed
    result = subprocess.run(
        ['ssh', target, 'test -d ~/Projects/charon/.charon_state && echo found'],
        capture_output=True, text=True, timeout=15,
    )
    remote_has_state = 'found' in (result.stdout or '')

    # Check rsync
    result = subprocess.run(
        ['ssh', target, 'which rsync'],
        capture_output=True, text=True, timeout=10,
    )
    if result.returncode != 0:
        print('Warning: rsync not found on remote. Install it: sudo apt install rsync')

    config = load_remotes()
    config['remotes'][name] = {
        'host': host,
        'user': user,
        'has_state': remote_has_state,
        'registered_at': _now(),
    }
    save_remotes(config)
    print(f'Registered remote "{name}" → {user}@{host}')
    if remote_has_state:
        print('  Remote has existing .charon_state/')
    else:
        print('  Remote has no .charon_state/ yet (will be created on first push)')


# ── Project resolution ────────────────────────────────────────────────

def resolve_project_paths(project: str) -> list[str]:
    """Given a project name, return relative paths within .charon_state/ to sync."""
    paths = list(GLOBAL_ALWAYS)

    # Find project directory
    registry_path = STATE_DIR / 'projects' / 'registry.json'
    project_dirs = []
    if registry_path.exists():
        registry = json.loads(registry_path.read_text())
        for entry in registry.get('projects', []):
            name = entry.get('name', '') or entry.get('project_name', '')
            pid = entry.get('project_id', '')
            if project.lower() in name.lower() or project.lower() in pid.lower():
                # Find matching directory
                for d in (STATE_DIR / 'projects').iterdir():
                    if d.is_dir() and (pid in d.name or name.lower().replace(' ', '-') in d.name.lower()):
                        project_dirs.append(f'projects/{d.name}/')

    if not project_dirs:
        # Glob fallback
        for d in (STATE_DIR / 'projects').iterdir():
            if d.is_dir() and project.lower() in d.name.lower():
                project_dirs.append(f'projects/{d.name}/')

    paths.extend(project_dirs)
    paths.append('projects/registry.json')

    # Find agents linked to this project
    agents_path = STATE_DIR / 'agents.json'
    agent_ids = []
    if agents_path.exists():
        agents = json.loads(agents_path.read_text())
        for agent in agents if isinstance(agents, list) else agents.get('agents', []):
            agent_project = agent.get('project', '') or agent.get('project_id', '')
            if project.lower() in agent_project.lower():
                agent_ids.append(agent.get('id', ''))

    # Add agent directories and their conversations
    for aid in agent_ids:
        if aid:
            paths.append(f'agents/{aid}/')
            paths.append(f'conversations/{aid}.jsonl')
            # Also session conversations linked to this agent
            for f in (STATE_DIR / 'conversations').glob(f'session-*.jsonl'):
                paths.append(f'conversations/{f.name}')

    # Goals for this project
    for d in (STATE_DIR / 'goals' / 'projects').glob('*'):
        if project.lower() in d.name.lower():
            paths.append(f'goals/projects/{d.name}')
    paths.append('goals/')

    # SQLite (always needed for context store)
    paths.append('charon.db')
    paths.append('memory.db')

    return paths


def build_rsync_args(project: str | None) -> list[str]:
    """Build rsync --include/--exclude args."""
    args = []

    # Always exclude these
    for exc in EXCLUDE:
        args.extend(['--exclude', exc])

    if project:
        # Project-specific: include only resolved paths
        paths = resolve_project_paths(project)
        for p in paths:
            args.extend(['--include', p])
            # Include parent dirs
            parts = Path(p).parts
            for i in range(len(parts) - 1):
                args.extend(['--include', str(Path(*parts[:i+1])) + '/'])
        args.extend(['--exclude', '*'])
    # else: sync everything (minus excludes)

    return args


# ── SQLite safe backup ────────────────────────────────────────────────

def safe_sqlite_backup(db_path: Path, backup_path: Path):
    """Create a WAL-safe backup of a SQLite database."""
    if not db_path.exists():
        return
    # Use sqlite3 .backup command for consistency
    result = subprocess.run(
        ['sqlite3', str(db_path), f'.backup {backup_path}'],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        # Fallback: plain copy (less safe but usually works)
        shutil.copy2(db_path, backup_path)


# ── Push ──────────────────────────────────────────────────────────────

def cmd_push(args):
    config = load_remotes()
    name = args.to or _default_remote(config)
    if not name:
        print('No remote specified and no default. Use: charon sync register <host>')
        sys.exit(1)
    remote = config['remotes'].get(name)
    if not remote:
        print(f'Remote "{name}" not found. Registered: {", ".join(config["remotes"])}')
        sys.exit(1)

    target = f'{remote["user"]}@{remote["host"]}'
    project = args.project

    print(f'Pushing state to {name} ({target})...')
    if project:
        print(f'  Project filter: {project}')

    # Safe SQLite backup
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        for db_name in ('charon.db', 'memory.db'):
            db = STATE_DIR / db_name
            if db.exists():
                backup = tmp_path / db_name
                print(f'  Backing up {db_name}...')
                safe_sqlite_backup(db, backup)
                # Copy backup into state dir temporarily for rsync
                shutil.copy2(backup, STATE_DIR / f'.sync_{db_name}')

        # Build rsync command
        rsync_args = build_rsync_args(project)
        remote_state = f'{target}:~/Projects/charon/.charon_state/'

        # Ensure remote directory exists
        subprocess.run(['ssh', target, 'mkdir -p ~/Projects/charon/.charon_state/'],
                       capture_output=True, timeout=15)

        cmd = [
            'rsync', '-avz', '--progress',
            *rsync_args,
            str(STATE_DIR) + '/',
            remote_state,
        ]
        print(f'  Syncing...')
        result = subprocess.run(cmd, timeout=300)

        # Push the safe SQLite backups
        for db_name in ('charon.db', 'memory.db'):
            safe = STATE_DIR / f'.sync_{db_name}'
            if safe.exists():
                subprocess.run([
                    'rsync', '-avz',
                    str(safe),
                    f'{remote_state}{db_name}',
                ], capture_output=True, timeout=120)
                safe.unlink()

        if result.returncode == 0:
            config.setdefault('last_sync', {})[name] = {
                'direction': 'push',
                'project': project,
                'at': _now(),
            }
            save_remotes(config)
            print(f'Push complete.')
        else:
            print(f'Push failed (rsync exit code {result.returncode})')
            sys.exit(1)


# ── Pull ──────────────────────────────────────────────────────────────

def cmd_pull(args):
    config = load_remotes()
    name = getattr(args, 'from') or args.from_remote or _default_remote(config)
    if not name:
        print('No remote specified. Use: charon sync pull --from <name>')
        sys.exit(1)
    remote = config['remotes'].get(name)
    if not remote:
        print(f'Remote "{name}" not found. Registered: {", ".join(config["remotes"])}')
        sys.exit(1)

    target = f'{remote["user"]}@{remote["host"]}'
    project = args.project

    print(f'Pulling state from {name} ({target})...')
    if project:
        print(f'  Project filter: {project}')

    rsync_args = build_rsync_args(project)
    remote_state = f'{target}:~/Projects/charon/.charon_state/'

    cmd = [
        'rsync', '-avz', '--progress',
        *rsync_args,
        remote_state,
        str(STATE_DIR) + '/',
    ]
    print(f'  Syncing...')
    result = subprocess.run(cmd, timeout=300)

    if result.returncode == 0:
        config.setdefault('last_sync', {})[name] = {
            'direction': 'pull',
            'project': project,
            'at': _now(),
        }
        save_remotes(config)
        print(f'Pull complete.')
    else:
        print(f'Pull failed (rsync exit code {result.returncode})')
        sys.exit(1)


# ── Status / List ─────────────────────────────────────────────────────

def cmd_status(args):
    config = load_remotes()
    remotes = config.get('remotes', {})
    if not remotes:
        print('No remotes registered. Use: charon sync register <host>')
        return

    print(f'Sync remotes ({len(remotes)}):')
    for name, info in remotes.items():
        host = info.get('host', '?')
        user = info.get('user', '?')
        has_state = '✓' if info.get('has_state') else '○'
        print(f'  {has_state} {name}: {user}@{host}')

    last = config.get('last_sync', {})
    if last:
        print(f'\nLast sync:')
        for name, info in last.items():
            direction = info.get('direction', '?')
            project = info.get('project', 'all')
            at = info.get('at', '?')
            arrow = '→' if direction == 'push' else '←'
            print(f'  {arrow} {name}: {direction} ({project}) at {at}')


def cmd_list(args):
    cmd_status(args)


# ── Helpers ───────────────────────────────────────────────────────────

def _default_remote(config: dict) -> str | None:
    remotes = config.get('remotes', {})
    if len(remotes) == 1:
        return next(iter(remotes))
    return None


# ── CLI ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description='Charon state sync')
    sub = parser.add_subparsers(dest='command')

    reg = sub.add_parser('register', help='Register a remote machine')
    reg.add_argument('host', help='SSH host (hostname or IP)')
    reg.add_argument('--user', help='SSH user (default: current user)')
    reg.add_argument('--name', help='Label for this remote (default: hostname)')

    push = sub.add_parser('push', help='Push state to remote')
    push.add_argument('--to', help='Remote name')
    push.add_argument('--project', help='Only sync this project')

    pull = sub.add_parser('pull', help='Pull state from remote')
    pull.add_argument('--from', dest='from_remote', help='Remote name')
    pull.add_argument('--project', help='Only sync this project')

    sub.add_parser('status', help='Show sync status')
    sub.add_parser('list', help='List registered remotes')

    args = parser.parse_args()

    if args.command == 'register':
        cmd_register(args)
    elif args.command == 'push':
        cmd_push(args)
    elif args.command == 'pull':
        cmd_pull(args)
    elif args.command in ('status', 'list'):
        cmd_status(args)
    else:
        parser.print_help()


if __name__ == '__main__':
    main()
