"""Reentrant asynchronous exclusion with cancellation-safe semaphore handoff."""

import anyio


class StateGate:
    def __init__(self):
        self._gate = anyio.Semaphore(1, max_value=1)
        self._owner: int | None = None
        self._depth = 0
        self.sequence = 0

    def statistics(self):
        return self._gate.statistics()

    async def __aenter__(self):
        task_id = anyio.get_current_task().id
        if self._owner != task_id:
            await anyio.lowlevel.checkpoint_if_cancelled()
            try:
                # A cancelled waiter may still be unwinding after the token was
                # released. Take the free token without queueing behind it.
                self._gate.acquire_nowait()
            except anyio.WouldBlock:
                await self._gate.acquire()
            self._owner = task_id
        self._depth += 1
        return self

    async def __aexit__(self, *_):
        self._depth -= 1
        if self._depth == 0:
            self.sequence += 1
            self._owner = None
            self._gate.release()
