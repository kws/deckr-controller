"""Tests for PluginService aggregator and plugin hosts."""

import pytest
import anyio
from unittest.mock import MagicMock

from deckr.pluginhost.messages import (
    ACTIONS_REGISTERED,
    ACTIONS_UNREGISTERED,
    ActionDescriptor,
    ALL_CONTROLLERS,
    ALL_HOSTS,
    REQUEST_ACTIONS,
    HostMessage,
    controller_address,
    host_address,
)
from deckr.controller.plugin.action_registry import ActionRegistry
from deckr.controller.plugin.events import ActionsChangedEvent
from deckr.transports.bus import EventBus
from deckr.core.component import RunContext
from deckr.python_plugin.interface import PluginAction

CONTROLLER_ID = "controller-main"
CONTROLLER_ADDR = controller_address(CONTROLLER_ID)


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
    bus = EventBus()
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
    bus = EventBus()
    registry = ActionRegistry(event_bus=bus, controller_id=CONTROLLER_ID)
    stopping = anyio.Event()
    mock_tg = MagicMock()
    mock_tg.start_soon = lambda fn, *a, **k: None
    ctx = RunContext(tg=mock_tg, stopping=stopping)
    await registry.start(ctx)

    msg = HostMessage(
        from_id=host_address("test_host"),
        to_id=CONTROLLER_ADDR,
        type=ACTIONS_REGISTERED,
        payload={
            "hostId": "test_host",
            "actionUuids": [StubAction.uuid],
            "actions": [
                {
                    "uuid": StubAction.uuid,
                }
            ],
        },
    )
    await registry._handle_actions_registered(msg)

    meta = await registry.get_action(StubAction.uuid)
    assert meta is not None
    assert meta.uuid == StubAction.uuid
    assert meta.host_id == "test_host"


@pytest.mark.asyncio
async def test_actions_registered_with_all_controllers_populates_registry():
    """actionsRegistered with to=all_controllers is handled by controller."""
    bus = EventBus()
    registry = ActionRegistry(event_bus=bus, controller_id=CONTROLLER_ID)
    stopping = anyio.Event()
    mock_tg = MagicMock()
    mock_tg.start_soon = lambda fn, *a, **k: None
    ctx = RunContext(tg=mock_tg, stopping=stopping)
    await registry.start(ctx)

    msg = HostMessage(
        from_id=host_address("test_host"),
        to_id=ALL_CONTROLLERS,
        type=ACTIONS_REGISTERED,
        payload={
            "hostId": "test_host",
            "actionUuids": [StubAction.uuid],
            "actions": [
                {
                    "uuid": StubAction.uuid,
                }
            ],
        },
    )
    await registry._handle_actions_registered(msg)

    meta = await registry.get_action(StubAction.uuid)
    assert meta is not None
    assert meta.uuid == StubAction.uuid
    assert meta.host_id == "test_host"


@pytest.mark.asyncio
async def test_actions_unregistered_removes_from_registry():
    """actionsUnregistered message removes actions from registry."""
    bus = EventBus()
    registry = ActionRegistry(event_bus=bus, controller_id=CONTROLLER_ID)
    stopping = anyio.Event()
    mock_tg = MagicMock()
    mock_tg.start_soon = lambda fn, *a, **k: None
    ctx = RunContext(tg=mock_tg, stopping=stopping)
    await registry.start(ctx)

    await registry._handle_actions_registered(
        HostMessage(
            from_id=host_address("test_host"),
            to_id=CONTROLLER_ADDR,
            type=ACTIONS_REGISTERED,
            payload={
                "hostId": "test_host",
                "actionUuids": [StubAction.uuid],
                "actions": [
                    {
                        "uuid": StubAction.uuid,
                    }
                ],
            },
        )
    )
    assert await registry.get_action(StubAction.uuid) is not None

    await registry._handle_actions_unregistered(
        HostMessage(
            from_id=host_address("test_host"),
            to_id=CONTROLLER_ADDR,
            type=ACTIONS_UNREGISTERED,
            payload={"hostId": "test_host", "actionUuids": [StubAction.uuid]},
        )
    )
    assert await registry.get_action(StubAction.uuid) is None


