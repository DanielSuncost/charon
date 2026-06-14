"""Tests for the shared cross-process OAuth refresh lock."""
import asyncio
import fcntl
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'apps' / 'core-daemon'))

from oauth_lock import locked_refresh


def _run(coro):
    return asyncio.run(coro)


def test_skips_refresh_when_disk_already_fresh(tmp_path):
    lock = str(tmp_path / 'auth.json.lock')
    calls = {'refresh': 0, 'read': 0}

    async def do():
        calls['refresh'] += 1
        return True

    ok = _run(locked_refresh(
        lock,
        read_from_disk=lambda: calls.__setitem__('read', calls['read'] + 1),
        is_fresh=lambda: True,
        do_refresh=do,
    ))
    assert ok is True
    assert calls['refresh'] == 0  # another process already refreshed
    assert calls['read'] == 1     # we re-read disk under the lock


def test_refreshes_when_stale(tmp_path):
    lock = str(tmp_path / 'auth.json.lock')
    state = {'fresh': False}

    async def do():
        state['fresh'] = True
        return True

    ok = _run(locked_refresh(
        lock,
        read_from_disk=lambda: None,
        is_fresh=lambda: state['fresh'],
        do_refresh=do,
    ))
    assert ok is True
    assert state['fresh'] is True


def test_falls_back_to_unlocked_refresh_when_lock_uncreatable(tmp_path):
    # lock parent is a regular file → mkdir/open fails → unlocked fallback
    afile = tmp_path / 'afile'
    afile.write_text('x')
    lock = str(afile / 'sub' / 'auth.lock')
    calls = {'refresh': 0}

    async def do():
        calls['refresh'] += 1
        return True

    ok = _run(locked_refresh(
        lock,
        read_from_disk=lambda: None,
        is_fresh=lambda: False,
        do_refresh=do,
    ))
    assert ok is True
    assert calls['refresh'] == 1


def test_times_out_and_uses_disk_state(tmp_path):
    lock = str(tmp_path / 'auth.json.lock')
    held = open(lock, 'w')
    fcntl.flock(held, fcntl.LOCK_EX)  # another "process" holds the lock
    try:
        calls = {'refresh': 0, 'read': 0}

        async def do():
            calls['refresh'] += 1
            return True

        ok = _run(locked_refresh(
            lock,
            read_from_disk=lambda: calls.__setitem__('read', calls['read'] + 1),
            is_fresh=lambda: True,
            do_refresh=do,
            wait_attempts=2,
            wait_interval=0.01,
        ))
        assert ok is True            # is_fresh() reported True after re-reading disk
        assert calls['refresh'] == 0  # never got the lock, so never refreshed
        assert calls['read'] == 1
    finally:
        fcntl.flock(held, fcntl.LOCK_UN)
        held.close()
