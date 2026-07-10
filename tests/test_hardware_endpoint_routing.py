from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import anyio
import pytest
from conftest import LaneHarness
from deckr.beacon import (
    BEACON_ADVERTISEMENT_STORE_POLICY,
    Beacon,
    BeaconAdvertisementSpec,
)
from deckr.components import RunContext
from deckr.concord import (
    CONCORD_CONTRACT_BUCKET_POLICY,
    CONCORD_TOKEN_BUCKET_POLICY,
    Concord,
    ContractValidityStatus,
)
from deckr.contracts.authority import ContractPointer
from deckr.contracts.messages import (
    controller_address,
    hardware_manager_address,
)
from deckr.hardware import messages as hw_messages
from deckr.hardware.descriptors import (
    CapabilityDescriptor,
    CapabilityRef,
    ControlDescriptor,
    DeviceDescriptor,
    DeviceRef,
)
from deckr.hardware.profiles import HARDWARE_FEATURE_ID, HardwareBeaconPayload
from deckr.testing import ConcordRuntimeHarness

from deckr.controller._controller_service import (
    ControllerService,
)
from deckr.controller._device_manager import DeviceManager
from deckr.controller._hardware import HardwareCommandService
from deckr.controller._hardware._routes import DeviceRouteRegistry
from deckr.controller.config import DeviceConfig, DeviceConfigMatch, Page, Profile

CONTROLLER_ID = "controller-main"
CONTRACT = ContractPointer(contractId="hardware-contract-1", generation=1)


def _device(device_id: str = "deck", fingerprint: str = "serial-a") -> DeviceDescriptor:
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
                        family="dev.deckr.output.raster",
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
                family="dev.deckr.device.power",
                type="screen",
                direction="command",
                access=("invokable",),
                commandTypes=("sleep", "wake"),
            ),
        ),
    )


class MemoryConfigService:
    def __init__(self, *configs: DeviceConfig) -> None:
        self._configs = {config.id: config for config in configs}
        self._subscribers: dict[
            str,
            set[anyio.abc.ObjectSendStream[DeviceConfig | None]],
        ] = {}

    async def match_device(
        self,
        *,
        fingerprint: str,
        labels: Mapping[str, str],
    ) -> DeviceConfig | None:
        matches = [
            config
            for config in self._configs.values()
            if config.enabled
            and config.match.fingerprint == fingerprint
            and all(
                labels.get(key) == value for key, value in config.match.labels.items()
            )
        ]
        if len(matches) > 1:
            raise ValueError("ambiguous config")
        return matches[0] if matches else None

    async def get_config(self, config_id: str) -> DeviceConfig | None:
        return self._configs.get(config_id)

    async def write_config(self, config: DeviceConfig) -> DeviceConfig:
        self._configs[config.id] = config
        await self._notify(config.id, config)
        return config

    async def remove_config(self, config_id: str) -> None:
        self._configs.pop(config_id, None)
        await self._notify(config_id, None)

    def subscribe(self, config_id: str) -> AsyncIterator[DeviceConfig | None]:
        return self._subscribe(config_id)

    async def _subscribe(self, config_id: str) -> AsyncIterator[DeviceConfig | None]:
        send, receive = anyio.create_memory_object_stream[DeviceConfig | None](10)
        self._subscribers.setdefault(config_id, set()).add(send)
        try:
            yield self._configs.get(config_id)
            async with receive:
                async for config in receive:
                    yield config
        finally:
            self._subscribers.get(config_id, set()).discard(send)
            await send.aclose()

    async def _notify(
        self,
        config_id: str,
        config: DeviceConfig | None,
    ) -> None:
        for subscriber in tuple(self._subscribers.get(config_id, set())):
            await subscriber.send(config)


def _config(
    *,
    config_id: str = "config-room-a",
    fingerprint: str = "serial-a",
    labels: dict[str, str] | None = None,
) -> DeviceConfig:
    return DeviceConfig(
        id=config_id,
        name="Test Device",
        match=DeviceConfigMatch(fingerprint=fingerprint, labels=labels or {}),
        profiles=[Profile(name="default", pages=[Page(controls=[])])],
    )


