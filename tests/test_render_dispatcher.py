"""Tests for async render dispatch, worker round-trips, and process-pool rendering."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import signal
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import anyio
import httpx
import pytest
from invariant import Node, SubGraphNode, dump_graph_data_uri, dump_graph_to_dict
from invariant.params import ref
from invariant.store.disk import DiskStore
from invariant_gfx.artifacts import BlobArtifact
from PIL import Image

from deckr.controller._command_router import DeviceOutput
from deckr.controller._device_layout import RasterImageFormat
from deckr.controller._render import (
    RenderImageFormat,
    RenderModel,
    RenderRequest,
    RenderResult,
    RenderService,
    RenderSource,
)
from deckr.controller._render_dispatcher import (
    ProcessPoolRenderBackend,
    RenderDispatcher,
    RenderWorkerImageFetchError,
    ThreadRenderBackend,
    _render_request_to_jpeg_worker,
)
from deckr.controller._render_observation import (
    ObservingRenderBackend,
    RenderObservationOptions,
)


class FakeHardwareCommandService:
    def __init__(self):
        self.set_raster_frame = AsyncMock()
        self.clear_raster = AsyncMock()


def _png_data_uri() -> str:
    image = Image.new("RGBA", (2, 2), (255, 0, 0, 255))
    import io

    buf = io.BytesIO()
    image.save(buf, format="PNG")
    encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


class ControlledBackend:
    """Backend that allows tests to control when each generation completes."""

    def __init__(self):
        self.calls: list[int] = []
        self.requests: list[RenderRequest] = []
        self._events: dict[int, anyio.Event] = {}

    async def render(self, request: RenderRequest) -> RenderResult:
        self.calls.append(request.generation)
        self.requests.append(request)
        event = self._events.setdefault(request.generation, anyio.Event())
        await event.wait()
        return RenderResult(
            context_id=request.context_id,
            binding_id=request.binding_id,
            control_id=request.control_id,
            generation=request.generation,
            frame=f"frame-{request.generation}".encode(),
        )

    def release(self, generation: int) -> None:
        self._events.setdefault(generation, anyio.Event()).set()

    async def aclose(self) -> None:
        return


def _solid_request() -> RenderRequest:
    graph = {
        "output": Node(
            op_name="stdlib:identity",
            params={"value": None},
            deps=[],
        )
    }
    return RenderRequest(
        context_id="ctx",
        control_id="0,0",
        generation=0,
        image_format=RenderImageFormat(width=72, height=72),
        graph=dump_graph_to_dict(graph, output="output"),
    )


class ImmediateBackend:
    def __init__(
        self,
        *,
        frame: bytes | None = b"frame",
        error: str | None = None,
    ):
        self.frame = frame
        self.error = error
        self.closed = False
        self.calls: list[RenderRequest] = []

    async def render(self, request: RenderRequest) -> RenderResult:
        self.calls.append(request)
        return RenderResult(
            context_id=request.context_id,
            binding_id=request.binding_id,
            control_id=request.control_id,
            generation=request.generation,
            frame=self.frame,
            error=self.error,
        )

    async def aclose(self) -> None:
        self.closed = True


def _request_with_graph(graph: dict) -> RenderRequest:
    return RenderRequest(
        config_id="device-1",
        context_id="ctx-1",
        binding_id="binding-1",
        control_id="0,0",
        generation=7,
        image_format=RenderImageFormat(width=72, height=64),
        graph=graph,
        context={"setting": "value"},
        source=RenderSource(
            provider_instance_id="provider-instance",
            provider_id="dev.deckr.clock",
            action_id="dev.deckr.clock.action.digital",
            action_instance_id="action-instance",
            action_message_id="message-1",
            action_causation_id="cause-1",
            trace={"traceParent": "00-abc"},
            command_type="set_frame",
            content_kind="invariant_graph",
            binding_output_generation=3,
        ),
    )


def _json_hash(value) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _frame_hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _read_observations(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()]


def _custom_graph() -> SubGraphNode:
    inner = {
        "bg": Node(
            op_name="gfx:create_solid",
            params={
                "size": ["${canvas.width}", "${canvas.height}"],
                "color": (32, 64, 96, 255),
            },
            deps=["canvas"],
        )
    }
    return SubGraphNode(
        params={"canvas": ref("canvas")},
        deps=["canvas"],
        graph=inner,
        output="bg",
    )


def _graph_data_uri() -> str:
    graph = _custom_graph()
    return dump_graph_data_uri(graph.graph, output=graph.output)


def _graph_data_uri_with_query_context() -> str:
    graph = {
        "bg": Node(
            op_name="gfx:create_solid",
            params={
                "size": ["${canvas.width}", "${canvas.height}"],
                "color": ref("color"),
            },
            deps=["canvas", "color"],
        )
    }
    return dump_graph_data_uri(
        graph,
        output="bg",
        context={"color": (32, 64, 96, 255)},
    )


@pytest.mark.asyncio
async def test_render_dispatcher_replaces_pending_and_drops_stale():
    command_service = FakeHardwareCommandService()

    backend = ControlledBackend()
    output = DeviceOutput(command_service, "dev", "0,0", "raster.bitmap")

    async with anyio.create_task_group() as tg:
        dispatcher = RenderDispatcher(
            command_service=command_service,
            config_id="dev",
            backend=backend,
            start_soon=tg.start_soon,
        )
        request = _solid_request()

        await dispatcher.submit_request(
            control_id="0,0",
            context_id="ctx",
            request=request,
            output=output,
        )
        await dispatcher.submit_request(
            control_id="0,0",
            context_id="ctx",
            request=request,
            output=output,
        )
        await dispatcher.submit_request(
            control_id="0,0",
            context_id="ctx",
            request=request,
            output=output,
        )

        with anyio.fail_after(1.0):
            while backend.calls != [1, 2]:
                await anyio.sleep(0.01)

        backend.release(2)
        with anyio.fail_after(1.0):
            while backend.calls != [1, 2, 3]:
                await anyio.sleep(0.01)
        command_service.set_raster_frame.assert_not_awaited()

        backend.release(3)
        with anyio.fail_after(1.0):
            while command_service.set_raster_frame.call_count != 1:
                await anyio.sleep(0.01)

        assert output.last_frame == b"frame-3"
        command_service.set_raster_frame.assert_awaited_once_with(
            "dev",
            "0,0",
            "raster.bitmap",
            b"frame-3",
        )
        backend.release(1)
        await anyio.sleep(0.05)
        command_service.set_raster_frame.assert_awaited_once()
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_render_dispatcher_preserves_existing_frame_when_render_returns_none():
    command_service = FakeHardwareCommandService()
    backend = ImmediateBackend(frame=None, error="image fetch failed")
    output = DeviceOutput(command_service, "dev", "0,0", "raster.bitmap")
    output.last_frame = b"previous-frame"

    async with anyio.create_task_group() as tg:
        dispatcher = RenderDispatcher(
            command_service=command_service,
            config_id="dev",
            backend=backend,
            start_soon=tg.start_soon,
        )
        await dispatcher.submit_request(
            control_id="0,0",
            context_id="ctx",
            request=_solid_request(),
            output=output,
        )

        with anyio.fail_after(1.0):
            while not backend.calls:
                await anyio.sleep(0.01)
        await anyio.sleep(0.05)

        assert output.last_frame == b"previous-frame"
        command_service.set_raster_frame.assert_not_awaited()
        command_service.clear_raster.assert_not_awaited()
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_thread_render_backend_skips_http_image_fetch_failures(
    monkeypatch,
    caplog,
):
    def fail_render(request):
        del request
        raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr(
        "deckr.controller._render_dispatcher.render_request_to_jpeg",
        fail_render,
    )
    caplog.set_level(logging.WARNING, logger="deckr.controller._render_dispatcher")
    backend = ThreadRenderBackend()

    result = await backend.render(_solid_request())

    assert result.frame is None
    assert result.error == "timed out"
    assert "image fetch failed" in caplog.text


def test_render_worker_wraps_http_image_fetch_failures(monkeypatch):
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
        "deckr.controller._render_dispatcher.render_request_to_jpeg",
        fail_render,
    )

    with pytest.raises(RenderWorkerImageFetchError, match="503 Service Unavailable"):
        _render_request_to_jpeg_worker(_solid_request())


@pytest.mark.asyncio
async def test_observing_render_backend_records_error(tmp_path: Path):
    path = tmp_path / "render.jsonl"
    backend = ObservingRenderBackend(
        ImmediateBackend(frame=None, error="boom"),
        controller_id="controller-main",
        options=RenderObservationOptions(path=path),
    )

    result = await backend.render(_request_with_graph({"output": "value"}))

    assert result.frame is None
    assert result.error == "boom"
    record = _read_observations(path)[0]
    assert record["frameSha256"] is None
    assert record["error"] == "boom"


@pytest.mark.asyncio
async def test_observing_render_backend_can_include_graph_and_context(
    tmp_path: Path,
):
    path = tmp_path / "render.jsonl"
    backend = ObservingRenderBackend(
        ImmediateBackend(),
        controller_id="controller-main",
        options=RenderObservationOptions(
            path=path,
            include_graph=True,
            include_context=True,
        ),
    )
    request = _request_with_graph({"output": {"params": {"title": "Clock"}}})

    await backend.render(request)

    record = _read_observations(path)[0]
    assert record["graph"] == request.graph
    assert record["context"] == request.context


@pytest.mark.asyncio
async def test_observing_render_backend_records_availability_source_metadata(
    tmp_path: Path,
):
    path = tmp_path / "render.jsonl"
    backend = ObservingRenderBackend(
        ImmediateBackend(),
        controller_id="controller-main",
        options=RenderObservationOptions(path=path),
    )
    request = _request_with_graph({"output": "value"})
    assert request.source is not None
    request = replace(
        request,
        binding_id=None,
        source=replace(
            request.source,
            command_type="controller_fallback",
            content_kind="overlay:unavailable_service",
            availability_cause="service",
            availability_state="unavailable",
            availability_source="provider_direct",
            availability_reason="sonos_service_unavailable",
        ),
    )

    await backend.render(request)

    record = _read_observations(path)[0]
    assert record["bindingId"] is None
    assert record["commandType"] == "controller_fallback"
    assert record["contentKind"] == "overlay:unavailable_service"
    assert record["availabilityCause"] == "service"
    assert record["availabilityState"] == "unavailable"
    assert record["availabilitySource"] == "provider_direct"
    assert record["availabilityReason"] == "sonos_service_unavailable"


@pytest.mark.asyncio
async def test_observing_render_backend_delegates_close(tmp_path: Path):
    delegate = ImmediateBackend()
    backend = ObservingRenderBackend(
        delegate,
        controller_id="controller-main",
        options=RenderObservationOptions(path=tmp_path / "render.jsonl"),
    )

    await backend.aclose()

    assert delegate.closed is True


@pytest.mark.asyncio
async def test_process_pool_render_backend_skips_http_image_fetch_failures(
    monkeypatch,
    caplog,
):
    async def fail_render(executor, request):
        del executor, request
        raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr(
        "deckr.controller._render_dispatcher._run_in_process_pool",
        fail_render,
    )
    caplog.set_level(logging.WARNING, logger="deckr.controller._render_dispatcher")
    backend = object.__new__(ProcessPoolRenderBackend)
    backend._executor = object()

    result = await backend.render(_solid_request())

    assert result.frame is None
    assert result.error == "timed out"
    assert "image fetch failed" in caplog.text


@pytest.mark.asyncio
async def test_process_pool_render_backend_renders_request():
    backend = ProcessPoolRenderBackend(max_workers=2)
    fmt = RasterImageFormat(width=72, height=72)
    service = RenderService()

    try:
        request = service.build_request(RenderModel(title="process"), fmt)
        assert request is not None

        result = await backend.render(request)

        assert result.frame is not None
        assert len(result.frame) > 100
    finally:
        await backend.aclose()


def test_process_pool_render_backend_workers_ignore_sigint():
    backend = ProcessPoolRenderBackend(max_workers=1)

    try:
        future = backend._executor.submit(_read_sigint_handler)
        assert future.result(timeout=10) == 1
    finally:
        backend._executor.shutdown(wait=True, cancel_futures=True)


def _sleep_and_return_pid(delay_ms: int) -> int:
    time.sleep(delay_ms / 1000)
    return os.getpid()


def _read_sigint_handler() -> int:
    current = signal.getsignal(signal.SIGINT)
    if current == signal.SIG_IGN:
        return 1
    if current == signal.SIG_DFL:
        return 0
    return -1


def _measure_pool_elapsed(max_workers: int, delay_ms: int) -> tuple[float, set[int]]:
    started = time.perf_counter()
    with ProcessPoolExecutor(max_workers=max_workers) as pool:
        fut_a = pool.submit(_sleep_and_return_pid, delay_ms)
        fut_b = pool.submit(_sleep_and_return_pid, delay_ms)
        pid_a = fut_a.result(timeout=30)
        pid_b = fut_b.result(timeout=30)
    elapsed = time.perf_counter() - started
    return elapsed, {pid_a, pid_b}


def _write_blob_to_store(cache_dir: str, payload: bytes) -> bytes:
    store = DiskStore(cache_dir=cache_dir)
    blob = BlobArtifact(data=payload, content_type="application/octet-stream")
    store.put("test:blob", "a" * 64, blob)
    return store.get("test:blob", "a" * 64).data
