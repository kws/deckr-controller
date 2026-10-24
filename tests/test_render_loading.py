"""Provisional output never accepts a base or finishes the real request."""

from unittest.mock import AsyncMock

import anyio
import pytest

from deckr.controller._render import RenderResult
from deckr.controller._render_dispatcher import RenderDispatcher
from test_support.action_runtime import notifications, running_controller
from test_support.managed_hardware import eventually
from test_support.render_dispatch import FakeHardwareCommandService, ImmediateBackend
from test_support.rendering import GatedBackend, parameters, real_backend, result_for


@pytest.mark.asyncio
async def test_queued_feedback_cannot_replace_a_final_frame():
    output = AsyncMock()
    async with anyio.create_task_group() as group:
        dispatcher = RenderDispatcher(
            command_service=FakeHardwareCommandService(),
            config_id="dev",
            backend=ImmediateBackend(),
            start_soon=group.start_soon,
        )
        ticket = await dispatcher.reserve_base(
            control_id="key", context_id="ctx", binding_id="b", output=output
        )
        revision = dispatcher.composition_revision(ticket)
        ready = False
        state = dispatcher._controls["key"]
        async with state.io_lock:

            async def queued():
                await dispatcher.publish_feedback(
                    "key", revision, b"loading", is_pending=lambda: not ready
                )

            group.start_soon(queued)
            await anyio.sleep(0)
            ready = True
        await dispatcher._apply_result(
            RenderResult(
                context_id="ctx",
                binding_id="b",
                control_id="key",
                generation=revision,
                frame=b"final",
            )
        )
    output.write.assert_awaited_once_with(b"final")


@pytest.mark.asyncio
async def test_failure_restores_same_binding_but_never_retired_content():
    output = AsyncMock()
    dispatcher = RenderDispatcher(
        command_service=FakeHardwareCommandService(),
        config_id="dev",
        backend=ImmediateBackend(),
        start_soon=lambda *args: None,
    )
    ticket = await dispatcher.reserve_base(
        control_id="key", context_id="ctx", binding_id="b", output=output
    )
    revision = dispatcher.composition_revision(ticket)
    await dispatcher._apply_result(
        RenderResult(
            context_id="ctx",
            binding_id="b",
            control_id="key",
            generation=revision,
            frame=b"accepted",
        )
    )
    await dispatcher.publish_feedback(
        "key", revision, b"loading", is_pending=lambda: True
    )
    assert dispatcher._controls["key"].accepted_frame == b"accepted"
    await dispatcher.restore_feedback("key", revision)
    assert output.write.await_args.args == (b"accepted",)
    await dispatcher.publish_feedback(
        "key", revision, b"loading", is_pending=lambda: True
    )
    await dispatcher.reserve_base(
        control_id="key", context_id="next", binding_id="next", output=output
    )
    output.reset_mock()
    await dispatcher.restore_feedback("key", revision)
    output.write.assert_not_awaited()
    assert dispatcher._controls["key"].accepted_frame is None


@pytest.mark.asyncio
async def test_loading_precedes_final_without_accepting_placeholder(
    managed_substrate, monkeypatch
):
    from deckr.controller._bindings import _render_policy as policy

    # Hold the final frame until the independently delivered loading frame is
    # observed. Expiry is exercised separately below, not by runner CPU speed.
    monkeypatch.setattr(
        policy,
        "DEFAULT_LIMITS",
        policy.DEFAULT_LIMITS.model_copy(update={"max_render_time_ms": 10000}),
    )
    async with running_controller(managed_substrate, with_output=True) as h:
        backend = real_backend(h, GatedBackend())
        renderer = h.device.components.rendering
        router = h.device.context_for_control("key-1").presentation
        h.commands.reset_mock()
        request = await h.channel.send("binding_render", parameters(h))
        await eventually(lambda: bool(backend.gates))
        await eventually(lambda: h.commands.await_count == 1)
        assert not notifications(h, "binding_render_result")
        assert router.store.content.raster is None
        assert not next(iter(renderer.jobs.values())).done
        backend.gates[0].set()
        result = await result_for(h, request)
        assert result["status"] == "dispatched", repr(result)
        await eventually(lambda: h.commands.await_count == 2)
        assert router.store.content.raster is not None
        messages = [c.args[0].message for c in h.commands.await_args_list]
        assert messages[0].message_id != result["commandMessageId"]
        assert messages[1].message_id == result["commandMessageId"]


@pytest.mark.asyncio
async def test_expired_loading_clears_without_publishing_late_final(
    managed_substrate, monkeypatch
):
    import deckr.controller._bindings._render_policy as policy

    monkeypatch.setattr(
        policy,
        "DEFAULT_LIMITS",
        policy.DEFAULT_LIMITS.model_copy(update={"max_render_time_ms": 300}),
    )
    async with running_controller(managed_substrate, with_output=True) as h:
        backend = real_backend(h, GatedBackend())
        h.commands.reset_mock()
        request = await h.channel.send("binding_render", parameters(h))
        await eventually(lambda: h.commands.await_count == 1)
        assert (await result_for(h, request))["code"] == "deadline_exceeded"
        await eventually(lambda: h.commands.await_count == 2)
        assert h.commands.await_args.args[0].message.body["commandType"] == "clear"
        backend.gates[0].set()
        await anyio.sleep(0.05)
        assert h.commands.await_count == 2


@pytest.mark.asyncio
async def test_feedback_uses_reserved_capacity_when_raster_threads_are_busy():
    from deckr.controller._raster import RasterFormat
    from deckr.controller._render_feedback import loading_frame
    from deckr.controller._render_workers import _THREAD_SLOTS

    held = 0
    try:
        while _THREAD_SLOTS.acquire(blocking=False):
            held += 1
        with anyio.fail_after(0.5):
            assert await loading_frame(RasterFormat(72, 72))
    finally:
        for _ in range(held):
            _THREAD_SLOTS.release()


@pytest.mark.asyncio
async def test_provider_feedback_prevents_automatic_loading(managed_substrate):
    async with running_controller(managed_substrate, with_output=True) as h:
        backend = real_backend(h, GatedBackend())
        h.commands.reset_mock()
        base = await h.channel.send("binding_render", parameters(h))
        await eventually(lambda: len(backend.started) == 1)
        context = h.device.context_for_control("key-1")
        await h.channel.send(
            "binding_overlay",
            {
                "binding": context.metadata.model_dump(
                    by_alias=True, mode="json", exclude_none=True
                ),
                "template": "unavailable",
                "generation": 1,
                "overlayId": "provider-state",
            },
        )
        await eventually(lambda: context.presentation.store.overlay is not None)
        await anyio.sleep(0.16)
        assert h.commands.await_count == 0
        await h.device.components.rendering.retire_binding(context.metadata.binding_id)
        assert (await result_for(h, base))["status"] == "superseded"
        for gate in backend.gates:
            gate.set()
