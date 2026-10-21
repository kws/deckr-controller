"""Rendering contracts exercised through the production managed service boundary."""

import base64
import io
from dataclasses import replace

import anyio
import pytest
from deckr.actions.rendering import RenderContent, RenderImage
from PIL import Image
from test_action_runtime_integration import notifications, open_page, running_controller

from deckr.controller._bindings._rendering import DEFAULT_LIMITS
from deckr.controller._render_content import (
    ContentFailure,
    decode_image,
    render_content,
)
from deckr.controller._render_dispatcher import ThreadRenderBackend
from test_support.managed_hardware import eventually


def png(size=(10, 5), color=(255, 0, 0, 255)):
    buffer = io.BytesIO()
    Image.new("RGBA", size, color).save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode("ascii")


def parameters(h, *, text="Kitchen", operation="set"):
    binding = h.device.context_for_control("key-1").metadata
    result = {
        "target": {
            "kind": "binding",
            "bindingId": binding.binding_id,
            "generation": binding.output_generation,
        },
        "requirementName": "display",
        "operation": operation,
    }
    if operation == "set":
        result["content"] = {
            "format": "dev.deckr.render.text.v1",
            "data": {"text": text},
        }
    return result


def real_backend(h, backend=None):
    backend = backend or ThreadRenderBackend()
    h.device._render_backend = backend
    h.device._rendering.ports = replace(h.device._rendering.ports, backend=backend)
    h.device._render_dispatcher._backend = backend
    return backend


async def result_for(h, request):
    await eventually(
        lambda: any(
            e["requestMessageId"] == request.message_id
            for e in notifications(h, "binding_render_result")
        )
    )
    return next(
        e
        for e in notifications(h, "binding_render_result")
        if e["requestMessageId"] == request.message_id
    )


@pytest.mark.asyncio
async def test_support_render_dispatch_and_clear(managed_substrate):
    async with running_controller(managed_substrate, with_output=True) as h:
        real_backend(h)
        support = notifications(h, "binding_attached")[-1]["rendering"]
        assert support["version"] == 1
        assert support["targets"][0]["requirementName"] == "display"
        h.commands.reset_mock()
        request = await h.channel.send("binding_render", parameters(h))
        result = await result_for(h, request)
        assert result["status"] == "dispatched", result
        await eventually(lambda: h.commands.await_count == 1)
        hardware = h.commands.await_args.args[0].message
        assert hardware.message_id == result["commandMessageId"]
        assert hardware.body["commandType"] == "set_frame"
        with Image.open(
            io.BytesIO(base64.b64decode(hardware.body["params"]["image"]))
        ) as image:
            assert image.size == (72, 72)
        event = next(
            r.message
            for r in h.received
            if r.authorization.body.name == "binding_render_result"
        )
        assert event.causation_id == request.message_id
        request = await h.channel.send(
            "binding_render", parameters(h, operation="clear")
        )
        assert (await result_for(h, request))["status"] == "dispatched"
        await eventually(lambda: h.commands.await_count == 2)
        assert h.commands.await_args.args[0].message.body["commandType"] == "clear"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation,code",
    [
        ("stale", "target_not_current"),
        ("unknown", "target_not_current"),
        ("format", "unsupported_format"),
        ("invalid", "invalid_schema"),
        ("text-limit", "limit_exceeded"),
        ("bad-image", "invalid_content"),
    ],
)
async def test_render_rejections_preserve_output(managed_substrate, mutation, code):
    async with running_controller(managed_substrate, with_output=True) as h:
        real_backend(h)
        p = parameters(h)
        if mutation == "stale":
            p["target"]["generation"] += 1
        if mutation == "unknown":
            p["target"]["bindingId"] = "unknown"
        if mutation == "format":
            p["content"] = {"format": "org.example.render.graph.v1", "data": {}}
        if mutation == "invalid":
            p["content"]["data"]["font"] = "unknown"
        if mutation == "text-limit":
            p["content"]["data"]["text"] = "x" * 1025
        if mutation == "bad-image":
            p["content"] = {
                "format": "dev.deckr.render.image.v1",
                "data": {"image": "https://example.org/test.png", "encoding": "png"},
            }
        h.commands.reset_mock()
        if code == "invalid_schema":
            with pytest.raises(ValueError, match="Service payload does not match"):
                await h.channel.send("binding_render", p)
            assert h.commands.await_count == 0
            return
        request = await h.channel.send("binding_render", p)
        result = await result_for(h, request)
        assert result["code"] == code
        assert h.commands.await_count == 0


class GatedBackend(ThreadRenderBackend):
    def __init__(self):
        self.started = []
        self.gates = []

    async def render(self, request):
        gate = anyio.Event()
        self.started.append(request)
        self.gates.append(gate)
        await gate.wait()
        return await super().render(request)


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
        await eventually(lambda: h.device._rendering.active.get("key-1", 0) == 0)
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
        await eventually(lambda: h.device._rendering.active.get("key-1", 0) == 0)
        assert h.commands.await_count == 0


