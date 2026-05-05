from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import anyio
import pytest
from conftest import LaneHarness
from deckr.actions.endpoints import action_provider_address
from deckr.actions.messages import ActionDescriptor, ActionProviderCatalog
from deckr.actions.state import action_provider_catalog_key
from deckr.components import RunContext
from deckr.state import (
    DEFAULT_DISCOVERY_STATE_STORE_NAME,
    DEFAULT_LEASE_STATE_STORE_NAME,
    EndpointPresence,
    presence_endpoint_key,
)

from deckr.controller.action_provider.action_registry import ActionRegistry
from deckr.controller.action_provider.builtin import BUILTIN_ACTION_PROVIDER_ID
from deckr.controller.action_provider.events import ActionsChangedEvent

CONTROLLER_ID = "controller-main"
ACTION_UUID = "test.stub.action"
PROVIDER_INSTANCE_ID = "python-dev.deckr.clock"
PROVIDER_ID = "dev.deckr.clock"


def _state_bus() -> LaneHarness:
    return LaneHarness("actions", default_endpoint="controller:controller-main")


def _catalog(
    provider_instance_id: str = PROVIDER_INSTANCE_ID,
    *,
    session_id: str = "session-1",
    action_uuid: str = ACTION_UUID,
    provider_id: str = PROVIDER_ID,
    labels: dict[str, str] | None = None,
) -> ActionProviderCatalog:
    return ActionProviderCatalog(
        providerInstanceId=provider_instance_id,
        providerEndpoint=action_provider_address(provider_instance_id),
        providerId=provider_id,
        sessionId=session_id,
        timestamp=datetime.now(UTC),
        labels=labels or {"room": "office"},
        actions={
            action_uuid: ActionDescriptor(
                actionId=action_uuid,
                name=f"Action {action_uuid}",
                providerId=provider_id,
            )
        },
    )


async def _put_presence(bus: LaneHarness, session_id: str = "session-1") -> None:
    endpoint = action_provider_address(PROVIDER_INSTANCE_ID)
    await bus.deckr.state(DEFAULT_LEASE_STATE_STORE_NAME).put(
        presence_endpoint_key(lane="actions", endpoint=endpoint),
        EndpointPresence(
            endpoint=endpoint,
            lane="actions",
            sessionId=session_id,
            timestamp=datetime.now(UTC),
            ttlSeconds=30,
            metadata={},
        ),
    )


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
async def test_action_registry_uses_catalog_as_action_availability_source():
    bus = _state_bus()
    lease_state = bus.deckr.state(DEFAULT_LEASE_STATE_STORE_NAME)
    discovery_state = bus.deckr.state(DEFAULT_DISCOVERY_STATE_STORE_NAME)
    registry = ActionRegistry(
        lease_state=lease_state,
        discovery_state=discovery_state,
        controller_id=CONTROLLER_ID,
    )

    async def scenario(events):
        await _put_presence(bus)
        await discovery_state.put(
            action_provider_catalog_key(PROVIDER_INSTANCE_ID),
            _catalog(),
        )
        with anyio.fail_after(1):
            while await registry.get_action(ACTION_UUID) is None:
                await anyio.sleep(0.01)

        meta = await registry.get_action(ACTION_UUID)
        assert meta is not None
        assert meta.provider_instance_id == PROVIDER_INSTANCE_ID
        assert meta.provider_id == PROVIDER_ID
        assert meta.provider_labels == {"room": "office"}
        assert events[-1].registered == [f"{PROVIDER_INSTANCE_ID}::{ACTION_UUID}"]

    await _run_registry(registry, scenario)


