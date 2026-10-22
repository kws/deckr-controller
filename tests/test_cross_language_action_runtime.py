import base64
import io
import os
import sys
from dataclasses import replace
from pathlib import Path

import anyio
import pytest
from deckr.hardware import CapabilityRef
from deckr.hardware import messages as hw
from PIL import Image

from test_support.action_runtime import page_descriptor
from test_support.interop_controller import barrier, foreign_controller
from test_support.managed_hardware import (
    eventually,
)
from test_support.rendering import (
    GatedBackend,
    direct_parameters,
    parameters,
    png,
    real_backend,
)

INTEROP = os.environ.get("DECKR_INTEROP_ROOT")


if INTEROP:
    sys.path.insert(0, str(Path(INTEROP) / "scripts"))
    from action_runtime_support import Peer


pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.parametrize("language", ("python", "typescript", "rust")),
    pytest.mark.skipif(
        not INTEROP,
        reason="Run scripts/controller_action_runtime.py in deckr-integration-tests",
    ),
]


async def test_lifecycle_matching_input_and_output(language, tmp_path):
    async with foreign_controller(language, tmp_path) as h:
        created = await h.foreign.event("action_instance_created")
        attached = await h.foreign.event("binding_attached")
        binding = h.device.context_for_control("key-1").metadata
        assert created["contract"] == attached["contract"]
        assert attached["body"]["event"]["binding"]["bindingId"] == binding.binding_id
        matches = {
            m["requirementName"]: m
            for m in attached["body"]["event"]["binding"]["matchedCapabilities"]
        }
        assert matches["press"]["capability"] == matches["press-alias"]["capability"]
        assert matches["display"]["direction"] == "output"
        live = h.controller._hardware.route_for_config("config-a")
        assert created["contract"]["contractId"] != live.contract.contract_id
        capability = CapabilityRef(
            deviceRef=live.ref, controlId="key-1", capabilityId="input"
        )
        assert await h.hardware.handle_hardware_message(
            hw.hardware_message(
                sender=h.hardware.endpoint.address,
                sender_session_id=h.hardware.endpoint.session_id,
                recipient=h.endpoint.address,
                recipient_session_id=h.endpoint.session_id,
                contract=live.contract,
                message_type=hw.CONTROL_INPUT,
                subject=hw.hardware_subject_for_capability(capability),
                body=hw.ControlInputMessage(
                    deviceRef=live.ref,
                    controlId="key-1",
                    capabilityId="input",
                    eventType="down",
                    sequence=1,
                    value={"eventType": "down"},
                ),
            )
        )
        event = await h.foreign.event("capability_input")
        assert event["body"]["event"]["binding"]["bindingId"] == binding.binding_id
        h.commands.reset_mock()
        output = {
            "binding": binding.model_dump(by_alias=True, mode="json"),
            "capability": capability.model_copy(
                update={"capability_id": "raster"}
            ).model_dump(by_alias=True, mode="json"),
            "commandType": "clear",
            "generation": binding.output_generation,
            "params": {},
        }
        await h.foreign.send(
            "binding_output",
            {
                **output,
                "capability": {**output["capability"], "capabilityId": "outside"},
            },
        )
        await h.foreign.send("binding_output", output)
        await barrier(h)
        await eventually(lambda: h.commands.await_count == 1)
        command = h.commands.await_args.args[0].message
        assert (
            command.contract == live.contract and command.body["commandType"] == "clear"
        )
        assert len(await h.foreign.events("capability_input")) == 1