@pytest.mark.parametrize("fit", ["contain", "cover", "stretch"])
def test_inline_geometry_and_alpha(fit):
    content = RenderContent(
        format="dev.deckr.render.image.v1",
        data={
            "image": png(color=(255, 0, 0, 128)),
            "encoding": "png",
            "fit": fit,
            "background": "#0000FF",
        },
    )
    result = render_content(content, 20, 20, DEFAULT_LIMITS)
    with Image.open(io.BytesIO(result)) as image:
        assert image.size == (20, 20)
        assert image.getpixel((10, 10)) == (128, 0, 127)
        assert image.getpixel((10, 0)) == (
            (0, 0, 255) if fit == "contain" else (128, 0, 127)
        )


def test_image_limits_before_decode_and_encoding_check():
    data = RenderImage(image=png(), encoding="png")
    with pytest.raises(ContentFailure, match="limit_exceeded"):
        decode_image(data, DEFAULT_LIMITS.model_copy(update={"max_image_pixels": 1}))
    with pytest.raises(ContentFailure, match="invalid_content"):
        decode_image(data.model_copy(update={"encoding": "jpeg"}), DEFAULT_LIMITS)


def direct_parameters(h, *, image=None, operation="set_frame"):
    binding = h.device.context_for_control("key-1").metadata
    match = next(
        m for m in binding.matched_capabilities if m.requirement_name == "display"
    )
    return {
        "binding": binding.model_dump(by_alias=True, mode="json", exclude_none=True),
        "capability": match.capability.model_dump(
            by_alias=True, mode="json", exclude_none=True
        ),
        "generation": binding.output_generation,
        "commandType": operation,
        "params": {}
        if operation == "clear"
        else {"image": image or png((72, 72)), "encoding": "png"},
    }


async def drained(h):
    await eventually(lambda: h.device._rendering.active.get("key-1", 0) == 0)


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
            lambda: h.device._render_dispatcher._controls["key-1"].active_render_count
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
async def test_deadline_and_busy_do_not_allow_late_dispatch(
    managed_substrate, monkeypatch
):
    import deckr.controller._bindings._rendering as rendering

    monkeypatch.setattr(
        rendering,
        "DEFAULT_LIMITS",
        DEFAULT_LIMITS.model_copy(update={"max_render_time_ms": 300}),
    )
    async with running_controller(managed_substrate, with_output=True) as h:
        backend = real_backend(h, GatedBackend())
        h.commands.reset_mock()
        a = await h.channel.send("binding_render", parameters(h, text="A"))
        await eventually(lambda: len(backend.started) == 1)
        b = await h.channel.send("binding_render", parameters(h, text="B"))
        await eventually(lambda: len(backend.started) == 2)
        busy = await h.channel.send("binding_render", parameters(h, text="C"))
        assert (await result_for(h, busy))["code"] == "busy"
        assert (await result_for(h, b))["code"] == "deadline_exceeded"
        assert (await result_for(h, a))["status"] == "superseded"
        for gate in backend.gates:
            gate.set()
        await drained(h)
        assert h.commands.await_count == 0


@pytest.mark.asyncio
async def test_authority_rechecked_after_output_lock(managed_substrate, monkeypatch):
    async with running_controller(managed_substrate, with_output=True) as h:
        backend = real_backend(h, GatedBackend())
        h.commands.reset_mock()
        request = await h.channel.send("binding_render", parameters(h))
        await eventually(lambda: len(backend.started) == 1)
        host = h.device
        async with host._render_dispatcher._controls["key-1"].io_lock:
            backend.gates[0].set()
            await eventually(
                lambda: host._render_dispatcher._controls["key-1"]
                .io_lock.statistics()
                .tasks_waiting
                == 1
            )
            monkeypatch.setattr(
                host._command_service, "has_authority", lambda *args: False
            )
        result = await result_for(h, request)
        assert result["status"] == "superseded"
        assert h.commands.await_count == 0


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
async def test_late_hardware_rejection_is_correlated_without_rollback(
    managed_substrate,
):
    from deckr.hardware import messages as hw

    async with running_controller(managed_substrate, with_output=True) as h:
        real_backend(h)
        h.commands.reset_mock()
        old = await h.channel.send("binding_render", parameters(h, text="old"))
        await result_for(h, old)
        await eventually(lambda: h.commands.await_count == 1)
        command = h.commands.await_args.args[0].message
        newer = await h.channel.send("binding_render", parameters(h, text="new"))
        await result_for(h, newer)
        await eventually(lambda: h.commands.await_count == 2)
        body = hw.hardware_body_from_message(command)
        rejection = hw.CommandRejectedMessage(
            deviceRef=body.device_ref,
            controlId=body.control_id,
            capabilityId=body.capability_id,
            commandType=body.command_type,
            reason="unsupported",
        )
        reply = await h.hardware_endpoint.reply_to(
            command,
            message_type=hw.COMMAND_REJECTED,
            body=rejection.model_dump(by_alias=True, mode="json", exclude_none=True),
            subject=command.subject,
        )
        await eventually(lambda: len(notifications(h, "binding_render_result")) == 3)
        event = notifications(h, "binding_render_result")[-1]
        assert event["stage"] == "hardware"
        assert event["requestMessageId"] == old.message_id
        assert event["commandMessageId"] == command.message_id
        await h.substrate.publish(reply)
        barrier = await h.channel.send(
            "binding_render", {**parameters(h), "requirementName": "missing"}
        )
        await result_for(h, barrier)
        assert (
            len(
                [
                    e
                    for e in notifications(h, "binding_render_result")
                    if e["stage"] == "hardware"
                ]
            )
            == 1
        )
        assert h.commands.await_count == 2