@pytest.mark.asyncio
async def test_action_registry_filters_by_provider_instance_and_labels():
    bus = _state_bus()
    lease_state = bus.deckr.state(DEFAULT_LEASE_STATE_STORE_NAME)
    discovery_state = bus.deckr.state(DEFAULT_DISCOVERY_STATE_STORE_NAME)
    registry = ActionRegistry(
        lease_state=lease_state,
        discovery_state=discovery_state,
        controller_id=CONTROLLER_ID,
    )

    async def scenario(events):
        del events
        await _put_presence(bus)
        await discovery_state.put(
            action_provider_catalog_key(PROVIDER_INSTANCE_ID),
            _catalog(),
        )
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
async def test_action_registry_provider_settings_authority_uses_catalog_session():
    bus = _state_bus()
    lease_state = bus.deckr.state(DEFAULT_LEASE_STATE_STORE_NAME)
    discovery_state = bus.deckr.state(DEFAULT_DISCOVERY_STATE_STORE_NAME)
    registry = ActionRegistry(
        lease_state=lease_state,
        discovery_state=discovery_state,
        controller_id=CONTROLLER_ID,
    )

    async def scenario(events):
        del events
        await _put_presence(bus, session_id="old")
        await discovery_state.put(
            action_provider_catalog_key(PROVIDER_INSTANCE_ID),
            _catalog(session_id="old"),
        )
        with anyio.fail_after(1):
            while registry.provider_session_id(PROVIDER_INSTANCE_ID) != "old":
                await anyio.sleep(0.01)
        assert registry.provider_instance_provides_provider(PROVIDER_INSTANCE_ID, PROVIDER_ID)

        await _put_presence(bus, session_id="new")
        await discovery_state.put(
            action_provider_catalog_key(PROVIDER_INSTANCE_ID),
            _catalog(session_id="new"),
        )
        with anyio.fail_after(1):
            while registry.provider_session_id(PROVIDER_INSTANCE_ID) != "new":
                await anyio.sleep(0.01)
        assert registry.provider_instance_provides_provider(PROVIDER_INSTANCE_ID, PROVIDER_ID)

    await _run_registry(registry, scenario)


@pytest.mark.asyncio
async def test_action_registry_rejects_mismatched_catalog_payload_identity():
    bus = _state_bus()
    lease_state = bus.deckr.state(DEFAULT_LEASE_STATE_STORE_NAME)
    discovery_state = bus.deckr.state(DEFAULT_DISCOVERY_STATE_STORE_NAME)
    registry = ActionRegistry(
        lease_state=lease_state,
        discovery_state=discovery_state,
        controller_id=CONTROLLER_ID,
    )

    async def scenario(events):
        await _put_presence(bus)
        await discovery_state.put(
            action_provider_catalog_key(PROVIDER_INSTANCE_ID),
            _catalog("python.other"),
        )
        await anyio.sleep(0.05)

        assert await registry.get_action(ACTION_UUID) is None
        assert events == []

    await _run_registry(registry, scenario)


@pytest.mark.asyncio
async def test_action_registry_catalog_session_change_refreshes_action_metadata():
    bus = _state_bus()
    lease_state = bus.deckr.state(DEFAULT_LEASE_STATE_STORE_NAME)
    discovery_state = bus.deckr.state(DEFAULT_DISCOVERY_STATE_STORE_NAME)
    registry = ActionRegistry(
        lease_state=lease_state,
        discovery_state=discovery_state,
        controller_id=CONTROLLER_ID,
    )

    async def scenario(events):
        catalog_key = action_provider_catalog_key(PROVIDER_INSTANCE_ID)
        await _put_presence(bus, session_id="old")
        await discovery_state.put(catalog_key, _catalog(session_id="old"))
        with anyio.fail_after(1):
            while await registry.get_action(ACTION_UUID) is None:
                await anyio.sleep(0.01)

        await _put_presence(bus, session_id="new")
        await discovery_state.put(catalog_key, _catalog(session_id="new"))
        with anyio.fail_after(1):
            while registry.provider_session_id(PROVIDER_INSTANCE_ID) != "new":
                await anyio.sleep(0.01)

        qualified = f"{PROVIDER_INSTANCE_ID}::{ACTION_UUID}"
        assert events[-1].registered == [qualified]
        assert events[-1].unregistered == [qualified]

    await _run_registry(registry, scenario)


