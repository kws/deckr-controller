"""Rendering contracts exercised through the production managed service boundary."""

import base64
import gc
import io
import threading
import weakref
from unittest.mock import AsyncMock

import anyio
import pytest
from PIL import Image

from deckr.controller import _render_workers as workers
from deckr.controller._bindings._render_policy import DEFAULT_LIMITS
from test_support.action_runtime import notifications, running_controller
from test_support.managed_hardware import eventually
from test_support.rendering import (
    GatedBackend,
    direct_parameters,
    drained,
    parameters,
    real_backend,
    result_for,
)


@pytest.mark.asyncio
async def test_render_rechecks_current_facts_without_reparsing_content(
    managed_substrate, monkeypatch
):
    import deckr.action_runtime.rendering as validation

    async with running_controller(managed_substrate, with_output=True) as h:
        backend = real_backend(h, GatedBackend())
        request = await h.channel.send("binding_render", parameters(h))
        await eventually(lambda: bool(backend.gates))
        renderer = h.device.components.rendering
        job = next(iter(renderer.jobs.values()))
        context = renderer._context(job.message, job.lease)

        def already_parsed(*args, **kwargs):
            raise AssertionError("Admitted content must not be parsed again")

        monkeypatch.setattr(
            validation, "action_runtime_body_from_service_message", already_parsed
        )
        for _ in range(100):
            job.command.validate(context)
        with pytest.raises(validation.RenderRejection, match="unauthorized"):
            job.command.validate(context.model_copy(update={"local_authority": False}))
        with pytest.raises(validation.RenderRejection, match="target_not_current"):
            job.command.validate(
                context.model_copy(update={"hardware_authority": False})
            )
        binding = context.bindings[job.lease.binding_id]
        changed = binding.model_copy(
            update={
                "binding": binding.binding.model_copy(
                    update={"output_generation": binding.binding.output_generation + 1}
                )
            }
        )
        with pytest.raises(validation.RenderRejection, match="target_not_current"):
            job.command.validate(
                context.model_copy(update={"bindings": {job.lease.binding_id: changed}})
            )
        backend.gates[0].set()
        assert (await result_for(h, request))["status"] == "dispatched"


@pytest.mark.asyncio
async def test_completed_render_releases_job_but_keeps_duplicate_fence(
    managed_substrate,
):
    async with running_controller(managed_substrate, with_output=True) as h:
        backend = real_backend(h, GatedBackend())
        request = await h.channel.send("binding_render", parameters(h))
        await eventually(lambda: bool(backend.gates))
        renderer = h.device.components.rendering
        job = weakref.ref(next(iter(renderer.jobs.values())))
        backend.gates[0].set()
        assert (await result_for(h, request))["status"] == "dispatched"
        await drained(h)
        gc.collect()
        assert job() is None, (
            "Completed render history retains payload and binding lease"
        )
        await h.substrate.publish(request)
        barrier = await h.channel.send(
            "binding_render", {**parameters(h), "requirementName": "missing"}
        )
        await result_for(h, barrier)
        assert len(backend.gates) == 1


