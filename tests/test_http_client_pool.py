from __future__ import annotations

import asyncio

import httpx

from charon.providers.http_client import AsyncClientPool


def test_client_pool_reuses_client_on_same_loop():
    async def run():
        transport = httpx.MockTransport(
            lambda request: httpx.Response(200, json={'ok': True})
        )
        pool = AsyncClientPool(timeout=2)
        first, reused_first = await pool.get(transport=transport)
        second, reused_second = await pool.get(transport=transport)
        assert first is second
        assert reused_first is False
        assert reused_second is True
        response = await second.get('http://test/reuse')
        assert response.json() == {'ok': True}
        await pool.aclose()

    asyncio.run(run())


def test_client_pool_never_moves_client_between_event_loops():
    pool = AsyncClientPool(timeout=2)

    async def get_client_id():
        client, reused = await pool.get()
        return id(client), reused

    first_id, first_reused = asyncio.run(get_client_id())
    second_id, second_reused = asyncio.run(get_client_id())

    assert first_reused is False
    assert second_reused is False
    assert first_id != second_id
