from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import anyio
import pytest
from conftest import LaneHarness
from deckr.contracts.messages import controller_address, hardware_manager_address
from deckr.hardware import messages as hw_messages
from deckr.hardware.descriptors import (
    CapabilityDescriptor,
    CapabilityRef,
    ControlDescriptor,
    DeviceDescriptor,
    DeviceRef,
)
from deckr.state import (
    DeviceClaim,
    EndpointPresence,
    HardwareInventory,
    HardwareInventoryDevice,
    StateUnavailable,
    device_claim_key,
    presence_endpoint_key,
)

import deckr.controller._controller_service as controller_module
from deckr.controller._controller_service import ControllerService, OwnedDeviceClaim
from deckr.controller._hardware_service import (
    DeviceRouteRegistry,
    HardwareCommandService,
    LiveDeviceRoute,
)
from deckr.controller.config import (
    DeviceConfig,
    DeviceConfigMatch,
    NullDeviceConfigService,
    Profile,
)


def _device(device_id: str, fingerprint: str) -> DeviceDescriptor:
    return DeviceDescriptor(
        deviceId=device_id,
        displayName="Test Device",
        fingerprint=fingerprint,
        controls=(
            ControlDescriptor(
                controlId="0,0",
                kind="key",
                outputCapabilities=(
                    CapabilityDescriptor(
                        capabilityId="raster.bitmap",
                        family="deckr.output.raster",
                        type="bitmap",
                        direction="output",
                        access=("settable",),
                        commandTypes=("set_frame", "clear"),
                    ),
                ),
            ),
        ),
        capabilities=(
            CapabilityDescriptor(
                capabilityId="device.power",
                family="deckr.device.power",
                type="screen",
                direction="command",
                access=("invokable",),
                commandTypes=("sleep", "wake"),
            ),
        ),
    )


@pytest.mark.asyncio
async def test_manager_local_device_ids_do_not_collide_in_registry_or_commands():
    bus = LaneHarness("hardware_messages", default_endpoint="controller:controller-main")
    command_service = HardwareCommandService(
        bus.endpoint("controller:controller-main"),
        controller_id="controller-main",
    )
    registry = DeviceRouteRegistry()
    ref_a = DeviceRef(manager_id="room-a", device_id="deck")
    ref_b = DeviceRef(manager_id="room-b", device_id="deck")

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
    command_service.register_device(
        config_id="config-room-a",
        ref=ref_a,
        device=_device("deck", "serial-a"),
    )
    command_service.register_device(
        config_id="config-room-b",
        ref=ref_b,
        device=_device("deck", "serial-b"),
    )

    async with (
        bus.subscribe(hardware_manager_address("room-a")) as stream_a,
        bus.subscribe(hardware_manager_address("room-b")) as stream_b,
    ):
        await command_service.set_raster_frame(
            "config-room-a",
            "0,0",
            "raster.bitmap",
            b"a",
        )
        await command_service.clear_raster(
            "config-room-b",
            "0,0",
            "raster.bitmap",
        )
        msg_a = await stream_a.receive()
        msg_b = await stream_b.receive()

    assert registry.get_by_ref(ref_a).config_id == "config-room-a"
    assert registry.get_by_ref(ref_b).config_id == "config-room-b"
    assert msg_a.recipient.endpoint == hardware_manager_address("room-a")
    assert msg_b.recipient.endpoint == hardware_manager_address("room-b")
    assert hw_messages.hardware_capability_ref_from_subject(msg_a.subject) == (
        CapabilityRef(
            deviceRef=DeviceRef(managerId="room-a", deviceId="deck"),
            controlId="0,0",
            capabilityId="raster.bitmap",
        )
    )
    assert hw_messages.hardware_capability_ref_from_subject(msg_b.subject) == (
        CapabilityRef(
            deviceRef=DeviceRef(managerId="room-b", deviceId="deck"),
            controlId="0,0",
            capabilityId="raster.bitmap",
        )
    )


