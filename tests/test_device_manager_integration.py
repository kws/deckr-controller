"""DeviceManager integration tests. Uses mock devices (no VirtualDevice)."""

from unittest.mock import AsyncMock, MagicMock

import anyio
import pytest
import pytest_asyncio
from conftest import LaneHarness
from deckr.contracts.messages import DeckrMessage
from deckr.hardware.descriptors import (
    DECKR_INPUT_BUTTON,
    DECKR_INPUT_ENCODER,
    DECKR_INPUT_TOUCH,
    DECKR_OUTPUT_RASTER,
    CapabilityDescriptor,
    ControlDescriptor,
    ControlGeometry,
    DeviceDescriptor,
    DeviceRef,
)
from deckr.pluginhost.messages import (
    context_subject,
    controller_address,
    host_address,
    plugin_message,
)
from invariant import Node, SubGraphNode, dump_graph_output_data_uri
from invariant.params import ref

from deckr.controller._device_manager import DeviceManager
from deckr.controller._render import RenderResult
from deckr.controller.config._data import Control, DeviceConfig, Page, Profile
from deckr.controller.plugin.provider import ActionMetadata

CONTROLLER_ID = "controller-main"
CONTROLLER_ADDR = controller_address(CONTROLLER_ID)
HOST_ID = "python"
HOST_ADDR = host_address(HOST_ID)


def _plugin_bus() -> LaneHarness:
    return LaneHarness("plugin_messages", default_endpoint=HOST_ADDR)


def _plugin_command(
    message_type: str,
    payload: dict | None = None,
    *,
    config_id: str = "test-device",
    context_id: str,
    action_instance_id: str,
    binding_id: str,
    page_session_id: str | None = None,
) -> DeckrMessage:
    return plugin_message(
        sender=HOST_ADDR,
        recipient=CONTROLLER_ADDR,
        message_type=message_type,
        body=payload or {},
        subject=context_subject(
            context_id,
            config_id=config_id,
            action_instance_id=action_instance_id,
            binding_id=binding_id,
            page_session_id=page_session_id,
        ),
    )


async def _plugin_command_for_active_binding(
    manager: DeviceManager,
    message_type: str,
    payload: dict | None = None,
    *,
    slot_id: str = "0,0",
) -> DeckrMessage:
    ctx = await manager.action_contexts.get(slot_id)
    assert ctx is not None
    return _plugin_command(
        message_type,
        payload,
        context_id=ctx.id,
        action_instance_id=ctx.action_instance_id,
        binding_id=ctx.binding_id,
        page_session_id=ctx.page_session_id,
    )


def _make_slot(
    slot_id: str,
    row: int = 0,
    col: int = 0,
    slot_type: str = "key",
    gestures: list[str] | None = None,
    has_display: bool = True,
) -> ControlDescriptor:
    if gestures is None:
        gestures = ["key_down", "key_up"]
    input_capabilities = []
    if "key_down" in gestures or "key_up" in gestures:
        input_capabilities.append(
            CapabilityDescriptor(
                capabilityId="button.momentary",
                family=DECKR_INPUT_BUTTON,
                type="momentary",
                direction="input",
                access=("emits",),
                eventTypes=("down", "up"),
            )
        )
    if "press" in gestures or "key_down" in gestures or "key_up" in gestures:
        input_capabilities.append(
            CapabilityDescriptor.model_validate(
                {
                    "capabilityId": "button.press",
                    "family": DECKR_INPUT_BUTTON,
                    "type": "activation",
                    "direction": "input",
                    "access": ["emits"],
                    "eventTypes": ["press"],
                }
            )
        )
    if "encoder_rotate" in gestures:
        input_capabilities.append(
            CapabilityDescriptor(
                capabilityId="encoder.relative",
                family=DECKR_INPUT_ENCODER,
                type="relative",
                direction="input",
                access=("emits",),
                eventTypes=("rotate",),
            )
        )
    if "touch_tap" in gestures or "touch_swipe" in gestures:
        event_types = []
        if "touch_tap" in gestures:
            event_types.append("tap")
        if "touch_swipe" in gestures:
            event_types.append("swipe")
        input_capabilities.append(
            CapabilityDescriptor(
                capabilityId="touch.gesture",
                family=DECKR_INPUT_TOUCH,
                type="gesture",
                direction="input",
                access=("emits",),
                eventTypes=tuple(event_types),
            )
        )
    output_capabilities = []
    if has_display:
        output_capabilities.append(
            CapabilityDescriptor.model_validate(
                {
                    "capabilityId": "raster.bitmap",
                    "family": DECKR_OUTPUT_RASTER,
                    "type": "bitmap",
                    "direction": "output",
                    "access": ["settable"],
                    "commandTypes": ["set_frame", "clear"],
                    "constraints": [
                        {
                            "type": "fixed",
                            "subject": "width",
                            "value": 72,
                            "unit": "pixel",
                        },
                        {
                            "type": "fixed",
                            "subject": "height",
                            "value": 72,
                            "unit": "pixel",
                        },
                    ],
                }
            )
        )
    return ControlDescriptor(
        controlId=slot_id,
        kind=slot_type,
        geometry=ControlGeometry(x=col, y=row, width=1, height=1, unit="grid"),
        inputCapabilities=tuple(input_capabilities),
        outputCapabilities=tuple(output_capabilities),
    )


