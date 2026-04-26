"""Tests for async render dispatch, worker round-trips, and process-pool rendering."""

from __future__ import annotations

import base64
import os
import signal
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from unittest.mock import AsyncMock

import anyio
import pytest
from deckr.hardware.messages import HardwareImageFormat
from invariant import Node, SubGraphNode, dump_graph_output_data_uri
from invariant.params import ref
from invariant_gfx.artifacts import BlobArtifact
from PIL import Image

from deckr.controller._command_router import DeviceOutput
from deckr.controller._render import (
    RenderImageFormat,
    RenderModel,
    RenderRequest,
    RenderResult,
    RenderService,
    render_request_to_jpeg,
)
from deckr.controller._render_dispatcher import (
    ProcessPoolRenderBackend,
    RenderDispatcher,
)
from deckr.controller.invariant.executor import ProcessSafeDiskStore


class FakeHardwareCommandService:
    def __init__(self):
        self.set_image = AsyncMock()
        self.clear_slot = AsyncMock()
        self.sleep_screen = AsyncMock()
        self.wake_screen = AsyncMock()


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
        self._events: dict[int, anyio.Event] = {}

    async def render(self, request: RenderRequest) -> RenderResult:
        self.calls.append(request.generation)
        event = self._events.setdefault(request.generation, anyio.Event())
        await event.wait()
        return RenderResult(
            context_id=request.context_id,
            slot_id=request.slot_id,
            generation=request.generation,
            frame=f"frame-{request.generation}".encode(),
        )

    def release(self, generation: int) -> None:
        self._events.setdefault(generation, anyio.Event()).set()

    async def aclose(self) -> None:
        return


def _solid_request() -> RenderRequest:
    return RenderRequest(
        context_id="ctx",
        slot_id="0,0",
        generation=0,
        image_format=RenderImageFormat(width=72, height=72),
        graph={"graph": {}, "output": "output"},
    )


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
    return dump_graph_output_data_uri(graph.graph, graph.output)


@pytest.mark.asyncio
async def test_render_dispatcher_replaces_pending_and_drops_stale():
    command_service = FakeHardwareCommandService()

    backend = ControlledBackend()
    output = DeviceOutput(command_service, "dev", "0,0")

    async with anyio.create_task_group() as tg:
        dispatcher = RenderDispatcher(
            command_service=command_service,
            config_id="dev",
            backend=backend,
            start_soon=tg.start_soon,
        )
        request = _solid_request()

        await dispatcher.submit_request(
            slot_id="0,0",
            context_id="ctx",
            request=request,
            output=output,
        )
        await dispatcher.submit_request(
            slot_id="0,0",
            context_id="ctx",
            request=request,
            output=output,
        )
        await dispatcher.submit_request(
            slot_id="0,0",
            context_id="ctx",
            request=request,
            output=output,
        )

        with anyio.fail_after(1.0):
            while backend.calls != [1]:
                await anyio.sleep(0.01)

        backend.release(1)
        with anyio.fail_after(1.0):
            while backend.calls != [1, 3]:
                await anyio.sleep(0.01)

        backend.release(3)
        with anyio.fail_after(1.0):
            while command_service.set_image.call_count != 1:
                await anyio.sleep(0.01)

        assert output.last_frame == b"frame-3"
        command_service.set_image.assert_awaited_once_with("dev", "0,0", b"frame-3")
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_render_dispatcher_clear_slot_blocks_stale_completion():
    command_service = FakeHardwareCommandService()

    backend = ControlledBackend()
    output = DeviceOutput(command_service, "dev", "0,0")

    async with anyio.create_task_group() as tg:
        dispatcher = RenderDispatcher(
            command_service=command_service,
            config_id="dev",
            backend=backend,
            start_soon=tg.start_soon,
        )
        await dispatcher.submit_request(
            slot_id="0,0",
            context_id="ctx",
            request=_solid_request(),
            output=output,
        )
        with anyio.fail_after(1.0):
            while backend.calls != [1]:
                await anyio.sleep(0.01)

        await dispatcher.clear_slot("0,0", context_id="ctx", output=output)
        backend.release(1)
        await anyio.sleep(0.05)

        command_service.clear_slot.assert_awaited_once_with("dev", "0,0")
        command_service.set_image.assert_not_awaited()
        assert output.last_frame is None
        tg.cancel_scope.cancel()


@pytest.mark.parametrize(
    ("model", "case_id"),
    [
        (RenderModel(title="Hello"), "title"),
        (RenderModel(image=_png_data_uri()), "image"),
        (RenderModel(overlay_type="alert"), "alert"),
        (RenderModel(overlay_type="unavailable"), "unavailable"),
        (RenderModel(overlay_type="blank"), "blank"),
        (RenderModel(image=_graph_data_uri()), "graph"),
    ],
    ids=["title", "image", "alert", "unavailable", "blank", "graph"],
)
def test_render_request_to_jpeg_round_trips_common_render_types(model, case_id):
    fmt = HardwareImageFormat(width=72, height=72)
    request = RenderService().build_request(
        model,
        fmt,
        context_id=f"ctx:{case_id}",
        slot_id="0,0",
    )
    assert request is not None

    frame = render_request_to_jpeg(request)

    assert isinstance(frame, bytes)
    assert len(frame) > 100


@pytest.mark.asyncio
async def test_process_pool_render_backend_renders_request():
    backend = ProcessPoolRenderBackend(max_workers=2)
    fmt = HardwareImageFormat(width=72, height=72)
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


def test_process_pool_executor_parallelism():
    parallel_elapsed, parallel_pids = _measure_pool_elapsed(max_workers=2, delay_ms=700)
    serial_elapsed, serial_pids = _measure_pool_elapsed(max_workers=1, delay_ms=700)

    assert len(parallel_pids) == 2
    assert len(serial_pids) == 1
    assert parallel_elapsed > 0
    assert serial_elapsed > 0


def _write_blob_to_store(cache_dir: str, payload: bytes) -> bytes:
    store = ProcessSafeDiskStore(cache_dir=cache_dir)
    blob = BlobArtifact(data=payload, content_type="application/octet-stream")
    store.put("test:blob", "a" * 64, blob)
    return store.get("test:blob", "a" * 64).data


def test_process_safe_disk_store_survives_concurrent_writers(tmp_path: Path):
    payload_a = b"a" * 1024
    payload_b = b"b" * 1024

    with ProcessPoolExecutor(max_workers=2) as pool:
        fut_a = pool.submit(_write_blob_to_store, str(tmp_path), payload_a)
        fut_b = pool.submit(_write_blob_to_store, str(tmp_path), payload_b)
        result_a = fut_a.result(timeout=30)
        result_b = fut_b.result(timeout=30)

    store = ProcessSafeDiskStore(cache_dir=tmp_path)
    final = store.get("test:blob", "a" * 64).data

    assert result_a in {payload_a, payload_b}
    assert result_b in {payload_a, payload_b}
    assert final in {payload_a, payload_b}
