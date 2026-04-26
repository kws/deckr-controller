"""DeviceManager integration tests. Uses mock devices (no VirtualDevice)."""

from unittest.mock import AsyncMock, MagicMock

import anyio
import pytest
import pytest_asyncio
from deckr.components import RunContext
from deckr.contracts.messages import DeckrMessage
from deckr.hardware import messages as hw_messages
from deckr.hardware.messages import (
    HardwareCoordinates,
    HardwareDevice,
    HardwareImageFormat,
    HardwareSlot,
)
from deckr.pluginhost.messages import (
    ACTIONS_REGISTERED,
    build_context_id,
    context_subject,
    controller_address,
    host_address,
    plugin_actions_subject,
    plugin_body_dict,
    plugin_message,
    plugin_message_for_host,
    subject_action_uuid,
    subject_context_id,
)
from deckr.transports.bus import EventBus
from invariant import Node, SubGraphNode, dump_graph_output_data_uri
from invariant.params import ref

from deckr.controller._device_manager import DeviceManager
from deckr.controller._render import RenderResult
from deckr.controller.config._data import Control, DeviceConfig, Page, Profile
from deckr.controller.plugin.provider import ActionMetadata
from deckr.controller.settings import SettingsTarget

CONTROLLER_ID = "controller-main"
CONTROLLER_ADDR = controller_address(CONTROLLER_ID)
HOST_ID = "python"
HOST_ADDR = host_address(HOST_ID)


def _plugin_bus() -> EventBus:
    return EventBus("plugin_messages")


def _context_id(config_id: str = "test-device", slot_id: str = "0,0") -> str:
    return build_context_id(CONTROLLER_ID, config_id, slot_id)


def _plugin_command(
    message_type: str,
    payload: dict | None = None,
    *,
    config_id: str = "test-device",
    slot_id: str = "0,0",
) -> DeckrMessage:
    context_id = _context_id(config_id, slot_id)
    return plugin_message(
        sender=HOST_ADDR,
        recipient=CONTROLLER_ADDR,
        message_type=message_type,
        body=payload or {},
        subject=context_subject(context_id),
    )


def _actions_registered_message(action_uuid: str) -> DeckrMessage:
    return plugin_message(
        sender=HOST_ADDR,
        recipient=CONTROLLER_ADDR,
        message_type=ACTIONS_REGISTERED,
        body={
            "actionUuids": [action_uuid],
            "actions": [{"uuid": action_uuid}],
        },
        subject=plugin_actions_subject(HOST_ID),
    )


def _make_slot(
    slot_id: str,
    row: int = 0,
    col: int = 0,
    slot_type: str = "key",
    gestures: list[str] | None = None,
    has_display: bool = True,
) -> HardwareSlot:
    if gestures is None:
        gestures = ["key_down", "key_up"]
    return HardwareSlot(
        id=slot_id,
        coordinates=HardwareCoordinates(column=col, row=row),
        image_format=HardwareImageFormat(width=72, height=72) if has_display else None,
        slot_type=slot_type,
        gestures=gestures,
    )


def _make_mock_device(
    device_id: str = "test-device", slots: list[HardwareSlot] | None = None
) -> HardwareDevice:
    """Create device metadata for controller tests."""
    if slots is None:
        slots = [_make_slot("0,0"), _make_slot("1,0")]
    return HardwareDevice(
        id=device_id,
        name="Test Device",
        hid=f"mock:{device_id}",
        fingerprint=f"fingerprint:{device_id}",
        slots=slots,
    )


def _hardware_ref(device: HardwareDevice) -> hw_messages.HardwareDeviceRef:
    return hw_messages.HardwareDeviceRef(
        manager_id="manager-main",
        device_id=device.id,
    )


class FakeHardwareCommandService:
    def __init__(self):
        self.set_image = AsyncMock()
        self.clear_slot = AsyncMock()
        self.sleep_screen = AsyncMock()
        self.wake_screen = AsyncMock()


def _solid_key_graph() -> SubGraphNode:
    """Minimal graph: solid dark gray background (canvas size from context)."""
    inner = {
        "bg": Node(
            op_name="gfx:create_solid",
            params={
                "size": ["${canvas.width}", "${canvas.height}"],
                "color": (51, 51, 51, 255),  # #333333
            },
            deps=["canvas"],
        ),
    }
    return SubGraphNode(
        params={"canvas": ref("canvas")}, deps=["canvas"], graph=inner, output="bg"
    )