def _make_mock_device(
    device_id: str = "test-device", slots: list[ControlDescriptor] | None = None
) -> DeviceDescriptor:
    """Create device metadata for controller tests."""
    if slots is None:
        slots = [_make_slot("0,0"), _make_slot("1,0")]
    return DeviceDescriptor(
        deviceId=device_id,
        displayName="Test Device",
        fingerprint=f"fingerprint:{device_id}",
        controls=tuple(slots),
    )


def _hardware_ref(device: DeviceDescriptor) -> DeviceRef:
    return DeviceRef(
        managerId="manager-main",
        deviceId=device.device_id,
    )


class FakeHardwareCommandService:
    def __init__(self):
        self.set_raster_frame = AsyncMock()
        self.clear_raster = AsyncMock()
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
            binding_id=request.binding_id,
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
                                selector={"control_id": "0,0"},
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
    """Capability bindingOutput writes the selected raster capability."""
    from deckr.pluginhost.messages import BINDING_OUTPUT

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
        baseline_calls = command_service.set_raster_frame.call_count
        ctx = await manager.action_contexts.get("0,0")
        assert ctx is not None
        binding = ctx.metadata.model_copy(update={"output_generation": 1})
        msg = await _plugin_command_for_active_binding(
            manager,
            BINDING_OUTPUT,
            {
                "binding": binding.model_dump(
                    by_alias=True,
                    exclude_none=True,
                    mode="json",
                ),
                "capability": {
                    "deviceRef": {
                        "managerId": "manager-main",
                        "deviceId": "test-device",
                    },
                    "controlId": "0,0",
                    "capabilityId": "raster.bitmap",
                },
                "commandType": "set_frame",
                "params": {"image": "ZnJhbWU=", "encoding": "jpeg"},
                "generation": 1,
            },
        )
        with anyio.fail_after(0.2):
            await manager.handle_command(msg)

        with anyio.fail_after(5.0):
            while command_service.set_raster_frame.call_count <= baseline_calls:
                await anyio.sleep(0.01)
        tg.cancel_scope.cancel()

    assert command_service.set_raster_frame.call_count > baseline_calls
    call_args = command_service.set_raster_frame.call_args
    assert call_args[0][0] == "test-device"
    assert call_args[0][1] == "0,0"
    assert call_args[0][2] == "raster.bitmap"
    assert call_args[0][3] == b"frame"


