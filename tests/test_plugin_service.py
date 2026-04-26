"""Tests for PluginService aggregator and plugin hosts."""

from unittest.mock import MagicMock

import anyio
import pytest
from deckr.contracts.messages import controllers_broadcast, plugin_hosts_broadcast
from deckr.core.component import RunContext
from deckr.pluginhost.messages import (
    ACTIONS_REGISTERED,
    ACTIONS_UNREGISTERED,
    REQUEST_ACTIONS,
    ActionDescriptor,
    DeckrMessage,
    controller_address,
    host_address,
    plugin_actions_subject,
    plugin_message,
    plugin_message_for_host,
)
from deckr.python_plugin.interface import PluginAction
from deckr.transports.bus import EventBus

from deckr.controller.plugin.action_registry import ActionRegistry
from deckr.controller.plugin.events import ActionsChangedEvent

CONTROLLER_ID = "controller-main"
CONTROLLER_ADDR = controller_address(CONTROLLER_ID)


def _plugin_bus() -> EventBus:
    return EventBus("plugin_messages")


def _hardware_bus() -> EventBus:
    return EventBus("hardware_events")


def _actions_payload(host_id: str = "test_host") -> dict:
    return {
        "hostId": host_id,
        "actionUuids": [StubAction.uuid],
        "actions": [{"uuid": StubAction.uuid}],
    }


def _actions_registered_message(
    *,
    host_id: str = "test_host",
    recipient=CONTROLLER_ADDR,
) -> DeckrMessage:
    return plugin_message(
        sender=host_address(host_id),
        recipient=recipient,
        message_type=ACTIONS_REGISTERED,
        payload=_actions_payload(host_id),
        subject=plugin_actions_subject(host_id),
    )


def _actions_unregistered_message(
    *,
    host_id: str = "test_host",
    recipient=CONTROLLER_ADDR,
) -> DeckrMessage:
    return plugin_message(
        sender=host_address(host_id),
        recipient=recipient,
        message_type=ACTIONS_UNREGISTERED,
        payload={"hostId": host_id, "actionUuids": [StubAction.uuid]},
        subject=plugin_actions_subject(host_id),
    )


def _request_actions_message() -> DeckrMessage:
    return plugin_message(
        sender=CONTROLLER_ADDR,
        recipient=plugin_hosts_broadcast(),
        message_type=REQUEST_ACTIONS,
        payload={},
        subject=plugin_actions_subject(),
    )


class StubAction:
    """Minimal PluginAction for testing."""

    uuid: str = "test.stub.action"

    async def on_will_appear(self, event, context):
        pass

    async def on_will_disappear(self, event, context):
        pass

    async def on_key_up(self, event, context):
        pass

    async def on_key_down(self, event, context):
        pass


class StubPluginHost:
    """Minimal PluginHost that returns a known action."""

    name: str = "stub"

    def __init__(self, action: PluginAction | None = None):
        self._action = action or StubAction()

    async def start(self, ctx) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def get_action(self, uuid: str) -> PluginAction | None:
        if uuid == self._action.uuid:
            return self._action
        return None

    async def emit_actions_registered(self, send) -> None:
        pass


@pytest.mark.asyncio
async def test_action_registry_aggregates_get_action():
    """ActionRegistry returns ActionMetadata from registry (populated by actionsRegistered)."""
    bus = _plugin_bus()
    registry = ActionRegistry(event_bus=bus, controller_id=CONTROLLER_ID)
    stopping = anyio.Event()
    mock_tg = MagicMock()
    mock_tg.start_soon = lambda fn, *a, **k: None
    ctx = RunContext(tg=mock_tg, stopping=stopping)
    await registry.start(ctx)

    # Manually populate registry (simulating actionsRegistered)
    registry._action_registry[f"stub::{StubAction.uuid}"] = (
        "stub",
        ActionDescriptor(uuid=StubAction.uuid),
    )

    meta = await registry.get_action(StubAction.uuid)
    assert meta is not None
    assert meta.uuid == StubAction.uuid
    assert meta.host_id == "stub"

    assert await registry.get_action("nonexistent.uuid") is None


