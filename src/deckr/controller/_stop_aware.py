from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import anyio


async def sleep_until_stopping(stopping: anyio.Event, seconds: float) -> bool:
    """Sleep for up to seconds, returning True when stopping interrupts the wait."""

    if stopping.is_set():
        return True
    with anyio.move_on_after(seconds):
        await stopping.wait()
    return stopping.is_set()


@asynccontextmanager
async def cancel_on_stopping(stopping: anyio.Event) -> AsyncIterator[None]:
    """Cancel the enclosed await when the controller shutdown event is set."""

    with anyio.CancelScope() as cancel_scope:
        async with anyio.create_task_group() as tg:

            async def cancel_when_stopping() -> None:
                await stopping.wait()
                cancel_scope.cancel()

            tg.start_soon(cancel_when_stopping)
            try:
                yield
            finally:
                tg.cancel_scope.cancel()