def test_non_square_orientation_and_static_image_validation():
    from deckr.controller._device_layout import RasterImageFormat
    from deckr.controller._render import (
        RenderModel,
        build_render_request,
        render_request_to_frame,
    )

    request = build_render_request(
        RenderModel(image="data:image/png;base64," + png((10, 20))),
        RasterImageFormat(width=20, height=10, rotation=90, format="PNG"),
    )
    with Image.open(io.BytesIO(render_request_to_frame(request))) as image:
        assert image.size == (20, 10)
    for raw in (base64.b64decode(png())[:-20], b"not png"):
        with pytest.raises(ContentFailure, match="invalid_content"):
            decode_image(
                RenderImage(image=base64.b64encode(raw).decode(), encoding="png"),
                DEFAULT_LIMITS,
            )
    out = io.BytesIO()
    Image.new("RGB", (2, 2), "red").save(
        out,
        format="PNG",
        save_all=True,
        append_images=[Image.new("RGB", (2, 2), "blue")],
        duration=100,
        loop=0,
    )
    with pytest.raises(ContentFailure, match="invalid_content"):
        decode_image(
            RenderImage(
                image=base64.b64encode(out.getvalue()).decode(), encoding="png"
            ),
            DEFAULT_LIMITS,
        )


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


@pytest.mark.asyncio
async def test_deadline_cancels_waiting_local_send(managed_substrate, monkeypatch):
    import deckr.controller._bindings._rendering as rendering

    monkeypatch.setattr(
        rendering,
        "DEFAULT_LIMITS",
        DEFAULT_LIMITS.model_copy(update={"max_render_time_ms": 300}),
    )
    async with running_controller(managed_substrate, with_output=True) as h:
        real_backend(h)
        h.commands.reset_mock()
        entered, release = anyio.Event(), anyio.Event()
        send = h.device._command_service.set_raster_frame

        async def blocked_send(*args, **kwargs):
            entered.set()
            await release.wait()
            await send(*args, **kwargs)

        monkeypatch.setattr(h.device._command_service, "set_raster_frame", blocked_send)
        request = await h.channel.send("binding_render", parameters(h))
        with anyio.fail_after(1):
            await entered.wait()
        assert (await result_for(h, request))["code"] == "deadline_exceeded"
        await drained(h)
        release.set()
        assert h.commands.await_count == 0


@pytest.mark.asyncio
async def test_direct_frames_do_not_require_rendering_support(
    managed_substrate, monkeypatch
):
    from deckr.actions.rendering import RenderingSupport

    import deckr.controller._bindings._rendering as rendering

    monkeypatch.setattr(
        rendering,
        "rendering_support",
        lambda context: RenderingSupport(version=1, targets=[]),
    )
    async with running_controller(managed_substrate, with_output=True) as h:
        assert notifications(h, "binding_attached")[-1]["rendering"]["targets"] == []
        h.commands.reset_mock()
        await h.channel.send("binding_output", direct_parameters(h))
        await eventually(lambda: h.commands.await_count == 1)
        assert h.commands.await_args.args[0].message.body["commandType"] == "set_frame"
        request = await h.channel.send("binding_render", parameters(h))
        assert (await result_for(h, request))["code"] == "unsupported_target"


@pytest.mark.asyncio
async def test_concurrent_duplicate_admission_is_atomic(managed_substrate):
    async with running_controller(managed_substrate, with_output=True) as h:
        backend = real_backend(h, GatedBackend())
        host = h.device
        h.commands.reset_mock()
        async with anyio.create_task_group() as tg:
            async with host.boundary:
                request = await h.channel.send("binding_render", parameters(h))
                tg.start_soon(host.handle_render_command, request)
                tg.start_soon(host.handle_render_command, request)
                await eventually(lambda: host.boundary.statistics().tasks_waiting >= 2)
            await eventually(lambda: len(backend.started) == 1)
            backend.gates[0].set()
            assert (await result_for(h, request))["status"] == "dispatched"
            await drained(h)
        assert len(backend.started) == 1
        assert h.commands.await_count == 1