@pytest.mark.asyncio
async def test_set_image_last_write_wins_same_slot(
    device_config_set_image, persistence_tmp_dir
):
    """bindingOutput rejects stale or mismatched output generations."""
    from deckr.pluginhost.messages import BINDING_OUTPUT

    device = _make_mock_device()
    plugin_bus = _plugin_bus()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=ActionMetadata(
            uuid=SetImageOnAppearAction.uuid,
            host_id="python",
        )
    )
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
        ctx = await manager.action_contexts.get("0,0")
        assert ctx is not None
        binding = ctx.metadata.model_copy(update={"output_generation": 1})
        msg = await _plugin_command_for_active_binding(
            manager,
            BINDING_OUTPUT,
            {
                "binding": binding.model_dump(
                    by_alias=True,
                    exclude_none=True,
                    mode="json",
                ),
                "capability": {
                    "deviceRef": {
                        "managerId": "manager-main",
                        "deviceId": "test-device",
                    },
                    "controlId": "0,0",
                    "capabilityId": "raster.bitmap",
                },
                "commandType": "set_frame",
                "params": {"image": "ZnJhbWU=", "encoding": "jpeg"},
                "generation": 2,
            },
        )

        await manager.handle_command(msg)

        command_service.set_raster_frame.assert_not_awaited()
        tg.cancel_scope.cancel()


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
                                selector={"control_id": "0,0"},
                                action=NoopAction.uuid,
                                settings={},
                            )
                        ]
                    ),
                    Page(
                        controls=[
                            Control(
                                selector={"control_id": "0,0"},
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
                                selector={"control_id": "0,0"},
                                action=NoopAction.uuid,
                                settings={},
                            ),
                            Control(
                                selector={"control_id": "1,0"},
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
async def test_config_reload_clears_runtime_settings_overlay(persistence_tmp_dir):
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
                                selector={"control_id": "0,0"},
                                action=NoopAction.uuid,
                                settings={
                                    "label": "from-config",
                                    "nested": {"role": {"page": "root"}},
                                },
                            )
                        ]
                    )
                ],
            )
        ],
    )
    reloaded_config = DeviceConfig(
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
                                selector={"control_id": "0,0"},
                                action=NoopAction.uuid,
                                settings={"label": "from-reload"},
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
        ctx = await manager.action_contexts.get("0,0")
        assert ctx is not None
        settings = await ctx.plugin_context.get_settings()
        assert settings.label == "from-config"
        assert settings.nested == {"role": {"page": "root"}}

        await ctx.plugin_context.set_settings({"label": "runtime", "extra": True})
        runtime_settings = await ctx.plugin_context.get_settings()
        assert runtime_settings.label == "runtime"
        assert runtime_settings.extra is True

        await manager._on_config_changed(reloaded_config)
        reloaded_ctx = await manager.action_contexts.get("0,0")
        assert reloaded_ctx is not None
        reloaded_settings = await reloaded_ctx.plugin_context.get_settings()
        assert vars(reloaded_settings) == {"label": "from-reload"}


@pytest.mark.asyncio
async def test_clear_page_can_skip_hardware_output_for_disconnect(persistence_tmp_dir):
    device = _make_mock_device()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=ActionMetadata(
            uuid=NoopAction.uuid,
            host_id="python",
        )
    )
    command_service = FakeHardwareCommandService()
    manager = DeviceManager(
        controller_id=CONTROLLER_ID,
        device=device,
        hardware_ref=_hardware_ref(device),
        command_service=command_service,
        config=DeviceConfig(
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
                                    selector={"control_id": "0,0"},
                                    action=NoopAction.uuid,
                                    settings={},
                                )
                            ]
                        )
                    ],
                )
            ],
        ),
        manager=registry,
        plugin_bus=_plugin_bus(),
        start_soon=lambda fn, *a, **k: None,
    )

    await manager.set_page(profile="default", page=0)
    assert await manager.action_contexts.get("0,0") is not None
    command_service.clear_raster.reset_mock()
    command_service.clear_raster.side_effect = LookupError("No live hardware route")

    await manager.clear_page(clear_outputs=False)

    command_service.clear_raster.assert_not_awaited()
    assert await manager.action_contexts.get("0,0") is None


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
                                selector={"control_id": "0,0"},
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
                                selector={"control_id": "0,0"},
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
        command_service.set_raster_frame.reset_mock()

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

        tg.cancel_scope.cancel()
