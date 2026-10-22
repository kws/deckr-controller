"""Rendering contracts exercised through the production managed service boundary."""

import anyio
import pytest

from test_support.action_runtime import open_page, running_controller
from test_support.managed_hardware import eventually
from test_support.rendering import (
    GatedBackend,
    direct_parameters,
    drained,
    parameters,
    png,
    real_backend,
    result_for,
)


@pytest.mark.asyncio
async def test_old_render_cannot_overwrite_newer_clear(managed_substrate):
    async with running_controller(managed_substrate, with_output=True) as h:
        backend = real_backend(h, GatedBackend())
        h.commands.reset_mock()
        old = await h.channel.send("binding_render", parameters(h, text="old"))
        await eventually(lambda: len(backend.started) == 1)
        clear = await h.channel.send("binding_render", parameters(h, operation="clear"))
        assert (await result_for(h, clear))["status"] == "dispatched"
        assert (await result_for(h, old))["status"] == "superseded"
        backend.gates[0].set()
        await eventually(
            lambda: h.device.components.rendering.active.get("key-1", 0) == 0
        )
        assert h.commands.await_count == 1
        assert h.commands.await_args.args[0].message.body["commandType"] == "clear"


@pytest.mark.asyncio
async def test_newer_failure_does_not_restore_old_render(managed_substrate):
    async with running_controller(managed_substrate, with_output=True) as h:
        backend = real_backend(h, GatedBackend())
        h.commands.reset_mock()
        old = await h.channel.send("binding_render", parameters(h, text="old"))
        await eventually(lambda: len(backend.started) == 1)
        p = parameters(h)
        p["content"] = {
            "format": "dev.deckr.render.image.v1",
            "data": {"encoding": "png", "image": "bm90LWFuLWltYWdl"},
        }
        newer = await h.channel.send("binding_render", p)
        assert (await result_for(h, newer))["status"] == "failed"
        assert (await result_for(h, old))["status"] == "superseded"
        backend.gates[0].set()
        await eventually(
            lambda: h.device.components.rendering.active.get("key-1", 0) == 0
        )
        assert h.commands.await_count == 0


@pytest.mark.asyncio
async def test_newer_success_wins_reverse_completion_and_duplicate_delivery(
    managed_substrate,
):
    async with running_controller(managed_substrate, with_output=True) as h:
        backend = real_backend(h, GatedBackend())
        h.commands.reset_mock()
        old = await h.channel.send("binding_render", parameters(h, text="old"))
        await eventually(lambda: len(backend.started) == 1)
        new = await h.channel.send("binding_render", parameters(h, text="new"))
        await eventually(lambda: len(backend.started) == 2)
        await h.substrate.publish(new)
        backend.gates[1].set()
        assert (await result_for(h, new))["status"] == "dispatched"
        backend.gates[0].set()
        await drained(h)
        assert (await result_for(h, old))["status"] == "superseded"
        await h.substrate.publish(new)
        barrier = await h.channel.send(
            "binding_render", {**parameters(h), "requirementName": "missing"}
        )
        await result_for(h, barrier)
        assert len(backend.started) == 2
        assert h.commands.await_count == 1


@pytest.mark.asyncio
async def test_direct_frame_shares_order_and_requires_image_bytes(managed_substrate):
    async with running_controller(managed_substrate, with_output=True) as h:
        backend = real_backend(h, GatedBackend())
        h.commands.reset_mock()
        old = await h.channel.send("binding_render", parameters(h))
        await eventually(lambda: len(backend.started) == 1)
        data = direct_parameters(h)
        await h.channel.send("binding_output", data)
        await eventually(lambda: h.commands.await_count == 1)
        assert (
            h.commands.await_args.args[0].message.body["params"]["image"]
            == data["params"]["image"]
        )
        backend.gates[0].set()
        await drained(h)
        assert (await result_for(h, old))["status"] == "superseded"
        for image in (
            "https://example.org/image.png",
            "data:image/png;base64," + png(),
            png((3, 4)),
        ):
            await h.channel.send("binding_output", direct_parameters(h, image=image))
            # A following render result gives a transport-order barrier.
            barrier = await h.channel.send(
                "binding_render", {**parameters(h), "requirementName": "missing"}
            )
            await result_for(h, barrier)
            await drained(h)
            assert h.commands.await_count == 1