@pytest.mark.asyncio
async def test_direct_command_drops_when_device_is_no_longer_live():
    bus = LaneHarness("hardware_messages", default_endpoint="controller:controller-main")
    command_service = HardwareCommandService(
        bus.endpoint("controller:controller-main"),
        controller_id="controller-main",
    )

    await command_service.wake_device("config-room-a")

    command_service.register_device(
        config_id="config-room-a",
        ref=DeviceRef(manager_id="room-a", device_id="deck"),
        device=_device("deck", "serial-a"),
    )

    async with bus.subscribe(hardware_manager_address("room-a")) as stream:
        await command_service.wake_device("config-room-a")
        message = await stream.receive()

    ref = hw_messages.hardware_capability_ref_from_subject(message.subject)
    assert ref == CapabilityRef(
        deviceRef=DeviceRef(managerId="room-a", deviceId="deck"),
        capabilityId="device.power",
    )
    body = hw_messages.hardware_body_from_message(message)
    assert isinstance(body, hw_messages.ControlCommandMessage)
    assert body.command_type == "wake"


@pytest.mark.asyncio
async def test_manager_presence_loss_cleans_only_configs_for_lost_manager_endpoint():
    bus = LaneHarness("hardware_messages", default_endpoint="controller:controller-main")
    controller = ControllerService(
        hardware_endpoint=bus.endpoint("controller:controller-main"),
        state=bus.deckr.state(),
        config_service=NullDeviceConfigService(),
        settings_service=None,
        controller_id="controller-main",
    )
    controller.on_device_disconnected = AsyncMock()
    ref_a = DeviceRef(manager_id="room-a", device_id="deck")
    ref_b = DeviceRef(manager_id="room-b", device_id="deck")

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
    controller._command_service.register_device(config_id="config-room-a", ref=ref_a, device=_device("deck", "serial-a"))
    controller._command_service.register_device(config_id="config-room-b", ref=ref_b, device=_device("deck", "serial-b"))
    await _put_presence(bus, "room-a")
    await _put_presence(bus, "room-b")
    await _put_inventory(bus, "room-a", fingerprint="serial-a")
    await _put_inventory(bus, "room-b", fingerprint="serial-b")
    claim_a = await _put_claim(bus, controller, "room-a", "deck")
    claim_b = await _put_claim(bus, controller, "room-b", "deck")
    controller._owned_claims[device_claim_key(manager_id="room-a", device_id="deck")] = (
        OwnedDeviceClaim(
            key=device_claim_key(manager_id="room-a", device_id="deck"),
            config_id="config-room-a",
            ref=ref_a,
            revision=claim_a.revision,
        )
    )
    controller._owned_claims[device_claim_key(manager_id="room-b", device_id="deck")] = (
        OwnedDeviceClaim(
            key=device_claim_key(manager_id="room-b", device_id="deck"),
            config_id="config-room-b",
            ref=ref_b,
            revision=claim_b.revision,
        )
    )

    async with anyio.create_task_group() as tg:
        tg.start_soon(controller._manager_presence_loop)
        await anyio.sleep(0.01)
        await bus.deckr.state().delete(
            presence_endpoint_key(
                lane="hardware_messages",
                endpoint=hardware_manager_address("room-a"),
            )
        )
        with anyio.fail_after(1):
            while controller.on_device_disconnected.await_count < 1:
                await anyio.sleep(0.01)
        tg.cancel_scope.cancel()

    controller.on_device_disconnected.assert_awaited_once_with("config-room-a")
    assert controller._device_registry.get("config-room-a") is None
    assert controller._device_registry.get("config-room-b") is not None


