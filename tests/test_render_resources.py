"""Resource bounds under large frames and failed controller startup."""

import gc
import os
import weakref
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import anyio
import pytest
from deckr.components import RunContext
from invariant import OpRegistry
from invariant_gfx.artifacts import ImageArtifact
from PIL import Image

from deckr.controller import _render_process, _render_workers, _runtime_service
from deckr.controller._controller_service import ControllerService
from deckr.controller._hardware._commands import HardwareCommandService
from deckr.controller._raster import RasterFormat
from deckr.controller._render import RenderModel, RenderService
from deckr.controller.invariant.executor import build_executor
from test_support.render_workers import block_in_process


@pytest.mark.asyncio
async def test_entire_process_pool_can_be_replaced_after_stalled_jobs(tmp_path):
    await _render_process.close_idle()
    count = _render_workers.PROCESS_WORKERS
    markers = [tmp_path / f"worker-{index}" for index in range(count)]
    backend = _render_workers.ProcessPoolRenderBackend()
    try:
        async with anyio.create_task_group() as group:
            for path in markers:
                group.start_soon(
                    _render_workers.run_process, block_in_process, str(path)
                )
            with anyio.fail_after(10):
                while not all(path.exists() for path in markers):
                    await anyio.sleep(0.01)
            group.cancel_scope.cancel()
        for path in markers:
            with pytest.raises(ProcessLookupError):
                os.kill(int(path.read_text()), 0)

        results = []

        async def render(index):
            request = RenderService().build_request(
                RenderModel(title=f"Recovered {index}"),
                RasterFormat(72, 72),
                control_id=f"key-{index}",
            )
            results.append(
                await _render_workers.render_with_retry(
                    backend,
                    request,
                    is_current=lambda: True,
                    # Recovery includes eight fresh interpreter/font imports.
                    # Test bounded replacement independently of host CPU speed;
                    # deadline rejection has its own deterministic gated tests.
                    timeout=10,
                )
            )

        async with anyio.create_task_group() as group:
            for index in range(14):
                group.start_soon(render, index)
        assert len(results) == 14
        assert all(result.frame and result.error is None for result in results)
        assert len(_render_process._pool()) <= count
    finally:
        await backend.aclose()


@pytest.mark.asyncio
async def test_disconnect_during_initial_config_closes_subscription():
    closed = anyio.Event()

    async def subscribe():
        try:
            yield object()
            await anyio.sleep_forever()
        finally:
            closed.set()

    stream = subscribe()
    controller = SimpleNamespace(
        _config_service=SimpleNamespace(subscribe=lambda _: stream),
        _device_disconnect_events={},
        _hardware=SimpleNamespace(route_for_config=lambda _: None),
    )
    try:
        await ControllerService._device_lifecycle(
            controller, SimpleNamespace(config_id="gone"), object()
        )
        assert closed.is_set(), (
            "Aborted device setup leaves its config subscription open"
        )
    finally:
        await stream.aclose()


def test_hardware_rejection_history_releases_quiet_and_disconnected_claims(monkeypatch):
    import deckr.controller._hardware._commands as commands

    now = [0]
    monkeypatch.setattr(commands.time, "monotonic", lambda: now[0])
    service = HardwareCommandService(
        route_lookup=lambda _: None, claim_lookup=lambda _: None
    )

    def observe(config):
        owner = SimpleNamespace()
        service.observe_render_command(config, config, "key", "raster", "clear", owner)

    observe("expired")
    now[0] = 61
    service.prune_render_commands()
    assert not service._render_rejections
    observe("disconnect")
    observe("still-live")
    service.forget_render_commands("disconnect")
    assert set(service._render_rejections) == {"still-live"}


def test_large_images_do_not_fill_the_memory_cache_by_entry_count(
    tmp_path, monkeypatch
):
    # The dependency's registry is a singleton; this standalone factory probe
    # must not register a second set of operations in the suite's executor.
    monkeypatch.setattr(OpRegistry, "_instance", None)
    monkeypatch.setattr(OpRegistry, "_initialized", False)
    store = build_executor(cache_dir=tmp_path).store
    refs = []
    try:
        for index in range(40):
            image = ImageArtifact(Image.new("RGBA", (512, 512), (index, 0, 0, 255)))
            refs.append(weakref.ref(image))
            store.put("image", f"{index:064x}", image)
        del image
        gc.collect()
        # 40 MiB of pixels must not remain resident in a 16 MiB L1 budget.
        assert sum(ref() is not None for ref in refs) <= 16
        recovered = store.get("image", f"{0:064x}")
        assert recovered.image.getpixel((0, 0)) == (0, 0, 0, 255)
    finally:
        store.l1.clear()
        store.l2.close()


@pytest.mark.asyncio
async def test_default_startup_failure_reaps_warmed_process(monkeypatch):
    await _render_process.close_idle()
    pids = []

    async def warmup():
        pids.append(await _render_workers.run_process(os.getpid))
        raise ValueError("warmup failed after creating a worker")

    monkeypatch.setattr(_render_workers, "prepare_process_rendering", warmup)
    endpoint_context = SimpleNamespace(
        __aenter__=AsyncMock(return_value=SimpleNamespace(session_id="test")),
        __aexit__=AsyncMock(),
    )
    context = SimpleNamespace(
        open_endpoint=lambda *a, **kw: endpoint_context,
        kv_bucket=lambda *a: None,
    )
    monkeypatch.setattr(
        _runtime_service, "build_config_service", lambda *a, **kw: object()
    )
    monkeypatch.setattr(
        _runtime_service, "build_settings_service", lambda *a, **kw: object()
    )
    monkeypatch.setattr(_runtime_service, "DeckrServices", lambda **kw: object())
    monkeypatch.setattr(
        _runtime_service,
        "ControllerActionService",
        lambda **kw: SimpleNamespace(aclose=AsyncMock()),
    )
    service = _runtime_service.ControllerRuntimeService(
        runtime_name="resource-startup",
        runtime=_runtime_service.build_controller_runtime(
            config_source={}, base_dir=Path.cwd(), controller_id="test"
        ),
        context=context,
        beacon=object(),
        concord=object(),
    )
    service._component_manager = SimpleNamespace(
        run=object(),
        add_component=AsyncMock(),
        stop=AsyncMock(),
    )
    try:
        for _ in range(3):
            with pytest.raises(ValueError, match="warmup failed"):
                await service.start(
                    RunContext(
                        tg=SimpleNamespace(start=AsyncMock(), start_soon=object()),
                        stopping=anyio.Event(),
                    )
                )
            for pid in pids:
                with pytest.raises(ProcessLookupError):
                    os.kill(pid, 0)
    finally:
        await _render_process.close_idle()