def _solid_key_image() -> str:
    graph = _solid_key_graph()
    return dump_graph_output_data_uri(graph.graph, graph.output)


class ControlledFrameBackend:
    """Backend used by tests to control completion order without blocking commands."""

    def __init__(self):
        self.calls: list[int] = []
        self._events: dict[int, anyio.Event] = {}

    async def render(self, request) -> RenderResult:
        self.calls.append(request.generation)
        event = self._events.setdefault(request.generation, anyio.Event())
        await event.wait()
        return RenderResult(
            context_id=request.context_id,
            slot_id=request.slot_id,
            generation=request.generation,
            frame=f"frame-{request.generation}".encode(),
        )

    def release(self, generation: int) -> None:
        self._events.setdefault(generation, anyio.Event()).set()

    async def aclose(self) -> None:
        return


class SetImageOnAppearAction:
    """Minimal action that sets a graph-backed image on will_appear."""

    uuid: str = "test.virtual.setops"

    async def on_will_appear(self, event, context):
        await context.set_image(_solid_key_image())

    async def on_will_disappear(self, event, context):
        pass


class MockPlugin:
    def __init__(self, action):
        self._action = action
        self.name = "test_plugin"

    async def provides_actions(self):
        return [self._action.uuid]

    async def get_action(self, uuid: str):
        if uuid == self._action.uuid:
            return self._action
        return None


