"""Loop-aware reusable HTTP clients for streaming providers.

Interactive Charon keeps one asyncio loop for its lifetime, so a provider can
reuse TCP/TLS connections across turns.  Some embedded/agent runtimes still use
short-lived loops; this pool therefore keeps a separate client per live loop
instead of ever moving an ``httpx.AsyncClient`` across loop boundaries.
"""
from __future__ import annotations

import asyncio
import importlib.util
import threading
import weakref
from dataclasses import dataclass
from typing import Any

import httpx


_HTTP2_AVAILABLE = importlib.util.find_spec('h2') is not None


@dataclass
class _ClientEntry:
    loop_ref: weakref.ReferenceType[asyncio.AbstractEventLoop]
    key: tuple[Any, ...]
    client: httpx.AsyncClient


class AsyncClientPool:
    """Own one keep-alive client per event loop and transport configuration."""

    def __init__(
        self,
        *,
        timeout: float | httpx.Timeout = 300.0,
        max_connections: int = 20,
        max_keepalive_connections: int = 10,
        keepalive_expiry: float = 60.0,
    ) -> None:
        self._timeout = timeout
        self._limits = httpx.Limits(
            max_connections=max_connections,
            max_keepalive_connections=max_keepalive_connections,
            keepalive_expiry=keepalive_expiry,
        )
        self._entries: dict[int, _ClientEntry] = {}
        self._lock = threading.Lock()

    async def get(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[httpx.AsyncClient, bool]:
        """Return ``(client, reused)`` for the current running loop."""
        loop = asyncio.get_running_loop()
        loop_id = id(loop)
        key = (id(transport) if transport is not None else None,)
        stale: httpx.AsyncClient | None = None

        with self._lock:
            entry = self._entries.get(loop_id)
            if entry is not None:
                entry_loop = entry.loop_ref()
                if (
                    entry_loop is loop
                    and entry.key == key
                    and not entry.client.is_closed
                ):
                    return entry.client, True
                stale = entry.client
                self._entries.pop(loop_id, None)

            client_kwargs: dict[str, Any] = {
                'timeout': self._timeout,
                'limits': self._limits,
                'http2': _HTTP2_AVAILABLE,
            }
            if transport is not None:
                client_kwargs['transport'] = transport
            if headers:
                client_kwargs['headers'] = headers
            client = httpx.AsyncClient(**client_kwargs)
            self._entries[loop_id] = _ClientEntry(
                loop_ref=weakref.ref(loop),
                key=key,
                client=client,
            )

        if stale is not None and not stale.is_closed:
            try:
                await stale.aclose()
            except Exception:
                pass
        self._prune_closed_loops(exclude=loop_id)
        return client, False

    def _prune_closed_loops(self, *, exclude: int | None = None) -> None:
        """Forget clients whose owning loop no longer exists or is closed.

        A client bound to a closed loop cannot be cleanly awaited. Dropping it is
        still preferable to reusing its invalid transport in a new loop.
        """
        with self._lock:
            stale_ids = []
            for loop_id, entry in self._entries.items():
                if loop_id == exclude:
                    continue
                loop = entry.loop_ref()
                if loop is None or loop.is_closed():
                    stale_ids.append(loop_id)
            for loop_id in stale_ids:
                self._entries.pop(loop_id, None)

    async def aclose(self) -> None:
        """Close clients owned by the current or other still-running loops."""
        current = asyncio.get_running_loop()
        with self._lock:
            entries = list(self._entries.values())
            self._entries.clear()

        remote_futures = []
        for entry in entries:
            client = entry.client
            if client.is_closed:
                continue
            owner = entry.loop_ref()
            try:
                if owner is current:
                    await client.aclose()
                elif owner is not None and owner.is_running():
                    remote_futures.append(
                        asyncio.run_coroutine_threadsafe(client.aclose(), owner)
                    )
            except Exception:
                continue
        for future in remote_futures:
            try:
                future.result(timeout=2)
            except Exception:
                pass


__all__ = ['AsyncClientPool']
