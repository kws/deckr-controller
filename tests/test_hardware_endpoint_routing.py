from __future__ import annotations

from unittest.mock import AsyncMock

import anyio
import pytest
from deckr.contracts.messages import hardware_manager_address
from deckr.hardware import messages as hw_messages
from deckr.transports.bus import EventBus

from deckr.controller._controller_service import ControllerService
from deckr.controller._hardware_service import (
    HardwareCommandService,
    HardwareDeviceRegistry,
)
from deckr.controller.config import NullDeviceConfigService
from deckr.controller.settings import InMemorySettingsService


def _device(device_id: str, fingerprint: str) -> hw_messages.HardwareDevice:
    return hw_messages.HardwareDevice(
        id=device_id,
        name="Test Device",
        hid=f"hid:{fingerprint}",
        fingerprint=fingerprint,
        slots=[],
    )


@pytest.mark.asyncio
async def test_manager_local_device_ids_do_not_collide_in_registry_or_commands():
    bus = EventBus("hardware_messages")
    command_service = HardwareCommandService(bus, controller_id="controller-main")
    registry = HardwareDeviceRegistry()
    ref_a = hw_messages.HardwareDeviceRef(manager_id="room-a", device_id="deck")
    ref_b = hw_messages.HardwareDeviceRef(manager_id="room-b", device_id="deck")

    await bus.claim_local_endpoint(hardware_manager_address("room-a"))
    await bus.claim_local_endpoint(hardware_manager_address("room-b"))
    registry.connect(
        config_id="config-room-a",
        ref=ref_a,
        device=_device("deck", "serial-a"),
    )
    registry.connect(
        config_id="config-room-b",
        ref=ref_b,
        device=_device("deck", "serial-b"),
    )
    command_service.register_device(config_id="config-room-a", ref=ref_a)
    command_service.register_device(config_id="config-room-b", ref=ref_b)

    async with bus.subscribe() as stream:
        await command_service.set_image("config-room-a", "0,0", b"a")
        await command_service.clear_slot("config-room-b", "0,0")
        msg_a = await stream.receive()
        msg_b = await stream.receive()

    assert registry.get_by_ref(ref_a).config_id == "config-room-a"
    assert registry.get_by_ref(ref_b).config_id == "config-room-b"
    assert msg_a.recipient.endpoint == hardware_manager_address("room-a")
    assert msg_b.recipient.endpoint == hardware_manager_address("room-b")
    assert hw_messages.hardware_control_ref_from_subject(msg_a.subject) == (
        hw_messages.HardwareControlRef(
            manager_id="room-a",
            device_id="deck",
            control_id="0,0",
            control_kind="slot",
        )
    )
    assert hw_messages.hardware_control_ref_from_subject(msg_b.subject) == (
        hw_messages.HardwareControlRef(
            manager_id="room-b",
            device_id="deck",
            control_id="0,0",
            control_kind="slot",
        )
    )


@pytest.mark.asyncio
async def test_command_routing_requires_reachable_hardware_manager_endpoint():
    bus = EventBus("hardware_messages")
    command_service = HardwareCommandService(bus, controller_id="controller-main")
    command_service.register_device(
        config_id="config-room-a",
        ref=hw_messages.HardwareDeviceRef(manager_id="room-a", device_id="deck"),
    )

    with pytest.raises(LookupError, match="not reachable"):
        await command_service.wake_screen("config-room-a")

    await bus.claim_local_endpoint(hardware_manager_address("room-a"))
    async with bus.subscribe() as stream:
        await command_service.wake_screen("config-room-a")
        message = await stream.receive()

    assert message.recipient.endpoint == hardware_manager_address("room-a")
    assert hw_messages.hardware_device_ref_from_message(message) == (
        hw_messages.HardwareDeviceRef(manager_id="room-a", device_id="deck")
    )


@pytest.mark.asyncio
async def test_route_loss_cleans_only_configs_for_lost_manager_endpoint():
    bus = EventBus("hardware_messages")
    controller = ControllerService(
        driver_bus=bus,
        config_service=NullDeviceConfigService(),
        settings_service=InMemorySettingsService(),
        controller_id="controller-main",
    )
    controller.on_device_disconnected = AsyncMock()
    ref_a = hw_messages.HardwareDeviceRef(manager_id="room-a", device_id="deck")
    ref_b = hw_messages.HardwareDeviceRef(manager_id="room-b", device_id="deck")

    controller._device_registry.connect(
        config_id="config-room-a",
        ref=ref_a,
        device=_device("deck", "serial-a"),
    )
    controller._device_registry.connect(
        config_id="config-room-b",
        ref=ref_b,
        device=_device("deck", "serial-b"),
    )
    controller._command_service.register_device(config_id="config-room-a", ref=ref_a)
    controller._command_service.register_device(config_id="config-room-b", ref=ref_b)
    await bus.route_table.claim_endpoint(
        endpoint=hardware_manager_address("room-a"),
        lane="hardware_messages",
        client_id="websocket:room-a",
        client_kind="remote",
        transport_kind="websocket",
        transport_id="ws-main",
        claim_source="message_sender",
    )
    await bus.route_table.claim_endpoint(
        endpoint=hardware_manager_address("room-b"),
        lane="hardware_messages",
        client_id="websocket:room-b",
        client_kind="remote",
        transport_kind="websocket",
        transport_id="ws-main",
        claim_source="message_sender",
    )

    async with anyio.create_task_group() as tg:
        tg.start_soon(controller._route_event_loop)
        await anyio.sleep(0.01)
        await bus.route_table.client_disconnected("websocket:room-a")
        with anyio.fail_after(1):
            while controller.on_device_disconnected.await_count < 1:
                await anyio.sleep(0.01)
        tg.cancel_scope.cancel()

    controller.on_device_disconnected.assert_awaited_once_with("config-room-a")
    assert controller._device_registry.get("config-room-a") is None
    assert controller._device_registry.get("config-room-b") is not None


@pytest.mark.asyncio
async def test_device_disconnect_tears_down_without_hardware_clears():
    bus = EventBus("hardware_messages")
    controller = ControllerService(
        driver_bus=bus,
        config_service=NullDeviceConfigService(),
        settings_service=InMemorySettingsService(),
        controller_id="controller-main",
    )
    ctrl_ctx = AsyncMock()
    await controller._controller_contexts.set("config-room-a", ctrl_ctx)

    await controller.on_device_disconnected("config-room-a")

    ctrl_ctx.clear_page.assert_awaited_once_with(clear_outputs=False)
    assert await controller._controller_contexts.get("config-room-a") is None