class MockPluginHost:
    """Message-native plugin host for testing."""

    def __init__(self, action, event_bus):
        self._action = action
        self._event_bus = event_bus
        self.name = HOST_ID
        self.controller_id = CONTROLLER_ID
        from deckr.core.util.anyio import AsyncMap

        self._plugins = AsyncMap()
        self._registry = None

    async def _ensure_plugins(self):
        if await self._plugins.get("test") is None:
            await self._plugins.set("test", MockPlugin(self._action))

    async def start(self, ctx):
        from deckr.plugin_hosts.python.context_registry import (
            PluginContextRegistry,
        )
        from deckr.plugin_hosts.python.message_context import MessagePluginContext

        def create_context(*, context_id: str, settings: dict | None = None):
            return MessagePluginContext(
                event_bus=self._event_bus,
                host_id=self.name,
                controller_id=self.controller_id,
                context_id=context_id,
                settings=settings,
            )

        self._registry = PluginContextRegistry(create_context)
        ctx.tg.start_soon(self._subscription_loop)
        await self._ensure_plugins()
        await self._event_bus.send(_actions_registered_message(self._action.uuid))

    async def _subscription_loop(self):
        from deckr.pluginhost.messages import (
            DIAL_ROTATE,
            HERE_ARE_SETTINGS,
            KEY_DOWN,
            KEY_UP,
            REQUEST_ACTIONS,
            TOUCH_SWIPE,
            TOUCH_TAP,
            WILL_APPEAR,
            WILL_DISAPPEAR,
        )
        from deckr.python_plugin.events import (
            DialRotate,
            KeyDown,
            KeyUp,
            TouchSwipe,
            TouchTap,
            WillAppear,
            WillDisappear,
        )

        def event_from_payload(event_type, payload):
            event_data = payload.get("event", payload)
            event_data = {**event_data, "context": context_id}
            return event_type.model_validate(event_data)

        HOST_MSG_TYPES = frozenset(
            {
                REQUEST_ACTIONS,
                HERE_ARE_SETTINGS,
                WILL_APPEAR,
                WILL_DISAPPEAR,
                KEY_UP,
                KEY_DOWN,
                DIAL_ROTATE,
                TOUCH_TAP,
                TOUCH_SWIPE,
            }
        )
        if self._event_bus is None or self._registry is None:
            return
        async with self._event_bus.subscribe() as stream:
            async for event in stream:
                if not isinstance(event, DeckrMessage) or not plugin_message_for_host(
                    event, self.name
                ):
                    continue
                if event.message_type not in HOST_MSG_TYPES:
                    continue
                msg = event
                if msg.message_type == REQUEST_ACTIONS:
                    await self._event_bus.send(
                        _actions_registered_message(self._action.uuid)
                    )
                elif msg.message_type == HERE_ARE_SETTINGS:
                    payload = plugin_body_dict(msg)
                    context_id = subject_context_id(msg.subject) or ""
                    settings = payload.get("settings", {})
                    if context_id:
                        self._registry.deliver_settings(context_id, settings)
                elif msg.message_type == WILL_APPEAR:
                    payload = plugin_body_dict(msg)
                    context_id = subject_context_id(msg.subject) or ""
                    event_data = payload.get("event", payload)
                    event_data = {**event_data, "context": context_id}
                    ev = WillAppear.model_validate(event_data)
                    action_uuid = subject_action_uuid(msg.subject) or ""
                    action = await self._get_action(action_uuid)
                    if action is None:
                        return
                    settings = payload.get("settings", {}) or {}
                    ctx = self._registry.get_or_create(
                        ev.context, settings=dict(settings)
                    )
                    await action.on_will_appear(ev, ctx)
                elif msg.message_type == WILL_DISAPPEAR:
                    payload = plugin_body_dict(msg)
                    context_id = subject_context_id(msg.subject) or ""
                    event_data = payload.get("event", payload)
                    event_data = {**event_data, "context": context_id}
                    ev = WillDisappear.model_validate(event_data)
                    action_uuid = subject_action_uuid(msg.subject) or ""
                    action = await self._get_action(action_uuid)
                    if action is None:
                        return
                    context_id = ev.context
                    ctx = self._registry.get(context_id)
                    if ctx is not None:
                        try:
                            await action.on_will_disappear(ev, ctx)
                        finally:
                            self._registry.remove(context_id)
                elif msg.message_type == KEY_UP:
                    payload = plugin_body_dict(msg)
                    context_id = subject_context_id(msg.subject) or ""
                    action_uuid = subject_action_uuid(msg.subject) or ""
                    ev = event_from_payload(KeyUp, payload)
                    action = await self._get_action(action_uuid)
                    if action is not None and hasattr(action, "on_key_up"):
                        ctx = self._registry.get(ev.context)
                        if ctx is None:
                            ctx = self._registry.get_or_create(
                                ev.context, settings=None
                            )
                        await action.on_key_up(ev, ctx)
                elif msg.message_type == KEY_DOWN:
                    payload = plugin_body_dict(msg)
                    context_id = subject_context_id(msg.subject) or ""
                    action_uuid = subject_action_uuid(msg.subject) or ""
                    ev = event_from_payload(KeyDown, payload)
                    action = await self._get_action(action_uuid)
                    if action is not None and hasattr(action, "on_key_down"):
                        ctx = self._registry.get(ev.context)
                        if ctx is None:
                            ctx = self._registry.get_or_create(
                                ev.context, settings=None
                            )
                        await action.on_key_down(ev, ctx)
                elif msg.message_type == DIAL_ROTATE:
                    payload = plugin_body_dict(msg)
                    context_id = subject_context_id(msg.subject) or ""
                    action_uuid = subject_action_uuid(msg.subject) or ""
                    ev = event_from_payload(DialRotate, payload)
                    action = await self._get_action(action_uuid)
                    if action is not None and hasattr(action, "on_dial_rotate"):
                        ctx = self._registry.get(ev.context)
                        if ctx is None:
                            ctx = self._registry.get_or_create(
                                ev.context, settings=None
                            )
                        await action.on_dial_rotate(ev, ctx)
                elif msg.message_type == TOUCH_TAP:
                    payload = plugin_body_dict(msg)
                    context_id = subject_context_id(msg.subject) or ""
                    action_uuid = subject_action_uuid(msg.subject) or ""
                    ev = event_from_payload(TouchTap, payload)
                    action = await self._get_action(action_uuid)
                    if action is not None and hasattr(action, "on_touch_tap"):
                        ctx = self._registry.get(ev.context)
                        if ctx is None:
                            ctx = self._registry.get_or_create(
                                ev.context, settings=None
                            )
                        await action.on_touch_tap(ev, ctx)
                elif msg.message_type == TOUCH_SWIPE:
                    payload = plugin_body_dict(msg)
                    context_id = subject_context_id(msg.subject) or ""
                    action_uuid = subject_action_uuid(msg.subject) or ""
                    ev = event_from_payload(TouchSwipe, payload)
                    action = await self._get_action(action_uuid)
                    if action is not None and hasattr(action, "on_touch_swipe"):
                        ctx = self._registry.get(ev.context)
                        if ctx is None:
                            ctx = self._registry.get_or_create(
                                ev.context, settings=None
                            )
                        await action.on_touch_swipe(ev, ctx)

    async def stop(self):
        pass

    async def _get_action(self, uuid: str):
        if uuid == self._action.uuid:
            return self._action
        return None