async def test_page_targets_generations_and_notifications(language, tmp_path):
    async with foreign_controller(language, tmp_path) as h:
        target = parameters(h)["target"]
        await h.foreign.send(
            "open_page",
            {
                "target": {**target, "generation": target["generation"] + 1},
                "descriptor": page_descriptor(),
            },
        )
        await barrier(h)
        assert not await h.foreign.events("page_session_opened")
        await h.foreign.send(
            "open_page", {"target": target, "descriptor": page_descriptor()}
        )
        opened = (await h.foreign.event("page_session_opened"))["body"]["event"][
            "pageSession"
        ]
        assert opened["generation"] == 0
        page_target = {
            "kind": "page_session",
            "pageSessionId": opened["pageSessionId"],
            "generation": 0,
        }
        await h.foreign.send(
            "replace_page", {"target": page_target, "descriptor": page_descriptor()}
        )
        replacement = (await h.foreign.event("page_session_opened", count=2))["body"][
            "event"
        ]["pageSession"]
        assert replacement["generation"] == 1
        await h.foreign.send("close_page", {"target": page_target})
        await barrier(h)
        assert not await h.foreign.events("page_session_closed")
        await h.foreign.send("close_page", {"target": {**page_target, "generation": 1}})
        closed = (await h.foreign.event("page_session_closed"))["body"]["event"][
            "pageSession"
        ]
        assert closed["generation"] == 1
        assert h.device.active_page_session() is None
        assert await h.foreign.events("binding_detached")


async def test_render_formats_rejections_and_correlation(language, tmp_path):
    async with foreign_controller(language, tmp_path) as h:
        real_backend(h)
        support = (await h.foreign.event("binding_attached"))["body"]["event"][
            "rendering"
        ]
        assert (
            support["version"] == 1
            and support["targets"][0]["requirementName"] == "display"
        )
        h.commands.reset_mock()
        for content in (
            parameters(h)["content"],
            {
                "format": "dev.deckr.render.image.v1",
                "data": {"image": png(), "encoding": "png"},
            },
        ):
            request = await h.foreign.send(
                "binding_render", {**parameters(h), "content": content}
            )
            result = await h.foreign.result(request)
            assert result["status"] == "dispatched", result
            await eventually(
                lambda result=result: any(
                    c.args[0].message.message_id == result["commandMessageId"]
                    for c in h.commands.await_args_list
                )
            )
            hardware = h.commands.await_args.args[0].message
            with Image.open(
                io.BytesIO(base64.b64decode(hardware.body["params"]["image"]))
            ) as image:
                assert image.size == (72, 72)
        for content, code in (
            ({"format": "org.example.graph.v1", "data": {}}, "unsupported_format"),
            (
                {"format": "dev.deckr.render.text.v1", "data": {"text": "x" * 1025}},
                "limit_exceeded",
            ),
            (
                {
                    "format": "dev.deckr.render.image.v1",
                    "data": {
                        "image": "https://example.org/image.png",
                        "encoding": "png",
                    },
                },
                "invalid_content",
            ),
        ):
            request = await h.foreign.send(
                "binding_render", {**parameters(h), "content": content}
            )
            assert (await h.foreign.result(request))["code"] == code
        assert h.commands.await_count == 2
        clear = await h.foreign.send("binding_render", parameters(h, operation="clear"))
        assert (await h.foreign.result(clear))["status"] == "dispatched"
        await eventually(lambda: h.commands.await_count == 3)
        assert h.commands.await_args.args[0].message.body["commandType"] == "clear"


async def test_render_reverse_completion_and_clear(language, tmp_path):
    async with foreign_controller(language, tmp_path) as h:
        backend = real_backend(h, GatedBackend())
        h.commands.reset_mock()
        old = await h.foreign.send("binding_render", parameters(h, text="old"))
        await eventually(lambda: len(backend.started) == 1)
        new = await h.foreign.send("binding_render", parameters(h, text="new"))
        await eventually(lambda: len(backend.started) == 2)
        backend.gates[1].set()
        assert (await h.foreign.result(new))["status"] == "dispatched"
        backend.gates[0].set()
        assert (await h.foreign.result(old))["status"] == "superseded"
        pending = await h.foreign.send("binding_render", parameters(h))
        await eventually(lambda: len(backend.started) == 3)
        clear = await h.foreign.send("binding_render", parameters(h, operation="clear"))
        assert (await h.foreign.result(clear))["status"] == "dispatched"
        backend.gates[2].set()
        assert (await h.foreign.result(pending))["status"] == "superseded"
        await eventually(
            lambda: h.device.components.rendering.active.get("key-1", 0) == 0
        )
        assert h.commands.await_count == 2


