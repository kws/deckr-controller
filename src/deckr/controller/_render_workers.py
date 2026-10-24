"""Bounded render execution; AnyIO owns reusable process workers for the event loop."""

from __future__ import annotations

import asyncio
import base64
import contextvars
import logging
import os
import signal
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Protocol

import anyio
import httpx

from deckr.controller import _render_process
from deckr.controller._raster import RasterFormat
from deckr.controller._render import (
    RenderModel,
    RenderRequest,
    RenderResult,
    RenderService,
    render_request_to_frame,
)
from deckr.controller._render_resources import ImageResources

logger = logging.getLogger(__name__)
RENDER_TIMEOUT_SECONDS = 2.0
# Execution stays bounded, including abandoned threads that have not exited.
# Callers bound waiting work per control and cancel it at the render deadline.
PROCESS_WORKERS = 8
_PROCESS_SLOTS = threading.BoundedSemaphore(PROCESS_WORKERS)
_PROCESS_LIMITER = anyio.CapacityLimiter(PROCESS_WORKERS)
_THREAD_SLOTS = threading.BoundedSemaphore(8)
_THREAD_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="deckr-render")
_RENDER_DEADLINE = contextvars.ContextVar("render_deadline", default=None)


async def _acquire_slot(slots):
    # A thread may release capacity after its owner has been cancelled. Keep the
    # semaphore process-wide without blocking the event loop or consuming another
    # worker thread just to wait. Check cancellation before claiming capacity.
    await anyio.lowlevel.checkpoint()
    while not slots.acquire(blocking=False):
        await anyio.sleep(0.005)


async def run_process(function, *args, limiter=None, deadline=None, timings=None):
    # Waiting for a narrower workload limit must not reserve an idle process.
    queued = time.monotonic()
    async with limiter or _PROCESS_LIMITER:
        slots = _PROCESS_SLOTS
        await _acquire_slot(slots)
        started = time.monotonic()
        if timings is not None:
            timings["graphQueueMs"] = (started - queued) * 1000
        try:
            await anyio.lowlevel.checkpoint()
            if deadline is None:
                return await _render_process.run(function, *args)
            # Preserve a warm worker only once execution capacity is owned.
            # Superseded queue entries remain cancellable; running CPU work may
            # finish, but never outlive the request's original absolute deadline.
            with (
                anyio.CancelScope(shield=True),
                anyio.fail_after(max(0, deadline - time.monotonic())),
            ):
                return await _render_process.run(function, *args)
        finally:
            if timings is not None:
                timings["graphWorkerMs"] = (time.monotonic() - started) * 1000
            slots.release()


async def run_thread(function, *args, slots=None, pool=None):
    slots = slots if slots is not None else _THREAD_SLOTS
    await _acquire_slot(slots)
    # Own a fixed pool: cancellation before AnyIO 4.13 invokes a queued job can
    # strand its worker (and thread-local cache connection) outside the idle pool.
    # The concurrent future owns capacity until it finishes or is cancelled while
    # still queued. Cancelling the waiter never releases a running job's slot.
    context = contextvars.copy_context()
    try:
        future = (pool or _THREAD_POOL).submit(context.run, function, *args)
    except BaseException:
        slots.release()
        raise
    future.add_done_callback(lambda _: slots.release())
    try:
        return await asyncio.wrap_future(future)
    finally:
        # Does nothing once execution has started. Queued obsolete work must not
        # run after its caller's deadline or binding retirement.
        future.cancel()


async def prepare_thread_rendering():
    """Initialize local raster composition before admitting device bindings."""
    # A tiny local image loads the executor, graphics operations and composition
    # recipe once. Concurrent first frames must not all pay that cold-start cost
    # inside their advertised deadline. No hardware or observation is involved.
    from io import BytesIO

    from PIL import Image

    def prepare():
        image = BytesIO()
        Image.new("RGB", (1, 1)).save(image, format="PNG")
        request = RenderService().build_request(
            RenderModel(
                image="data:image/png;base64,"
                + base64.b64encode(image.getvalue()).decode("ascii")
            ),
            RasterFormat(1, 1),
        )
        render_request_to_frame(request)

    await run_thread(prepare)


