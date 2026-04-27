"""Async render backends and per-device render dispatch with stale-result dropping."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Protocol

import anyio

from deckr.controller._render import RenderRequest, RenderResult, render_request_to_jpeg

if TYPE_CHECKING:
    from deckr.controller._hardware_service import HardwareCommandService


class SlotOutput(Protocol):
    async def write(self, frame: bytes) -> None: ...

    async def clear(self) -> None: ...


logger = logging.getLogger(__name__)


def _init_render_worker() -> None:
    """Let the parent process own Ctrl-C handling for render workers.

    Without this, ProcessPoolExecutor workers inherit the terminal's SIGINT and can
    emit noisy KeyboardInterrupt tracebacks while the controller is already shutting
    down gracefully.
    """

    signal.signal(signal.SIGINT, signal.SIG_IGN)


def default_render_workers() -> int:
    """Default process-pool size for render workers."""

    cpu_count = os.cpu_count() or 1
    return min(4, max(1, cpu_count - 1))


class RenderBackend(Protocol):
    """Async backend that turns a RenderRequest into a RenderResult."""

    async def render(self, request: RenderRequest) -> RenderResult: ...

    async def aclose(self) -> None: ...


class ThreadRenderBackend:
    """Same-process backend that still renders off the event loop."""

    async def render(self, request: RenderRequest) -> RenderResult:
        try:
            frame = await anyio.to_thread.run_sync(render_request_to_jpeg, request)
            return RenderResult(
                context_id=request.context_id,
                binding_id=request.binding_id,
                slot_id=request.slot_id,
                generation=request.generation,
                frame=frame,
            )
        except Exception as exc:
            logger.exception(
                "Thread render failed for %s:%s gen=%s",
                request.context_id,
                request.slot_id,
                request.generation,
            )
            return RenderResult(
                context_id=request.context_id,
                binding_id=request.binding_id,
                slot_id=request.slot_id,
                generation=request.generation,
                frame=None,
                error=str(exc),
            )

    async def aclose(self) -> None:
        return


class ProcessPoolRenderBackend:
    """Multiprocess render backend using ProcessPoolExecutor."""

    def __init__(self, *, max_workers: int | None = None):
        self._executor = ProcessPoolExecutor(
            max_workers=max_workers or default_render_workers(),
            initializer=_init_render_worker,
        )

    async def render(self, request: RenderRequest) -> RenderResult:
        try:
            loop = asyncio.get_running_loop()
            frame = await loop.run_in_executor(
                self._executor, render_request_to_jpeg, request
            )
            return RenderResult(
                context_id=request.context_id,
                binding_id=request.binding_id,
                slot_id=request.slot_id,
                generation=request.generation,
                frame=frame,
            )
        except Exception as exc:
            logger.exception(
                "Process render failed for %s:%s gen=%s",
                request.context_id,
                request.slot_id,
                request.generation,
            )
            return RenderResult(
                context_id=request.context_id,
                binding_id=request.binding_id,
                slot_id=request.slot_id,
                generation=request.generation,
                frame=None,
                error=str(exc),
            )

    async def aclose(self) -> None:
        self._executor.shutdown(wait=True, cancel_futures=True)


@dataclass(slots=True)
class _SlotRenderState:
    generation: int = 0
    context_id: str | None = None
    binding_id: str | None = None
    output: SlotOutput | None = None
    running: bool = False
    pending_request: RenderRequest | None = None
    io_lock: anyio.Lock = field(default_factory=anyio.Lock)


class RenderDispatcher:
    """Per-device dispatcher that enforces last-write-wins by slot generation."""

    def __init__(
        self,
        *,
        command_service: HardwareCommandService,
        config_id: str,
        backend: RenderBackend,
        start_soon,
    ):
        self._command_service = command_service
        self._config_id = config_id
        self._backend = backend
        self._start_soon = start_soon
        self._lock = anyio.Lock()
        self._slots: dict[str, _SlotRenderState] = {}

    async def submit_request(
        self,
        *,
        slot_id: str,
        context_id: str,
        binding_id: str | None = None,
        request: RenderRequest | None,
        output: SlotOutput | None = None,
    ) -> int:
        """Submit a request for a slot, replacing any older pending work."""

        async with self._lock:
            state = self._slots.setdefault(slot_id, _SlotRenderState())
            state.generation += 1
            generation = state.generation
            state.context_id = context_id
            state.binding_id = binding_id
            if output is not None:
                state.output = output

            if request is None:
                state.pending_request = None
                return generation

            request = replace(
                request,
                context_id=context_id,
                binding_id=binding_id,
                slot_id=slot_id,
                generation=generation,
            )
            if state.running:
                state.pending_request = request
            else:
                state.running = True
                self._start_soon(self._run_slot, slot_id, request)
            return generation

    async def clear_slot(
        self,
        slot_id: str,
        *,
        context_id: str | None = None,
        binding_id: str | None = None,
        output: SlotOutput | None = None,
        clear_output: bool = True,
    ) -> int:
        """Invalidate queued/running renders for a slot and clear the device slot."""

        async with self._lock:
            state = self._slots.setdefault(slot_id, _SlotRenderState())
            state.generation += 1
            generation = state.generation
            if context_id is not None:
                state.context_id = context_id
            state.binding_id = binding_id
            if output is not None:
                state.output = output
            state.pending_request = None
            io_lock = state.io_lock
            target_output = state.output

        if clear_output:
            async with io_lock:
                if target_output is not None:
                    await target_output.clear()
                else:
                    await self._command_service.clear_slot(self._config_id, slot_id)
        return generation

    async def _run_slot(self, slot_id: str, request: RenderRequest) -> None:
        current = request
        while True:
            result = await self._backend.render(current)
            await self._apply_result(result)

            async with self._lock:
                state = self._slots.get(slot_id)
                if state is None:
                    return
                next_request = state.pending_request
                if next_request is None:
                    state.running = False
                    return
                state.pending_request = None
            current = next_request

    async def _apply_result(self, result: RenderResult) -> None:
        async with self._lock:
            state = self._slots.get(result.slot_id)
            if state is None:
                return
            io_lock = state.io_lock
            target_output = state.output

        async with io_lock:
            async with self._lock:
                state = self._slots.get(result.slot_id)
                if state is None:
                    return
                if state.generation != result.generation:
                    return
                if state.context_id != result.context_id:
                    return
                if state.binding_id != result.binding_id:
                    return
                target_output = state.output

            if result.frame is None:
                return
            if target_output is not None:
                await target_output.write(result.frame)
            else:
                await self._command_service.set_image(
                    self._config_id,
                    result.slot_id,
                    result.frame,
                )