async def test_hardware_rejection_after_newer_render(language, tmp_path):
    async with foreign_controller(language, tmp_path) as h:
        real_backend(h)
        h.commands.reset_mock()
        old = await h.foreign.send("binding_render", parameters(h, text="old"))
        await h.foreign.result(old)
        await eventually(lambda: h.commands.await_count == 1)
        command = h.commands.await_args.args[0].message
        new = await h.foreign.send("binding_render", parameters(h, text="new"))
        await h.foreign.result(new)
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
        event = await h.foreign.event(
            "binding_render_result", lambda e: e["stage"] == "hardware"
        )
        result = event["body"]["event"]
        assert (
            result["requestMessageId"] == old
            and result["commandMessageId"] == command.message_id
        )
        await h.substrate.publish(reply)
        await barrier(h)
        assert (
            len(
                [
                    e
                    for e in await h.foreign.events("binding_render_result")
                    if e["body"]["event"]["stage"] == "hardware"
                ]
            )
            == 1
        )
        assert h.commands.await_count == 2


async def test_failed_render_and_direct_frame_supersede_older_work(language, tmp_path):
    async with foreign_controller(language, tmp_path) as h:
        backend = real_backend(h, GatedBackend())
        h.commands.reset_mock()
        old = await h.foreign.send("binding_render", parameters(h))
        await eventually(lambda: len(backend.started) == 1)
        failed = await h.foreign.send(
            "binding_render",
            {
                **parameters(h),
                "content": {
                    "format": "dev.deckr.render.image.v1",
                    "data": {"encoding": "png", "image": "bm90LWFuLWltYWdl"},
                },
            },
        )
        assert (await h.foreign.result(failed))["status"] == "failed"
        backend.gates[0].set()
        assert (await h.foreign.result(old))["status"] == "superseded"
        await eventually(
            lambda: h.device.components.rendering.active.get("key-1", 0) == 0
        )
        assert h.commands.await_count == 0
        pending = await h.foreign.send("binding_render", parameters(h))
        await eventually(lambda: len(backend.started) == 2)
        direct = direct_parameters(h)
        await h.foreign.send("binding_output", direct)
        await eventually(lambda: h.commands.await_count == 1)
        backend.gates[1].set()
        assert (await h.foreign.result(pending))["status"] == "superseded"
        assert (
            h.commands.await_args.args[0].message.body["params"]["image"]
            == direct["params"]["image"]
        )
        for image in ("https://example.org/image.png", png((3, 4))):
            await h.foreign.send("binding_output", direct_parameters(h, image=image))
            await barrier(h)
        assert h.commands.await_count == 1


