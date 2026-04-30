from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import anyio
import pytest
from conftest import LaneHarness
from deckr.components import RunContext
from deckr.contracts.messages import host_address
from deckr.pluginhost.messages import ActionDescriptor, PluginActionCatalog
from deckr.state import (
    EndpointPresence,
    plugin_action_catalog_key,
    presence_endpoint_key,
)

from deckr.controller.plugin.action_registry import ActionRegistry
from deckr.controller.plugin.builtin import BUILTIN_ACTION_PROVIDER_ID
from deckr.controller.plugin.events import ActionsChangedEvent

CONTROLLER_ID = "controller-main"
ACTION_UUID = "test.stub.action"


def _state_bus() -> LaneHarness:
    return LaneHarness("plugin_messages", default_endpoint="controller:controller-main")


def _presence(host_id: str, *, session_id: str = "session-1") -> EndpointPresence:
    return EndpointPresence(
        endpoint=host_address(host_id),
        lane="plugin_messages",
        sessionId=session_id,
        timestamp=datetime.now(UTC),
        ttlSeconds=15,
        metadata={"runtime": "test"},
    )


def _catalog(
    host_id: str,
    *,
    session_id: str = "session-1",
    action_uuid: str = ACTION_UUID,
    plugin_uuid: str | None = "test.plugin",
) -> PluginActionCatalog:
    return PluginActionCatalog(
        hostId=host_id,
        hostEndpoint=host_address(host_id),
        sessionId=session_id,
        timestamp=datetime.now(UTC),
        ttlSeconds=15,
        actions={
            action_uuid: ActionDescriptor(
                actionId=action_uuid,
                name=f"Action {action_uuid}",
                pluginId=plugin_uuid,
            )
        },
    )


async def _run_registry(registry: ActionRegistry, state, callback):
    events: list[ActionsChangedEvent] = []

    async def on_changed(event: ActionsChangedEvent) -> None:
        events.append(event)

    registry._on_actions_changed = on_changed
    stopping = anyio.Event()
    async with anyio.create_task_group() as tg:
        await registry.start(RunContext(tg=tg, stopping=stopping))
        await callback(events)
        tg.cancel_scope.cancel()
    del state


@pytest.mark.asyncio
async def test_action_registry_uses_catalog_only_with_matching_host_presence():
    bus = _state_bus()
    state = bus.deckr.state()
    registry = ActionRegistry(state=state, controller_id=CONTROLLER_ID)

    async def scenario(events):
        await state.put(plugin_action_catalog_key("python"), _catalog("python"))
        await anyio.sleep(0.05)
        assert await registry.get_action(ACTION_UUID) is None

        await state.put(
            presence_endpoint_key(
                lane="plugin_messages",
                endpoint=host_address("python"),
            ),
            _presence("python"),
        )
        with anyio.fail_after(1):
            while await registry.get_action(ACTION_UUID) is None:
                await anyio.sleep(0.01)

        meta = await registry.get_action(ACTION_UUID)
        assert meta is not None
        assert meta.host_id == "python"
        assert meta.plugin_uuid == "test.plugin"
        assert events[-1].registered == ["python::test.stub.action"]

    await _run_registry(registry, state, scenario)


@pytest.mark.asyncio
async def test_action_registry_proves_live_host_plugin_ownership():
    bus = _state_bus()
    state = bus.deckr.state()
    registry = ActionRegistry(state=state, controller_id=CONTROLLER_ID)

    async def scenario(events):
        del events
        assert not registry.host_provides_plugin("python", "test.plugin")
        await state.put(
            presence_endpoint_key(
                lane="plugin_messages",
                endpoint=host_address("python"),
            ),
            _presence("python"),
        )
        await state.put(plugin_action_catalog_key("python"), _catalog("python"))
        with anyio.fail_after(1):
            while not registry.host_provides_plugin("python", "test.plugin"):
                await anyio.sleep(0.01)

        assert registry.host_provides_plugin("python", "test.plugin")
        assert not registry.host_provides_plugin("other", "test.plugin")
        assert not registry.host_provides_plugin("python", "other.plugin")
        assert not registry.host_provides_plugin("python", "")
        assert not registry.host_provides_plugin(BUILTIN_ACTION_PROVIDER_ID, "test.plugin")

    await _run_registry(registry, state, scenario)


@pytest.mark.asyncio
async def test_action_registry_denies_stale_or_unowned_plugin_settings_authority():
    bus = _state_bus()
    state = bus.deckr.state()
    registry = ActionRegistry(state=state, controller_id=CONTROLLER_ID)

    async def scenario(events):
        del events
        await state.put(plugin_action_catalog_key("python"), _catalog("python"))
        await anyio.sleep(0.05)
        assert not registry.host_provides_plugin("python", "test.plugin")

        await state.put(
            presence_endpoint_key(
                lane="plugin_messages",
                endpoint=host_address("python"),
            ),
            _presence("python", session_id="new"),
        )
        await state.put(
            plugin_action_catalog_key("python"),
            _catalog("python", session_id="old"),
        )
        await anyio.sleep(0.05)
        assert not registry.host_provides_plugin("python", "test.plugin")

        await state.put(
            plugin_action_catalog_key("python"),
            _catalog("python", session_id="new", plugin_uuid=None),
        )
        await anyio.sleep(0.05)
        assert not registry.host_provides_plugin("python", "test.plugin")

    await _run_registry(registry, state, scenario)


