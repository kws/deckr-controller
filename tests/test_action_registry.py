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
from deckr.state import EndpointPresence, presence_endpoint_key

from deckr.controller.action_provider.action_registry import ActionRegistry
from deckr.controller.action_provider.builtin import BUILTIN_ACTION_PROVIDER_ID
from deckr.controller.action_provider.events import ActionsChangedEvent

CONTROLLER_ID = "controller-main"
ACTION_UUID = "test.stub.action"
PROVIDER_INSTANCE_ID = "python.clock"
PROVIDER_ID = "clock"


def _state_bus() -> LaneHarness:
    return LaneHarness("actions", default_endpoint="controller:controller-main")


def _presence(
    provider_instance_id: str = PROVIDER_INSTANCE_ID,
    *,
    session_id: str = "session-1",
) -> EndpointPresence:
    return EndpointPresence(
        endpoint=action_provider_address(provider_instance_id),
        lane="actions",
        sessionId=session_id,
        timestamp=datetime.now(UTC),
        ttlSeconds=15,
        metadata={"runtime": "test"},
    )


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
        ttlSeconds=15,
        labels=labels or {"room": "office"},
        actions={
            action_uuid: ActionDescriptor(
                actionId=action_uuid,
                name=f"Action {action_uuid}",
                providerId=provider_id,
            )
        },
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
async def test_action_registry_uses_catalog_only_with_matching_presence():
    bus = _state_bus()
    state = bus.deckr.state()
    registry = ActionRegistry(state=state, controller_id=CONTROLLER_ID)

    async def scenario(events):
        await state.put(action_provider_catalog_key(PROVIDER_INSTANCE_ID), _catalog())
        await anyio.sleep(0.05)
        assert await registry.get_action(ACTION_UUID) is None

        await state.put(
            presence_endpoint_key(
                lane="actions",
                endpoint=action_provider_address(PROVIDER_INSTANCE_ID),
            ),
            _presence(),
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
    state = bus.deckr.state()
    registry = ActionRegistry(state=state, controller_id=CONTROLLER_ID)

    async def scenario(events):
        del events
        await state.put(
            presence_endpoint_key(
                lane="actions",
                endpoint=action_provider_address(PROVIDER_INSTANCE_ID),
            ),
            _presence(),
        )
        await state.put(action_provider_catalog_key(PROVIDER_INSTANCE_ID), _catalog())
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
async def test_action_registry_provider_settings_authority_uses_live_session():
    bus = _state_bus()
    state = bus.deckr.state()
    registry = ActionRegistry(state=state, controller_id=CONTROLLER_ID)

    async def scenario(events):
        del events
        await state.put(action_provider_catalog_key(PROVIDER_INSTANCE_ID), _catalog())
        await anyio.sleep(0.05)
        assert not registry.provider_instance_provides_provider(
            PROVIDER_INSTANCE_ID,
            PROVIDER_ID,
        )

        await state.put(
            presence_endpoint_key(
                lane="actions",
                endpoint=action_provider_address(PROVIDER_INSTANCE_ID),
            ),
            _presence(session_id="new"),
        )
        await state.put(
            action_provider_catalog_key(PROVIDER_INSTANCE_ID),
            _catalog(session_id="old"),
        )
        await anyio.sleep(0.05)
        assert registry.provider_session_id(PROVIDER_INSTANCE_ID) == "new"
        assert not registry.provider_instance_provides_provider(
            PROVIDER_INSTANCE_ID,
            PROVIDER_ID,
        )

        await state.put(
            action_provider_catalog_key(PROVIDER_INSTANCE_ID),
            _catalog(session_id="new"),
        )
        with anyio.fail_after(1):
            while not registry.provider_instance_provides_provider(
                PROVIDER_INSTANCE_ID,
                PROVIDER_ID,
            ):
                await anyio.sleep(0.01)

    await _run_registry(registry, scenario)


@pytest.mark.asyncio
async def test_action_registry_rejects_mismatched_catalog_payload_identity():
    bus = _state_bus()
    state = bus.deckr.state()
    registry = ActionRegistry(state=state, controller_id=CONTROLLER_ID)

    async def scenario(events):
        await state.put(
            presence_endpoint_key(
                lane="actions",
                endpoint=action_provider_address(PROVIDER_INSTANCE_ID),
            ),
            _presence(),
        )
        await state.put(
            action_provider_catalog_key(PROVIDER_INSTANCE_ID),
            _catalog("python.other"),
        )
        await anyio.sleep(0.05)

        assert await registry.get_action(ACTION_UUID) is None
        assert events == []

    await _run_registry(registry, scenario)


@pytest.mark.asyncio
async def test_action_registry_session_change_unregisters_stale_catalog():
    bus = _state_bus()
    state = bus.deckr.state()
    registry = ActionRegistry(state=state, controller_id=CONTROLLER_ID)

    async def scenario(events):
        presence_key = presence_endpoint_key(
            lane="actions",
            endpoint=action_provider_address(PROVIDER_INSTANCE_ID),
        )
        catalog_key = action_provider_catalog_key(PROVIDER_INSTANCE_ID)
        await state.put(presence_key, _presence(session_id="old"))
        await state.put(catalog_key, _catalog(session_id="old"))
        with anyio.fail_after(1):
            while await registry.get_action(ACTION_UUID) is None:
                await anyio.sleep(0.01)

        await state.put(presence_key, _presence(session_id="new"))
        with anyio.fail_after(1):
            while await registry.get_action(ACTION_UUID) is not None:
                await anyio.sleep(0.01)

        await state.put(catalog_key, _catalog(session_id="new"))
        with anyio.fail_after(1):
            while await registry.get_action(ACTION_UUID) is None:
                await anyio.sleep(0.01)

        qualified = f"{PROVIDER_INSTANCE_ID}::{ACTION_UUID}"
        assert events[-2].unregistered == [qualified]
        assert events[-1].registered == [qualified]

    await _run_registry(registry, scenario)


@pytest.mark.asyncio
async def test_action_registry_loads_builtin_actions_without_provider_catalogs():
    bus = _state_bus()
    state = bus.deckr.state()
    registry = ActionRegistry(state=state, controller_id=CONTROLLER_ID)
    stopping = anyio.Event()
    mock_tg = MagicMock()
    mock_tg.start_soon = lambda fn, *a, **k: None

    await registry.start(RunContext(tg=mock_tg, stopping=stopping))

    goto_page = await registry.get_action("deckr.controller.builtin.gotopage")
    assert goto_page is not None
    assert goto_page.provider_instance_id == BUILTIN_ACTION_PROVIDER_ID
