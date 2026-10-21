"""Hardware commands and deliveries through real managed claims."""

import base64

import pytest
from deckr.hardware import CapabilityRef, DeviceRef, HardwareUnavailable
from deckr.hardware import messages as hw

from test_support.managed_hardware import _image, eventually, setup


@pytest.mark.asyncio
async def test_raster_and_power_commands_use_managed_descriptor_validation(
    managed_substrate,
):
    async with setup(substrate=managed_substrate) as h:
        output = h.service.command_service
        image = _image(format="PNG")
        await output.set_raster_frame("config-a", "key-1", "raster", image)
        await eventually(lambda: h.commands.await_count == 1)
        command = h.commands.await_args.args[0]
        assert command.contract == h.service.route_for_config("config-a").contract
        assert command.body["params"]["image"] == base64.b64encode(image).decode()
        assert command.body["params"]["encoding"] == "png"
        await output.clear_raster("config-a", "key-1", "raster")
        await output.sleep_device("config-a")
        await output.wake_device("config-a")
        await eventually(lambda: h.commands.await_count == 4)
        assert [
            call.args[0].body["commandType"] for call in h.commands.await_args_list
        ] == ["set_frame", "clear", "sleep", "wake"]
        for capability, data in (("missing", image), ("raster", b"invalid")):
            with pytest.raises((ValueError, HardwareUnavailable)):
                await output.set_raster_frame("config-a", "key-1", capability, data)
        await output.clear_raster("missing-config", "key-1", "raster")
        assert h.commands.await_count == 4


@pytest.mark.asyncio
async def test_power_commands_without_capability_are_ignored(managed_substrate):
    async with setup(power=False, substrate=managed_substrate) as h:
        await h.service.command_service.sleep_device("config-a")
        await h.service.command_service.wake_device("config-a")
        h.commands.assert_not_awaited()


def input_message(h):
    live = h.service.route_for_config("config-a")
    body = hw.ControlInputMessage(
        deviceRef=live.ref,
        controlId="key-1",
        capabilityId="input",
        eventType="down",
        sequence=1,
        value={"eventType": "down"},
    )
    return hw.hardware_message(
        sender=h.manager_endpoint.address,
        sender_session_id=h.manager_endpoint.session_id,
        recipient=h.endpoint.address,
        recipient_session_id=h.endpoint.session_id,
        contract=live.contract,
        message_type=hw.CONTROL_INPUT,
        subject=hw.hardware_subject_for_capability(
            CapabilityRef(deviceRef=live.ref, controlId="key-1", capabilityId="input")
        ),
        body=body,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid", ["session", "contract", "device", "capability", "value", "recipient"]
)
async def test_managed_input_rejects_invalid_scope_then_delivers_valid_input(
    invalid, managed_substrate
):
    async with setup(substrate=managed_substrate) as h:
        valid = input_message(h)
        updates = {}
        body = dict(valid.body)
        if invalid == "session":
            updates["sender_session_id"] = "old-session"
        elif invalid == "contract":
            updates["contract"] = valid.contract.model_copy(update={"generation": 2})
        elif invalid == "recipient":
            updates["recipient_session_id"] = "old-controller"
        else:
            body.update(
                {
                    "deviceRef": DeviceRef(
                        managerId="other", deviceId="device-a"
                    ).model_dump(by_alias=True)
                }
                if invalid == "device"
                else {"capabilityId": "missing"}
                if invalid == "capability"
                else {"value": {"eventType": "unknown"}}
            )
            updates["body"] = body
        await h.substrate.publish(valid.model_copy(update=updates))
        await h.substrate.publish(valid)
        await eventually(lambda: h.callbacks.on_hardware_control_input.await_count == 1)
        assert (
            h.callbacks.on_hardware_control_input.await_args.args[1].message_id
            == valid.message_id
        )
        assert h.service.route_for_config("config-a") is not None


@pytest.mark.asyncio
async def test_command_rejection_keeps_claim_authority(managed_substrate):
    async with setup(substrate=managed_substrate) as h:
        original = h.service.route_for_config("config-a")
        h.commands.return_value = False
        await h.service.command_service.clear_raster("config-a", "key-1", "raster")
        await eventually(
            lambda: h.callbacks.on_hardware_command_rejected.await_count == 1
        )
        event = h.callbacks.on_hardware_command_rejected.await_args.args[1]
        assert event.command_type == "clear"
        assert h.service.route_for_config("config-a") == original