def _beacon(bus: LaneHarness) -> Beacon:
    return Beacon(bus.substrate.kv_bucket(BEACON_ADVERTISEMENT_STORE_POLICY))


def _concord(bus: LaneHarness) -> Concord:
    return ConcordRuntimeHarness(
        contract_store=bus.substrate.kv_bucket(CONCORD_CONTRACT_BUCKET_POLICY),
        token_store=bus.substrate.kv_bucket(CONCORD_TOKEN_BUCKET_POLICY),
    ).concord


def _claims(controller: ControllerService):
    return controller._hardware.snapshot().owned_claims  # noqa: SLF001


def _claim(controller: ControllerService):
    claims = _claims(controller)
    assert len(claims) == 1
    return claims[0]


def _route(controller: ControllerService, config_id: str):
    return controller._hardware.route_for_config(config_id)  # noqa: SLF001


def _routes(controller: ControllerService):
    return controller._hardware.snapshot().live_routes  # noqa: SLF001


async def _reconcile(controller: ControllerService, reason: str) -> None:
    await controller._hardware.reconcile(reason=reason)  # noqa: SLF001


async def _advertise_hardware(
    beacon: Beacon,
    *,
    manager_id: str = "room-a",
    session_id: str = "manager-session",
    advertisement_id: str = "hardware-ad-1",
    device: DeviceDescriptor | None = None,
    labels: dict[str, str] | None = None,
):
    descriptor = device or _device()
    ref = DeviceRef(managerId=manager_id, deviceId=descriptor.device_id)
    payload = HardwareBeaconPayload(
        managerId=manager_id,
        managerEndpoint=hardware_manager_address(manager_id),
        sessionId=session_id,
        labels=labels or {},
        devices={
            descriptor.device_id: {
                "deviceRef": ref.model_dump(by_alias=True, exclude_none=True),
                "descriptor": descriptor.model_dump(
                    by_alias=True,
                    exclude_none=True,
                    mode="json",
                ),
            }
        },
    )
    return await beacon.advertise(
        BeaconAdvertisementSpec(
            feature_id=HARDWARE_FEATURE_ID,
            endpoint=hardware_manager_address(manager_id),
            session_id=session_id,
            advertisement_id=advertisement_id,
            payload=payload.to_dict(),
            labels=payload.labels,
        )
    )


@asynccontextmanager
async def _running_controller(
    *,
    config_service: MemoryConfigService,
):
    hardware_bus = LaneHarness(
        "hardware_messages",
        default_endpoint=controller_address(CONTROLLER_ID),
    )
    beacon = _beacon(hardware_bus)
    concord = _concord(hardware_bus)
    registry = MagicMock()
    registry.get_action = AsyncMock(return_value=None)
    controller = ControllerService(
        endpoint=hardware_bus.endpoint(controller_address(CONTROLLER_ID)).session,
        beacon=beacon,
        concord=concord,
        config_service=config_service,
        settings_service=MagicMock(),
        controller_id=CONTROLLER_ID,
        action_registry=registry,
    )
    async with anyio.create_task_group() as tg:
        beacon.start(tg)
        concord.start(tg)
        await controller.start(RunContext(tg=tg, stopping=anyio.Event()))
        try:
            yield controller, beacon, concord
        finally:
            await controller.stop()
            tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_controller_service_background_loops_exit_when_stopping_is_set():
    hardware_bus = LaneHarness(
        "hardware_messages",
        default_endpoint=controller_address(CONTROLLER_ID),
    )
    beacon = _beacon(hardware_bus)
    concord = _concord(hardware_bus)
    registry = MagicMock()
    registry.get_action = AsyncMock(return_value=None)
    render_backend = MagicMock()
    render_backend.aclose = AsyncMock()
    controller = ControllerService(
        endpoint=hardware_bus.endpoint(controller_address(CONTROLLER_ID)).session,
        beacon=beacon,
        concord=concord,
        config_service=MemoryConfigService(_config()),
        settings_service=MagicMock(),
        controller_id=CONTROLLER_ID,
        action_registry=registry,
        render_backend=render_backend,
    )
    stopping = anyio.Event()

    with anyio.fail_after(1):
        async with anyio.create_task_group() as tg:
            await controller.start(RunContext(tg=tg, stopping=stopping))
            await anyio.sleep(0.05)
            stopping.set()

    await controller.stop()
    render_backend.aclose.assert_awaited_once()