class MockPluginService:
    """Plugin manager that uses message bus with test actions."""

    def __init__(self, action=None, start_soon=None):
        self._action = action or SetImageOnAppearAction()
        self._bus = _plugin_bus()
        self._start_soon = start_soon
        self._adapter = None
        self._command_handler = None

    async def start(self, start_soon=None):
        start_soon = start_soon or self._start_soon
        if start_soon is None:
            return
        host = MockPluginHost(self._action, self._bus)
        stopping = anyio.Event()
        mock_tg = MagicMock()
        mock_tg.start_soon = lambda fn, *a, **k: None
        host_ctx = RunContext(tg=mock_tg, stopping=stopping)
        await host.start(host_ctx)
        start_soon(self._command_subscription_loop)

    async def _command_subscription_loop(self):
        from deckr.pluginhost.messages import COMMAND_MESSAGE_TYPES

        async with self._bus.subscribe() as stream:
            async for event in stream:
                if (
                    isinstance(event, DeckrMessage)
                    and event.message_type in COMMAND_MESSAGE_TYPES
                ):
                    if self._command_handler is not None:
                        await self._command_handler(event)

    async def get_action(self, uuid: str):
        if uuid == self._action.uuid:
            return ActionMetadata(
                uuid=self._action.uuid,
                host_id="python",
            )
        return None

    def register_command_handler(self, handler):
        self._command_handler = handler


class NoopAction:
    uuid: str = "test.virtual.noop"

    async def on_will_appear(self, event, context):
        pass

    async def on_will_disappear(self, event, context):
        pass


@pytest_asyncio.fixture
def device_config_set_image():
    """Config: one profile, one page, one control on slot 0,0 with SetImageOnAppearAction."""
    return DeviceConfig(
        id="test-device",
        name="Test Device",
        match={"fingerprint": "fingerprint:test-device"},
        profiles=[
            Profile(
                name="default",
                pages=[
                    Page(
                        controls=[
                            Control(
                                slot="0,0",
                                action=SetImageOnAppearAction.uuid,
                                settings={},
                            )
                        ]
                    )
                ],
            )
        ],
    )


@pytest.mark.asyncio
async def test_key_press_renders_to_device(
    device_config_set_image, persistence_tmp_dir
):
    """Graph-backed setImage returns promptly and the frame is written asynchronously."""
    from deckr.pluginhost.messages import SET_IMAGE

    device = _make_mock_device()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=ActionMetadata(
            uuid=SetImageOnAppearAction.uuid,
            host_id="python",
        )
    )
    plugin_bus = _plugin_bus()
    command_service = FakeHardwareCommandService()

    async with anyio.create_task_group() as tg:
        manager = DeviceManager(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=command_service,
            config=device_config_set_image,
            manager=registry,
            plugin_bus=plugin_bus,
            start_soon=tg.start_soon,
        )
        await manager.set_page(profile="default", page=0)
        baseline_calls = command_service.set_image.call_count
        msg = _plugin_command(SET_IMAGE, {"image": _solid_key_image()})
        with anyio.fail_after(0.2):
            await manager.handle_command(msg)

        with anyio.fail_after(5.0):
            while command_service.set_image.call_count <= baseline_calls:
                await anyio.sleep(0.01)
        tg.cancel_scope.cancel()

    assert command_service.set_image.call_count > baseline_calls
    call_args = command_service.set_image.call_args
    assert call_args[0][0] == "test-device"
    assert call_args[0][1] == "0,0"
    assert len(call_args[0][2]) > 0


