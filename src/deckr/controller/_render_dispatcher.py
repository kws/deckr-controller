"""Async render backends and per-device render dispatch with stale-result dropping."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, NamedTuple, Protocol

import anyio
import httpx

from deckr.controller._render import (
    RenderRequest,
    RenderResult,
    render_request_to_frame,
)

if TYPE_CHECKING:
    from deckr.controller._hardware import HardwareCommandService


class ControlOutput(Protocol):
    async def write(self, frame: bytes) -> None: ...

    async def clear(self) -> None: ...

    async def write_tagged(self, frame: bytes, message_id: str) -> None: ...

    async def clear_tagged(self, message_id: str) -> None: ...


logger = logging.getLogger(__name__)
MAX_ACTIVE_RENDERS_PER_CONTROL = 2
RENDER_TIMEOUT_SECONDS = 2.0


async def render_with_retry(
    backend, request, *, is_current, timeout=RENDER_TIMEOUT_SECONDS
):
    """Retry pure worker work once; never retry a hardware send or action input."""
    with anyio.fail_after(timeout):
        for attempt in range(2):
            if not is_current():
                return None
            try:
                result = await backend.render(request)
            except Exception:
                if attempt:
                    raise
            else:
                if not result.error or attempt:
                    return result
            await anyio.sleep(0)
    return None


def _init_render_worker() -> None:
    """Let the parent process own Ctrl-C handling for render workers.

    Without this, ProcessPoolExecutor workers inherit the terminal's SIGINT and can
    emit noisy KeyboardInterrupt tracebacks while the controller is already shutting
    down gracefully.
    """

    signal.signal(signal.SIGINT, signal.SIG_IGN)


class RenderWorkerImageFetchError(Exception):
    """Pickle-safe wrapper for image fetch failures raised inside render workers."""


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
            frame = await anyio.to_thread.run_sync(render_request_to_frame, request)
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
        except (httpx.HTTPError, RenderWorkerImageFetchError) as exc:
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
    return await loop.run_in_executor(
        executor, _render_request_to_frame_worker, request
    )


def _render_request_to_frame_worker(request: RenderRequest) -> bytes:
    try:
        return render_request_to_frame(request)
    except httpx.HTTPError as exc:
        raise RenderWorkerImageFetchError(str(exc)) from None


@dataclass(slots=True)
class _ControlRenderState:
    generation: int = 0
    base_revision: int = 0
    context_id: str | None = None
    binding_id: str | None = None
    output: ControlOutput | None = None
    active_render_count: int = 0
    pending_request: RenderRequest | None = None
    io_lock: anyio.Lock = field(default_factory=anyio.Lock)


class RenderTicket(NamedTuple):
    control_id: str
    context_id: str
    binding_id: str
    base_revision: int


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
        completion_transaction: Callable | None = None,
    ):
        self._command_service = command_service
        self._config_id = config_id
        self._backend = backend
        self._start_soon = start_soon
        self._result_authorizer = result_authorizer
        self._completion_transaction = completion_transaction
        self._lock = anyio.Lock()
        self._controls: dict[str, _ControlRenderState] = {}
        self._pending_clears: dict[str, int] = {}
        self._clearing: set[str] = set()

    def set_runner(self, start_soon) -> None:
        self._start_soon = start_soon

    @asynccontextmanager
    async def output_transaction(self):
        """Enter the application's presentation-result admission boundary."""
        if self._completion_transaction is None:
            yield
        else:
            async with self._completion_transaction():
                yield

    async def reserve_base(self, *, control_id, context_id, binding_id, output):
        """Admit a base update; even a later decoder failure invalidates old work."""
        async with self._lock:
            state = self._controls.setdefault(control_id, _ControlRenderState())
            state.base_revision += 1
            state.generation += 1
            state.context_id = context_id
            state.binding_id = binding_id
            state.output = output
            state.pending_request = None
            return RenderTicket(control_id, context_id, binding_id, state.base_revision)

    def base_is_current(self, ticket):
        control_id, context_id, binding_id, revision = ticket
        state = self._controls.get(control_id)
        return bool(
            state is not None
            and (state.context_id, state.binding_id, state.base_revision)
            == (context_id, binding_id, revision)
            and (
                self._result_authorizer is None
                or self._result_authorizer(control_id, binding_id, context_id)
            )
        )

    def composition_revision(self, ticket):
        return self._controls[ticket[0]].generation

    async def publish_base(
        self,
        ticket,
        composition_revision,
        frame,
        *,
        message_id,
        authorize,
        committed,
        recompose,
    ):
        """Validate at ordered handoff, then send without holding device state.

        The per-control output lock orders dispatched bytes. A revoked queued
        result is refused before handoff. An initiated send cannot be recalled.
        """
        state = self._controls[ticket.control_id]
        async with state.io_lock:
            await authorize()
            async with self.output_transaction(), self._lock:
                if not self.base_is_current(ticket):
                    raise ValueError("superseded")
                if state.generation != composition_revision:
                    return False
                output = state.output
                if output is None:
                    raise ValueError("target_not_current")
            if frame is None:
                await output.clear_tagged(message_id)
            else:
                await output.write_tagged(frame, message_id)
            async with self.output_transaction():
                changed = False
                async with self._lock:
                    if self.base_is_current(ticket):
                        changed = state.generation != composition_revision
                        state.generation += 1
                        state.pending_request = None
                        committed()
                if changed:
                    # Overlay changes may commit while bytes are in flight.
                    # Recompose with the newly accepted base, invalidating the
                    # overlay render that still contained the previous base.
                    await recompose()
            return True

    async def submit_request(
        self,
        *,
        control_id: str,
        context_id: str,
        binding_id: str | None = None,
        request: RenderRequest | None,
        output: ControlOutput | None = None,
        base_update: bool = True,
    ) -> int:
        """Submit a request for a control, replacing any older pending work."""

        async with self._lock:
            state = self._controls.setdefault(control_id, _ControlRenderState())
            if base_update:
                state.base_revision += 1
            state.generation += 1
            generation = state.generation
            state.context_id = context_id
            state.binding_id = binding_id
            if output is not None:
                state.output = output

            if request is None:
                state.pending_request = None
                logger.debug(
                    "Render dispatcher route claimed config=%s control=%s "
                    "binding=%s context=%s generation=%s",
                    self._config_id,
                    control_id,
                    binding_id,
                    context_id,
                    generation,
                )
                return generation

            request = replace(
                request,
                config_id=self._config_id,
                context_id=context_id,
                binding_id=binding_id,
                control_id=control_id,
                generation=generation,
            )
            if state.active_render_count >= MAX_ACTIVE_RENDERS_PER_CONTROL:
                replaced_pending = state.pending_request is not None
                state.pending_request = request
                logger.debug(
                    "Render dispatcher request queued config=%s control=%s "
                    "binding=%s context=%s generation=%s active=%s "
                    "replaced_pending=%s start_now=False",
                    self._config_id,
                    control_id,
                    binding_id,
                    context_id,
                    generation,
                    state.active_render_count,
                    replaced_pending,
                )
            else:
                state.active_render_count += 1
                self._start_soon(self._run_control, control_id, request)
                logger.debug(
                    "Render dispatcher request queued config=%s control=%s "
                    "binding=%s context=%s generation=%s active=%s "
                    "replaced_pending=False start_now=True",
                    self._config_id,
                    control_id,
                    binding_id,
                    context_id,
                    generation,
                    state.active_render_count,
                )
            return generation

    async def clear_control(
        self,
        control_id: str,
        *,
        context_id: str | None = None,
        binding_id: str | None = None,
        output: ControlOutput | None = None,
        clear_output: bool = True,
        base_update: bool = True,
    ) -> int:
        """Invalidate queued/running renders for a control and clear its output."""

        async with self._lock:
            state = self._controls.setdefault(control_id, _ControlRenderState())
            if base_update:
                state.base_revision += 1
            state.generation += 1
            generation = state.generation
            if context_id is not None:
                state.context_id = context_id
            state.binding_id = binding_id
            if output is not None:
                state.output = output
            state.pending_request = None

        logger.debug(
            "Render dispatcher clear queued config=%s control=%s binding=%s "
            "context=%s generation=%s clear_output=%s",
            self._config_id,
            control_id,
            binding_id,
            context_id,
            generation,
            clear_output,
        )

        if clear_output:
            self._pending_clears[control_id] = generation
            if control_id not in self._clearing:
                self._clearing.add(control_id)
                self._start_soon(self._clear_output, control_id)
        return generation

    async def _clear_output(self, control_id: str) -> None:
        try:
            while control_id in self._pending_clears:
                generation = self._pending_clears.pop(control_id)
                try:
                    state = self._controls[control_id]
                    async with state.io_lock:
                        async with self.output_transaction(), self._lock:
                            if state.generation != generation:
                                continue
                            if (
                                self._result_authorizer is not None
                                and not self._result_authorizer(
                                    control_id, state.binding_id, state.context_id
                                )
                            ):
                                continue
                            output = state.output
                        if output is not None:
                            with anyio.fail_after(2):
                                await output.clear()
                except Exception:
                    logger.warning(
                        "Hardware clear failed for %s", control_id, exc_info=True
                    )
        finally:
            self._clearing.discard(control_id)

    async def _run_control(self, control_id: str, request: RenderRequest) -> None:
        current = request
        while True:
            try:

                def is_current(current=current):
                    state = self._controls.get(control_id)
                    return state is not None and state.generation == current.generation

                result = await render_with_retry(
                    self._backend, current, is_current=is_current
                )
                if result is not None:
                    await self._apply_result(result)
            except Exception:
                logger.warning(
                    "Render or output failed for %s", control_id, exc_info=True
                )

            async with self._lock:
                state = self._controls.get(control_id)
                if state is None:
                    return
                next_request = state.pending_request
                if next_request is None:
                    state.active_render_count = max(0, state.active_render_count - 1)
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
            async with self.output_transaction(), self._lock:
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
                with anyio.fail_after(2):
                    await target_output.write(result.frame)
