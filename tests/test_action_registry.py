from __future__ import annotations

from unittest.mock import MagicMock

import anyio
import pytest
from conftest import LaneHarness
from deckr.actions.endpoints import action_provider_address
from deckr.beacon import (
    DEFAULT_BEACON_ADVERTISEMENT_STORE_NAME,
    BeaconAdvertisement,
    BeaconAdvertisementSpec,
    BeaconDiscovery,
    BeaconService,
    beacon_advertisement_key,
)
from deckr.components import RunContext
from deckr.profiles import ACTIONS_FEATURE_ID, ActionsBeaconPayload

from deckr.controller.action_provider.action_registry import ActionRegistry
from deckr.controller.action_provider.builtin import BUILTIN_ACTION_PROVIDER_ID
from deckr.controller.action_provider.events import ActionsChangedEvent

CONTROLLER_ID = "controller-main"
ACTION_UUID = "test.stub.action"
PROVIDER_INSTANCE_ID = "python-dev.deckr.clock"
PROVIDER_ID = "dev.deckr.clock"


def _state_bus() -> LaneHarness:
    return LaneHarness("actions", default_endpoint="controller:controller-main")


def _beacon(bus: LaneHarness) -> BeaconService:
    return BeaconService(
        BeaconDiscovery(bus.deckr.state(DEFAULT_BEACON_ADVERTISEMENT_STORE_NAME))
    )


def _actions_payload(
    provider_instance_id: str = PROVIDER_INSTANCE_ID,
    *,
    session_id: str = "session-1",
    action_uuid: str = ACTION_UUID,
    provider_id: str = PROVIDER_ID,
    labels: dict[str, str] | None = None,
) -> ActionsBeaconPayload:
    return ActionsBeaconPayload(
        providerInstanceId=provider_instance_id,
        providerEndpoint=action_provider_address(provider_instance_id),
        providerId=provider_id,
        sessionId=session_id,
        labels=labels or {"room": "office"},
        actions={
            action_uuid: {
                "actionId": action_uuid,
                "name": f"Action {action_uuid}",
                "providerId": provider_id,
            }
        },
    )


async def _advertise_actions(
    beacon: BeaconService,
    payload: ActionsBeaconPayload | None = None,
    *,
    advertisement_id: str = "ad-1",
    session_id: str = "session-1",
) -> BeaconAdvertisement:
    payload = payload or _actions_payload(session_id=session_id)
    advertisement = await beacon.ensure_advertisement(
        BeaconAdvertisementSpec(
            feature_id=ACTIONS_FEATURE_ID,
            endpoint=payload.provider_endpoint,
            session_id=payload.session_id,
            advertisement_id=advertisement_id,
            payload=payload.to_dict(),
            labels=payload.labels,
        )
    )
    await advertisement.publish()
    return advertisement


async def _run_registry(registry: ActionRegistry, callback):
    events: list[ActionsChangedEvent] = []

    async def on_changed(event: ActionsChangedEvent) -> None:
        events.append(event)

    registry._on_actions_changed = on_changed
    stopping = anyio.Event()
    async with anyio.create_task_group() as tg:
        await registry.start(RunContext(tg=tg, stopping=stopping))
        await callback(events)
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_action_registry_uses_beacon_actions_as_availability_source():
    bus = _state_bus()
    beacon = _beacon(bus)
    registry = ActionRegistry(beacon, controller_id=CONTROLLER_ID)

    async def scenario(events):
        await _advertise_actions(beacon)
        with anyio.fail_after(1):
            while await registry.get_action(ACTION_UUID) is None:
                await anyio.sleep(0.01)

        meta = await registry.get_action(ACTION_UUID)
        assert meta is not None
        assert meta.provider_instance_id == PROVIDER_INSTANCE_ID
        assert meta.provider_id == PROVIDER_ID
        assert meta.provider_labels == {"room": "office"}
        assert meta.provider_session_id == "session-1"
        assert events[-1].registered == [f"{PROVIDER_INSTANCE_ID}::{ACTION_UUID}"]

    await _run_registry(registry, scenario)


@pytest.mark.asyncio
async def test_action_registry_filters_by_provider_instance_and_labels():
    bus = _state_bus()
    beacon = _beacon(bus)
    registry = ActionRegistry(beacon, controller_id=CONTROLLER_ID)

    async def scenario(events):
        del events
        await _advertise_actions(beacon)
        with anyio.fail_after(1):
            while await registry.get_action(ACTION_UUID) is None:
                await anyio.sleep(0.01)

        assert await registry.get_action(
            ACTION_UUID,
            provider_instance_id=PROVIDER_INSTANCE_ID,
        )
        assert await registry.get_action(
            ACTION_UUID,
            provider_labels={"room": "office"},
        )
        assert await registry.get_action(
            ACTION_UUID,
            provider_labels={"room": "kitchen"},
        ) is None

    await _run_registry(registry, scenario)


