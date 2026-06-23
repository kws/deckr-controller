from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import anyio
import pytest
from conftest import LaneHarness
from deckr.beacon import (
    BEACON_ADVERTISEMENT_STORE_POLICY,
    AdvertisementRecord,
    Beacon,
    BeaconAdvertisementSpec,
    Candidate,
    beacon_advertisement_key,
)
from deckr.components import RunContext
from deckr.concord import (
    CONCORD_CONTRACT_BUCKET_POLICY,
    CONCORD_MAINTENANCE_BUCKET_POLICY,
    CONCORD_TOKEN_BUCKET_POLICY,
    Concord,
    ContractValidity,
    ContractValidityStatus,
)
from deckr.contracts.messages import (
    TraceContext,
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
from deckr.substrates.nats_kv import KvUnavailable

from deckr.controller._controller_service import ControllerService
from deckr.controller._device_manager import DeviceManager
from deckr.controller._endpoint_messages import send_with_endpoint_identity
from deckr.controller._hardware_service import (
    DeviceRouteRegistry,
    HardwareCommandService,
)
from deckr.controller.config import DeviceConfig, DeviceConfigMatch, Page, Profile

CONTROLLER_ID = "controller-main"


@pytest.mark.asyncio
async def test_send_with_endpoint_identity_restamps_sender_and_preserves_metadata():
    bus = LaneHarness(
        "hardware_messages",
        default_endpoint=controller_address(CONTROLLER_ID),
    )
    endpoint = bus.endpoint(controller_address(CONTROLLER_ID)).session
    manager_endpoint = bus.endpoint(hardware_manager_address("room-a"))
    original = hw_messages.control_command_message(
        controller_id="stale-controller",
        sender_session_id="stale-session",
        manager_id="room-a",
        device_id="deck",
        capability_id="raster.bitmap",
        command_type="clear",
        control_id="0,0",
        params={},
        recipient_session_id=manager_endpoint.session_id,
    ).model_copy(
        update={
            "ttl_ms": 1234,
            "causation_id": "cause-1",
            "trace": TraceContext(
                traceParent=(
                    "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-00"
                )
            ),
        }
    )

    async with manager_endpoint.subscribe() as stream:
        sent = await send_with_endpoint_identity(endpoint, original)
        with anyio.fail_after(1):
            received = await stream.receive()

    assert received == sent
    assert received.sender == controller_address(CONTROLLER_ID)
    assert received.sender_session_id == endpoint.session_id
    assert received.recipient == original.recipient
    assert received.recipient_session_id == original.recipient_session_id
    assert received.subject == original.subject
    assert received.message_type == original.message_type
    assert received.body == original.body
    assert received.ttl_ms == original.ttl_ms
    assert received.causation_id == original.causation_id
    assert received.trace == original.trace


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


def _hardware_candidate(
    *,
    manager_id: str = "room-a",
    session_id: str = "manager-session",
    advertisement_id: str = "hardware-ad-1",
    device: DeviceDescriptor | None = None,
) -> Candidate:
    descriptor = device or _device()
    ref = DeviceRef(managerId=manager_id, deviceId=descriptor.device_id)
    payload = HardwareBeaconPayload(
        managerId=manager_id,
        managerEndpoint=hardware_manager_address(manager_id),
        sessionId=session_id,
        labels={},
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
    advertisement = AdvertisementRecord(
        advertisementId=advertisement_id,
        featureId=HARDWARE_FEATURE_ID,
        advertiser=hardware_manager_address(manager_id),
        endpoint=hardware_manager_address(manager_id),
        sessionId=session_id,
        refreshSeq=1,
        ttlSeconds=30,
        labels={},
        payload=payload.to_dict(),
    )
    return Candidate(
        key=beacon_advertisement_key(
            feature_id=HARDWARE_FEATURE_ID,
            advertisement_id=advertisement_id,
        ),
        advertisement=advertisement,
        revision=1,
        observed_at=datetime.now(UTC),
    )


def _beacon(bus: LaneHarness) -> Beacon:
    return Beacon(bus.substrate.kv_bucket(BEACON_ADVERTISEMENT_STORE_POLICY))


@pytest.mark.asyncio
async def test_hardware_candidates_use_exact_beacon_fallback_when_cache_unavailable():
    class ExactFallbackBeacon:
        def __init__(self) -> None:
            self.reads = 0
            self.exact_reads = 0

        def candidates(self, feature_id: str):
            assert feature_id == HARDWARE_FEATURE_ID
            self.reads += 1
            raise KvUnavailable("stale")

        async def candidates_exact(self, feature_id: str):
            assert feature_id == HARDWARE_FEATURE_ID
            self.exact_reads += 1
            return (_hardware_candidate(),)

    beacon = ExactFallbackBeacon()
    controller = ControllerService.__new__(ControllerService)
    controller._beacon = beacon

    candidates = await ControllerService._hardware_candidates_from_beacon(controller)

    assert set(candidates) == {("room-a", "deck")}
    assert beacon.reads == 1
    assert beacon.exact_reads == 1


def _concord(bus: LaneHarness) -> Concord:
    return Concord(
        bus.substrate.kv_bucket(CONCORD_CONTRACT_BUCKET_POLICY),
        bus.substrate.kv_bucket(CONCORD_TOKEN_BUCKET_POLICY),
        bus.substrate.kv_bucket(CONCORD_MAINTENANCE_BUCKET_POLICY),
    )


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
    registry.provider_session_id.return_value = None
    registry.provider_instance_provides_provider.return_value = False
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
    command_service = HardwareCommandService(
        bus.endpoint("controller:controller-main").session,
        controller_id="controller-main",
    )
    registry = DeviceRouteRegistry()
    ref_a = DeviceRef(manager_id="room-a", device_id="deck")
    ref_b = DeviceRef(manager_id="room-b", device_id="deck")

    registry.connect(config_id="config-room-a", ref=ref_a, device=_device("deck", "a"))
    registry.connect(config_id="config-room-b", ref=ref_b, device=_device("deck", "b"))
    command_service.register_device(
        config_id="config-room-a",
        ref=ref_a,
        device=_device("deck", "a"),
    )
    command_service.register_device(
        config_id="config-room-b",
        ref=ref_b,
        device=_device("deck", "b"),
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
    registry.provider_session_id.return_value = None
    registry.provider_instance_provides_provider.return_value = False
    controller = ControllerService(
        endpoint=hardware_bus.endpoint(controller_address(CONTROLLER_ID)).session,
        beacon=beacon,
        concord=concord,
        config_service=MemoryConfigService(_config()),
        settings_service=MagicMock(),
        controller_id=CONTROLLER_ID,
        action_registry=registry,
    )
    caplog.set_level("INFO", logger="deckr.controller._controller_service")

    async with anyio.create_task_group() as tg:
        beacon.start(tg)
        concord.start(tg)
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

        await controller._reconcile_hardware_current_state(
            reason="test duplicate beacon"
        )
        tg.cancel_scope.cancel()

    assert len(controller._owned_claims) == 1
    owned = next(iter(controller._owned_claims.values()))
    assert owned.current_sessions[str(hardware_manager_address("room-a"))] == (
        "live-session"
    )
    assert "Multiple hardware Beacon advertisements describe device room-a/deck" in (
        caplog.text
    )
    assert "hardware_manager_mirabox-rust-001" in caplog.text
    assert "hardware_manager_test" in caplog.text


@pytest.mark.asyncio
async def test_pending_hardware_claim_connects_from_exact_validity_when_hot_refresh_unavailable(
    monkeypatch,
):
    async with _running_controller(config_service=MemoryConfigService(_config())) as (
        controller,
        beacon,
        concord,
    ):
        handle = await _advertise_hardware(beacon, session_id="manager-session")

        with anyio.fail_after(1):
            while not controller._owned_claims:
                await anyio.sleep(0.01)
        owned = next(iter(controller._owned_claims.values()))
        await concord.attach(
            owned.contract,
            participant=hardware_manager_address("room-a"),
            session_id="manager-session",
        )
        await handle.aclose()
        await beacon.wait_current()

        async def unavailable_refresh() -> ContractValidity:
            return ContractValidity(ContractValidityStatus.UNAVAILABLE)

        monkeypatch.setattr(owned.agreement, "refresh", unavailable_refresh)

        await controller._reconcile_hardware_current_state(
            reason="test exact-valid hot-unavailable claim"
        )

        live = controller._device_registry.get("config-room-a")
        assert live is not None
        assert live.ref == DeviceRef(managerId="room-a", deviceId="deck")
        assert live.manager_session_id == "manager-session"


@pytest.mark.asyncio
async def test_hardware_claim_stays_pending_until_manager_token_attaches():
    async with _running_controller(config_service=MemoryConfigService(_config())) as (
        controller,
        beacon,
        _concord,
    ):
        await _advertise_hardware(beacon)

        with anyio.fail_after(1):
            while not controller._owned_claims:
                await anyio.sleep(0.01)

        owned = next(iter(controller._owned_claims.values()))
        await anyio.sleep(0.06)
        await controller._reconcile_hardware_current_state(reason="test pending claim")

        assert next(iter(controller._owned_claims.values())).claim_id == owned.claim_id
        assert controller._device_registry.all() == ()


@pytest.mark.asyncio
async def test_pending_hardware_claim_survives_missing_beacon_candidate():
    async with _running_controller(config_service=MemoryConfigService(_config())) as (
        controller,
        beacon,
        concord,
    ):
        handle = await _advertise_hardware(beacon, session_id="manager-session")

        with anyio.fail_after(1):
            while not controller._owned_claims:
                await anyio.sleep(0.01)
        owned = next(iter(controller._owned_claims.values()))

        await handle.aclose()
        await beacon.wait_current()
        await controller._reconcile_hardware_current_state(reason="test missing beacon")

        assert next(iter(controller._owned_claims.values())).claim_id == owned.claim_id
        assert (await concord.validate(owned.contract)).status == (
            ContractValidityStatus.NOT_YET_FULFILLED
        )
        assert controller._device_registry.all() == ()


@pytest.mark.asyncio
async def test_pending_hardware_claim_recovers_when_manager_attaches_later():
    async with _running_controller(config_service=MemoryConfigService(_config())) as (
        controller,
        beacon,
        concord,
    ):
        await _advertise_hardware(beacon, session_id="manager-session")

        with anyio.fail_after(1):
            while not controller._owned_claims:
                await anyio.sleep(0.01)
        owned = next(iter(controller._owned_claims.values()))

        await anyio.sleep(0.06)
        await controller._reconcile_hardware_current_state(reason="test still pending")

        assert next(iter(controller._owned_claims.values())).claim_id == owned.claim_id
        assert controller._device_registry.all() == ()

        await concord.attach(
            owned.contract,
            participant=hardware_manager_address("room-a"),
            session_id="manager-session",
        )
        await controller._reconcile_hardware_current_state(reason="test late attach")

        live = controller._device_registry.get("config-room-a")
        assert live is not None
        assert live.ref == DeviceRef(managerId="room-a", deviceId="deck")
        assert next(iter(controller._owned_claims.values())).claim_id == owned.claim_id


@pytest.mark.asyncio
async def test_pending_hardware_claim_replaced_on_manager_session_change():
    async with _running_controller(config_service=MemoryConfigService(_config())) as (
        controller,
        beacon,
        concord,
    ):
        handle = await _advertise_hardware(beacon, session_id="old-session")

        with anyio.fail_after(1):
            while not controller._owned_claims:
                await anyio.sleep(0.01)
        owned = next(iter(controller._owned_claims.values()))

        await handle.aclose()
        await beacon.wait_current()
        await _advertise_hardware(
            beacon,
            session_id="new-session",
            advertisement_id="hardware-ad-2",
        )
        await controller._reconcile_hardware_current_state(
            reason="test session replacement"
        )

        assert (await concord.validate(owned.contract)).status == (
            ContractValidityStatus.CANCELLED
        )

        replacement = next(iter(controller._owned_claims.values()))
        assert replacement.claim_id != owned.claim_id
        assert replacement.current_sessions[
            str(hardware_manager_address("room-a"))
        ] == ("new-session")


@pytest.mark.asyncio
async def test_pending_hardware_claim_kept_when_config_changes():
    config_service = MemoryConfigService(_config())
    async with _running_controller(config_service=config_service) as (
        controller,
        beacon,
        concord,
    ):
        handle = await _advertise_hardware(beacon, session_id="manager-session")

        with anyio.fail_after(1):
            while not controller._owned_claims:
                await anyio.sleep(0.01)
        first = next(iter(controller._owned_claims.values()))

        await config_service.write_config(
            _config().model_copy(update={"name": "Changed Test Device"})
        )
        await controller._reconcile_hardware_current_state(reason="test config change")

        assert next(iter(controller._owned_claims.values())).claim_id == first.claim_id
        assert (await concord.validate(first.contract)).status == (
            ContractValidityStatus.NOT_YET_FULFILLED
        )
        await handle.aclose()


@pytest.mark.asyncio
async def test_hardware_claim_becomes_live_after_concord_manager_token():
    async with _running_controller(config_service=MemoryConfigService(_config())) as (
        controller,
        beacon,
        concord,
    ):
        await _advertise_hardware(beacon)

        with anyio.fail_after(1):
            while not controller._owned_claims:
                await anyio.sleep(0.01)
        owned = next(iter(controller._owned_claims.values()))

        await concord.attach(
            owned.contract,
            participant=hardware_manager_address("room-a"),
            session_id="manager-session",
        )
        await controller._reconcile_hardware_current_state(reason="test manager token")

        with anyio.fail_after(1):
            while controller._device_registry.get("config-room-a") is None:
                await anyio.sleep(0.01)

        live = controller._device_registry.get("config-room-a")
        assert live is not None
        assert live.ref == DeviceRef(managerId="room-a", deviceId="deck")


@pytest.mark.asyncio
async def test_hardware_claim_is_preserved_when_config_is_removed():
    config_service = MemoryConfigService(_config())
    async with _running_controller(config_service=config_service) as (
        controller,
        beacon,
        concord,
    ):
        await _advertise_hardware(beacon)

        with anyio.fail_after(1):
            while not controller._owned_claims:
                await anyio.sleep(0.01)
        owned = next(iter(controller._owned_claims.values()))
        await concord.attach(
            owned.contract,
            participant=hardware_manager_address("room-a"),
            session_id="manager-session",
        )
        await controller._reconcile_hardware_current_state(reason="test manager token")

        with anyio.fail_after(1):
            while controller._device_registry.get("config-room-a") is None:
                await anyio.sleep(0.01)
        manager = None
        with anyio.fail_after(1):
            while manager is None:
                manager = await controller._controller_contexts.get("config-room-a")
                await anyio.sleep(0.01)

        await config_service.remove_config("config-room-a")

        with anyio.fail_after(1):
            while manager._config_active:
                await anyio.sleep(0.01)

        assert controller._owned_claims
        assert controller._device_registry.get("config-room-a") is not None
        assert await controller._controller_contexts.get("config-room-a") is manager
        assert (await concord.validate(owned.contract)).status == (
            ContractValidityStatus.VALID
        )

        await config_service.write_config(_config())

        with anyio.fail_after(1):
            while not manager._config_active:
                await anyio.sleep(0.01)

        assert controller._device_registry.get("config-room-a") is not None
        assert await controller._controller_contexts.get("config-room-a") is manager


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
            while not controller._owned_claims:
                await anyio.sleep(0.01)
        owned = next(iter(controller._owned_claims.values()))
        original_claim_id = owned.claim_id
        await concord.attach(
            owned.contract,
            participant=hardware_manager_address("room-a"),
            session_id="manager-session",
        )
        await controller._reconcile_hardware_current_state(reason="test manager token")

        with anyio.fail_after(1):
            while controller._device_registry.get("config-room-a") is None:
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
        await controller._reconcile_hardware_current_state(reason="test rematch")

        with anyio.fail_after(1):
            while controller._device_registry.get("config-room-b") is None:
                await anyio.sleep(0.01)

        current_owned = next(iter(controller._owned_claims.values()))
        assert current_owned.claim_id == original_claim_id
        assert current_owned.config_id == "config-room-b"
        assert controller._device_registry.get("config-room-a") is None
        assert controller._device_registry.get("config-room-b") is not None
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
            while not controller._owned_claims:
                await anyio.sleep(0.01)
        owned = next(iter(controller._owned_claims.values()))
        await concord.attach(
            owned.contract,
            participant=hardware_manager_address("room-a"),
            session_id="manager-session",
        )
        await controller._reconcile_hardware_current_state(reason="test manager token")

        with anyio.fail_after(1):
            await background_started.wait()

        live = controller._device_registry.get("config-room-a")
        assert live is not None
        await controller._disconnect_live(
            live,
            release_claim=False,
            reason="test device disconnect",
        )

        with anyio.fail_after(1):
            await background_stopped.wait()

        assert controller._device_registry.get("config-room-a") is None


@pytest.mark.asyncio
async def test_live_hardware_claim_ignores_advertisement_id_change():
    async with _running_controller(config_service=MemoryConfigService(_config())) as (
        controller,
        beacon,
        concord,
    ):
        handle = await _advertise_hardware(beacon, advertisement_id="hardware-ad-1")

        with anyio.fail_after(1):
            while not controller._owned_claims:
                await anyio.sleep(0.01)
        owned = next(iter(controller._owned_claims.values()))
        await concord.attach(
            owned.contract,
            participant=hardware_manager_address("room-a"),
            session_id="manager-session",
        )
        await controller._reconcile_hardware_current_state(reason="test manager token")
        with anyio.fail_after(1):
            while controller._device_registry.get("config-room-a") is None:
                await anyio.sleep(0.01)

        await handle.aclose()
        await _advertise_hardware(beacon, advertisement_id="hardware-ad-2")
        await controller._reconcile_hardware_current_state(
            reason="test advertisement id change"
        )

        live = controller._device_registry.get("config-room-a")
        assert live is not None
        current_owned = next(iter(controller._owned_claims.values()))
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
            while not controller._owned_claims:
                await anyio.sleep(0.01)
        owned = next(iter(controller._owned_claims.values()))
        await concord.attach(
            owned.contract,
            participant=hardware_manager_address("room-a"),
            session_id="old-session",
        )
        await controller._reconcile_hardware_current_state(reason="test manager token")
        with anyio.fail_after(1):
            while controller._device_registry.get("config-room-a") is None:
                await anyio.sleep(0.01)

        await handle.aclose()
        await controller._reconcile_hardware_current_state(reason="test missing beacon")
        assert controller._device_registry.get("config-room-a") is not None

        await _advertise_hardware(
            beacon,
            session_id="new-session",
            advertisement_id="hardware-ad-2",
        )
        await controller._reconcile_hardware_current_state(reason="test session change")

        assert (await concord.validate(owned.contract)).status == (
            ContractValidityStatus.CANCELLED
        )
        assert controller._device_registry.get("config-room-a") is None
        current_owned = next(iter(controller._owned_claims.values()))
        assert current_owned.claim_id != owned.claim_id
        assert current_owned.current_sessions[
            str(hardware_manager_address("room-a"))
        ] == "new-session"

        await concord.attach(
            current_owned.contract,
            participant=hardware_manager_address("room-a"),
            session_id="new-session",
        )
        await controller._reconcile_hardware_current_state(
            reason="test replacement manager token"
        )

        assert controller._device_registry.get("config-room-a") is not None


@pytest.mark.asyncio
async def test_hardware_beacon_requires_matching_config_labels():
    async with _running_controller(
        config_service=MemoryConfigService(_config(labels={"room": "office"}))
    ) as (controller, beacon, _concord):
        await _advertise_hardware(beacon, labels={"room": "kitchen"})
        await anyio.sleep(0.1)

        assert controller._owned_claims == {}
        assert controller._device_registry.all() == ()