@pytest.mark.asyncio
async def test_startup_burst_waits_for_shared_workers_without_losing_frames(
    managed_substrate, monkeypatch
):
    from deckr.controller._bindings import _render_policy as policy

    # This test deliberately occupies every worker while admitting fourteen
    # requests. Verify queueing independently of instrumented runner CPU speed;
    # the deadline/retirement tests below exercise expiry with controlled gates.
    monkeypatch.setattr(
        policy,
        "DEFAULT_LIMITS",
        DEFAULT_LIMITS.model_copy(update={"max_render_time_ms": 10000}),
    )
    await workers.prepare_thread_rendering()
    slots = threading.BoundedSemaphore(8)
    monkeypatch.setattr(workers, "_THREAD_SLOTS", slots)
    async with running_controller(
        managed_substrate, with_output=True, control_count=14
    ) as h:
        real_backend(h)
        h.commands.reset_mock()
        for _ in range(8):
            slots.acquire()
        try:
            requests = [
                await h.channel.send(
                    "binding_render", parameters(h, control_id=f"key-{i}")
                )
                for i in range(1, 15)
            ]
            rendering = h.device.components.rendering
            await eventually(lambda: len(rendering.jobs) == 14)
            assert all(not job.done for job in rendering.jobs.values())
            assert all(job.command_id is None for job in rendering.jobs.values())
            assert not notifications(h, "binding_render_result")
        finally:
            for _ in range(8):
                slots.release()
        final_ids = set()
        for request in requests:
            result = await result_for(h, request)
            assert result["status"] == "dispatched"
            final_ids.add(result["commandMessageId"])
        await drained(h)
        # Result notifications and hardware frames use independent NATS lanes.
        await eventually(
            lambda: {
                call.args[0].message.message_id for call in h.commands.await_args_list
            }
            >= final_ids
        )
        commands = [call.args[0].message for call in h.commands.await_args_list]
        assert 14 <= len(commands) <= 28  # At most one provisional frame per job.
        assert {c.message_id for c in commands} >= final_ids
        latest = {c.body["controlId"]: c.message_id for c in commands}
        assert set(latest.values()) == final_ids
        assert {c.body["controlId"] for c in commands} == {
            f"key-{i}" for i in range(1, 15)
        }
        for command in commands:
            assert command.body["commandType"] == "set_frame"
            with Image.open(
                io.BytesIO(base64.b64decode(command.body["params"]["image"]))
            ) as image:
                assert image.size == (72, 72)


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["deadline", "retire"])
async def test_queued_render_is_bounded_and_cannot_dispatch_after_expiry_or_retirement(
    managed_substrate, monkeypatch, stop
):
    import deckr.controller._bindings._render_policy as policy
    import deckr.controller._bindings._rendering as rendering

    admission = threading.BoundedSemaphore(2)
    execution = anyio.Semaphore(1)
    monkeypatch.setattr(rendering, "_ADMISSION_SLOTS", admission)
    import deckr.controller._bindings._render_execution as execution_module
    import deckr.controller._render_feedback as feedback

    original = execution_module.run_thread

    async def limited(*args, **kwargs):
        async with execution:
            return await original(*args, **kwargs)

    monkeypatch.setattr(execution_module, "run_thread", limited)
    monkeypatch.setattr(feedback, "LOADING_DELAY", 10)
    if stop == "deadline":
        monkeypatch.setattr(
            policy,
            "DEFAULT_LIMITS",
            DEFAULT_LIMITS.model_copy(update={"max_render_time_ms": 300}),
        )
    async with running_controller(
        managed_substrate, with_output=True, control_count=3
    ) as h:
        backend = real_backend(h)
        observe = AsyncMock(wraps=backend.render)
        monkeypatch.setattr(backend, "render", observe)
        h.commands.reset_mock()
        await execution.acquire()
        try:
            first = await h.channel.send("binding_render", parameters(h))
            queued = await h.channel.send(
                "binding_render", parameters(h, control_id="key-2")
            )
            await eventually(lambda: execution.statistics().tasks_waiting == 2)
            refused = await h.channel.send(
                "binding_render", parameters(h, control_id="key-3")
            )
            assert (await result_for(h, refused))["code"] == "busy"
            if stop == "retire":
                binding = h.device.context_for_control("key-2").metadata.binding_id
                await h.device.components.rendering.retire_binding(binding)
            result = await result_for(h, queued)
            assert result["code"] == (
                "deadline_exceeded" if stop == "deadline" else "target_not_current"
            )
            observe.assert_not_awaited()
            await eventually(
                lambda: execution.statistics().tasks_waiting
                == (0 if stop == "deadline" else 1)
            )
        finally:
            execution.release()
        await result_for(h, first)
        await eventually(lambda: not any(h.device.components.rendering.active.values()))
        assert observe.await_count == (1 if stop == "retire" else 0)
        assert h.commands.await_count == (1 if stop == "retire" else 0)
        assert admission.acquire(blocking=False)
        assert admission.acquire(blocking=False)
        assert not admission.acquire(blocking=False)
        admission.release()
        admission.release()


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


@pytest.mark.asyncio
async def test_superseded_jobs_release_capacity_and_deadline_prevents_late_dispatch(
    managed_substrate, monkeypatch
):
    import deckr.controller._bindings._render_policy as rendering

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
        await eventually(
            lambda: h.device.components.rendering.active.get("key-1", 0) == 1
        )
        latest = await h.channel.send("binding_render", parameters(h, text="C"))
        assert (await result_for(h, latest))["code"] == "deadline_exceeded"
        assert (await result_for(h, b))["status"] == "superseded"
        assert (await result_for(h, a))["status"] == "superseded"
        for gate in backend.gates:
            gate.set()
        await drained(h)
        await eventually(lambda: h.commands.await_count == 2)
        assert [
            call.args[0].message.body["commandType"]
            for call in h.commands.await_args_list
        ] == ["set_frame", "clear"]


@pytest.mark.asyncio
async def test_authority_rechecked_after_output_lock(managed_substrate, monkeypatch):
    async with running_controller(managed_substrate, with_output=True) as h:
        backend = real_backend(h, GatedBackend())
        h.commands.reset_mock()
        request = await h.channel.send("binding_render", parameters(h))
        await eventually(lambda: len(backend.started) == 1)
        host = h.device
        async with host.components.dispatcher._controls["key-1"].io_lock:
            backend.gates[0].set()
            await eventually(
                lambda: host.components.dispatcher._controls["key-1"]
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


@pytest.mark.asyncio
async def test_deadline_cancels_waiting_local_send(managed_substrate, monkeypatch):
    import deckr.controller._bindings._render_policy as rendering

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

    import deckr.controller._bindings._render_policy as rendering

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