@pytest.mark.asyncio
async def test_manager_local_device_ids_do_not_collide_in_registry_or_commands():
    bus = LaneHarness(
        "hardware_messages", default_endpoint="controller:controller-main"
    )
    registry = DeviceRouteRegistry()
    command_service = HardwareCommandService(
        bus.endpoint("controller:controller-main").session,
        route_lookup=registry.get,
    )
    ref_a = DeviceRef(manager_id="room-a", device_id="deck")
    ref_b = DeviceRef(manager_id="room-b", device_id="deck")

    registry.connect(
        config_id="config-room-a",
        ref=ref_a,
        device=_device("deck", "a"),
        contract=CONTRACT,
    )
    registry.connect(
        config_id="config-room-b",
        ref=ref_b,
        device=_device("deck", "b"),
        contract=ContractPointer(contractId="hardware-contract-2", generation=1),
    )
    async with (
        bus.subscribe(hardware_manager_address("room-a")) as stream_a,
        bus.subscribe(hardware_manager_address("room-b")) as stream_b,
    ):
        await command_service.set_raster_frame(
            "config-room-a", "0,0", "raster.bitmap", b"a"
        )
        await command_service.clear_raster("config-room-b", "0,0", "raster.bitmap")
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
async def test_hardware_claim_uses_newest_duplicate_device_beacon_advertisement(
    caplog,
):
    hardware_bus = LaneHarness(
        "hardware_messages",
        default_endpoint=controller_address(CONTROLLER_ID),
    )
    beacon = _beacon(hardware_bus)
    concord = _concord(hardware_bus)
    registry = MagicMock()
    registry.get_action = AsyncMock(return_value=None)
    controller = ControllerService(
        endpoint=hardware_bus.endpoint(controller_address(CONTROLLER_ID)).session,
        beacon=beacon,
        concord=concord,
        config_service=MemoryConfigService(_config()),
        settings_service=MagicMock(),
        controller_id=CONTROLLER_ID,
        action_registry=registry,
    )
    caplog.set_level("INFO", logger="deckr.controller._hardware._discovery")

    async with anyio.create_task_group() as tg:
        beacon.start(tg)
        concord.start(tg)
        await controller._hardware.start(tg, anyio.Event())  # noqa: SLF001
        await _advertise_hardware(
            beacon,
            manager_id="room-a",
            session_id="live-session",
            advertisement_id="hardware_manager_test",
        )
        await _advertise_hardware(
            beacon,
            manager_id="room-a",
            session_id="live-session",
            advertisement_id="hardware_manager_mirabox-rust-001",
        )
        await controller._hardware.wait_current()  # noqa: SLF001
        await _reconcile(controller, "test duplicate beacon")
        tg.cancel_scope.cancel()

    assert len(_claims(controller)) == 1
    owned = _claim(controller)
    assert owned.current_sessions[str(hardware_manager_address("room-a"))] == (
        "live-session"
    )
    assert "Multiple hardware Beacon advertisements describe device room-a/deck" in (
        caplog.text
    )
    assert "hardware_manager_mirabox-rust-001" in caplog.text
    assert "hardware_manager_test" in caplog.text