@pytest.mark.asyncio
async def test_manager_presence_session_change_invalidates_owned_device():
    bus = LaneHarness("hardware_messages", default_endpoint="controller:controller-main")
    controller = ControllerService(
        hardware_endpoint=bus.endpoint("controller:controller-main"),
        state=bus.deckr.state(),
        config_service=NullDeviceConfigService(),
        settings_service=None,
        controller_id="controller-main",
    )
    controller.on_device_disconnected = AsyncMock()
    ref = DeviceRef(manager_id="room-a", device_id="deck")
    claim_key = device_claim_key(manager_id="room-a", device_id="deck")
    controller._device_registry.connect(
        config_id="config-room-a",
        ref=ref,
        device=_device("deck", "serial-a"),
    )
    controller._command_service.register_device(config_id="config-room-a", ref=ref, device=_device("deck", "serial-a"))
    claim_entry = await bus.deckr.state().create(
        claim_key,
        DeviceClaim(
            claimedByEndpoint=controller_address("controller-main"),
            claimedBySessionId=controller._session_id,
            timestamp=datetime.now(UTC),
            ttlSeconds=15,
        ),
    )
    controller._owned_claims[claim_key] = OwnedDeviceClaim(
        key=claim_key,
        config_id="config-room-a",
        ref=ref,
        revision=claim_entry.revision,
    )
    await _put_presence(bus, "room-a", session_id="old-manager-session")

    async with anyio.create_task_group() as tg:
        tg.start_soon(controller._manager_presence_loop)
        await anyio.sleep(0.01)
        await _put_presence(bus, "room-a", session_id="new-manager-session")
        with anyio.fail_after(1):
            while controller.on_device_disconnected.await_count < 1:
                await anyio.sleep(0.01)
        tg.cancel_scope.cancel()

    controller.on_device_disconnected.assert_awaited_once_with("config-room-a")
    assert controller._device_registry.get("config-room-a") is None
    assert await bus.deckr.state().get(claim_key) is None


@pytest.mark.asyncio
async def test_device_disconnect_tears_down_without_hardware_clears():
    bus = LaneHarness("hardware_messages", default_endpoint="controller:controller-main")
    controller = ControllerService(
        hardware_endpoint=bus.endpoint("controller:controller-main"),
        state=bus.deckr.state(),
        config_service=NullDeviceConfigService(),
        settings_service=None,
        controller_id="controller-main",
    )
    ctrl_ctx = AsyncMock()
    await controller._controller_contexts.set("config-room-a", ctrl_ctx)

    await controller.on_device_disconnected("config-room-a")

    ctrl_ctx.clear_page.assert_awaited_once_with(clear_outputs=False)
    assert await controller._controller_contexts.get("config-room-a") is None


@pytest.mark.asyncio
async def test_device_reconnect_replaces_existing_context_without_hardware_clears():
    bus = LaneHarness("hardware_messages", default_endpoint="controller:controller-main")
    controller = ControllerService(
        hardware_endpoint=bus.endpoint("controller:controller-main"),
        state=bus.deckr.state(),
        config_service=NullDeviceConfigService(),
        settings_service=None,
        controller_id="controller-main",
    )
    controller._start_soon = lambda fn, *args: None
    ctrl_ctx = AsyncMock()
    await controller._controller_contexts.set("config-room-a", ctrl_ctx)
    live = LiveDeviceRoute(
        config_id="config-room-a",
        ref=DeviceRef(manager_id="room-a", device_id="deck"),
        device=_device("deck", "serial-a"),
    )

    await controller.on_device_connected(live, initial_config=None)

    ctrl_ctx.clear_page.assert_awaited_once_with(clear_outputs=False)
    assert await controller._controller_contexts.get("config-room-a") is None


