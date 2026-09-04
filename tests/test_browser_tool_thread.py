from __future__ import annotations

import asyncio
import threading
import time
import warnings

import pytest

from charon.tools import browser_tool


class _AliveThread:
    def is_alive(self) -> bool:
        return True


class _ReadyLoop:
    def is_closed(self) -> bool:
        return False

    def is_running(self) -> bool:
        return True


def test_existing_starting_browser_thread_waits_for_loop_readiness(monkeypatch):
    ready = threading.Event()
    loop = _ReadyLoop()
    monkeypatch.setattr(browser_tool, '_thread', _AliveThread())
    monkeypatch.setattr(browser_tool, '_loop', None)
    monkeypatch.setattr(browser_tool, '_ready', ready)

    def finish_startup() -> None:
        time.sleep(0.02)
        monkeypatch.setattr(browser_tool, '_loop', loop)
        ready.set()

    starter = threading.Thread(target=finish_startup)
    starter.start()
    try:
        assert browser_tool._ensure_thread() is loop
    finally:
        starter.join()


def test_run_closes_coroutine_when_browser_loop_startup_fails(monkeypatch):
    monkeypatch.setattr(
        browser_tool,
        '_ensure_thread',
        lambda: (_ for _ in ()).throw(RuntimeError('loop failed')),
    )

    coro = asyncio.sleep(0)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always')
        with pytest.raises(RuntimeError, match='loop failed'):
            browser_tool._run(coro)
        del coro

    assert not any('was never awaited' in str(item.message) for item in caught)
