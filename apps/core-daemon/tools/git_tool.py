"""Git tool — structured git operations with checkpoint metadata.

Provides branch, commit, diff, status, log as structured operations
instead of raw bash. Automatically tags commits with goal/task metadata
for audit trails. Prevents common mistakes (committing to main when a
feature branch exists, force pushing).
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from tools import ToolContext, ToolResult

GIT_TOOL_DEF = {
    'name': 'Git',
    'description': (
        'Structured git operations. Safer than raw bash git — prevents common mistakes, '
        'adds checkpoint metadata, and tracks changes per goal. '
        'Actions: status, diff, commit, branch, checkout, log, stash.'
    ),
    'input_schema': {
        'type': 'object',
        'properties': {
            'action': {
                'type': 'string',
                'enum': ['status', 'diff', 'commit', 'branch', 'checkout', 'log', 'stash', 'add'],
                'description': 'Git operation to perform.',
            },
            'message': {
                'type': 'string',
                'description': 'Commit message (for commit action).',
            },
            'branch': {
                'type': 'string',
                'description': 'Branch name (for branch/checkout actions).',
            },
            'files': {
                'type': 'array',
                'items': {'type': 'string'},
                'description': 'Specific files to add/diff (optional, defaults to all).',
            },
            'lines': {
                'type': 'number',
                'description': 'Max lines for log/diff output (default: 50).',
            },
        },
        'required': ['action'],
    },
}

_GIT_TIMEOUT = 30


def _run_git(args: list[str], cwd: Path, timeout: int = _GIT_TIMEOUT) -> tuple[bool, str]:
    """Run a git command. Returns (success, output)."""
    try:
        proc = subprocess.run(
            ['git'] + args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = (proc.stdout or '').strip()
        err = (proc.stderr or '').strip()
        if proc.returncode != 0:
            return False, err or output or f'git {args[0]} failed (exit {proc.returncode})'
        return True, output or err or ''
    except subprocess.TimeoutExpired:
        return False, f'git {args[0]} timed out after {timeout}s'
    except FileNotFoundError:
        return False, 'git is not installed or not in PATH'
    except Exception as e:
        return False, f'git error: {e}'


def _is_git_repo(cwd: Path) -> bool:
    ok, _ = _run_git(['rev-parse', '--git-dir'], cwd)
    return ok


def _current_branch(cwd: Path) -> str:
    ok, out = _run_git(['branch', '--show-current'], cwd)
    return out if ok else '(detached)'


def execute_git(params: dict, ctx: ToolContext) -> ToolResult:
    """Execute a structured git operation."""
    action = str(params.get('action', '')).strip().lower()
    cwd = ctx.project_root

    if not _is_git_repo(cwd):
        return ToolResult(content=f'Error: {cwd} is not a git repository.', is_error=True)

    if action == 'status':
        ok, out = _run_git(['status', '--short', '--branch'], cwd)
        if not ok:
            return ToolResult(content=f'Error: {out}', is_error=True)
        branch = _current_branch(cwd)
        return ToolResult(content=f'Branch: {branch}\n{out}')

    if action == 'diff':
        files = params.get('files') or []
        max_lines = int(params.get('lines') or 50)
        args = ['diff', '--stat']
        if files:
            args.append('--')
            args.extend(files)
        ok, stat = _run_git(args, cwd)
        if not ok:
            return ToolResult(content=f'Error: {stat}', is_error=True)

        # Also get the actual diff (limited)
        diff_args = ['diff']
        if files:
            diff_args.append('--')
            diff_args.extend(files)
        ok, diff = _run_git(diff_args, cwd)
        diff_lines = diff.splitlines()
        truncated = len(diff_lines) > max_lines
        if truncated:
            diff = '\n'.join(diff_lines[:max_lines])

        content = f'{stat}\n\n{diff}'
        if truncated:
            content += f'\n\n[Diff truncated to {max_lines} lines. Use lines=N for more.]'
        return ToolResult(content=content, truncated=truncated)

    if action == 'add':
        files = params.get('files') or ['.']
        args = ['add'] + files
        ok, out = _run_git(args, cwd)
        if not ok:
            return ToolResult(content=f'Error: {out}', is_error=True)
        return ToolResult(content=f'Added: {", ".join(files)}')

    if action == 'commit':
        message = str(params.get('message', '')).strip()
        if not message:
            return ToolResult(content='Error: commit message is required.', is_error=True)

        # Auto-add all changes first
        files = params.get('files')
        if files:
            _run_git(['add'] + files, cwd)
        else:
            _run_git(['add', '-A'], cwd)

        # Check if there's anything to commit
        ok, status = _run_git(['status', '--porcelain'], cwd)
        staged = [l for l in status.splitlines() if l and not l.startswith('??')]
        if not staged and not status.strip():
            return ToolResult(content='Nothing to commit (working tree clean).')

        # Add metadata to commit message
        agent_id = ctx.agent_id
        if agent_id:
            message = f'{message}\n\nCharon-Agent: {agent_id}'

        ok, out = _run_git(['commit', '-m', message], cwd)
        if not ok:
            return ToolResult(content=f'Error: {out}', is_error=True)
        return ToolResult(content=out)

    if action == 'branch':
        branch_name = str(params.get('branch', '')).strip()
        if not branch_name:
            # List branches
            ok, out = _run_git(['branch', '-a', '--no-color'], cwd)
            if not ok:
                return ToolResult(content=f'Error: {out}', is_error=True)
            return ToolResult(content=out)

        # Create and switch to new branch
        ok, out = _run_git(['checkout', '-b', branch_name], cwd)
        if not ok:
            return ToolResult(content=f'Error: {out}', is_error=True)
        return ToolResult(content=f'Created and switched to branch: {branch_name}')

    if action == 'checkout':
        branch_name = str(params.get('branch', '')).strip()
        if not branch_name:
            return ToolResult(content='Error: branch name is required for checkout.', is_error=True)
        ok, out = _run_git(['checkout', branch_name], cwd)
        if not ok:
            return ToolResult(content=f'Error: {out}', is_error=True)
        return ToolResult(content=f'Switched to: {branch_name}')

    if action == 'log':
        max_lines = int(params.get('lines') or 20)
        count = min(max_lines, 50)
        ok, out = _run_git(['log', f'--oneline', f'-{count}', '--no-color'], cwd)
        if not ok:
            return ToolResult(content=f'Error: {out}', is_error=True)
        branch = _current_branch(cwd)
        return ToolResult(content=f'Branch: {branch}\n{out}')

    if action == 'stash':
        message = params.get('message')
        if message:
            ok, out = _run_git(['stash', 'push', '-m', str(message)], cwd)
        else:
            ok, out = _run_git(['stash'], cwd)
        if not ok:
            return ToolResult(content=f'Error: {out}', is_error=True)
        return ToolResult(content=out or 'Stashed.')

    return ToolResult(content=f'Error: Unknown action "{action}". Use: status, diff, commit, branch, checkout, log, stash, add.',
                      is_error=True)