@pytest.mark.asyncio
async def test_device_lifecycle_renders_once_before_listening_for_config_changes(
    monkeypatch,
):
    bus = LaneHarness("hardware_messages", default_endpoint="controller:controller-main")
    config = DeviceConfig(
        id="config-room-a",
        name="Room A",
        match=DeviceConfigMatch(fingerprint="serial-a", manager_id="room-a"),
        profiles=[Profile(name="default", pages=[])],
    )
    controller = ControllerService(
        hardware_endpoint=bus.endpoint("controller:controller-main"),
        state=bus.deckr.state(),
        config_service=_MatchingConfigService(config),
        settings_service=None,
        controller_id="controller-main",
    )
    live = controller._device_registry.connect(
        config_id="config-room-a",
        ref=DeviceRef(manager_id="room-a", device_id="deck"),
        device=_device("deck", "serial-a"),
    )
    managers = []

    class FakeDeviceManager:
        def __init__(self, **kwargs):
            self.config_stream = kwargs["config_stream"]
            self.set_page_count = 0
            self.config_change_count = 0
            self.listener_started = anyio.Event()
            managers.append(self)

        async def set_page(self):
            self.set_page_count += 1

        async def _config_listener(self):
            self.listener_started.set()
            async for _config in self.config_stream:
                self.config_change_count += 1

        async def clear_page(self, *, clear_outputs: bool = True):
            pass

    monkeypatch.setattr(
        "deckr.controller._controller_service.DeviceManager",
        FakeDeviceManager,
    )

    async with anyio.create_task_group() as tg:
        tg.start_soon(controller._device_lifecycle, live, config)
        with anyio.fail_after(1):
            while not managers:
                await anyio.sleep(0.01)
            while not managers[0].listener_started.is_set():
                await anyio.sleep(0.01)
        await anyio.sleep(0.05)
        assert managers[0].set_page_count == 1
        assert managers[0].config_change_count == 0
        await controller.on_device_disconnected("config-room-a")
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_claim_loss_retries_cached_matching_inventory():
    bus = LaneHarness("hardware_messages", default_endpoint="controller:controller-main")
    config = DeviceConfig(
        id="config-room-a",
        name="Room A",
        match=DeviceConfigMatch(fingerprint="serial-a", manager_id="room-a"),
        profiles=[Profile(name="default", pages=[])],
    )
    controller = ControllerService(
        hardware_endpoint=bus.endpoint("controller:controller-main"),
        state=bus.deckr.state(),
        config_service=_MatchingConfigService(config),
        settings_service=None,
        controller_id="controller-main",
    )
    controller.on_device_connected = AsyncMock()
    claim_key = device_claim_key(manager_id="room-a", device_id="deck")
    await bus.deckr.state().create(
        claim_key,
        DeviceClaim(
            claimedByEndpoint=controller_address("other"),
            claimedBySessionId="other-session",
            timestamp=datetime.now(UTC),
            ttlSeconds=15,
        ),
    )
    await _put_presence(bus, "room-a", session_id="manager-session")
    await _put_inventory(
        bus,
        "room-a",
        fingerprint="serial-a",
        session_id="manager-session",
    )

    await controller._reconcile_hardware_current_state(reason="test snapshot")
    assert controller._device_registry.get("config-room-a") is None

    await bus.deckr.state().delete(claim_key)
    await controller._reconcile_hardware_current_state(reason="test snapshot")

    claim = await bus.deckr.state().get(claim_key)
    assert claim is not None
    assert claim.value["claimedByEndpoint"] == "controller:controller-main"
    assert controller._device_registry.get("config-room-a") is not None


