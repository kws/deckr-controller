"""Bounded render execution; AnyIO owns reusable process workers for the event loop."""

from __future__ import annotations

import logging
import os
import signal
import threading
from typing import Protocol

import anyio
import anyio.to_process
import httpx

from deckr.controller._render import (
    RenderRequest,
    RenderResult,
    render_request_to_frame,
)

logger = logging.getLogger(__name__)
RENDER_TIMEOUT_SECONDS = 2.0
# Admission is nonblocking and includes work awaiting process/thread capacity.
_PROCESS_SLOTS = threading.BoundedSemaphore(8)
_THREAD_SLOTS = threading.BoundedSemaphore(8)


class RenderBusy(RuntimeError):
    """No bounded execution capacity is available."""


async def run_process(function, *args, limiter=None):
    if not _PROCESS_SLOTS.acquire(blocking=False):
        raise RenderBusy("render workers busy")
    try:
        return await anyio.to_process.run_sync(
            function, *args, cancellable=True, limiter=limiter
        )
    finally:
        _PROCESS_SLOTS.release()


async def run_thread(function, *args, slots=None):
    slots = slots if slots is not None else _THREAD_SLOTS
    if not slots.acquire(blocking=False):
        raise RenderBusy("render workers busy")
    started = False
    abandoned = False
    lock = threading.Lock()

    def invoke():
        nonlocal started
        with lock:
            if abandoned:
                return None
            started = True
        try:
            return function(*args)
        finally:
            # An abandoned thread retains its slot until it actually exits.
            slots.release()

    try:
        return await anyio.to_thread.run_sync(invoke, abandon_on_cancel=True)
    finally:
        with lock:
            if not started:
                abandoned = True
                slots.release()


async def render_with_retry(
    backend, request, *, is_current, timeout=RENDER_TIMEOUT_SECONDS
):
    """Retry pure worker work once; never replay a hardware send or action input."""
    with anyio.fail_after(timeout):
        for attempt in range(2):
            if not is_current():
                return None
            try:
                result = await backend.render(request)
            except RenderBusy:
                raise
            except Exception:
                if attempt:
                    raise
            else:
                if not result.error or attempt:
                    return result
            await anyio.sleep(0)
    return None


def _init_render_worker() -> None:
    signal.signal(signal.SIGINT, signal.SIG_IGN)


class RenderWorkerImageFetchError(Exception):
    """Pickle-safe image-fetch failure."""


def _render_request_to_frame_worker(request: RenderRequest) -> bytes:
    _init_render_worker()
    try:
        return render_request_to_frame(request)
    except httpx.HTTPError as exc:
        raise RenderWorkerImageFetchError(str(exc)) from None


def default_render_workers() -> int:
    return min(4, max(1, (os.cpu_count() or 1) - 1))


class RenderBackend(Protocol):
    async def render(self, request: RenderRequest) -> RenderResult: ...
    async def aclose(self) -> None: ...


class _OwnedRenderBackend:
    def __init__(self):
        self._closed = False
        self._jobs: dict[anyio.CancelScope, anyio.Event] = {}

    async def _execute(self, request):
        raise NotImplementedError

    async def render(self, request: RenderRequest) -> RenderResult:
        if self._closed:
            raise RuntimeError("Render backend is closed")
        done = anyio.Event()
        with anyio.CancelScope() as scope:
            self._jobs[scope] = done
            try:
                frame = await self._execute(request)
                error = None
            except RenderBusy:
                raise
            except (httpx.HTTPError, RenderWorkerImageFetchError) as exc:
                logger.warning("Render image fetch failed: %s", exc)
                frame, error = None, str(exc)
            except Exception as exc:
                logger.warning("Render worker failed", exc_info=True)
                frame, error = None, str(exc)
            finally:
                self._jobs.pop(scope, None)
                done.set()
            return RenderResult(
                context_id=request.context_id,
                binding_id=request.binding_id,
                control_id=request.control_id,
                generation=request.generation,
                frame=frame,
                error=error,
            )
        raise RuntimeError("Render backend closed during work")

    async def aclose(self) -> None:
        self._closed = True
        jobs = tuple(self._jobs.items())
        for scope, _ in jobs:
            scope.cancel()
        with anyio.CancelScope(shield=True):
            with anyio.fail_after(1):
                for _, done in jobs:
                    await done.wait()


class ThreadRenderBackend(_OwnedRenderBackend):
    """Bounded trusted local rendering; cancellation abandons a still-running thread."""

    async def _execute(self, request):
        return await run_thread(render_request_to_frame, request)


class ProcessPoolRenderBackend(_OwnedRenderBackend):
    """Cancellable rendering in AnyIO's managed process pool."""

    def __init__(self, *, max_workers: int | None = None):
        super().__init__()
        self._limiter = anyio.CapacityLimiter(max_workers or default_render_workers())

    async def _execute(self, request):
        return await run_process(
            _render_request_to_frame_worker, request, limiter=self._limiter
        )