@pytest.mark.asyncio
async def test_actions_registered_emits_actions_changed_event():
    """actionsRegistered message causes ActionsChangedEvent to be emitted on the bus."""
    bus = EventBus()
    received_events = []

    async def capture_events():
        async with bus.subscribe() as stream:
            async for envelope in stream:
                event = envelope.message
                if isinstance(event, ActionsChangedEvent):
                    received_events.append(event)

    registry = ActionRegistry(event_bus=bus, controller_id=CONTROLLER_ID)
    stopping = anyio.Event()

    async with anyio.create_task_group() as tg:
        ctx = RunContext(tg=tg, stopping=stopping)
        await registry.start(ctx)
        tg.start_soon(capture_events)
        await anyio.sleep(0.01)

        msg = HostMessage(
            from_id=host_address("test_host"),
            to_id=ALL_CONTROLLERS,
            type=ACTIONS_REGISTERED,
            payload={
                "hostId": "test_host",
                "actionUuids": [StubAction.uuid],
                "actions": [
                    {
                        "uuid": StubAction.uuid,
                    }
                ],
            },
        )
        await bus.send(msg)
        await anyio.sleep(0.05)

        tg.cancel_scope.cancel()

    assert len(received_events) == 1
    assert received_events[0].registered == [f"test_host::{StubAction.uuid}"]
    assert received_events[0].unregistered == []