@pytest.mark.asyncio
async def test_same_endpoint_old_session_claim_blocks_until_claim_loss():
    bus = LaneHarness("hardware_messages", default_endpoint="controller:controller-main")
    config = DeviceConfig(
        id="config-room-a",
        name="Room A",
        match=DeviceConfigMatch(fingerprint="serial-a", manager_id="room-a"),
        profiles=[Profile(name="default", pages=[])],
    )
    controller = ControllerService(
        hardware_endpoint=bus.endpoint("controller:controller-main"),
        state=bus.deckr.state(),
        config_service=_MatchingConfigService(config),
        settings_service=None,
        controller_id="controller-main",
    )
    controller.on_device_connected = AsyncMock()
    claim_key = device_claim_key(manager_id="room-a", device_id="deck")
    await bus.deckr.state().create(
        claim_key,
        DeviceClaim(
            claimedByEndpoint=controller_address("controller-main"),
            claimedBySessionId="old-session",
            timestamp=datetime.now(UTC),
            ttlSeconds=15,
        ),
    )
    await _put_presence(bus, "room-a", session_id="manager-session")
    await _put_inventory(
        bus,
        "room-a",
        fingerprint="serial-a",
        session_id="manager-session",
    )

    await controller._reconcile_hardware_current_state(reason="test snapshot")

    assert controller._device_registry.get("config-room-a") is None
    assert controller._owned_claims == {}
    assert claim_key in controller._blocked_claim_revisions

    await bus.deckr.state().delete(claim_key)
    await controller._reconcile_hardware_current_state(reason="test snapshot")

    claim = await bus.deckr.state().get(claim_key)
    assert claim is not None
    assert claim.value["claimedByEndpoint"] == "controller:controller-main"
    assert claim.value["claimedBySessionId"] == controller._session_id
    assert controller._device_registry.get("config-room-a") is not None


@pytest.mark.asyncio
async def test_broker_snapshot_inventory_removal_revokes_live_device():
    bus = LaneHarness("hardware_messages", default_endpoint="controller:controller-main")
    controller = ControllerService(
        hardware_endpoint=bus.endpoint("controller:controller-main"),
        state=bus.deckr.state(),
        config_service=NullDeviceConfigService(),
        settings_service=None,
        controller_id="controller-main",
    )
    controller.on_device_disconnected = AsyncMock()
    ref = DeviceRef(manager_id="room-a", device_id="deck")
    controller._device_registry.connect(
        config_id="config-room-a",
        ref=ref,
        device=_device("deck", "serial-a"),
    )
    controller._command_service.register_device(config_id="config-room-a", ref=ref, device=_device("deck", "serial-a"))
    claim_key = device_claim_key(manager_id="room-a", device_id="deck")
    claim_entry = await _put_claim(bus, controller, "room-a", "deck")
    controller._owned_claims[claim_key] = OwnedDeviceClaim(
        key=claim_key,
        config_id="config-room-a",
        ref=ref,
        revision=claim_entry.revision,
    )
    await _put_presence(bus, "room-a", session_id="manager-session")
    await _put_inventory(
        bus,
        "room-a",
        fingerprint="serial-a",
        session_id="manager-session",
    )
    await controller._reconcile_hardware_current_state(reason="test snapshot")

    await bus.deckr.state().put(
        "inventory.hardware.room-a",
        HardwareInventory(
            managerId="room-a",
            managerEndpoint=hardware_manager_address("room-a"),
            sessionId="manager-session",
            timestamp=datetime.now(UTC),
            ttlSeconds=15,
            devices={},
        ),
    )
    await controller._reconcile_hardware_current_state(reason="test snapshot")

    controller.on_device_disconnected.assert_awaited_once_with("config-room-a")
    assert controller._device_registry.get("config-room-a") is None
    assert await bus.deckr.state().get(claim_key) is None