@pytest.mark.asyncio
async def test_actions_registered_populates_registry():
    """actionsRegistered message populates action registry."""
    bus = _plugin_bus()
    registry = ActionRegistry(event_bus=bus, controller_id=CONTROLLER_ID)
    stopping = anyio.Event()
    mock_tg = MagicMock()
    mock_tg.start_soon = lambda fn, *a, **k: None
    ctx = RunContext(tg=mock_tg, stopping=stopping)
    await registry.start(ctx)

    msg = _actions_registered_message()
    await registry._handle_actions_registered(msg)

    meta = await registry.get_action(StubAction.uuid)
    assert meta is not None
    assert meta.uuid == StubAction.uuid
    assert meta.host_id == "test_host"


@pytest.mark.asyncio
async def test_actions_registered_with_controller_broadcast_populates_registry():
    """actionsRegistered with controller broadcast recipient is handled."""
    bus = _plugin_bus()
    registry = ActionRegistry(event_bus=bus, controller_id=CONTROLLER_ID)
    stopping = anyio.Event()
    mock_tg = MagicMock()
    mock_tg.start_soon = lambda fn, *a, **k: None
    ctx = RunContext(tg=mock_tg, stopping=stopping)
    await registry.start(ctx)

    msg = _actions_registered_message(recipient=controllers_broadcast())
    await registry._handle_actions_registered(msg)

    meta = await registry.get_action(StubAction.uuid)
    assert meta is not None
    assert meta.uuid == StubAction.uuid
    assert meta.host_id == "test_host"


@pytest.mark.asyncio
async def test_actions_unregistered_removes_from_registry():
    """actionsUnregistered message removes actions from registry."""
    bus = _plugin_bus()
    registry = ActionRegistry(event_bus=bus, controller_id=CONTROLLER_ID)
    stopping = anyio.Event()
    mock_tg = MagicMock()
    mock_tg.start_soon = lambda fn, *a, **k: None
    ctx = RunContext(tg=mock_tg, stopping=stopping)
    await registry.start(ctx)

    await registry._handle_actions_registered(
        _actions_registered_message()
    )
    assert await registry.get_action(StubAction.uuid) is not None

    await registry._handle_actions_unregistered(
        _actions_unregistered_message()
    )
    assert await registry.get_action(StubAction.uuid) is None


@pytest.mark.asyncio
async def test_actions_registered_emits_actions_changed_event():
    """actionsRegistered message publishes an ActionsChangedEvent through the callback."""
    bus = _plugin_bus()
    received_events = []

    async def on_actions_changed(event: ActionsChangedEvent) -> None:
        received_events.append(event)

    registry = ActionRegistry(
        event_bus=bus,
        controller_id=CONTROLLER_ID,
        on_actions_changed=on_actions_changed,
    )
    stopping = anyio.Event()

    async with anyio.create_task_group() as tg:
        ctx = RunContext(tg=tg, stopping=stopping)
        await registry.start(ctx)
        await anyio.sleep(0.01)

        msg = _actions_registered_message(recipient=controllers_broadcast())
        await bus.send(msg)
        await anyio.sleep(0.05)

        tg.cancel_scope.cancel()

    assert len(received_events) == 1
    assert received_events[0].registered == [f"test_host::{StubAction.uuid}"]
    assert received_events[0].unregistered == []