@pytest.mark.asyncio
async def test_set_image_last_write_wins_same_slot(
    device_config_set_image, persistence_tmp_dir
):
    """Rapid successive graph-backed setImage commands only apply the newest frame."""
    from deckr.pluginhost.messages import SET_IMAGE

    device = _make_mock_device()
    plugin_bus = _plugin_bus()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=ActionMetadata(
            uuid=SetImageOnAppearAction.uuid,
            host_id="python",
        )
    )
    backend = ControlledFrameBackend()
    command_service = FakeHardwareCommandService()

    async with anyio.create_task_group() as tg:
        manager = DeviceManager(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=command_service,
            config=device_config_set_image,
            manager=registry,
            plugin_bus=plugin_bus,
            start_soon=tg.start_soon,
            render_backend=backend,
        )
        await manager.set_page(profile="default", page=0)
        initial_generation = manager._render_dispatcher._slots["0,0"].generation

        msg = _plugin_command(SET_IMAGE, {"image": _solid_key_image()})

        await manager.handle_command(msg)
        await manager.handle_command(msg)

        with anyio.fail_after(1.0):
            while backend.calls != [initial_generation + 1]:
                await anyio.sleep(0.01)

        backend.release(initial_generation + 1)
        with anyio.fail_after(1.0):
            while backend.calls != [initial_generation + 1, initial_generation + 2]:
                await anyio.sleep(0.01)

        backend.release(initial_generation + 2)
        with anyio.fail_after(1.0):
            while command_service.set_image.call_count != 1:
                await anyio.sleep(0.01)

        command_service.set_image.assert_awaited_once_with(
            "test-device", "0,0", f"frame-{initial_generation + 2}".encode()
        )
        tg.cancel_scope.cancel()


@pytest.mark.skip(reason="Hangs: MockPluginService/adapter blocks; needs investigation")
@pytest.mark.asyncio
async def test_key_down_event_delivered_to_plugin(
    device_config_set_image, persistence_tmp_dir
):
    """KeyDownMessage and KeyUpMessage are translated and delivered via EventTranslator."""
    received = []

    class RecordKeyEventsAction:
        uuid: str = "test.virtual.record"

        async def on_will_appear(self, event, context):
            await context.set_image(_solid_key_image())

        async def on_will_disappear(self, event, context):
            pass

        async def on_key_down(self, event, context):
            received.append(("key_down", event.context, event.slot_id))

        async def on_key_up(self, event, context):
            received.append(("key_up", event.context, event.slot_id))

    device = _make_mock_device()
    registry = MockPluginService(action=RecordKeyEventsAction(), start_soon=None)
    config = DeviceConfig(
        id="test-device",
        name="Test Device",
        match={"fingerprint": "fingerprint:test-device"},
        profiles=[
            Profile(
                name="default",
                pages=[
                    Page(
                        controls=[
                            Control(
                                slot="0,0",
                                action=RecordKeyEventsAction.uuid,
                                settings={},
                            )
                        ]
                    )
                ],
            )
        ],
    )
    async with anyio.create_task_group() as tg:
        await registry.start(tg.start_soon)
        manager = DeviceManager(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=FakeHardwareCommandService(),
            config=config,
            manager=registry,
            plugin_bus=registry._bus,
            start_soon=tg.start_soon,
        )
        registry.register_command_handler(manager.handle_command)
        await manager.set_page(profile="default", page=0)
        await anyio.sleep(0.1)
        await manager.on_event(
            hw_messages.KeyDownMessage(device_id="test-device", key_id="0,0")
        )
        await manager.on_event(
            hw_messages.KeyUpMessage(device_id="test-device", key_id="0,0")
        )
    expected_context = build_context_id(CONTROLLER_ID, "test-device", "0,0")
    assert ("key_down", expected_context, "0,0") in received
    assert ("key_up", expected_context, "0,0") in received


@pytest.mark.asyncio
async def test_settings_isolated_by_page_same_slot(persistence_tmp_dir):
    """Same slot on different pages keeps separate settings."""
    device = _make_mock_device()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=ActionMetadata(
            uuid=NoopAction.uuid,
            host_id="python",
        )
    )
    plugin_bus = _plugin_bus()

    config = DeviceConfig(
        id="test-device",
        name="Test Device",
        match={"fingerprint": "fingerprint:test-device"},
        profiles=[
            Profile(
                name="default",
                pages=[
                    Page(
                        controls=[
                            Control(
                                slot="0,0",
                                action=NoopAction.uuid,
                                settings={},
                            )
                        ]
                    ),
                    Page(
                        controls=[
                            Control(
                                slot="0,0",
                                action=NoopAction.uuid,
                                settings={},
                            )
                        ]
                    ),
                ],
            )
        ],
    )

    async with anyio.create_task_group():

        def start_soon(*args, **kwargs):
            pass

        manager = DeviceManager(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=FakeHardwareCommandService(),
            config=config,
            manager=registry,
            plugin_bus=plugin_bus,
            start_soon=start_soon,
        )
        await manager.set_page(profile="default", page=0)
        await anyio.sleep(0.05)
        page0_ctx = await manager.action_contexts.get("0,0")
        await page0_ctx.plugin_context.set_settings({"marker": "page0"})

        await manager.set_page(profile="default", page=1)
        page1_ctx = await manager.action_contexts.get("0,0")
        await page1_ctx.plugin_context.set_settings({"marker": "page1"})

        await manager.set_page(profile="default", page=0)
        page0_ctx_reload = await manager.action_contexts.get("0,0")
        page0_settings = await page0_ctx_reload.plugin_context.get_settings()
        assert page0_settings.marker == "page0"

        await manager.set_page(profile="default", page=1)
        page1_ctx_reload = await manager.action_contexts.get("0,0")
        page1_settings = await page1_ctx_reload.plugin_context.get_settings()
        assert page1_settings.marker == "page1"