@pytest.mark.asyncio
async def test_pending_hardware_claim_survives_missing_beacon_candidate():
    async with _running_controller(config_service=MemoryConfigService(_config())) as (
        controller,
        beacon,
        concord,
    ):
        handle = await _advertise_hardware(beacon, session_id="manager-session")

        with anyio.fail_after(1):
            while not _claims(controller):
                await anyio.sleep(0.01)
        owned = _claim(controller)

        await handle.aclose()
        await beacon.wait_current()
        await _reconcile(controller, "test missing beacon")

        assert _claim(controller).claim_id == owned.claim_id
        assert (await concord.validate(owned.contract)).status == (
            ContractValidityStatus.NOT_YET_FULFILLED
        )
        assert _routes(controller) == ()


@pytest.mark.asyncio
async def test_pending_hardware_claim_recovers_when_manager_attaches_later():
    async with _running_controller(config_service=MemoryConfigService(_config())) as (
        controller,
        beacon,
        concord,
    ):
        await _advertise_hardware(beacon, session_id="manager-session")

        with anyio.fail_after(1):
            while not _claims(controller):
                await anyio.sleep(0.01)
        owned = _claim(controller)

        await anyio.sleep(0.06)
        await _reconcile(controller, "test still pending")

        assert _claim(controller).claim_id == owned.claim_id
        assert _routes(controller) == ()

        await concord.attach(
            owned.contract,
            participant=hardware_manager_address("room-a"),
            session_id="manager-session",
        )
        await _reconcile(controller, "test late attach")

        live = _route(controller, "config-room-a")
        assert live is not None
        assert live.ref == DeviceRef(managerId="room-a", deviceId="deck")
        assert _claim(controller).claim_id == owned.claim_id


@pytest.mark.asyncio
async def test_removed_config_rematches_claimed_hardware_to_new_matching_config():
    config_service = MemoryConfigService(_config())
    async with _running_controller(config_service=config_service) as (
        controller,
        beacon,
        concord,
    ):
        await _advertise_hardware(beacon)

        with anyio.fail_after(1):
            while not _claims(controller):
                await anyio.sleep(0.01)
        owned = _claim(controller)
        original_claim_id = owned.claim_id
        await concord.attach(
            owned.contract,
            participant=hardware_manager_address("room-a"),
            session_id="manager-session",
        )
        await _reconcile(controller, "test manager token")

        with anyio.fail_after(1):
            while _route(controller, "config-room-a") is None:
                await anyio.sleep(0.01)
        old_manager = None
        with anyio.fail_after(1):
            while old_manager is None:
                old_manager = await controller._controller_contexts.get("config-room-a")
                await anyio.sleep(0.01)

        await config_service.remove_config("config-room-a")
        with anyio.fail_after(1):
            while old_manager.config_active:
                await anyio.sleep(0.01)

        await config_service.write_config(_config(config_id="config-room-b"))
        await _reconcile(controller, "test rematch")

        with anyio.fail_after(1):
            while _route(controller, "config-room-b") is None:
                await anyio.sleep(0.01)

        current_owned = _claim(controller)
        assert current_owned.claim_id == original_claim_id
        assert current_owned.config_id == "config-room-b"
        assert _route(controller, "config-room-a") is None
        assert _route(controller, "config-room-b") is not None
        assert await controller._controller_contexts.get("config-room-a") is None
        assert await controller._controller_contexts.get("config-room-b") is not None
        assert (await concord.validate(current_owned.contract)).status == (
            ContractValidityStatus.VALID
        )