@pytest.mark.asyncio
async def test_actions_unregistered_emits_actions_changed_event():
    """actionsUnregistered message causes ActionsChangedEvent to be emitted on the bus."""
    bus = EventBus()
    received_events = []

    async def capture_events():
        async with bus.subscribe() as stream:
            async for envelope in stream:
                event = envelope.message
                if isinstance(event, ActionsChangedEvent):
                    received_events.append(event)

    registry = ActionRegistry(event_bus=bus, controller_id=CONTROLLER_ID)
    stopping = anyio.Event()

    async with anyio.create_task_group() as tg:
        ctx = RunContext(tg=tg, stopping=stopping)
        await registry.start(ctx)
        tg.start_soon(capture_events)
        await anyio.sleep(0.01)

        # First register the action
        await bus.send(
            HostMessage(
                from_id=host_address("test_host"),
                to_id=ALL_CONTROLLERS,
                type=ACTIONS_REGISTERED,
                payload={
                    "hostId": "test_host",
                    "actionUuids": [StubAction.uuid],
                    "actions": [{"uuid": StubAction.uuid}],
                },
            )
        )
        with anyio.fail_after(1.0):
            while len(received_events) < 1:
                await anyio.sleep(0.01)
        assert received_events[0].registered == [f"test_host::{StubAction.uuid}"]
        received_events.clear()

        # Then unregister
        await bus.send(
            HostMessage(
                from_id=host_address("test_host"),
                to_id=ALL_CONTROLLERS,
                type=ACTIONS_UNREGISTERED,
                payload={"hostId": "test_host", "actionUuids": [StubAction.uuid]},
            )
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
    from deckr.pluginhost.messages import (
        HostMessage,
        ACTIONS_REGISTERED,
        ACTIONS_UNREGISTERED,
    )

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
            payload = {
                "hostId": self.name,
                "actionUuids": [StubAction.uuid],
                "actions": [{"uuid": StubAction.uuid}],
            }
            msg = HostMessage(
                from_id=host_address(self.name),
                to_id=CONTROLLER_ADDR,
                type=ACTIONS_REGISTERED,
                payload=payload,
            )
            await self._event_bus.send(msg)

        async def _subscription_loop(self):
            if self._event_bus is None:
                return
            async with self._event_bus.subscribe() as stream:
                async for envelope in stream:
                    event = envelope.message
                    if not isinstance(event, HostMessage) or not event.for_host(
                        self.name
                    ):
                        continue
                    if event.type not in HOST_MSG_TYPES:
                        continue
                    # Minimal: only handle requestActions for this test
                    if event.type == "requestActions":
                        payload = {
                            "hostId": self.name,
                            "actionUuids": [StubAction.uuid],
                            "actions": [{"uuid": StubAction.uuid}],
                        }
                        resp = HostMessage(
                            from_id=host_address(self.name),
                            to_id=CONTROLLER_ADDR,
                            type=ACTIONS_REGISTERED,
                            payload=payload,
                        )
                        await self._event_bus.send(resp)

        async def stop(self):
            msg = HostMessage(
                from_id=host_address(self.name),
                to_id=CONTROLLER_ADDR,
                type=ACTIONS_UNREGISTERED,
                payload={"hostId": self.name, "actionUuids": [StubAction.uuid]},
            )
            await self._event_bus.send(msg)

    bus = EventBus()
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
    """ControllerService sends requestActions to all_hosts when it comes online."""
    sent_messages = []
    bus = EventBus()

    original_send = bus.send

    async def capture_send(msg):
        sent_messages.append(msg)
        await original_send(msg)

    bus.send = capture_send
    from deckr.controller._controller_service import ControllerService
    from deckr.controller.config import FileSystemConfigService
    from deckr.controller.settings import InMemorySettingsService

    registry = ActionRegistry(event_bus=bus, controller_id=CONTROLLER_ID)
    config_service = FileSystemConfigService()
    settings_service = InMemorySettingsService()
    controller = ControllerService(
        driver_bus=EventBus(),
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
        getattr(m, "type", None) == REQUEST_ACTIONS
        and getattr(m, "to_id", None) == ALL_HOSTS
        and getattr(m, "from_id", None) == CONTROLLER_ADDR
        for m in sent_messages
    )
    assert request_actions_sent, (
        f"Expected requestActions to all_hosts; got: {[(getattr(m, 'type'), getattr(m, 'to_id')) for m in sent_messages]}"
    )


@pytest.mark.asyncio
async def test_host_handles_request_actions_and_emits():
    """When host receives requestActions, it sends actionsRegistered on the bus."""
    from deckr.pluginhost.messages import HostMessage, ACTIONS_REGISTERED

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
                async for envelope in stream:
                    event = envelope.message
                    if not isinstance(event, HostMessage) or not event.for_host(
                        self.name
                    ):
                        continue
                    if event.type not in HOST_MSG_TYPES:
                        continue
                    if event.type == "requestActions":
                        emit_called.append(True)
                        payload = {
                            "hostId": self.name,
                            "actionUuids": [StubAction.uuid],
                            "actions": [{"uuid": StubAction.uuid}],
                        }
                        msg = HostMessage(
                            from_id=host_address(self.name),
                            to_id=CONTROLLER_ADDR,
                            type=ACTIONS_REGISTERED,
                            payload=payload,
                        )
                        await self._event_bus.send(msg)

        async def stop(self):
            pass

    bus = EventBus()
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

        # Send requestActions to all_hosts (simulates controller startup)
        request_msg = HostMessage(
            from_id=CONTROLLER_ADDR,
            to_id=ALL_HOSTS,
            type=REQUEST_ACTIONS,
            payload={},
        )
        await bus.send(request_msg)
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
    from deckr.controller.config import FileSystemConfigService
    from deckr.controller.settings import InMemorySettingsService

    bus = EventBus()
    registry = ActionRegistry(event_bus=bus, controller_id=CONTROLLER_ID)
    config_service = FileSystemConfigService()
    settings_service = InMemorySettingsService()
    controller = ControllerService(
        driver_bus=EventBus(),
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
