from __future__ import annotations

import logging

import anyio
import httpx
import pytest

from deckr.controller._raster import RasterFormat
from deckr.controller._render import (
    RenderModel,
    RenderService,
)
from deckr.controller._render_workers import (
    ProcessPoolRenderBackend,
    RenderWorkerImageFetchError,
    ThreadRenderBackend,
    _render_request_to_frame_worker,
)
from test_support.render_dispatch import (
    _solid_request,
)


@pytest.mark.asyncio
async def test_thread_render_backend_skips_http_image_fetch_failures(
    monkeypatch,
    caplog,
):
    def fail_render(request):
        del request
        raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr(
        "deckr.controller._render_workers.render_request_to_frame",
        fail_render,
    )
    caplog.set_level(logging.WARNING, logger="deckr.controller._render_workers")
    backend = ThreadRenderBackend()

    result = await backend.render(_solid_request())

    assert result.frame is None
    assert result.error == "timed out"
    assert "image fetch failed" in caplog.text


def test_render_worker_wraps_http_image_fetch_failures(monkeypatch):
    monkeypatch.setattr(
        "deckr.controller._render_workers._init_render_worker", lambda: None
    )

    def fail_render(request):
        del request
        response = httpx.Response(
            503,
            request=httpx.Request("GET", "http://127.0.0.1/image.png"),
        )
        raise httpx.HTTPStatusError(
            "503 Service Unavailable",
            request=response.request,
            response=response,
        )

    monkeypatch.setattr(
        "deckr.controller._render_workers.render_request_to_frame",
        fail_render,
    )

    with pytest.raises(RenderWorkerImageFetchError, match="503 Service Unavailable"):
        _render_request_to_frame_worker(_solid_request())


@pytest.mark.asyncio
async def test_process_pool_render_backend_skips_http_image_fetch_failures(
    monkeypatch,
    caplog,
):
    async def fail_render(self, request):
        del self, request
        raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr(
        ProcessPoolRenderBackend,
        "_execute",
        fail_render,
    )
    caplog.set_level(logging.WARNING, logger="deckr.controller._render_workers")
    backend = ProcessPoolRenderBackend()

    result = await backend.render(_solid_request())

    assert result.frame is None
    assert result.error == "timed out"
    assert "image fetch failed" in caplog.text


@pytest.mark.asyncio
async def test_process_pool_render_backend_renders_request():
    backend = ProcessPoolRenderBackend(max_workers=2)
    fmt = RasterFormat(width=72, height=72)
    service = RenderService()

    try:
        request = service.build_request(RenderModel(title="process"), fmt)
        assert request is not None

        result = await backend.render(request)

        assert result.frame is not None
        assert len(result.frame) > 100
    finally:
        await backend.aclose()


@pytest.mark.asyncio
async def test_process_pool_render_backend_workers_ignore_sigint():
    from deckr.controller._render_workers import run_process
    from test_support.render_workers import initialized_sigint_handler

    assert await run_process(initialized_sigint_handler) == 1


@pytest.mark.asyncio
async def test_backend_close_cancels_active_work_promptly():
    class BlockedBackend(ProcessPoolRenderBackend):
        async def _execute(self, request):
            entered.set()
            try:
                await anyio.sleep_forever()
            finally:
                exited.set()

    entered, exited = anyio.Event(), anyio.Event()
    backend = BlockedBackend()
    errors = []

    async def render():
        try:
            await backend.render(_solid_request())
        except RuntimeError as exc:
            errors.append(exc)

    async with anyio.create_task_group() as tg:
        tg.start_soon(render)
        await entered.wait()
        with anyio.fail_after(0.5):
            await backend.aclose()
        assert exited.is_set()
    assert errors and not backend._jobs