@pytest.mark.asyncio
async def test_device_manager_background_work_is_scoped_to_device_lifecycle(
    monkeypatch,
):
    original_start = DeviceManager.start
    background_started = anyio.Event()
    background_stopped = anyio.Event()

    async def start_with_sentinel(self, tg, stopping) -> None:
        await original_start(self, tg, stopping)

        async def sentinel() -> None:
            background_started.set()
            try:
                await anyio.sleep_forever()
            finally:
                background_stopped.set()

        tg.start_soon(sentinel)

    monkeypatch.setattr(DeviceManager, "start", start_with_sentinel)
    async with _running_controller(config_service=MemoryConfigService(_config())) as (
        controller,
        beacon,
        concord,
    ):
        await _advertise_hardware(beacon)

        with anyio.fail_after(1):
            while not _claims(controller):
                await anyio.sleep(0.01)
        owned = _claim(controller)
        await concord.attach(
            owned.contract,
            participant=hardware_manager_address("room-a"),
            session_id="manager-session",
        )
        await _reconcile(controller, "test manager token")

        with anyio.fail_after(1):
            await background_started.wait()

        live = _route(controller, "config-room-a")
        assert live is not None
        await controller._hardware.disconnect_config(  # noqa: SLF001
            live.config_id,
            release_claim=False,
            reason="test device disconnect",
        )

        with anyio.fail_after(1):
            await background_stopped.wait()

        assert _route(controller, "config-room-a") is None


@pytest.mark.asyncio
async def test_live_hardware_claim_ignores_advertisement_id_change():
    async with _running_controller(config_service=MemoryConfigService(_config())) as (
        controller,
        beacon,
        concord,
    ):
        handle = await _advertise_hardware(beacon, advertisement_id="hardware-ad-1")

        with anyio.fail_after(1):
            while not _claims(controller):
                await anyio.sleep(0.01)
        owned = _claim(controller)
        await concord.attach(
            owned.contract,
            participant=hardware_manager_address("room-a"),
            session_id="manager-session",
        )
        await _reconcile(controller, "test manager token")
        with anyio.fail_after(1):
            while _route(controller, "config-room-a") is None:
                await anyio.sleep(0.01)

        await handle.aclose()
        await _advertise_hardware(beacon, advertisement_id="hardware-ad-2")
        await _reconcile(controller, "test advertisement id change")

        live = _route(controller, "config-room-a")
        assert live is not None
        current_owned = _claim(controller)
        assert current_owned.claim_id == owned.claim_id


@pytest.mark.asyncio
async def test_live_hardware_claim_is_replaced_on_manager_session_change():
    async with _running_controller(config_service=MemoryConfigService(_config())) as (
        controller,
        beacon,
        concord,
    ):
        handle = await _advertise_hardware(beacon, session_id="old-session")

        with anyio.fail_after(1):
            while not _claims(controller):
                await anyio.sleep(0.01)
        owned = _claim(controller)
        await concord.attach(
            owned.contract,
            participant=hardware_manager_address("room-a"),
            session_id="old-session",
        )
        await _reconcile(controller, "test manager token")
        with anyio.fail_after(1):
            while _route(controller, "config-room-a") is None:
                await anyio.sleep(0.01)

        await handle.aclose()
        await _reconcile(controller, "test missing beacon")
        assert _route(controller, "config-room-a") is not None

        await _advertise_hardware(
            beacon,
            session_id="new-session",
            advertisement_id="hardware-ad-2",
        )
        await _reconcile(controller, "test session change")

        assert (await concord.validate(owned.contract)).status == (
            ContractValidityStatus.CANCELLED
        )
        assert _route(controller, "config-room-a") is None
        current_owned = _claim(controller)
        assert current_owned.claim_id != owned.claim_id
        assert current_owned.current_sessions[
            str(hardware_manager_address("room-a"))
        ] == "new-session"

        await concord.attach(
            current_owned.contract,
            participant=hardware_manager_address("room-a"),
            session_id="new-session",
        )
        await _reconcile(controller, "test replacement manager token")

        assert _route(controller, "config-room-a") is not None


@pytest.mark.asyncio
async def test_hardware_beacon_requires_matching_config_labels():
    async with _running_controller(
        config_service=MemoryConfigService(_config(labels={"room": "office"}))
    ) as (controller, beacon, _concord):
        await _advertise_hardware(beacon, labels={"room": "kitchen"})
        await anyio.sleep(0.1)

        assert _claims(controller) == ()
        assert _routes(controller) == ()
