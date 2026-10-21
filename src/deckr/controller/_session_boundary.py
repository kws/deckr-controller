"""Serialized local event handling; adapters never execute inside this boundary."""

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import anyio


@dataclass(frozen=True, slots=True)
class SessionEvent:
    kind: str
    payload: Any = None


class SessionBoundary:
    def __init__(self):
        self._lock = anyio.Lock()
        self._depth = 0
        self.sequence = 0

    def statistics(self):
        return self._lock.statistics()

    async def __aenter__(self):
        owner = self._lock.statistics().owner
        if owner is None or owner.id != anyio.get_current_task().id:
            await self._lock.acquire()
        self._depth += 1
        return self

    async def __aexit__(self, *_):
        self._depth -= 1
        if self._depth == 0:
            self.sequence += 1
            self._lock.release()

    @asynccontextmanager
    async def event(self, event: SessionEvent):
        async with self:
            yield event