@pytest.mark.asyncio
async def test_action_registry_keeps_catalog_omitted_from_prefix_observation():
    bus = _state_bus()
    lease_state = bus.deckr.state(DEFAULT_LEASE_STATE_STORE_NAME)
    discovery_state = bus.deckr.state(DEFAULT_DISCOVERY_STATE_STORE_NAME)
    registry = ActionRegistry(
        lease_state=lease_state,
        discovery_state=discovery_state,
        controller_id=CONTROLLER_ID,
    )

    async def scenario(events):
        await _put_presence(bus)
        await discovery_state.put(
            action_provider_catalog_key(PROVIDER_INSTANCE_ID),
            _catalog(),
        )
        with anyio.fail_after(1):
            while await registry.get_action(ACTION_UUID) is None:
                await anyio.sleep(0.01)

        async def empty_items(prefix: str = ""):
            del prefix
            return ()

        discovery_state.items = empty_items
        await registry._reconcile_current_state(reason="test omitted catalog")

        assert await registry.get_action(ACTION_UUID) is not None
        assert events[-1].registered == [f"{PROVIDER_INSTANCE_ID}::{ACTION_UUID}"]

    await _run_registry(registry, scenario)


@pytest.mark.asyncio
async def test_action_registry_requires_exact_provider_presence_absence():
    bus = _state_bus()
    lease_state = bus.deckr.state(DEFAULT_LEASE_STATE_STORE_NAME)
    discovery_state = bus.deckr.state(DEFAULT_DISCOVERY_STATE_STORE_NAME)
    registry = ActionRegistry(
        lease_state=lease_state,
        discovery_state=discovery_state,
        controller_id=CONTROLLER_ID,
    )

    async def scenario(events):
        await _put_presence(bus)
        await discovery_state.put(
            action_provider_catalog_key(PROVIDER_INSTANCE_ID),
            _catalog(),
        )
        with anyio.fail_after(1):
            while await registry.get_action(ACTION_UUID) is None:
                await anyio.sleep(0.01)

        async def empty_items(prefix: str = ""):
            del prefix
            return ()

        lease_state.items = empty_items
        await registry._reconcile_current_state(reason="test omitted presence")
        assert await registry.get_action(ACTION_UUID) is not None

        await lease_state.delete(
            presence_endpoint_key(
                lane="actions",
                endpoint=action_provider_address(PROVIDER_INSTANCE_ID),
            )
        )
        await registry._reconcile_current_state(reason="test confirmed absence")

        assert await registry.get_action(ACTION_UUID) is None
        assert events[-1].unregistered == [f"{PROVIDER_INSTANCE_ID}::{ACTION_UUID}"]

    await _run_registry(registry, scenario)


@pytest.mark.asyncio
async def test_action_registry_does_not_make_stale_catalog_live_without_presence():
    bus = _state_bus()
    discovery_state = bus.deckr.state(DEFAULT_DISCOVERY_STATE_STORE_NAME)
    registry = ActionRegistry(
        lease_state=bus.deckr.state(DEFAULT_LEASE_STATE_STORE_NAME),
        discovery_state=discovery_state,
        controller_id=CONTROLLER_ID,
    )

    async def scenario(_events):
        await discovery_state.put(
            action_provider_catalog_key(PROVIDER_INSTANCE_ID),
            _catalog(),
        )
        await registry._reconcile_current_state(reason="stale catalog")

        assert await registry.get_action(ACTION_UUID) is None
        assert registry.provider_session_id(PROVIDER_INSTANCE_ID) is None

    await _run_registry(registry, scenario)


@pytest.mark.asyncio
async def test_action_registry_loads_builtin_actions_without_provider_catalogs():
    bus = _state_bus()
    registry = ActionRegistry(
        lease_state=bus.deckr.state(DEFAULT_LEASE_STATE_STORE_NAME),
        discovery_state=bus.deckr.state(DEFAULT_DISCOVERY_STATE_STORE_NAME),
        controller_id=CONTROLLER_ID,
    )
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
