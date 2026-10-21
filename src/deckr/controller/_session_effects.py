"""Bounded, ordered adapter execution outside the device transition boundary."""

import logging
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import anyio

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Effect:
    name: str
    run: Callable[[], Awaitable[None]]
    timeout: float = 1.0
    cleanup: bool = False


class SessionEffects:
    """FIFO per resource; independent resources never wait for one another.

    Input is delivered once. A timeout is a failure, never a retry. Admission is
    bounded so a stalled adapter cannot accumulate unlimited work. The caller
    must cancel local interactions when input admission fails. Cleanup has its
    own bounded reserve, while preserving FIFO ordering with ordinary work.
    """

    def __init__(self, start_soon: Callable, *, capacity: int = 256):
        self._start_soon = start_soon
        self._capacity = capacity
        self._queues: dict[str, deque[Effect]] = {}
        self._count = 0
        self._cleanup_count = 0
        self._capacity_callback = None
        self._notifying_capacity = False
        self._idle = anyio.Event()
        self._idle.set()

    def set_runner(self, start_soon: Callable) -> None:
        self._start_soon = start_soon

    def when_capacity_available(self, callback: Callable[[], Awaitable[None]]) -> None:
        """Coalesce a reconciliation wakeup for work refused before execution."""
        self._capacity_callback = callback

    def submit(self, resource: str, effect: Effect) -> bool:
        used = (
            self._cleanup_count if effect.cleanup else self._count - self._cleanup_count
        )
        if used >= self._capacity:
            logger.warning("Device effect queue full; refusing %s", effect.name)
            return False
        if self._count == 0 and not self._notifying_capacity:
            self._idle = anyio.Event()
        self._count += 1
        self._cleanup_count += effect.cleanup
        queue = self._queues.get(resource)
        if queue is None:
            self._queues[resource] = deque([effect])
            try:
                self._start_soon(self._run, resource)
            except RuntimeError:
                self._queues.pop(resource)
                self._count -= 1
                self._cleanup_count -= effect.cleanup
                if self._count == 0:
                    self._idle.set()
                logger.warning("Device effect runner closed; refusing %s", effect.name)
                return False
        else:
            queue.append(effect)
        return True

    async def _run(self, resource: str) -> None:
        queue = self._queues[resource]
        try:
            while queue:
                effect = queue[0]
                try:
                    with anyio.fail_after(effect.timeout):
                        await effect.run()
                except Exception:
                    logger.warning(
                        "Device effect failed: %s", effect.name, exc_info=True
                    )
                finally:
                    queue.popleft()
                    self._count -= 1
                    self._cleanup_count -= effect.cleanup
                if (
                    self._capacity_callback is not None
                    and not self._notifying_capacity
                    and self._count - self._cleanup_count < self._capacity
                ):
                    callback, self._capacity_callback = self._capacity_callback, None
                    self._notifying_capacity = True
                    try:
                        await callback()
                    except Exception:
                        logger.warning("Capacity reconciliation failed", exc_info=True)
                    finally:
                        self._notifying_capacity = False
        finally:
            self._count -= len(queue)
            self._cleanup_count -= sum(effect.cleanup for effect in queue)
            self._queues.pop(resource, None)
            if self._count == 0 and not self._notifying_capacity:
                self._idle.set()

    async def idle(self) -> None:
        """Drain current lifecycle/input effects, for shutdown and test barriers."""
        while self._count or self._notifying_capacity:
            await self._idle.wait()