@pytest.mark.asyncio
async def test_settings_isolated_by_slot_same_action(persistence_tmp_dir):
    """Same action on different slots keeps separate settings."""
    device = _make_mock_device()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=ActionMetadata(
            uuid=NoopAction.uuid,
            host_id="python",
        )
    )
    plugin_bus = _plugin_bus()

    config = DeviceConfig(
        id="test-device",
        name="Test Device",
        match={"fingerprint": "fingerprint:test-device"},
        profiles=[
            Profile(
                name="default",
                pages=[
                    Page(
                        controls=[
                            Control(
                                slot="0,0",
                                action=NoopAction.uuid,
                                settings={},
                            ),
                            Control(
                                slot="1,0",
                                action=NoopAction.uuid,
                                settings={},
                            ),
                        ]
                    )
                ],
            )
        ],
    )

    async with anyio.create_task_group():

        def start_soon(*args, **kwargs):
            pass

        manager = DeviceManager(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=FakeHardwareCommandService(),
            config=config,
            manager=registry,
            plugin_bus=plugin_bus,
            start_soon=start_soon,
        )
        await manager.set_page(profile="default", page=0)
        await anyio.sleep(0.05)
        slot_a = await manager.action_contexts.get("0,0")
        slot_b = await manager.action_contexts.get("1,0")
        await slot_a.plugin_context.set_settings({"slot_marker": "A"})
        await slot_b.plugin_context.set_settings({"slot_marker": "B"})

        await manager.set_page(profile="default", page=0)
        slot_a_reload = await manager.action_contexts.get("0,0")
        slot_b_reload = await manager.action_contexts.get("1,0")
        settings_a = await slot_a_reload.plugin_context.get_settings()
        settings_b = await slot_b_reload.plugin_context.get_settings()
        assert settings_a.slot_marker == "A"
        assert settings_b.slot_marker == "B"


@pytest.mark.asyncio
async def test_set_page_reconciles_and_prunes_stale_settings(
    device_config_set_image, persistence_tmp_dir
):
    """set_page triggers _reconcile_persistence and prunes stale keys."""
    device = _make_mock_device()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=ActionMetadata(
            uuid=SetImageOnAppearAction.uuid,
            host_id="python",
        )
    )
    plugin_bus = _plugin_bus()
    async with anyio.create_task_group():

        def start_soon(*args, **kwargs):
            pass

        manager = DeviceManager(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=FakeHardwareCommandService(),
            config=device_config_set_image,
            manager=registry,
            plugin_bus=plugin_bus,
            start_soon=start_soon,
        )
        stale_target = SettingsTarget.for_context(
            controller_id=CONTROLLER_ID,
            config_id="test-device",
            profile_id="stale-profile",
            page_id="9",
            slot_id="0,0",
            action_uuid="stale.action",
        )
        await manager._settings_service.merge(stale_target, {"stale": True})
        assert await manager._settings_service.get(stale_target) == {"stale": True}

        await manager.set_page(profile="default", page=0)

        assert await manager._settings_service.get(stale_target) == {}


class ConfigurableActionRegistry:
    """Registry that can add/remove actions for testing on_actions_changed.

    Uses qualified IDs (host_id::action_uuid) internally to match ActionRegistry.
    """

    def __init__(self):
        self._actions: dict[str, ActionMetadata] = {}

    def _qualified_id(self, host_id: str, action_uuid: str) -> str:
        return f"{host_id}::{action_uuid}"

    async def get_action(self, address: str) -> ActionMetadata | None:
        if "::" in address:
            return self._actions.get(address)
        for key, meta in self._actions.items():
            if key.endswith(f"::{address}"):
                return meta
        return None

    def add_action(self, action_uuid: str, meta: ActionMetadata) -> None:
        qualified = self._qualified_id(meta.host_id, action_uuid)
        self._actions[qualified] = meta

    def remove_action(self, action_uuid: str, host_id: str) -> None:
        qualified = self._qualified_id(host_id, action_uuid)
        self._actions.pop(qualified, None)

    def get_builtin_action(self, uuid: str):
        return None


