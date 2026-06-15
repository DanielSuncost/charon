"""Checkpoint manager — shadow git snapshots for safe rollback.

Creates a bare git repo separate from the user's project that tracks
file changes without polluting their repo. Uses GIT_DIR + GIT_WORK_TREE
separation.

Usage:
    mgr = CheckpointManager(state_dir, working_dir)
    cp_id = mgr.snapshot("before iteration 3")
    mgr.rollback(cp_id)
    mgr.diff(cp_id)
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass
class Checkpoint:
    id: str            # git commit sha
    label: str
    timestamp: str
    files_changed: int
    summary: str       # short diff stat


class CheckpointManager:
    """Shadow git repo for transparent filesystem snapshots.

    The shadow repo lives at:
        {state_dir}/checkpoints/{hash(working_dir)}/

    The user's working directory is the GIT_WORK_TREE.
    No .git folder is created in the user's project.
    """

    def __init__(self, state_dir: Path, working_dir: Path, scope: list[str] | None = None):
        self.state_dir = Path(state_dir)
        self.working_dir = Path(working_dir).resolve()
        self.scope = scope or []

        # Shadow repo keyed by working dir hash
        dir_hash = hashlib.sha256(str(self.working_dir).encode()).hexdigest()[:12]
        self.git_dir = self.state_dir / 'checkpoints' / dir_hash
        self._initialized = False

    def _env(self) -> dict[str, str]:
        """Git environment with shadow GIT_DIR."""
        env = os.environ.copy()
        env['GIT_DIR'] = str(self.git_dir)
        env['GIT_WORK_TREE'] = str(self.working_dir)
        # Suppress user's git hooks
        env['GIT_CONFIG_NOSYSTEM'] = '1'
        env.pop('GIT_HOOKS_PATH', None)
        # Ensure author info is always set (CI / bare env safety)
        env.setdefault('GIT_AUTHOR_NAME', 'charon-checkpoint')
        env.setdefault('GIT_AUTHOR_EMAIL', 'charon@localhost')
        env.setdefault('GIT_COMMITTER_NAME', 'charon-checkpoint')
        env.setdefault('GIT_COMMITTER_EMAIL', 'charon@localhost')
        return env

    def _git(self, *args: str, check: bool = True, timeout: int = 30) -> subprocess.CompletedProcess:
        """Run a git command against the shadow repo."""
        return self._git_env(None, *args, check=check, timeout=timeout)

    def _git_env(self, extra_env: dict | None, *args: str, check: bool = True,
                 timeout: int = 30) -> subprocess.CompletedProcess:
        """Run a git command, optionally overriding env (e.g. GIT_INDEX_FILE)."""
        self._ensure_init()
        env = self._env()
        if extra_env:
            env.update(extra_env)
        result = subprocess.run(
            ['git'] + list(args),
            cwd=str(self.working_dir),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if check and result.returncode != 0:
            raise RuntimeError(f'git {" ".join(args)} failed: {result.stderr.strip()}')
        return result

    def _ensure_init(self) -> None:
        """Initialize the shadow repo if it doesn't exist."""
        if self._initialized:
            return
        if not self.git_dir.exists():
            self.git_dir.mkdir(parents=True, exist_ok=True)
            # Init with only GIT_DIR (no WORK_TREE — git init --bare rejects it)
            init_env = os.environ.copy()
            init_env['GIT_DIR'] = str(self.git_dir)
            init_env['GIT_CONFIG_NOSYSTEM'] = '1'
            init_env.pop('GIT_WORK_TREE', None)
            init_env.setdefault('GIT_AUTHOR_NAME', 'charon-checkpoint')
            init_env.setdefault('GIT_AUTHOR_EMAIL', 'charon@localhost')
            init_env.setdefault('GIT_COMMITTER_NAME', 'charon-checkpoint')
            init_env.setdefault('GIT_COMMITTER_EMAIL', 'charon@localhost')
            subprocess.run(
                ['git', 'init', '--bare'],
                cwd=str(self.git_dir),
                env=init_env,
                capture_output=True,
                text=True,
                check=True,
            )
            # Create initial empty commit so we always have a HEAD
            commit_env = self._env()
            subprocess.run(
                ['git', 'commit', '--allow-empty', '-m', 'checkpoint-init'],
                cwd=str(self.working_dir),
                env=commit_env,
                capture_output=True,
                text=True,
                check=True,
            )
        self._initialized = True

    def snapshot(self, label: str = '') -> str:
        """Create a checkpoint. Returns checkpoint ID (commit SHA).

        If scope is set, only tracks files within scope paths.
        Otherwise tracks everything in working_dir.
        """
        # Add files to the shadow index
        if self.scope:
            for scope_path in self.scope:
                full = self.working_dir / scope_path
                if full.is_file():
                    self._git('add', '-f', scope_path, check=False)
                elif full.is_dir():
                    self._git('add', '-f', '-A', scope_path, check=False)
        else:
            self._git('add', '-A', check=False)

        # Check if there are any changes staged
        diff_result = self._git('diff', '--cached', '--stat', check=False)
        files_changed = 0
        summary = 'no changes'
        if diff_result.stdout.strip():
            stat_lines = diff_result.stdout.strip().splitlines()
            summary = stat_lines[-1].strip() if stat_lines else 'changes'
            files_changed = len(stat_lines) - 1  # last line is summary

        timestamp = datetime.now(timezone.utc).isoformat()
        label = label or f'checkpoint-{int(time.time())}'
        msg = f'{label}\n\ntimestamp: {timestamp}'

        # Commit (even if empty — so we have a restore point)
        self._git('commit', '--allow-empty', '-m', msg, check=False)

        # Get the SHA
        result = self._git('rev-parse', 'HEAD')
        sha = result.stdout.strip()

        return sha

    def rollback(self, checkpoint_id: str) -> bool:
        """Restore working directory to a previous checkpoint.

        Uses `git reset --hard` rather than `git checkout -- .`: checkout only
        restores paths present in the target tree, so files that a discarded
        iteration *added* (and which were captured by that iteration's
        snapshot) would survive and leak into the best-known state. reset --hard
        also removes those, while still restoring modified/deleted files.
        It only touches files tracked in the shadow repo — genuinely untracked
        files (e.g. out-of-scope when a scope is set) are left alone.
        """
        try:
            self._git('reset', '--hard', checkpoint_id, check=True)
            return True
        except (RuntimeError, subprocess.TimeoutExpired):
            return False

    def diff(self, checkpoint_id: str) -> str:
        """Show diff between current state and a checkpoint."""
        # First add current state to index for comparison
        if self.scope:
            for scope_path in self.scope:
                self._git('add', '-f', scope_path, check=False)
        else:
            self._git('add', '-A', check=False)

        result = self._git('diff', checkpoint_id, '--cached', '--stat', check=False)
        return result.stdout.strip()

    def changed_paths_under(self, checkpoint_id: str, paths: list[str]) -> list[str]:
        """Return which files under `paths` differ from `checkpoint_id`.

        Used to enforce frozen paths: anything returned here was modified,
        added, or deleted under a frozen path since the checkpoint — regardless
        of whether the change came from Write/Edit or from a shell command.

        Staging the working tree (so newly-created files are seen) must NOT
        mutate the persistent index: when snapshots are scope-limited, a stray
        `git add -A` here would stage out-of-scope files that the next scoped
        snapshot would then commit — and a later rollback to an earlier (scoped)
        checkpoint would *delete* them. So we stage into a throwaway index file
        and leave the real index untouched.
        """
        clean = [p.strip().strip('/') for p in (paths or []) if p.strip()]
        if not clean:
            return []
        self._ensure_init()
        tmp_index = self.git_dir / f'.frozen-index-{os.getpid()}'
        extra = {'GIT_INDEX_FILE': str(tmp_index)}
        try:
            # Seed the throwaway index from the checkpoint, then overlay the
            # working tree, so the diff reflects working-tree vs checkpoint.
            self._git_env(extra, 'read-tree', checkpoint_id, check=False)
            self._git_env(extra, 'add', '-A', check=False)
            result = self._git_env(extra, 'diff', checkpoint_id, '--cached',
                                   '--name-only', '--', *clean, check=False)
        finally:
            try:
                tmp_index.unlink()
            except OSError:
                pass
        return [l.strip() for l in result.stdout.splitlines() if l.strip()]

    def diff_full(self, checkpoint_id: str) -> str:
        """Full diff (not just stat) between current state and a checkpoint."""
        if self.scope:
            for scope_path in self.scope:
                self._git('add', '-f', scope_path, check=False)
        else:
            self._git('add', '-A', check=False)

        result = self._git('diff', checkpoint_id, '--cached', check=False)
        return result.stdout.strip()

    def list_checkpoints(self, limit: int = 20) -> list[Checkpoint]:
        """List recent checkpoints with labels and timestamps."""
        result = self._git(
            'log', '--format=%H|%s|%ai', f'-{limit}',
            check=False,
        )
        if not result.stdout.strip():
            return []

        checkpoints = []
        for line in result.stdout.strip().splitlines():
            parts = line.split('|', 2)
            if len(parts) < 3:
                continue
            sha, label, ts = parts
            if label == 'checkpoint-init':
                continue
            checkpoints.append(Checkpoint(
                id=sha.strip(),
                label=label.strip(),
                timestamp=ts.strip(),
                files_changed=0,
                summary='',
            ))
        return checkpoints

    def exists(self, checkpoint_id: str) -> bool:
        """Check if a checkpoint ID is valid."""
        result = self._git('cat-file', '-t', checkpoint_id, check=False)
        return result.returncode == 0 and 'commit' in result.stdout


__all__ = ['CheckpointManager', 'Checkpoint']
