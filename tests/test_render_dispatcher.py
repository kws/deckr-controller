from __future__ import annotations

from unittest.mock import AsyncMock

import anyio
import pytest

from deckr.controller._presentation import DeviceOutput
from deckr.controller._render_dispatcher import RenderDispatcher
from deckr.controller._render_workers import (
    render_with_retry,
)
from test_support.render_dispatch import (
    ControlledBackend,
    FakeHardwareCommandService,
    ImmediateBackend,
    _solid_request,
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
        with anyio.fail_after(1):
            while backend.calls != [1]:
                await anyio.sleep(0)
        await dispatcher.submit_request(
            control_id="0,0",
            context_id="ctx",
            request=request,
            output=output,
        )
        with anyio.fail_after(1):
            while backend.calls != [1, 2]:
                await anyio.sleep(0)
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
async def test_worker_retry_is_bounded_and_checks_supersession():
    backend = ImmediateBackend(frame=None, error="worker unavailable")
    result = await render_with_retry(backend, _solid_request(), is_current=lambda: True)
    assert result.error == "worker unavailable"
    assert len(backend.calls) == 2
    backend.calls.clear()
    result = await render_with_retry(
        backend, _solid_request(), is_current=lambda: not backend.calls
    )
    assert result is None
    assert len(backend.calls) == 1


@pytest.mark.asyncio
async def test_slow_output_does_not_hold_the_device_boundary():
    from deckr.controller._state_gate import StateGate

    boundary = StateGate()
    entered, release = anyio.Event(), anyio.Event()
    output = AsyncMock()

    async def send(frame):
        entered.set()
        await release.wait()

    output.write.side_effect = send
    async with anyio.create_task_group() as tg:
        dispatcher = RenderDispatcher(
            command_service=FakeHardwareCommandService(),
            config_id="dev",
            backend=ImmediateBackend(),
            start_soon=tg.start_soon,
            completion_transaction=lambda: boundary,
        )
        await dispatcher.submit_request(
            control_id="0,0",
            context_id="old",
            binding_id="old",
            request=_solid_request(),
            output=output,
        )
        await entered.wait()
        with anyio.fail_after(0.5):
            async with boundary:
                await dispatcher.clear_control("0,0", clear_output=False)
        release.set()
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_failed_clear_does_not_drop_a_newer_queued_clear():
    entered, release, cleared = anyio.Event(), anyio.Event(), anyio.Event()
    first, successor = AsyncMock(), AsyncMock()

    async def fail_clear():
        entered.set()
        await release.wait()
        raise RuntimeError("hardware send failed")

    async def clear_successor():
        cleared.set()

    first.clear.side_effect = fail_clear
    successor.clear.side_effect = clear_successor
    async with anyio.create_task_group() as tg:
        dispatcher = RenderDispatcher(
            command_service=FakeHardwareCommandService(),
            config_id="dev",
            backend=ImmediateBackend(),
            start_soon=tg.start_soon,
        )
        await dispatcher.clear_control("0,0", output=first)
        await entered.wait()
        await dispatcher.clear_control("0,0", output=successor)
        release.set()
        with anyio.fail_after(0.5):
            await cleared.wait()
        first.clear.assert_awaited_once()
        successor.clear.assert_awaited_once()