ACTION_X_UUID = "test.action.x"


@pytest.mark.asyncio
async def test_on_actions_changed_registered_resolves_unavailable_slot(
    persistence_tmp_dir,
):
    """When action becomes available, on_actions_changed creates context for previously unavailable slot."""
    device = _make_mock_device()
    plugin_bus = _plugin_bus()
    registry = ConfigurableActionRegistry()
    # Initially no action - slot will show unavailable
    config = DeviceConfig(
        id="test-device",
        name="Test Device",
        match={"fingerprint": "fingerprint:test-device"},
        profiles=[
            Profile(
                name="default",
                pages=[
                    Page(
                        controls=[
                            Control(
                                slot="0,0",
                                action=ACTION_X_UUID,
                                settings={},
                            )
                        ]
                    )
                ],
            )
        ],
    )

    async with anyio.create_task_group():

        def start_soon(*args, **kwargs):
            pass

        manager = DeviceManager(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=FakeHardwareCommandService(),
            config=config,
            manager=registry,
            plugin_bus=plugin_bus,
            start_soon=start_soon,
        )
        await manager.set_page(profile="default", page=0)
        await anyio.sleep(0.05)

        # Slot should be unavailable (no context)
        ctx_before = await manager.action_contexts.get("0,0")
        assert ctx_before is None

        # Add action and notify
        registry.add_action(
            ACTION_X_UUID,
            ActionMetadata(
                uuid=ACTION_X_UUID,
                host_id="test_host",
            ),
        )
        await manager.on_actions_changed(
            registered=[f"test_host::{ACTION_X_UUID}"],
            unregistered=[],
        )

        # Slot should now have context
        ctx_after = await manager.action_contexts.get("0,0")
        assert ctx_after is not None
        assert ctx_after.action_uuid == ACTION_X_UUID


@pytest.mark.asyncio
async def test_on_actions_changed_unregistered_removes_context(persistence_tmp_dir):
    """When action becomes unavailable, on_actions_changed removes context and renders unavailable."""
    device = _make_mock_device()
    plugin_bus = _plugin_bus()
    registry = ConfigurableActionRegistry()
    registry.add_action(
        ACTION_X_UUID,
        ActionMetadata(
            uuid=ACTION_X_UUID,
            host_id="test_host",
        ),
    )
    config = DeviceConfig(
        id="test-device",
        name="Test Device",
        match={"fingerprint": "fingerprint:test-device"},
        profiles=[
            Profile(
                name="default",
                pages=[
                    Page(
                        controls=[
                            Control(
                                slot="0,0",
                                action=ACTION_X_UUID,
                                settings={},
                            )
                        ]
                    )
                ],
            )
        ],
    )

    async with anyio.create_task_group() as tg:
        command_service = FakeHardwareCommandService()
        manager = DeviceManager(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=command_service,
            config=config,
            manager=registry,
            plugin_bus=plugin_bus,
            start_soon=tg.start_soon,
        )
        await manager.set_page(profile="default", page=0)
        await anyio.sleep(0.05)

        # Slot should have context
        ctx_before = await manager.action_contexts.get("0,0")
        assert ctx_before is not None

        # Clear mock to isolate on_actions_changed effects
        command_service.set_image.reset_mock()

        # Remove action from registry to simulate unregister (otherwise the
        # "registered" handling would re-resolve and recreate the context)
        registry.remove_action(ACTION_X_UUID, "test_host")

        # Notify that action was unregistered (qualified ID)
        await manager.on_actions_changed(
            registered=[], unregistered=[f"test_host::{ACTION_X_UUID}"]
        )

        # Slot should no longer have context
        ctx_after = await manager.action_contexts.get("0,0")
        assert ctx_after is None

        # Unavailable overlay should have been rendered
        with anyio.fail_after(1.0):
            while command_service.set_image.call_count != 1:
                await anyio.sleep(0.01)
        command_service.set_image.assert_called_once()
        assert command_service.set_image.call_args[0][0] == "test-device"
        assert command_service.set_image.call_args[0][1] == "0,0"
        tg.cancel_scope.cancel()