async def test_overlay_composition_during_render_and_clear(language, tmp_path):
    async with foreign_controller(language, tmp_path) as h:
        backend = real_backend(h, GatedBackend())
        h.commands.reset_mock()
        base = await h.foreign.send("binding_render", parameters(h))
        await eventually(lambda: len(backend.started) == 1)
        binding = h.device.context_for_control("key-1").metadata.model_dump(
            by_alias=True, mode="json", exclude_none=True
        )
        await h.foreign.send(
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
        assert (await h.foreign.result(base))["status"] == "dispatched"
        backend.gates[1].set()
        await eventually(
            lambda: h.device.components.dispatcher._controls[
                "key-1"
            ].active_render_count
            == 0
        )
        assert h.commands.await_count == 1
        clear = await h.foreign.send("binding_render", parameters(h, operation="clear"))
        await eventually(lambda: len(backend.started) == 4)
        backend.gates[3].set()
        assert (await h.foreign.result(clear))["status"] == "dispatched"
        assert h.commands.await_args.args[0].message.body["commandType"] == "set_frame"
        await h.foreign.send(
            "binding_overlay_clear",
            {"binding": binding, "generation": 2, "overlayId": "test"},
        )
        await eventually(lambda: h.commands.await_count == 3)
        assert h.commands.await_args.args[0].message.body["commandType"] == "clear"


async def test_hardware_loss_fences_queued_render(language, tmp_path):
    async with foreign_controller(language, tmp_path) as h:
        attached = await h.foreign.event("binding_attached")
        backend = real_backend(h, GatedBackend())
        h.commands.reset_mock()
        await h.foreign.send("binding_render", parameters(h))
        await eventually(lambda: len(backend.started) == 1)
        await h.hardware.remove_device(
            "device-a", reason="cross-language queued render test"
        )
        await h.foreign.event("binding_detached")
        backend.gates[0].set()
        await eventually(
            lambda: h.device.components.rendering.active.get("key-1", 0) == 0
        )
        assert h.commands.await_count == 0
        assert (await h.foreign.peer.command("status"))["contract"] == attached[
            "contract"
        ]


async def test_provider_successor_fences_queued_render_and_old_binding(
    language, tmp_path
):
    async with foreign_controller(language, tmp_path) as h:
        old_event = await h.foreign.event("binding_attached")
        old_params = parameters(h)
        backend = real_backend(h, GatedBackend())
        h.commands.reset_mock()
        await h.foreign.send("binding_render", old_params)
        await eventually(lambda: len(backend.started) == 1)
        old_session = h.foreign.peer.ready["sessionId"]
        await h.foreign.peer.stop()
        await eventually(lambda: h.device.context_for_control("key-1") is None)
        backend.gates[0].set()
        await eventually(
            lambda: h.device.components.rendering.active.get("key-1", 0) == 0
        )
        assert h.commands.await_count == 0
        h.foreign.peer = Peer(
            language, h.peer_config, h.evidence / "provider-successor"
        )
        await h.foreign.peer.start()
        fresh = await h.foreign.event("binding_attached")
        assert (
            fresh["contract"] != old_event["contract"]
            and fresh["contract"]["generation"] == 1
        )
        assert h.foreign.peer.ready["sessionId"] != old_session
        assert (
            fresh["body"]["event"]["binding"]["bindingId"]
            != old_event["body"]["event"]["binding"]["bindingId"]
        )
        assert fresh["body"]["event"]["rendering"]
        real_backend(h)
        stale = await h.foreign.send("binding_render", old_params)
        assert (await h.foreign.result(stale))["code"] == "target_not_current"
        fresh_request = await h.foreign.send("binding_render", parameters(h))
        assert (await h.foreign.result(fresh_request))["status"] == "dispatched"
        await eventually(lambda: h.commands.await_count == 1)


async def test_service_loss_during_page_planning(language, tmp_path, monkeypatch):
    async with foreign_controller(language, tmp_path) as h:
        await h.foreign.send(
            "open_page",
            {"target": parameters(h)["target"], "descriptor": page_descriptor()},
        )
        await h.foreign.event("page_session_opened")
        session = h.device.active_page_session()
        bindings = h.device
        entered, release = anyio.Event(), anyio.Event()
        original = bindings.components.planning._build_page_plan

        async def delayed(*args, **kwargs):
            result = await original(*args, **kwargs)
            entered.set()
            await release.wait()
            return result

        monkeypatch.setattr(
            bindings.components.navigation,
            "bindings",
            replace(bindings.components.navigation.bindings, build_plan=delayed),
        )
        await h.foreign.send(
            "replace_page",
            {
                "target": {
                    "kind": "page_session",
                    "pageSessionId": session.page_session_id,
                    "generation": 0,
                },
                "descriptor": page_descriptor(),
            },
        )
        with anyio.fail_after(10):
            await entered.wait()
        await h.foreign.peer.stop()
        release.set()
        await eventually(lambda: bindings.active_page_session() is None)
        assert session.generation == 0