@pytest.mark.asyncio
async def test_action_registry_rejects_mismatched_catalog_payload_identity():
    bus = _state_bus()
    state = bus.deckr.state()
    registry = ActionRegistry(state=state, controller_id=CONTROLLER_ID)

    async def scenario(events):
        await state.put(
            presence_endpoint_key(
                lane="plugin_messages",
                endpoint=host_address("python"),
            ),
            _presence("python"),
        )
        await state.put(plugin_action_catalog_key("python"), _catalog("other"))
        await anyio.sleep(0.05)

        assert await registry.get_action(ACTION_UUID) is None
        assert events == []

    await _run_registry(registry, state, scenario)


@pytest.mark.asyncio
async def test_action_registry_catalog_delete_unregisters_actions():
    bus = _state_bus()
    state = bus.deckr.state()
    registry = ActionRegistry(state=state, controller_id=CONTROLLER_ID)

    async def scenario(events):
        await state.put(
            presence_endpoint_key(
                lane="plugin_messages",
                endpoint=host_address("python"),
            ),
            _presence("python"),
        )
        await state.put(plugin_action_catalog_key("python"), _catalog("python"))
        with anyio.fail_after(1):
            while await registry.get_action(ACTION_UUID) is None:
                await anyio.sleep(0.01)

        await state.delete(plugin_action_catalog_key("python"))
        with anyio.fail_after(1):
            while await registry.get_action(ACTION_UUID) is not None:
                await anyio.sleep(0.01)

        assert events[-1].unregistered == ["python::test.stub.action"]

    await _run_registry(registry, state, scenario)


@pytest.mark.asyncio
async def test_action_registry_host_session_change_unregisters_stale_catalog():
    bus = _state_bus()
    state = bus.deckr.state()
    registry = ActionRegistry(state=state, controller_id=CONTROLLER_ID)

    async def scenario(events):
        presence_key = presence_endpoint_key(
            lane="plugin_messages",
            endpoint=host_address("python"),
        )
        await state.put(presence_key, _presence("python", session_id="old"))
        await state.put(
            plugin_action_catalog_key("python"),
            _catalog("python", session_id="old"),
        )
        with anyio.fail_after(1):
            while await registry.get_action(ACTION_UUID) is None:
                await anyio.sleep(0.01)

        await state.put(presence_key, _presence("python", session_id="new"))
        with anyio.fail_after(1):
            while await registry.get_action(ACTION_UUID) is not None:
                await anyio.sleep(0.01)

        await state.put(
            plugin_action_catalog_key("python"),
            _catalog("python", session_id="new"),
        )
        with anyio.fail_after(1):
            while await registry.get_action(ACTION_UUID) is None:
                await anyio.sleep(0.01)

        assert events[-2].unregistered == ["python::test.stub.action"]
        assert events[-1].registered == ["python::test.stub.action"]

    await _run_registry(registry, state, scenario)


@pytest.mark.asyncio
async def test_action_registry_broker_snapshot_removes_actions_after_presence_loss():
    bus = _state_bus()
    state = bus.deckr.state()
    registry = ActionRegistry(state=state, controller_id=CONTROLLER_ID)
    events: list[ActionsChangedEvent] = []

    async def on_changed(event: ActionsChangedEvent) -> None:
        events.append(event)

    registry._on_actions_changed = on_changed
    presence_key = presence_endpoint_key(
        lane="plugin_messages",
        endpoint=host_address("python"),
    )
    await state.put(presence_key, _presence("python"))
    await state.put(plugin_action_catalog_key("python"), _catalog("python"))
    await registry._reconcile_current_state(reason="test snapshot")

    assert await registry.get_action(ACTION_UUID) is not None
    assert events[-1].registered == ["python::test.stub.action"]

    await state.delete(presence_key)
    await registry._reconcile_current_state(reason="test snapshot")

    assert await registry.get_action(ACTION_UUID) is None
    assert events[-1].unregistered == ["python::test.stub.action"]


@pytest.mark.asyncio
async def test_action_registry_broker_snapshot_removes_actions_after_catalog_loss():
    bus = _state_bus()
    state = bus.deckr.state()
    registry = ActionRegistry(state=state, controller_id=CONTROLLER_ID)
    events: list[ActionsChangedEvent] = []

    async def on_changed(event: ActionsChangedEvent) -> None:
        events.append(event)

    registry._on_actions_changed = on_changed
    catalog_key = plugin_action_catalog_key("python")
    await state.put(
        presence_endpoint_key(
            lane="plugin_messages",
            endpoint=host_address("python"),
        ),
        _presence("python"),
    )
    await state.put(catalog_key, _catalog("python"))
    await registry._reconcile_current_state(reason="test snapshot")

    assert await registry.get_action(ACTION_UUID) is not None
    assert events[-1].registered == ["python::test.stub.action"]

    await state.delete(catalog_key)
    await registry._reconcile_current_state(reason="test snapshot")

    assert await registry.get_action(ACTION_UUID) is None
    assert events[-1].unregistered == ["python::test.stub.action"]


@pytest.mark.asyncio
async def test_action_registry_loads_builtin_actions_without_plugin_catalogs():
    bus = _state_bus()
    state = bus.deckr.state()
    registry = ActionRegistry(state=state, controller_id=CONTROLLER_ID)
    stopping = anyio.Event()
    mock_tg = MagicMock()
    mock_tg.start_soon = lambda fn, *a, **k: None

    await registry.start(RunContext(tg=mock_tg, stopping=stopping))

    goto_page = await registry.get_action("deckr.plugin.builtin.gotopage")
    assert goto_page is not None
    assert goto_page.host_id == BUILTIN_ACTION_PROVIDER_ID
