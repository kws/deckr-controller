from __future__ import annotations

import base64
from unittest.mock import AsyncMock

import pytest
from deckr.contracts.authority import ContractPointer
from deckr.contracts.messages import HARDWARE_MESSAGES_LANE, hardware_manager_address
from deckr.hardware import messages as hw_messages
from deckr.hardware.descriptors import (
    DECKR_DEVICE_POWER,
    CapabilityDescriptor,
    CapabilityRef,
    DeviceDescriptor,
    DeviceRef,
)

from deckr.controller._hardware_service import HardwareCommandService


def _endpoint():
    return type("Endpoint", (), {"send": AsyncMock()})()


def _ref() -> DeviceRef:
    return DeviceRef(managerId="manager-a", deviceId="device-a")


def _contract() -> ContractPointer:
    return ContractPointer(contractId="contract-a", generation=1)


def _device(*, power: bool = False) -> DeviceDescriptor:
    capabilities = ()
    if power:
        capabilities = (
            CapabilityDescriptor(
                capabilityId="device-power",
                family=DECKR_DEVICE_POWER,
                type="screen",
                direction="command",
                access=("invokable",),
                commandTypes=("sleep", "wake"),
            ),
        )
    return DeviceDescriptor(
        deviceId="device-a",
        fingerprint="fingerprint:device-a",
        displayName="Device A",
        capabilities=capabilities,
    )


def _service(endpoint=None, *, power: bool = False) -> HardwareCommandService:
    endpoint = endpoint or _endpoint()
    service = HardwareCommandService(endpoint, controller_id="controller-main")
    service.register_device(
        config_id="config-a",
        ref=_ref(),
        device=_device(power=power),
        contract=_contract(),
        manager_session_id="manager-session",
    )
    return service


def _sent_body(endpoint) -> dict:
    return endpoint.send.await_args.kwargs["body"]


@pytest.mark.asyncio
async def test_set_raster_frame_sends_base64_hardware_command() -> None:
    endpoint = _endpoint()
    service = _service(endpoint)
    image = b"jpeg-bytes"

    await service.set_raster_frame("config-a", "key-1", "raster", image)

    endpoint.send.assert_awaited_once()
    sent = endpoint.send.await_args.kwargs
    assert sent["lane"] == HARDWARE_MESSAGES_LANE
    assert sent["recipient"] == hardware_manager_address("manager-a")
    assert sent["recipient_session_id"] == "manager-session"
    assert sent["message_type"] == hw_messages.CONTROL_COMMAND
    assert sent["contract"] == _contract()
    assert sent["subject"] == hw_messages.hardware_subject_for_capability(
        CapabilityRef(deviceRef=_ref(), controlId="key-1", capabilityId="raster")
    )
    assert _sent_body(endpoint)["commandType"] == "set_frame"
    assert _sent_body(endpoint)["params"]["image"] == base64.b64encode(image).decode(
        "ascii"
    )
    assert _sent_body(endpoint)["params"]["encoding"] == "jpeg"


@pytest.mark.asyncio
async def test_clear_raster_sends_clear_command() -> None:
    endpoint = _endpoint()
    service = _service(endpoint)

    await service.clear_raster("config-a", "key-1", "raster")

    endpoint.send.assert_awaited_once()
    assert _sent_body(endpoint)["commandType"] == "clear"
    assert _sent_body(endpoint)["params"] == {}


@pytest.mark.asyncio
async def test_sleep_and_wake_send_power_commands_when_advertised() -> None:
    endpoint = _endpoint()
    service = _service(endpoint, power=True)

    await service.sleep_device("config-a")
    await service.wake_device("config-a")

    assert endpoint.send.await_count == 2
    assert [
        call.kwargs["body"]["commandType"]
        for call in endpoint.send.await_args_list
    ] == ["sleep", "wake"]


@pytest.mark.asyncio
async def test_sleep_and_wake_drop_without_power_capability() -> None:
    endpoint = _endpoint()
    service = _service(endpoint, power=False)

    await service.sleep_device("config-a")
    await service.wake_device("config-a")

    endpoint.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_commands_for_unregistered_config_drop_without_send() -> None:
    endpoint = _endpoint()
    service = _service(endpoint, power=True)

    await service.set_raster_frame("missing", "key-1", "raster", b"jpeg")
    await service.clear_raster("missing", "key-1", "raster")
    await service.sleep_device("missing")
    await service.wake_device("missing")

    endpoint.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_send_control_command_requires_device_ref() -> None:
    endpoint = _endpoint()
    service = _service(endpoint)
    live = service._devices_by_config_id["config-a"]

    with pytest.raises(ValueError, match="deviceRef"):
        await service._send_control_command(
            live=live,
            ref=CapabilityRef(capabilityId="raster"),
            command_type="set_frame",
            params={},
        )
