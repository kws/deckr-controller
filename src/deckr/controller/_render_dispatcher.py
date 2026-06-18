"""Async render backends and per-device render dispatch with stale-result dropping."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Protocol

import anyio
import httpx

from deckr.controller._render import RenderRequest, RenderResult, render_request_to_jpeg

if TYPE_CHECKING:
    from deckr.controller._hardware_service import HardwareCommandService


class ControlOutput(Protocol):
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
                control_id=request.control_id,
                generation=request.generation,
                frame=frame,
            )
        except httpx.HTTPError as exc:
            logger.warning(
                "Thread render skipped after image fetch failed for %s:%s gen=%s: %s",
                request.context_id,
                request.control_id,
                request.generation,
                exc,
            )
            return RenderResult(
                context_id=request.context_id,
                binding_id=request.binding_id,
                control_id=request.control_id,
                generation=request.generation,
                frame=None,
                error=str(exc),
            )
        except Exception as exc:
            logger.exception(
                "Thread render failed for %s:%s gen=%s",
                request.context_id,
                request.control_id,
                request.generation,
            )
            return RenderResult(
                context_id=request.context_id,
                binding_id=request.binding_id,
                control_id=request.control_id,
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
            frame = await _run_in_process_pool(self._executor, request)
            return RenderResult(
                context_id=request.context_id,
                binding_id=request.binding_id,
                control_id=request.control_id,
                generation=request.generation,
                frame=frame,
            )
        except httpx.HTTPError as exc:
            logger.warning(
                "Process render skipped after image fetch failed for %s:%s gen=%s: %s",
                request.context_id,
                request.control_id,
                request.generation,
                exc,
            )
            return RenderResult(
                context_id=request.context_id,
                binding_id=request.binding_id,
                control_id=request.control_id,
                generation=request.generation,
                frame=None,
                error=str(exc),
            )
        except Exception as exc:
            logger.exception(
                "Process render failed for %s:%s gen=%s",
                request.context_id,
                request.control_id,
                request.generation,
            )
            return RenderResult(
                context_id=request.context_id,
                binding_id=request.binding_id,
                control_id=request.control_id,
                generation=request.generation,
                frame=None,
                error=str(exc),
            )

    async def aclose(self) -> None:
        self._executor.shutdown(wait=True, cancel_futures=True)


async def _run_in_process_pool(
    executor: ProcessPoolExecutor,
    request: RenderRequest,
) -> bytes:
    """Bridge to ProcessPoolExecutor while preserving worker initialization.

    AnyIO's process helper does not expose per-worker initializers, and the render
    backend needs `_init_render_worker()` so child processes ignore Ctrl-C.
    """

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(executor, render_request_to_jpeg, request)


@dataclass(slots=True)
class _ControlRenderState:
    generation: int = 0
    context_id: str | None = None
    binding_id: str | None = None
    output: ControlOutput | None = None
    running: bool = False
    pending_request: RenderRequest | None = None
    io_lock: anyio.Lock = field(default_factory=anyio.Lock)


class RenderDispatcher:
    """Per-device dispatcher that enforces last-write-wins by control generation."""

    def __init__(
        self,
        *,
        command_service: HardwareCommandService,
        config_id: str,
        backend: RenderBackend,
        start_soon,
        result_authorizer: Callable[[str, str | None, str | None], bool] | None = None,
    ):
        self._command_service = command_service
        self._config_id = config_id
        self._backend = backend
        self._start_soon = start_soon
        self._result_authorizer = result_authorizer
        self._lock = anyio.Lock()
        self._controls: dict[str, _ControlRenderState] = {}

    async def submit_request(
        self,
        *,
        control_id: str,
        context_id: str,
        binding_id: str | None = None,
        request: RenderRequest | None,
        output: ControlOutput | None = None,
    ) -> int:
        """Submit a request for a control, replacing any older pending work."""

        async with self._lock:
            state = self._controls.setdefault(control_id, _ControlRenderState())
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
                control_id=control_id,
                generation=generation,
            )
            if state.running:
                state.pending_request = request
            else:
                state.running = True
                self._start_soon(self._run_control, control_id, request)
            return generation

    async def clear_control(
        self,
        control_id: str,
        *,
        context_id: str | None = None,
        binding_id: str | None = None,
        output: ControlOutput | None = None,
        clear_output: bool = True,
    ) -> int:
        """Invalidate queued/running renders for a control and clear its output."""

        async with self._lock:
            state = self._controls.setdefault(control_id, _ControlRenderState())
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
        return generation

    async def _run_control(self, control_id: str, request: RenderRequest) -> None:
        current = request
        while True:
            result = await self._backend.render(current)
            await self._apply_result(result)

            async with self._lock:
                state = self._controls.get(control_id)
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
            state = self._controls.get(result.control_id)
            if state is None:
                return
            io_lock = state.io_lock
            target_output = state.output

        async with io_lock:
            async with self._lock:
                state = self._controls.get(result.control_id)
                if state is None:
                    return
                if state.generation != result.generation:
                    return
                if state.context_id != result.context_id:
                    return
                if state.binding_id != result.binding_id:
                    return
                if self._result_authorizer is not None and not (
                    self._result_authorizer(
                        result.control_id,
                        result.binding_id,
                        result.context_id,
                    )
                ):
                    return
                target_output = state.output

            if result.frame is None:
                return
            if target_output is not None:
                await target_output.write(result.frame)