@pytest.mark.asyncio
async def test_overlay_change_during_base_render_and_clear_preserves_overlay(
    managed_substrate,
):
    async with running_controller(managed_substrate, with_output=True) as h:
        backend = real_backend(h, GatedBackend())
        h.commands.reset_mock()
        base = await h.channel.send("binding_render", parameters(h))
        await eventually(lambda: len(backend.started) == 1)
        binding = h.device.context_for_control("key-1").metadata.model_dump(
            by_alias=True, mode="json", exclude_none=True
        )
        await h.channel.send(
            "binding_overlay",
            {
                "binding": binding,
                "template": "ok",
                "generation": 1,
                "overlayId": "test",
            },
        )
        await eventually(lambda: len(backend.started) == 2)
        backend.gates[0].set()
        await eventually(lambda: len(backend.started) == 3)
        backend.gates[2].set()
        result = await result_for(h, base)
        assert result["status"] == "dispatched", result
        backend.gates[1].set()  # The previous retained base must not return.
        await eventually(
            lambda: h.device.components.dispatcher._controls[
                "key-1"
            ].active_render_count
            == 0
        )
        assert h.commands.await_count == 1
        clear = await h.channel.send("binding_render", parameters(h, operation="clear"))
        await eventually(lambda: len(backend.started) == 4)
        backend.gates[3].set()
        assert (await result_for(h, clear))["status"] == "dispatched"
        assert h.commands.await_args.args[0].message.body["commandType"] == "set_frame"
        await h.channel.send(
            "binding_overlay_clear",
            {"binding": binding, "generation": 2, "overlayId": "test"},
        )
        await eventually(lambda: h.commands.await_count == 3)
        assert h.commands.await_args.args[0].message.body["commandType"] == "clear"


@pytest.mark.asyncio
async def test_hidden_binding_cannot_dispatch_old_render(managed_substrate):
    async with running_controller(managed_substrate, with_output=True) as h:
        backend = real_backend(h, GatedBackend())
        h.commands.reset_mock()
        request = await h.channel.send("binding_render", parameters(h))
        await eventually(lambda: len(backend.started) == 1)
        await open_page(h)
        count = h.commands.await_count
        backend.gates[0].set()
        result = await result_for(h, request)
        assert result["status"] == "superseded"
        await drained(h)
        assert h.commands.await_count == count


@pytest.mark.asyncio
async def test_overlay_update_commits_during_send_and_recomposes_accepted_base(
    managed_substrate, monkeypatch
):
    async with running_controller(managed_substrate, with_output=True) as h:
        real_backend(h)
        h.commands.reset_mock()
        host = h.device
        router = h.device.context_for_control("key-1")._router
        sending, release, overlay_attempt = anyio.Event(), anyio.Event(), anyio.Event()
        send = host._command_service.set_raster_frame
        show = router.show_overlay

        async def blocked_send(*args, **kwargs):
            if not sending.is_set():
                sending.set()
                await release.wait()
            await send(*args, **kwargs)

        async def observe_overlay(**kwargs):
            overlay_attempt.set()
            return await show(**kwargs)

        monkeypatch.setattr(host._command_service, "set_raster_frame", blocked_send)
        monkeypatch.setattr(router, "show_overlay", observe_overlay)
        request = await h.channel.send("binding_render", parameters(h))
        with anyio.fail_after(1):
            await sending.wait()
        binding = h.device.context_for_control("key-1").metadata.model_dump(
            by_alias=True, mode="json", exclude_none=True
        )
        await h.channel.send(
            "binding_overlay",
            {
                "binding": binding,
                "template": "ok",
                "generation": 1,
                "overlayId": "test",
            },
        )
        with anyio.fail_after(1):
            await overlay_attempt.wait()
        await eventually(lambda: router.store.overlay is not None)
        assert h.commands.await_count == 0
        release.set()
        assert (await result_for(h, request))["status"] == "dispatched"
        await eventually(lambda: h.commands.await_count == 2)
        assert router._store.overlay.template == "ok"
        assert router.store.content.image is not None
        images = [
            call.args[0].message.body["params"]["image"]
            for call in h.commands.await_args_list
        ]
        assert images[0] != images[1]
