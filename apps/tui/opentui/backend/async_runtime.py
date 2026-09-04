"""Long-lived asyncio runtime for the TUI backend.

The backend process is synchronous at its stdin boundary, but model providers
and the conversation engine are asynchronous.  Running ``asyncio.run()`` for
every chat destroys the loop (and therefore every keep-alive connection) after
each response.  ``AsyncRuntime`` owns one daemon loop for the process lifetime
and lets worker threads submit coroutines to it safely.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import threading
from collections.abc import Coroutine
from typing import Any


class AsyncRuntime:
    """A small, process-local asyncio host with deterministic shutdown."""

    def __init__(self, *, name: str = 'charon-async-runtime') -> None:
        self._name = name
        self._ready = threading.Event()
        self._closed = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread = threading.Thread(
            target=self._thread_main,
            name=name,
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout=5):
            raise RuntimeError('Charon async runtime did not start within 5 seconds')

    def _thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        self._ready.set()
        try:
            loop.run_forever()
        finally:
            pending = list(asyncio.all_tasks(loop))
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(
                    asyncio.gather(*pending, return_exceptions=True)
                )
            loop.run_until_complete(loop.shutdown_asyncgens())
            try:
                loop.run_until_complete(loop.shutdown_default_executor())
            except Exception:
                pass
            loop.close()
            self._closed.set()

    @property
    def loop(self) -> asyncio.AbstractEventLoop:
        loop = self._loop
        if loop is None or loop.is_closed() or not loop.is_running():
            raise RuntimeError('Charon async runtime is unavailable')
        return loop

    def submit(
        self,
        coro: Coroutine[Any, Any, Any],
    ) -> concurrent.futures.Future[Any]:
        """Submit a coroutine from any non-runtime thread."""
        if self._closed.is_set():
            coro.close()
            raise RuntimeError('Charon async runtime is closed')
        if threading.current_thread() is self._thread:
            coro.close()
            raise RuntimeError('Cannot synchronously submit from the async runtime thread')
        try:
            return asyncio.run_coroutine_threadsafe(coro, self.loop)
        except BaseException:
            coro.close()
            raise

    def run(self, coro: Coroutine[Any, Any, Any]) -> Any:
        """Submit and synchronously wait for a coroutine's result."""
        return self.submit(coro).result()

    def shutdown(self, *, timeout: float = 5.0) -> None:
        """Stop the loop after cancelling pending work. Idempotent."""
        if self._closed.is_set():
            return
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        if threading.current_thread() is not self._thread:
            self._thread.join(timeout=timeout)


__all__ = ['AsyncRuntime']