@pytest.mark.asyncio
async def test_broker_snapshot_claim_takeover_revokes_owned_device():
    bus = LaneHarness("hardware_messages", default_endpoint="controller:controller-main")
    controller = ControllerService(
        hardware_endpoint=bus.endpoint("controller:controller-main"),
        state=bus.deckr.state(),
        config_service=NullDeviceConfigService(),
        settings_service=None,
        controller_id="controller-main",
    )
    controller.on_device_disconnected = AsyncMock()
    ref = DeviceRef(manager_id="room-a", device_id="deck")
    controller._device_registry.connect(
        config_id="config-room-a",
        ref=ref,
        device=_device("deck", "serial-a"),
    )
    controller._command_service.register_device(config_id="config-room-a", ref=ref, device=_device("deck", "serial-a"))
    claim_key = device_claim_key(manager_id="room-a", device_id="deck")
    claim_entry = await _put_claim(bus, controller, "room-a", "deck")
    controller._owned_claims[claim_key] = OwnedDeviceClaim(
        key=claim_key,
        config_id="config-room-a",
        ref=ref,
        revision=claim_entry.revision,
    )
    await _put_presence(bus, "room-a", session_id="manager-session")
    await _put_inventory(
        bus,
        "room-a",
        fingerprint="serial-a",
        session_id="manager-session",
    )
    await bus.deckr.state().put(
        claim_key,
        DeviceClaim(
            claimedByEndpoint=controller_address("other"),
            claimedBySessionId="other-session",
            timestamp=datetime.now(UTC),
            ttlSeconds=15,
        ),
    )

    await controller._reconcile_hardware_current_state(reason="test snapshot")

    controller.on_device_disconnected.assert_awaited_once_with("config-room-a")
    assert controller._device_registry.get("config-room-a") is None
    assert claim_key not in controller._owned_claims
    claim = await bus.deckr.state().get(claim_key)
    assert claim is not None
    assert claim.value["claimedByEndpoint"] == "controller:other"


@pytest.mark.asyncio
async def test_hardware_snapshot_unavailable_keeps_live_device(monkeypatch):
    bus = LaneHarness("hardware_messages", default_endpoint="controller:controller-main")
    controller = ControllerService(
        hardware_endpoint=bus.endpoint("controller:controller-main"),
        state=bus.deckr.state(),
        config_service=NullDeviceConfigService(),
        settings_service=None,
        controller_id="controller-main",
    )
    ref = DeviceRef(manager_id="room-a", device_id="deck")
    controller._device_registry.connect(
        config_id="config-room-a",
        ref=ref,
        device=_device("deck", "serial-a"),
    )

    async def unavailable_items(*args, **kwargs):
        del args, kwargs
        raise StateUnavailable("nats down")

    monkeypatch.setattr(controller._state, "items", unavailable_items)

    with pytest.raises(StateUnavailable):
        await controller._reconcile_hardware_current_state(reason="test outage")

    assert controller._device_registry.get("config-room-a") is not None


@pytest.mark.asyncio
async def test_stop_releases_owned_claims_without_hardware_clears():
    bus = LaneHarness("hardware_messages", default_endpoint="controller:controller-main")
    controller = ControllerService(
        hardware_endpoint=bus.endpoint("controller:controller-main"),
        state=bus.deckr.state(),
        config_service=NullDeviceConfigService(),
        settings_service=None,
        controller_id="controller-main",
    )
    ctrl_ctx = AsyncMock()
    await controller._controller_contexts.set("config-room-a", ctrl_ctx)
    ref = DeviceRef(manager_id="room-a", device_id="deck")
    controller._device_registry.connect(
        config_id="config-room-a",
        ref=ref,
        device=_device("deck", "serial-a"),
    )
    controller._command_service.register_device(config_id="config-room-a", ref=ref, device=_device("deck", "serial-a"))
    claim_key = device_claim_key(manager_id="room-a", device_id="deck")
    entry = await bus.deckr.state().create(
        claim_key,
        DeviceClaim(
            claimedByEndpoint=controller_address("controller-main"),
            claimedBySessionId=controller._session_id,
            timestamp=datetime.now(UTC),
            ttlSeconds=15,
        ),
    )
    controller._owned_claims[claim_key] = OwnedDeviceClaim(
        key=claim_key,
        config_id="config-room-a",
        ref=ref,
        revision=entry.revision,
    )

    await controller.stop()

    assert await bus.deckr.state().get(claim_key) is None
    ctrl_ctx.clear_page.assert_awaited_once_with(clear_outputs=False)
    assert controller._device_registry.get("config-room-a") is None
    assert controller._command_service._devices_by_config_id == {}