@pytest.mark.asyncio
async def test_action_registry_provider_settings_authority_uses_beacon_session():
    bus = _state_bus()
    beacon = _beacon(bus)
    registry = ActionRegistry(beacon, controller_id=CONTROLLER_ID)

    async def scenario(events):
        del events
        old = await _advertise_actions(beacon, session_id="old")
        with anyio.fail_after(1):
            while registry.provider_session_id(PROVIDER_INSTANCE_ID) != "old":
                await anyio.sleep(0.01)
        assert registry.provider_instance_provides_provider(
            PROVIDER_INSTANCE_ID, PROVIDER_ID
        )

        await old.aclose()
        await _advertise_actions(beacon, session_id="new")
        with anyio.fail_after(1):
            while registry.provider_session_id(PROVIDER_INSTANCE_ID) != "new":
                await anyio.sleep(0.01)
        assert registry.provider_instance_provides_provider(
            PROVIDER_INSTANCE_ID, PROVIDER_ID
        )

    await _run_registry(registry, scenario)


@pytest.mark.asyncio
async def test_action_registry_rejects_mismatched_beacon_payload_identity():
    bus = _state_bus()
    beacon = _beacon(bus)
    registry = ActionRegistry(beacon, controller_id=CONTROLLER_ID)
    state = bus.deckr.state(DEFAULT_BEACON_ADVERTISEMENT_STORE_NAME)

    async def scenario(events):
        await state.put(
            beacon_advertisement_key(
                feature_id=ACTIONS_FEATURE_ID,
                advertisement_id="bad-ad",
            ),
            {
                "schema": "dev.deckr.beacon.advertisement.v1",
                "advertisementId": "bad-ad",
                "featureId": ACTIONS_FEATURE_ID,
                "advertiser": "action_provider:python.other",
                "endpoint": "action_provider:python.other",
                "sessionId": "session-1",
                "refreshSeq": 1,
                "ttlSeconds": 30,
                "payload": _actions_payload().to_dict(),
            },
        )
        await anyio.sleep(0.05)

        assert await registry.get_action(ACTION_UUID) is None
        assert events == []

    await _run_registry(registry, scenario)


@pytest.mark.asyncio
async def test_action_registry_beacon_session_change_refreshes_action_metadata():
    bus = _state_bus()
    beacon = _beacon(bus)
    registry = ActionRegistry(beacon, controller_id=CONTROLLER_ID)

    async def scenario(events):
        old = await _advertise_actions(beacon, session_id="old")
        with anyio.fail_after(1):
            while await registry.get_action(ACTION_UUID) is None:
                await anyio.sleep(0.01)

        await old.aclose()
        await _advertise_actions(beacon, session_id="new")
        with anyio.fail_after(1):
            while registry.provider_session_id(PROVIDER_INSTANCE_ID) != "new":
                await anyio.sleep(0.01)

        qualified = f"{PROVIDER_INSTANCE_ID}::{ACTION_UUID}"
        assert events[-1].registered == [qualified]
        assert events[-1].unregistered == [qualified]

    await _run_registry(registry, scenario)


@pytest.mark.asyncio
async def test_action_registry_prefers_latest_duplicate_provider_advertisement():
    bus = _state_bus()
    beacon = _beacon(bus)
    registry = ActionRegistry(beacon, controller_id=CONTROLLER_ID)

    async def scenario(events):
        await _advertise_actions(
            beacon,
            advertisement_id="z-old",
            session_id="old",
        )
        await _advertise_actions(
            beacon,
            advertisement_id="a-new",
            session_id="new",
        )
        with anyio.fail_after(1):
            while registry.provider_session_id(PROVIDER_INSTANCE_ID) != "new":
                await anyio.sleep(0.01)

        meta = await registry.get_action(ACTION_UUID)
        assert meta is not None
        assert meta.provider_session_id == "new"
        assert events[-1].registered == [f"{PROVIDER_INSTANCE_ID}::{ACTION_UUID}"]

    await _run_registry(registry, scenario)


@pytest.mark.asyncio
async def test_action_registry_removes_actions_when_beacon_advertisement_is_withdrawn():
    bus = _state_bus()
    beacon = _beacon(bus)
    registry = ActionRegistry(beacon, controller_id=CONTROLLER_ID)

    async def scenario(events):
        handle = await _advertise_actions(beacon)
        with anyio.fail_after(1):
            while await registry.get_action(ACTION_UUID) is None:
                await anyio.sleep(0.01)

        await handle.aclose()
        with anyio.fail_after(1):
            while await registry.get_action(ACTION_UUID) is not None:
                await anyio.sleep(0.01)

        assert events[-1].unregistered == [f"{PROVIDER_INSTANCE_ID}::{ACTION_UUID}"]
        assert registry.provider_session_id(PROVIDER_INSTANCE_ID) is None

    await _run_registry(registry, scenario)


@pytest.mark.asyncio
async def test_action_registry_loads_builtin_actions_without_provider_beacon_ads():
    bus = _state_bus()
    registry = ActionRegistry(_beacon(bus), controller_id=CONTROLLER_ID)
    stopping = anyio.Event()
    mock_tg = MagicMock()
    mock_tg.start_soon = lambda fn, *a, **k: None

    await registry.start(RunContext(tg=mock_tg, stopping=stopping))

    goto_page = await registry.get_action("dev.deckr.controller.builtin.action.go_to_page")
    assert goto_page is not None
    assert goto_page.provider_instance_id == BUILTIN_ACTION_PROVIDER_ID
    descriptor = await registry.get_action_descriptor(
        "dev.deckr.controller.builtin.action.go_to_page"
    )
    assert descriptor is not None
    assert descriptor.requirements is not None
    assert descriptor.requirements[0].event_types == ("up", "press", "tap")