@pytest.mark.asyncio
async def test_actions_unregistered_emits_actions_changed_event():
    """actionsUnregistered message publishes an ActionsChangedEvent through the callback."""
    bus = _plugin_bus()
    received_events = []

    async def on_actions_changed(event: ActionsChangedEvent) -> None:
        received_events.append(event)

    registry = ActionRegistry(
        event_bus=bus,
        controller_id=CONTROLLER_ID,
        on_actions_changed=on_actions_changed,
    )
    stopping = anyio.Event()

    async with anyio.create_task_group() as tg:
        ctx = RunContext(tg=tg, stopping=stopping)
        await registry.start(ctx)
        await anyio.sleep(0.01)

        # First register the action
        await bus.send(_actions_registered_message(recipient=controllers_broadcast()))
        with anyio.fail_after(1.0):
            while len(received_events) < 1:
                await anyio.sleep(0.01)
        assert received_events[0].registered == [f"test_host::{StubAction.uuid}"]
        received_events.clear()

        # Then unregister
        await bus.send(
            _actions_unregistered_message(recipient=controllers_broadcast())
        )
        with anyio.fail_after(1.0):
            while len(received_events) < 1:
                await anyio.sleep(0.01)

        tg.cancel_scope.cancel()

    assert len(received_events) == 1
    assert received_events[0].registered == []
    assert received_events[0].unregistered == [f"test_host::{StubAction.uuid}"]


@pytest.mark.asyncio
async def test_host_lifecycle_register_then_unregister():
    """Full lifecycle: host emits actionsRegistered on start, actionsUnregistered on stop."""

    HOST_MSG_TYPES = frozenset(
        {
            "requestActions",
            "getAction",
            "willAppear",
            "willDisappear",
            "keyUp",
            "keyDown",
            "dialRotate",
            "touchTap",
            "touchSwipe",
        }
    )

    class LifecycleHost:
        name = "lifecycle_test"
        _event_bus = None

        def __init__(self, event_bus):
            self._event_bus = event_bus

        async def start(self, ctx):
            ctx.tg.start_soon(self._subscription_loop)
            await self._event_bus.send(_actions_registered_message(host_id=self.name))

        async def _subscription_loop(self):
            if self._event_bus is None:
                return
            async with self._event_bus.subscribe() as stream:
                async for event in stream:
                    if not isinstance(event, DeckrMessage) or not plugin_message_for_host(
                        event, self.name
                    ):
                        continue
                    if event.message_type not in HOST_MSG_TYPES:
                        continue
                    # Minimal: only handle requestActions for this test
                    if event.message_type == "requestActions":
                        await self._event_bus.send(
                            _actions_registered_message(host_id=self.name)
                        )

        async def stop(self):
            await self._event_bus.send(
                _actions_unregistered_message(host_id=self.name)
            )

    bus = _plugin_bus()
    registry = ActionRegistry(event_bus=bus, controller_id=CONTROLLER_ID)
    stopping = anyio.Event()
    mock_tg = MagicMock()
    mock_tg.start_soon = lambda fn, *a, **k: None
    host_ctx = RunContext(tg=mock_tg, stopping=stopping)
    await registry.start(host_ctx)
    host = LifecycleHost(bus)

    async with anyio.create_task_group() as tg:
        tg.start_soon(registry._subscription_loop)
        await anyio.sleep(0.01)
        await host.start(host_ctx)
        await anyio.sleep(0.05)

        meta = await registry.get_action(StubAction.uuid)
        assert meta is not None
        assert meta.host_id == "lifecycle_test"

        await host.stop()
        await anyio.sleep(0.05)
        assert await registry.get_action(StubAction.uuid) is None

        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_controller_sends_request_actions_on_startup():
    """ControllerService sends requestActions to plugin-host broadcast on startup."""
    sent_messages = []
    bus = _plugin_bus()

    original_send = bus.send

    async def capture_send(msg):
        sent_messages.append(msg)
        await original_send(msg)

    bus.send = capture_send
    from deckr.controller._controller_service import ControllerService
    from deckr.controller.config import FileBackedDeviceConfigService
    from deckr.controller.settings import InMemorySettingsService

    registry = ActionRegistry(event_bus=bus, controller_id=CONTROLLER_ID)
    config_service = FileBackedDeviceConfigService()
    settings_service = InMemorySettingsService()
    controller = ControllerService(
        driver_bus=_hardware_bus(),
        config_service=config_service,
        settings_service=settings_service,
        controller_id=CONTROLLER_ID,
        action_registry=registry,
        plugin_bus=bus,
    )
    stopping = anyio.Event()
    mock_tg = MagicMock()
    mock_tg.start_soon = lambda fn, *a, **k: None
    ctx = RunContext(tg=mock_tg, stopping=stopping)
    await registry.start(ctx)
    await controller.start(ctx)

    request_actions_sent = any(
        isinstance(m, DeckrMessage)
        and m.message_type == REQUEST_ACTIONS
        and m.recipient == plugin_hosts_broadcast()
        and m.sender == CONTROLLER_ADDR
        for m in sent_messages
    )
    assert request_actions_sent, (
        f"Expected requestActions broadcast to plugin hosts; got: {sent_messages}"
    )