def _prepare_process():
    from invariant import dump_graph_to_dict

    from deckr.controller._bindings._render_policy import DEFAULT_LIMITS
    from deckr.controller._render_graph import render_graph
    from deckr.controller.invariant.recipes import icon_button

    graph = icon_button(icon="lucide:play", title="Ready")
    frame = render_graph(
        dump_graph_to_dict(graph.graph, output=graph.output),
        96,
        96,
        DEFAULT_LIMITS.model_copy(update={"max_render_time_ms": 30000}),
    )
    request = RenderService().build_request(
        RenderModel(image="data:image/png;base64," + base64.b64encode(frame).decode()),
        RasterFormat(96, 96),
    )
    render_request_to_frame(request)


async def prepare_process_rendering():
    """Pay graphics import and font initialization before exposing bindings."""
    async with anyio.create_task_group() as group:
        for _ in range(PROCESS_WORKERS):
            group.start_soon(run_process, _prepare_process)


async def render_with_retry(
    backend, request, *, is_current, timeout=RENDER_TIMEOUT_SECONDS
):
    """Retry pure worker work once; never replay a hardware send or action input."""
    token = _RENDER_DEADLINE.set(time.monotonic() + timeout)
    try:
        with anyio.fail_after(timeout):
            return await _render_attempts(backend, request, is_current)
    finally:
        _RENDER_DEADLINE.reset(token)


async def _render_attempts(backend, request, is_current):
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
    resources: ImageResources

    async def render(self, request: RenderRequest) -> RenderResult: ...
    async def aclose(self) -> None: ...


class _OwnedRenderBackend:
    def __init__(self):
        self.resources = ImageResources()
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
                from deckr.controller.invariant.ops.fetch_url import (
                    MAX_IMAGE_BYTES,
                    ImageResourceRequired,
                )

                deadline = (
                    _RENDER_DEADLINE.get() or time.monotonic() + RENDER_TIMEOUT_SECONDS
                )
                resources = dict(request.resources)
                if request.image_url:
                    resources[request.image_url] = await self.resources.get(
                        request.image_url,
                        max_bytes=MAX_IMAGE_BYTES,
                        deadline=deadline,
                        media_types=None,
                    )
                while True:
                    try:
                        frame = await self._execute(
                            replace(request, resources=resources)
                        )
                        break
                    except ImageResourceRequired as missing:
                        url = str(missing)
                        remaining = 8 * MAX_IMAGE_BYTES - sum(
                            len(blob.data) for blob in resources.values()
                        )
                        resources[url] = await self.resources.get(
                            url,
                            max_bytes=min(MAX_IMAGE_BYTES, remaining),
                            deadline=deadline,
                            media_types=None,
                        )
                error = None
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
            with anyio.fail_after(RENDER_TIMEOUT_SECONDS + 1):
                for _, done in jobs:
                    await done.wait()
        await self.resources.aclose()
        await _render_process.close_idle()


class ThreadRenderBackend(_OwnedRenderBackend):
    """Bounded trusted local rendering; cancellation abandons a still-running thread."""

    async def _execute(self, request):
        return await run_thread(render_request_to_frame, request)


class ProcessPoolRenderBackend(_OwnedRenderBackend):
    """Cancellable rendering in the controller-owned process pool."""

    def __init__(self, *, max_workers: int | None = None):
        super().__init__()
        self._limiter = anyio.CapacityLimiter(max_workers or default_render_workers())

    async def _execute(self, request):
        return await run_process(
            _render_request_to_frame_worker,
            request,
            limiter=self._limiter,
            deadline=_RENDER_DEADLINE.get(),
        )