@pytest.mark.asyncio
async def test_claim_refresh_unavailable_keeps_live_device(monkeypatch):
    bus = LaneHarness("hardware_messages", default_endpoint="controller:controller-main")
    controller = ControllerService(
        hardware_endpoint=bus.endpoint("controller:controller-main"),
        state=bus.deckr.state(),
        config_service=NullDeviceConfigService(),
        settings_service=None,
        controller_id="controller-main",
    )
    ref = DeviceRef(manager_id="room-a", device_id="deck")
    controller._device_registry.connect(
        config_id="config-room-a",
        ref=ref,
        device=_device("deck", "serial-a"),
    )
    controller._command_service.register_device(config_id="config-room-a", ref=ref, device=_device("deck", "serial-a"))
    claim_key = device_claim_key(manager_id="room-a", device_id="deck")
    controller._owned_claims[claim_key] = OwnedDeviceClaim(
        key=claim_key,
        config_id="config-room-a",
        ref=ref,
        revision=1,
    )
    controller._stopping = anyio.Event()

    async def unavailable_update(*args, **kwargs):
        del args, kwargs
        controller._stopping.set()
        raise StateUnavailable("nats down")

    monkeypatch.setattr(controller_module, "CLAIM_HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(controller._state, "update", unavailable_update)

    await controller._claim_refresh_loop()

    assert controller._device_registry.get("config-room-a") is not None
    assert controller._command_service._devices_by_config_id["config-room-a"].ref == ref
    assert claim_key in controller._owned_claims


async def _put_presence(
    bus: LaneHarness,
    manager_id: str,
    *,
    session_id: str | None = None,
) -> None:
    endpoint = hardware_manager_address(manager_id)
    await bus.deckr.state().put(
        presence_endpoint_key(lane="hardware_messages", endpoint=endpoint),
        EndpointPresence(
            endpoint=endpoint,
            lane="hardware_messages",
            sessionId=session_id or f"session-{manager_id}",
            timestamp=datetime.now(UTC),
            ttlSeconds=15,
            metadata={},
        ),
    )


async def _put_inventory(
    bus: LaneHarness,
    manager_id: str,
    *,
    fingerprint: str,
    session_id: str | None = None,
) -> None:
    await bus.deckr.state().put(
        f"inventory.hardware.{manager_id}",
        HardwareInventory(
            managerId=manager_id,
            managerEndpoint=hardware_manager_address(manager_id),
            sessionId=session_id or f"session-{manager_id}",
            timestamp=datetime.now(UTC),
            ttlSeconds=15,
            devices={
                "deck": HardwareInventoryDevice(
                    deviceRef=DeviceRef(
                        managerId=manager_id,
                        deviceId="deck",
                        fingerprint=fingerprint,
                    ),
                    descriptor=_device("deck", fingerprint),
                )
            },
        ),
    )


async def _put_claim(
    bus: LaneHarness,
    controller: ControllerService,
    manager_id: str,
    device_id: str,
):
    return await bus.deckr.state().create(
        device_claim_key(manager_id=manager_id, device_id=device_id),
        DeviceClaim(
            claimedByEndpoint=controller_address("controller-main"),
            claimedBySessionId=controller._session_id,
            timestamp=datetime.now(UTC),
            ttlSeconds=15,
        ),
    )


class _MatchingConfigService:
    def __init__(self, config: DeviceConfig) -> None:
        self.config = config

    async def match_device(self, *, fingerprint: str, manager_id: str):
        if (
            fingerprint == self.config.match.fingerprint
            and manager_id == self.config.match.manager_id
        ):
            return self.config
        return None

    def subscribe(self, config_id: str):
        del config_id
        return self._stream()

    async def _stream(self):
        yield self.config
        await anyio.sleep_forever()