@pytest.mark.asyncio
async def test_host_handles_request_actions_and_emits():
    """When host receives requestActions, it sends actionsRegistered on the bus."""

    emit_called = []
    HOST_MSG_TYPES = frozenset(
        {
            "requestActions",
            "getAction",
            "willAppear",
            "willDisappear",
            "keyUp",
            "keyDown",
            "dialRotate",
            "touchTap",
            "touchSwipe",
        }
    )

    class RequestActionsHost:
        name = "request_test"
        _event_bus = None

        def __init__(self, event_bus):
            self._event_bus = event_bus

        async def start(self, ctx):
            ctx.tg.start_soon(self._subscription_loop)

        async def _subscription_loop(self):
            if self._event_bus is None:
                return
            async with self._event_bus.subscribe() as stream:
                async for event in stream:
                    if not isinstance(event, DeckrMessage) or not plugin_message_for_host(
                        event, self.name
                    ):
                        continue
                    if event.message_type not in HOST_MSG_TYPES:
                        continue
                    if event.message_type == "requestActions":
                        emit_called.append(True)
                        await self._event_bus.send(
                            _actions_registered_message(host_id=self.name)
                        )

        async def stop(self):
            pass

    bus = _plugin_bus()
    registry = ActionRegistry(event_bus=bus, controller_id=CONTROLLER_ID)
    stopping = anyio.Event()
    mock_tg = MagicMock()
    mock_tg.start_soon = lambda fn, *a, **k: None
    ctx = RunContext(tg=mock_tg, stopping=stopping)
    await registry.start(ctx)

    async with anyio.create_task_group() as tg:
        tg.start_soon(registry._subscription_loop)
        await anyio.sleep(0.01)

        host_ctx = RunContext(tg=tg, stopping=stopping)
        host = RequestActionsHost(bus)
        await host.start(host_ctx)
        await anyio.sleep(0.02)

        # Send requestActions to plugin hosts (simulates controller startup).
        await bus.send(_request_actions_message())
        await anyio.sleep(0.05)

        assert len(emit_called) == 1, "Host should have sent actionsRegistered"
        meta = await registry.get_action(StubAction.uuid)
        assert meta is not None
        assert meta.host_id == "request_test"

        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_controller_empty_hosts_starts_ok():
    """ControllerService starts without plugin hosts (hosts are loaded by service_runner)."""
    from deckr.controller._controller_service import ControllerService
    from deckr.controller.config import FileBackedDeviceConfigService
    from deckr.controller.settings import InMemorySettingsService

    bus = _plugin_bus()
    registry = ActionRegistry(event_bus=bus, controller_id=CONTROLLER_ID)
    config_service = FileBackedDeviceConfigService()
    settings_service = InMemorySettingsService()
    controller = ControllerService(
        driver_bus=_hardware_bus(),
        config_service=config_service,
        settings_service=settings_service,
        controller_id=CONTROLLER_ID,
        action_registry=registry,
        plugin_bus=bus,
    )
    stopping = anyio.Event()

    async with anyio.create_task_group() as tg:
        ctx = RunContext(tg=tg, stopping=stopping)
        await registry.start(ctx)
        await controller.start(ctx)
        await anyio.sleep(0.05)

        # Builtin actions still work
        goto_page = await registry.get_action("deckr.plugin.builtin.gotopage")
        assert goto_page is not None

        stopping.set()
        await controller.stop()
        tg.cancel_scope.cancel()
