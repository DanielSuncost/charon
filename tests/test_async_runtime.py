from __future__ import annotations

import asyncio

from backend.async_runtime import AsyncRuntime


def test_async_runtime_reuses_one_event_loop_across_calls():
    runtime = AsyncRuntime(name='charon-test-runtime')

    async def loop_identity():
        await asyncio.sleep(0)
        return id(asyncio.get_running_loop())

    try:
        assert runtime.run(loop_identity()) == runtime.run(loop_identity())
    finally:
        runtime.shutdown()


def test_async_runtime_shutdown_is_idempotent():
    runtime = AsyncRuntime(name='charon-test-runtime-shutdown')
    runtime.shutdown()
    runtime.shutdown()
    assert runtime._closed.is_set()
