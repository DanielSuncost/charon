"""Shared cross-process OAuth refresh lock.

Codex and Anthropic OAuth refresh tokens are single-use: two Charon processes
(or two per-call provider instances) refreshing at the same time invalidate
each other's token. Both providers serialize refreshes through an flock on the
auth-store lockfile, re-reading disk state inside the lock so that only one
process actually performs the network refresh while the rest pick up the fresh
token from disk.

This is that orchestration, factored out of the two providers (which had
near-identical copies). Each provider supplies the three token-specific pieces:
how to re-read tokens from disk, how to tell whether the current token is
fresh, and how to perform the actual refresh.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Awaitable, Callable

try:
    from charon.infra.diagnostics import record as _diag
except Exception:  # diagnostics is best-effort and must never block import
    def _diag(*args, **kwargs):
        return None


async def locked_refresh(
    lock_path: str,
    *,
    read_from_disk: Callable[[], None],
    is_fresh: Callable[[], bool],
    do_refresh: Callable[[], Awaitable[bool]],
    wait_attempts: int = 30,
    wait_interval: float = 0.5,
) -> bool:
    """Perform an OAuth refresh under a cross-process file lock.

    Acquires an exclusive flock on `lock_path` (non-blocking first, then polling
    up to wait_attempts*wait_interval seconds). While holding the lock it calls
    read_from_disk() and skips the network refresh when is_fresh() already
    reports a valid token (another process refreshed first); otherwise it awaits
    do_refresh(). If the lockfile can't be created it falls back to an unlocked
    do_refresh(); if the wait times out it returns whatever is on disk.

    Returns True if the token is fresh afterwards.
    """
    import fcntl

    try:
        Path(lock_path).parent.mkdir(parents=True, exist_ok=True)
        lock_fd = open(lock_path, 'w')
    except Exception as e:
        # Can't create a lock — fall back to an unlocked refresh.
        _diag('oauth_lock', 'lockfile creation failed; refreshing without cross-process lock', error=e)
        return await do_refresh()

    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (IOError, OSError):
            # Another process holds the lock — wait for it to finish.
            for _ in range(wait_attempts):
                await asyncio.sleep(wait_interval)
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except (IOError, OSError):
                    continue
            else:
                # Timed out — use whatever the other process wrote.
                read_from_disk()
                return is_fresh()

        try:
            # Holding the lock: another process may have refreshed while we
            # waited, so re-read disk and re-check before spending the token.
            read_from_disk()
            if is_fresh():
                return True
            return await do_refresh()
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
    finally:
        lock_fd.close()


__all__ = ['locked_refresh']
