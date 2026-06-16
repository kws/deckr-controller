"""DeviceManager integration tests. Uses mock devices (no VirtualDevice)."""

from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

import anyio
import pytest
import pytest_asyncio
from conftest import LaneHarness
from deckr.actions.endpoints import action_provider_address
from deckr.actions.messages import (
    ACTION_INSTANCE_CREATED,
    ACTION_LIFECYCLE_REJECTED,
    BINDING_OUTPUT,
    CAPABILITY_INPUT,
    PAGE_SESSION_CLOSED,
    REPLACE_PAGE,
    SETTINGS_PATCH,
    SETTINGS_REQUEST,
    SETTINGS_SNAPSHOT,
    ActionAvailabilityEntry,
    ActionDescriptor,
    CapabilityInputBody,
    DynamicPageCommand,
    PageChildBindingDescriptor,
    PageChildBindingTarget,
    SettingsTargetRef,
    action_message,
    action_provider_instance_subject,
    context_subject,
)
from deckr.concord import (
    CONCORD_CONTRACT_BUCKET_POLICY,
    CONCORD_MAINTENANCE_BUCKET_POLICY,
    CONCORD_TOKEN_BUCKET_POLICY,
    Concord,
    ContractValidityStatus,
)
from deckr.contracts.messages import DeckrMessage, controller_address
from deckr.contracts.models import thaw_json
from deckr.hardware import messages as hw_messages
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
from deckr.lanes import EndpointSession
from invariant import Node, SubGraphNode, dump_graph_data_uri
from invariant.params import ref

from deckr.controller import _device_manager as device_manager_module
from deckr.controller._action_availability import (
    ActionAvailabilitySource,
    ActionAvailabilityState,
    ProviderActionKey,
)
from deckr.controller._action_interest import (
    ActionInterestSource,
    ActionInterestStrength,
)
from deckr.controller._action_provider_sessions import (
    ActionProviderSessionManager,
    ProviderSessionKey,
    ProviderSessionSnapshot,
    provider_session_key,
)
from deckr.controller._device_manager import DeviceManager
from deckr.controller._render import RenderResult
from deckr.controller.action_provider.events import (
    ActionCatalogChangedEvent,
    ProviderSessionSuccession,
)
from deckr.controller.action_provider.provider import ActionMetadata
from deckr.controller.config._data import Control, DeviceConfig, Page, Profile
from deckr.controller.settings import ConfigBackedSettingsService

CONTROLLER_ID = "controller-main"
CONTROLLER_ADDR = controller_address(CONTROLLER_ID)
PROVIDER_INSTANCE_ID = "python"
PROVIDER_ID = "test.provider"
PROVIDER_ADDR = action_provider_address(PROVIDER_INSTANCE_ID)
PROVIDER_SESSION_ID = "action-provider-session"


def _actions_bus() -> LaneHarness:
    return LaneHarness("actions", default_endpoint=CONTROLLER_ADDR)


def _actions_session(action_bus: LaneHarness) -> EndpointSession:
    return action_bus.endpoint(CONTROLLER_ADDR).session


def _concord(bus: LaneHarness) -> Concord:
    return Concord(
        bus.substrate.kv_bucket(CONCORD_CONTRACT_BUCKET_POLICY),
        bus.substrate.kv_bucket(CONCORD_TOKEN_BUCKET_POLICY),
        bus.substrate.kv_bucket(CONCORD_MAINTENANCE_BUCKET_POLICY),
    )


def _provider_session_manager(
    concord: Concord,
    action_bus: LaneHarness,
    start_soon,
) -> ActionProviderSessionManager:
    return ActionProviderSessionManager(
        controller_id=CONTROLLER_ID,
        controller_session_id=action_bus.session_id,
        concord=concord,
        start_soon=start_soon,
    )


class ReadyProviderSessions:
    def __init__(self) -> None:
        self.prepare_calls: list[tuple[ActionMetadata, ...]] = []
        self.refresh_calls: list[tuple[ProviderSessionKey, ...]] = []

    async def prepare_many(
        self, actions
    ) -> dict[ProviderSessionKey, ProviderSessionSnapshot]:
        prepared = tuple(actions)
        self.prepare_calls.append(prepared)
        return {
            key: ProviderSessionSnapshot(
                key=key,
                ready=True,
                terminal=False,
                status=ContractValidityStatus.VALID,
            )
            for action in prepared
            if (key := provider_session_key(action)) is not None
        }

    async def refresh_many(
        self, keys
    ) -> dict[ProviderSessionKey, ProviderSessionSnapshot]:
        prepared = tuple(keys)
        self.refresh_calls.append(prepared)
        return {
            key: ProviderSessionSnapshot(
                key=key,
                ready=True,
                terminal=False,
                status=ContractValidityStatus.VALID,
            )
            for key in prepared
        }

    def cached_ready(self, key: ProviderSessionKey) -> bool:
        return True

    async def valid(self, **kwargs) -> bool:
        raise AssertionError(f"unexpected provider-session valid call: {kwargs}")


class TemporarilyUnavailableProviderSessions(ReadyProviderSessions):
    def __init__(self) -> None:
        super().__init__()
        self.unavailable = False

    async def refresh_many(
        self, keys
    ) -> dict[ProviderSessionKey, ProviderSessionSnapshot]:
        prepared = tuple(keys)
        self.refresh_calls.append(prepared)
        self.unavailable = True
        return {
            key: ProviderSessionSnapshot(
                key=key,
                ready=False,
                terminal=False,
                status=ContractValidityStatus.UNAVAILABLE,
                reason="provider_session_unavailable",
            )
            for key in prepared
        }

    def cached_ready(self, key: ProviderSessionKey) -> bool:
        return not self.unavailable


def _action_command(
    message_type: str,
    payload: dict | None = None,
    *,
    config_id: str = "test-device",
    context_id: str,
    action_instance_id: str,
    binding_id: str,
    page_session_id: str | None = None,
) -> DeckrMessage:
    return action_message(
        sender=PROVIDER_ADDR,
        sender_session_id=PROVIDER_SESSION_ID,
        recipient=CONTROLLER_ADDR,
        message_type=message_type,
        body=payload or {},
        subject=context_subject(
            context_id,
            provider_instance_id=PROVIDER_INSTANCE_ID,
            provider_id=PROVIDER_ID,
            config_id=config_id,
            action_instance_id=action_instance_id,
            binding_id=binding_id,
            page_session_id=page_session_id,
        ),
    )


def _provider_settings_target(
    provider_id: str = "dev.deckr.clock",
    *,
    config_id: str = "test-device",
) -> SettingsTargetRef:
    return SettingsTargetRef(
        scope="action_provider_instance",
        controllerId=CONTROLLER_ID,
        configId=config_id,
        providerInstanceId=PROVIDER_INSTANCE_ID,
        providerId=provider_id,
    )


def _provider_settings_command(
    message_type: str,
    target: SettingsTargetRef,
    *,
    sender_provider_instance_id: str = PROVIDER_INSTANCE_ID,
    settings: dict | None = None,
) -> DeckrMessage:
    body: dict = {"target": target.to_dict()}
    if settings is not None:
        body["settings"] = settings
    return action_message(
        sender=action_provider_address(sender_provider_instance_id),
        sender_session_id=PROVIDER_SESSION_ID,
        recipient=CONTROLLER_ADDR,
        message_type=message_type,
        body=body,
        subject=action_provider_instance_subject(
            sender_provider_instance_id,
            provider_id=target.provider_id,
        ),
    )


def _metadata(
    uuid: str,
    *,
    provider_instance_id: str = PROVIDER_INSTANCE_ID,
    provider_id: str = PROVIDER_ID,
    provider_session_id: str | None = PROVIDER_SESSION_ID,
) -> ActionMetadata:
    return ActionMetadata(
        uuid=uuid,
        provider_instance_id=provider_instance_id,
        provider_id=provider_id,
        provider_session_id=provider_session_id,
    )


def _availability_entry(action_uuid: str) -> ActionAvailabilityEntry:
    return ActionAvailabilityEntry(
        actionId=action_uuid,
        status="available",
        descriptor=ActionDescriptor(
            actionId=action_uuid,
            providerId=PROVIDER_ID,
        ),
    )


def _seed_action_availability(
    manager: DeviceManager,
    *metadatas: ActionMetadata,
) -> None:
    for metadata in metadatas:
        manager._action_availability.record_available(metadata)


def _catalog_event(
    *,
    added: list[str] | None = None,
    removed: list[str] | None = None,
    updated: list[str] | None = None,
    successions: list[ProviderSessionSuccession] | None = None,
) -> ActionCatalogChangedEvent:
    return ActionCatalogChangedEvent(
        catalog_added=added or [],
        catalog_removed=removed or [],
        catalog_updated=updated or [],
        provider_session_successions=successions or [],
    )


def _provider_session_succession(
    *,
    provider_instance_id: str = PROVIDER_INSTANCE_ID,
    provider_id: str = PROVIDER_ID,
    previous_session_id: str = PROVIDER_SESSION_ID,
    successor_session_id: str = "new-provider-session",
    actions: list[str],
) -> ProviderSessionSuccession:
    return ProviderSessionSuccession(
        provider_instance_id=provider_instance_id,
        provider_id=provider_id,
        previous_session_id=previous_session_id,
        successor_session_id=successor_session_id,
        actions=actions,
    )


async def _action_command_for_active_binding(
    manager: DeviceManager,
    message_type: str,
    payload: dict | None = None,
    *,
    control_id: str = "0,0",
) -> DeckrMessage:
    ctx = await manager.action_contexts.get(control_id)
    assert ctx is not None
    return _action_command(
        message_type,
        payload,
        context_id=ctx.id,
        action_instance_id=ctx.action_instance_id,
        binding_id=ctx.binding_id,
        page_session_id=ctx.page_session_id,
    )


def _action_instance_command(
    message_type: str,
    payload: dict,
    *,
    context_id: str,
    action_instance_id: str,
    config_id: str = "test-device",
    sender_session_id: str = PROVIDER_SESSION_ID,
) -> DeckrMessage:
    return action_message(
        sender=PROVIDER_ADDR,
        sender_session_id=sender_session_id,
        recipient=CONTROLLER_ADDR,
        message_type=message_type,
        body=payload,
        subject=context_subject(
            context_id,
            provider_instance_id=PROVIDER_INSTANCE_ID,
            provider_id=PROVIDER_ID,
            config_id=config_id,
            action_instance_id=action_instance_id,
        ),
    )


def _page_session_command(
    message_type: str,
    payload: dict,
    *,
    session_id: str,
    context_id: str,
    action_instance_id: str,
    config_id: str = "test-device",
    sender_session_id: str = PROVIDER_SESSION_ID,
) -> DeckrMessage:
    return action_message(
        sender=PROVIDER_ADDR,
        sender_session_id=sender_session_id,
        recipient=CONTROLLER_ADDR,
        message_type=message_type,
        body=payload,
        subject=context_subject(
            context_id,
            provider_instance_id=PROVIDER_INSTANCE_ID,
            provider_id=PROVIDER_ID,
            config_id=config_id,
            action_instance_id=action_instance_id,
            page_session_id=session_id,
        ),
    )


def _make_control(
    control_id: str,
    row: int = 0,
    col: int = 0,
    kind: str = "key",
    events: list[str] | None = None,
    has_display: bool = True,
) -> ControlDescriptor:
    if events is None:
        events = ["momentary", "press"]
    input_capabilities = []
    if "momentary" in events:
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
    if "press" in events:
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
    if "rotate" in events:
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
    if "tap" in events or "swipe" in events:
        event_types = []
        if "tap" in events:
            event_types.append("tap")
        if "swipe" in events:
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
        controlId=control_id,
        kind=kind,
        geometry=ControlGeometry(x=col, y=row, width=1, height=1, unit="grid"),
        inputCapabilities=tuple(input_capabilities),
        outputCapabilities=tuple(output_capabilities),
    )


def _make_mock_device(
    device_id: str = "test-device",
    controls: list[ControlDescriptor] | None = None,
) -> DeviceDescriptor:
    """Create device metadata for controller tests."""
    if controls is None:
        controls = [_make_control("0,0"), _make_control("1,0")]
    return DeviceDescriptor(
        deviceId=device_id,
        displayName="Test Device",
        fingerprint=f"fingerprint:{device_id}",
        controls=tuple(controls),
    )


def _hardware_ref(device: DeviceDescriptor) -> DeviceRef:
    return DeviceRef(
        managerId="manager-main",
        deviceId=device.device_id,
    )


def _hardware_input(
    control_id: str,
    event_type: str,
    *,
    capability_id: str = "button.momentary",
    sequence: int | None = None,
) -> DeckrMessage:
    return hw_messages.control_input_message(
        manager_id="manager-main",
        sender_session_id="manager-session",
        device_id="test-device",
        control_id=control_id,
        capability_id=capability_id,
        event_type=event_type,
        sequence=sequence,
    )


class FakeHardwareCommandService:
    def __init__(self):
        self.set_raster_frame = AsyncMock()
        self.clear_raster = AsyncMock()
        self.sleep_device = AsyncMock()
        self.wake_device = AsyncMock()


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
    return dump_graph_data_uri(graph.graph, output=graph.output)


def _dynamic_page(page_id: str, *control_ids: str) -> DynamicPageCommand:
    return DynamicPageCommand(
        pageId=page_id,
        bindings=tuple(
            PageChildBindingDescriptor(
                controlId=control_id,
                target=PageChildBindingTarget(kind="self"),
                itemKey=f"item-{ix}",
            )
            for ix, control_id in enumerate(control_ids)
        ),
    )


def _dynamic_page_with_action_child(
    page_id: str,
    control_id: str,
    *,
    action_id: str,
    provider_instance_id: str,
) -> DynamicPageCommand:
    return DynamicPageCommand(
        pageId=page_id,
        bindings=(
            PageChildBindingDescriptor(
                controlId=control_id,
                target=PageChildBindingTarget(
                    kind="action",
                    actionId=action_id,
                    providerInstanceId=provider_instance_id,
                    instanceKey=control_id,
                ),
            ),
        ),
    )


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
            control_id=request.control_id,
            generation=request.generation,
            frame=f"frame-{request.generation}".encode(),
        )

    def release(self, generation: int) -> None:
        self._events.setdefault(generation, anyio.Event()).set()

    async def aclose(self) -> None:
        return


class SetRasterImageOnAppearAction:
    """Minimal action that sets a graph-backed raster image on will_appear."""

    uuid: str = "test.virtual.setops"

    async def on_will_appear(self, event, context):
        await context.set_raster_image(_solid_key_image())

    async def on_will_disappear(self, event, context):
        pass


class NoopAction:
    uuid: str = "test.virtual.noop"

    async def on_will_appear(self, event, context):
        pass

    async def on_will_disappear(self, event, context):
        pass


class MemoryConfigService:
    def __init__(self, config: DeviceConfig) -> None:
        self.config = config

    async def match_device(
        self,
        *,
        fingerprint: str,
        labels,
    ) -> DeviceConfig | None:
        del labels
        return self.config if self.config.match.fingerprint == fingerprint else None

    async def get_config(self, config_id: str) -> DeviceConfig | None:
        return self.config if self.config.id == config_id else None

    async def write_config(self, config: DeviceConfig) -> DeviceConfig:
        self.config = config
        return config

    def subscribe(self, config_id: str) -> AsyncIterator[DeviceConfig | None]:
        del config_id
        return self._subscribe()

    async def _subscribe(self) -> AsyncIterator[DeviceConfig | None]:
        yield self.config


def _provider_settings_config() -> DeviceConfig:
    return DeviceConfig(
        id="test-device",
        name="Test Device",
        match={"fingerprint": "fingerprint:test-device"},
        provider_settings={PROVIDER_INSTANCE_ID: {"timezone": "UTC"}},
        profiles=[Profile(name="default", pages=[Page(controls=[])])],
    )


def _provider_settings_device_manager(
    *,
    config_service: MemoryConfigService,
    actions_bus: LaneHarness,
    registry: MagicMock,
) -> DeviceManager:
    device = _make_mock_device()
    return DeviceManager(
        controller_id=CONTROLLER_ID,
        device=device,
        hardware_ref=_hardware_ref(device),
        command_service=FakeHardwareCommandService(),
        config=config_service.config,
        manager=registry,
        actions_bus=_actions_session(actions_bus),
        start_soon=lambda fn, *a, **k: None,
        settings_service=ConfigBackedSettingsService(
            controller_id=CONTROLLER_ID,
            config_service=config_service,
        ),
    )


async def _prepare_provider_settings_session(
    provider_sessions: ActionProviderSessionManager,
    concord: Concord,
    *,
    provider_id: str = "dev.deckr.clock",
) -> None:
    snapshot = await provider_sessions.prepare(
        _metadata("provider-settings", provider_id=provider_id)
    )
    assert snapshot is not None
    session = next(iter(provider_sessions._sessions.values()))
    await concord.attach(
        session.contract,
        participant=PROVIDER_ADDR,
        session_id=PROVIDER_SESSION_ID,
    )


async def _next_action_message(
    stream: AsyncIterator[DeckrMessage],
    *,
    timeout: float = 1.0,
) -> DeckrMessage:
    with anyio.fail_after(timeout):
        return await anext(stream)


async def _assert_no_action_message(
    stream: AsyncIterator[DeckrMessage],
    *,
    timeout: float = 0.1,
) -> None:
    with anyio.move_on_after(timeout) as scope:
        await anext(stream)
    assert scope.cancel_called


async def _drain_action_messages(
    stream: AsyncIterator[DeckrMessage],
    *,
    timeout: float = 0.05,
) -> None:
    while True:
        with anyio.move_on_after(timeout) as scope:
            await anext(stream)
        if scope.cancel_called:
            return


async def _collect_action_messages(
    stream: AsyncIterator[DeckrMessage],
    *,
    timeout: float = 0.05,
) -> list[DeckrMessage]:
    messages: list[DeckrMessage] = []
    while True:
        with anyio.move_on_after(timeout) as scope:
            message = await anext(stream)
        if scope.cancel_called:
            return messages
        messages.append(message)


async def _next_capability_input(
    stream: AsyncIterator[DeckrMessage],
) -> CapabilityInputBody:
    msg = await _next_action_message(stream)
    assert msg.message_type == CAPABILITY_INPUT
    return CapabilityInputBody.model_validate(msg.body)


@pytest.mark.asyncio
async def test_device_manager_starts_background_loops_explicitly():
    started: list[tuple[object, tuple[object, ...]]] = []
    device = _make_mock_device()
    config = DeviceConfig(
        id="test-device",
        name="Test Device",
        match={"fingerprint": "fingerprint:test-device"},
        profiles=[Profile(name="default", pages=[Page(controls=[])])],
    )

    class RecordingTaskGroup:
        def start_soon(self, fn, *args, **kwargs) -> None:
            started.append((fn, args))

    constructor_tg = RecordingTaskGroup()
    manager = DeviceManager(
        controller_id=CONTROLLER_ID,
        device=device,
        hardware_ref=_hardware_ref(device),
        command_service=FakeHardwareCommandService(),
        config=config,
        manager=MagicMock(),
        actions_bus=_actions_session(_actions_bus()),
        start_soon=constructor_tg.start_soon,
    )
    assert started == []

    stopping = anyio.Event()
    stopping.set()
    await manager.start(RecordingTaskGroup(), stopping)

    assert [fn.__name__ for fn, _ in started] == [
        "_page_timeout_loop",
    ]
    assert all(args == (stopping,) for _, args in started)
    for fn, args in started:
        await fn(*args)


def test_device_manager_tracks_connected_config_action_interest():
    device = _make_mock_device()
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
                                action="action.alpha",
                            )
                        ]
                    ),
                    Page(
                        controls=[
                            Control(
                                selector={"control_id": "1,0"},
                                action="action.beta",
                                provider_instance_id="provider-beta",
                                provider_labels={"room": "office"},
                            )
                        ]
                    ),
                ],
            )
        ],
    )
    manager = DeviceManager(
        controller_id=CONTROLLER_ID,
        device=device,
        hardware_ref=_hardware_ref(device),
        command_service=FakeHardwareCommandService(),
        config=config,
        manager=MagicMock(),
        actions_bus=_actions_session(_actions_bus()),
        start_soon=lambda fn, *a, **k: None,
        clock=lambda: 10.0,
    )

    snapshot = manager.action_interest_snapshot(now=10.0)

    assert [intent.action_uuid for intent in snapshot.strong_intents] == [
        "action.alpha",
        "action.beta",
    ]
    beta = next(
        intent for intent in snapshot.strong_intents if intent.action_uuid == "action.beta"
    )
    assert beta.provider_instance_id == "provider-beta"
    assert beta.provider_labels == (("room", "office"),)
    assert {record.source for record in snapshot.records} == {
        ActionInterestSource.CONNECTED_CONFIG
    }


@pytest.mark.asyncio
async def test_device_manager_tracks_visible_and_dynamic_page_action_interest():
    device = _make_mock_device()
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
                                action="action.owner",
                            )
                        ]
                    )
                ],
            )
        ],
    )

    async def get_action(address: str, **kwargs) -> ActionMetadata:
        del kwargs
        return _metadata(address)

    registry = MagicMock()
    registry.get_action = get_action
    registry.provider_session_id.return_value = PROVIDER_SESSION_ID

    async with anyio.create_task_group() as tg:
        manager = DeviceManager(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=FakeHardwareCommandService(),
            config=config,
            manager=registry,
            actions_bus=_actions_session(_actions_bus()),
            start_soon=tg.start_soon,
            clock=lambda: 20.0,
        )
        _seed_action_availability(
            manager,
            _metadata("action.owner"),
            _metadata("action.child", provider_instance_id=PROVIDER_INSTANCE_ID),
        )
        await manager.set_page(profile="default", page=0)
        static_snapshot = manager.action_interest_snapshot(now=20.0)
        assert any(
            record.source == ActionInterestSource.VISIBLE_BINDING
            and record.strength == ActionInterestStrength.STRONG
            and record.intent.action_uuid == "action.owner"
            for record in static_snapshot.records
        )

        owner = await manager.action_contexts.get("0,0")
        assert owner is not None
        await manager.open_page(
            descriptor=_dynamic_page_with_action_child(
                "dynamic-page",
                "1,0",
                action_id="action.child",
                provider_instance_id=PROVIDER_INSTANCE_ID,
            ),
            context_id=owner.id,
        )

        dynamic_snapshot = manager.action_interest_snapshot(now=20.0)
        assert any(
            record.source == ActionInterestSource.DYNAMIC_PAGE
            and record.strength == ActionInterestStrength.STRONG
            and record.intent.action_uuid == "action.child"
            for record in dynamic_snapshot.records
        )
        assert any(
            record.source == ActionInterestSource.VISIBLE_BINDING
            and record.strength == ActionInterestStrength.WARM
            and record.intent.action_uuid == "action.owner"
            for record in dynamic_snapshot.records
        )
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_provider_settings_patch_after_beacon_loss_is_ignored():
    config_service = MemoryConfigService(_provider_settings_config())
    action_bus = _actions_bus()
    concord = _concord(action_bus)
    provider_sessions = _provider_session_manager(
        concord,
        action_bus,
        lambda fn, *a, **k: None,
    )
    await _prepare_provider_settings_session(provider_sessions, concord)
    registry = MagicMock()
    registry.provider_session_id.return_value = None
    registry.provider_instance_provides_provider.return_value = False
    manager = _provider_settings_device_manager(
        config_service=config_service,
        actions_bus=action_bus,
        registry=registry,
    )

    async with action_bus.subscribe(PROVIDER_ADDR) as stream:
        await manager.handle_command(
            _provider_settings_command(
                SETTINGS_PATCH,
                _provider_settings_target(),
                settings={"timezone": "Europe/Amsterdam"},
            )
        )
        await _assert_no_action_message(stream)

    assert config_service.config.provider_settings[PROVIDER_INSTANCE_ID] == {
        "timezone": "UTC"
    }
    registry.provider_session_id.assert_not_called()
    registry.provider_instance_provides_provider.assert_called_once_with(
        PROVIDER_INSTANCE_ID,
        "dev.deckr.clock",
    )


@pytest.mark.asyncio
async def test_provider_settings_patch_from_non_owning_provider_is_ignored():
    config_service = MemoryConfigService(_provider_settings_config())
    action_bus = _actions_bus()
    registry = MagicMock()
    registry.provider_session_id.return_value = PROVIDER_SESSION_ID
    registry.provider_instance_provides_provider.return_value = False
    manager = _provider_settings_device_manager(
        config_service=config_service,
        actions_bus=action_bus,
        registry=registry,
    )

    async with action_bus.subscribe(action_provider_address("other")) as stream:
        await manager.handle_command(
            _provider_settings_command(
                SETTINGS_PATCH,
                _provider_settings_target(),
                sender_provider_instance_id="other",
                settings={"timezone": "Europe/Amsterdam"},
            )
        )
        await _assert_no_action_message(stream)

    assert config_service.config.provider_settings[PROVIDER_INSTANCE_ID] == {
        "timezone": "UTC"
    }
    registry.provider_instance_provides_provider.assert_not_called()


@pytest.mark.asyncio
async def test_provider_settings_request_for_unadvertised_provider_is_ignored():
    config_service = MemoryConfigService(_provider_settings_config())
    action_bus = _actions_bus()
    concord = _concord(action_bus)
    provider_sessions = _provider_session_manager(
        concord,
        action_bus,
        lambda fn, *a, **k: None,
    )
    await _prepare_provider_settings_session(provider_sessions, concord)
    registry = MagicMock()
    registry.provider_session_id.return_value = PROVIDER_SESSION_ID
    registry.provider_instance_provides_provider.return_value = False
    manager = _provider_settings_device_manager(
        config_service=config_service,
        actions_bus=action_bus,
        registry=registry,
    )

    async with action_bus.subscribe(PROVIDER_ADDR) as stream:
        await manager.handle_command(
            _provider_settings_command(
                SETTINGS_REQUEST,
                _provider_settings_target("other"),
            )
        )
        await _assert_no_action_message(stream)

    assert config_service.config.provider_settings[PROVIDER_INSTANCE_ID] == {
        "timezone": "UTC"
    }
    registry.provider_session_id.assert_not_called()
    registry.provider_instance_provides_provider.assert_called_once_with(
        PROVIDER_INSTANCE_ID,
        "other",
    )


@pytest.mark.asyncio
async def test_provider_settings_ignores_concord_session_validity():
    config_service = MemoryConfigService(_provider_settings_config())
    action_bus = _actions_bus()
    concord = _concord(action_bus)
    provider_sessions = _provider_session_manager(
        concord,
        action_bus,
        lambda fn, *a, **k: None,
    )
    await _prepare_provider_settings_session(provider_sessions, concord)
    session = next(iter(provider_sessions._sessions.values()))
    await concord.cancel(
        session.contract, participant=CONTROLLER_ADDR, reason="test invalid"
    )
    registry = MagicMock()
    registry.provider_session_id.return_value = PROVIDER_SESSION_ID
    registry.provider_instance_provides_provider.return_value = True
    manager = _provider_settings_device_manager(
        config_service=config_service,
        actions_bus=action_bus,
        registry=registry,
    )

    async with action_bus.subscribe(PROVIDER_ADDR) as stream:
        await manager.handle_command(
            _provider_settings_command(
                SETTINGS_PATCH,
                _provider_settings_target(),
                settings={"timezone": "Europe/Amsterdam"},
            )
        )
        reply = await _next_action_message(stream)

    assert config_service.config.provider_settings[PROVIDER_INSTANCE_ID] == {
        "timezone": "Europe/Amsterdam"
    }
    assert reply.message_type == SETTINGS_SNAPSHOT
    registry.provider_session_id.assert_called_once_with(PROVIDER_INSTANCE_ID)
    registry.provider_instance_provides_provider.assert_called_once_with(
        PROVIDER_INSTANCE_ID,
        "dev.deckr.clock",
    )


@pytest.mark.asyncio
async def test_malformed_settings_commands_are_ignored_without_crashing():
    config_service = MemoryConfigService(_provider_settings_config())
    action_bus = _actions_bus()
    registry = MagicMock()
    registry.provider_session_id.return_value = PROVIDER_SESSION_ID
    registry.provider_instance_provides_provider.return_value = True
    manager = _provider_settings_device_manager(
        config_service=config_service,
        actions_bus=action_bus,
        registry=registry,
    )

    target = _provider_settings_target()
    valid = _provider_settings_command(
        SETTINGS_PATCH,
        target,
        settings={"timezone": "Europe/Amsterdam"},
    )
    malformed_target = valid.model_copy(
        update={"body": {"target": "not-a-target", "settings": {}}}
    )
    malformed_settings = valid.model_copy(
        update={"body": {"target": target.to_dict(), "settings": "not-an-object"}}
    )

    async with action_bus.subscribe(PROVIDER_ADDR) as stream:
        await manager.handle_command(malformed_target)
        await manager.handle_command(malformed_settings)
        await _assert_no_action_message(stream)

    assert config_service.config.provider_settings[PROVIDER_INSTANCE_ID] == {
        "timezone": "UTC"
    }


@pytest_asyncio.fixture
def device_config_set_raster_image():
    """Config: one profile, one page, one control with SetRasterImageOnAppearAction."""
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
                                action=SetRasterImageOnAppearAction.uuid,
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
    device_config_set_raster_image, persistence_tmp_dir
):
    """Capability bindingOutput is rendered through the controller raster path."""
    from deckr.actions.messages import BINDING_OUTPUT

    device = _make_mock_device()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=_metadata(SetRasterImageOnAppearAction.uuid)
    )
    action_bus = _actions_bus()
    command_service = FakeHardwareCommandService()
    render_backend = ControlledFrameBackend()

    async with anyio.create_task_group() as tg:
        manager = DeviceManager(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=command_service,
            config=device_config_set_raster_image,
            manager=registry,
            actions_bus=_actions_session(action_bus),
            start_soon=tg.start_soon,
            render_backend=render_backend,
        )
        _seed_action_availability(
            manager,
            _metadata(SetRasterImageOnAppearAction.uuid),
        )
        await manager.set_page(profile="default", page=0)
        baseline_calls = command_service.set_raster_frame.call_count
        ctx = await manager.action_contexts.get("0,0")
        assert ctx is not None
        binding = ctx.metadata.model_copy(update={"output_generation": 1})
        msg = await _action_command_for_active_binding(
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
            while not render_backend.calls:
                await anyio.sleep(0.01)
        render_backend.release(render_backend.calls[-1])
        with anyio.fail_after(5.0):
            while command_service.set_raster_frame.call_count <= baseline_calls:
                await anyio.sleep(0.01)
        tg.cancel_scope.cancel()

    assert command_service.set_raster_frame.call_count > baseline_calls
    call_args = command_service.set_raster_frame.call_args
    assert call_args[0][0] == "test-device"
    assert call_args[0][1] == "0,0"
    assert call_args[0][2] == "raster.bitmap"
    assert call_args[0][3] == f"frame-{render_backend.calls[-1]}".encode()


@pytest.mark.asyncio
async def test_binding_output_accepts_graph_data_uri(
    device_config_set_raster_image, persistence_tmp_dir
):
    """Graph outputs from action providers are still controller-rendered images."""
    from deckr.actions.messages import BINDING_OUTPUT

    device = _make_mock_device()
    action_bus = _actions_bus()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=_metadata(SetRasterImageOnAppearAction.uuid)
    )
    command_service = FakeHardwareCommandService()
    render_backend = ControlledFrameBackend()

    async with anyio.create_task_group() as tg:
        manager = DeviceManager(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=command_service,
            config=device_config_set_raster_image,
            manager=registry,
            actions_bus=_actions_session(action_bus),
            start_soon=tg.start_soon,
            render_backend=render_backend,
        )
        _seed_action_availability(
            manager,
            _metadata(SetRasterImageOnAppearAction.uuid),
        )
        await manager.set_page(profile="default", page=0)
        ctx = await manager.action_contexts.get("0,0")
        assert ctx is not None
        binding = ctx.metadata.model_copy(update={"output_generation": 1})
        msg = await _action_command_for_active_binding(
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
                "params": {"image": _solid_key_image()},
                "generation": 1,
            },
        )

        await manager.handle_command(msg)

        with anyio.fail_after(5.0):
            while not render_backend.calls:
                await anyio.sleep(0.01)
        render_backend.release(render_backend.calls[-1])
        with anyio.fail_after(5.0):
            while command_service.set_raster_frame.call_count == 0:
                await anyio.sleep(0.01)
        tg.cancel_scope.cancel()

    call_args = command_service.set_raster_frame.call_args
    assert call_args[0][0] == "test-device"
    assert call_args[0][1] == "0,0"
    assert call_args[0][2] == "raster.bitmap"
    assert call_args[0][3] == f"frame-{render_backend.calls[-1]}".encode()


@pytest.mark.asyncio
async def test_binding_overlay_renders_and_expires(
    device_config_set_raster_image, persistence_tmp_dir
):
    """bindingOverlay renders transient controller-owned feedback."""
    from deckr.actions.messages import BINDING_OVERLAY

    device = _make_mock_device()
    action_bus = _actions_bus()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=_metadata(SetRasterImageOnAppearAction.uuid)
    )
    command_service = FakeHardwareCommandService()
    render_backend = ControlledFrameBackend()

    async with anyio.create_task_group() as tg:
        manager = DeviceManager(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=command_service,
            config=device_config_set_raster_image,
            manager=registry,
            actions_bus=_actions_session(action_bus),
            start_soon=tg.start_soon,
            render_backend=render_backend,
        )
        _seed_action_availability(
            manager,
            _metadata(SetRasterImageOnAppearAction.uuid),
        )
        await manager.set_page(profile="default", page=0)
        ctx = await manager.action_contexts.get("0,0")
        assert ctx is not None
        msg = await _action_command_for_active_binding(
            manager,
            BINDING_OVERLAY,
            {
                "binding": ctx.metadata.model_dump(
                    by_alias=True,
                    exclude_none=True,
                    mode="json",
                ),
                "template": "ok",
                "title": "OK",
                "durationSeconds": 1.0,
                "generation": 1,
            },
        )

        await manager.handle_command(msg)

        with anyio.fail_after(5.0):
            while not render_backend.calls:
                await anyio.sleep(0.01)
        render_backend.release(render_backend.calls[-1])
        with anyio.fail_after(5.0):
            while command_service.set_raster_frame.call_count == 0:
                await anyio.sleep(0.01)
        with anyio.fail_after(5.0):
            while command_service.clear_raster.call_count == 0:
                await anyio.sleep(0.01)
        tg.cancel_scope.cancel()

    call_args = command_service.set_raster_frame.call_args
    assert call_args[0][0] == "test-device"
    assert call_args[0][1] == "0,0"
    assert call_args[0][2] == "raster.bitmap"


@pytest.mark.asyncio
async def test_binding_activates_without_concord_provider_session(
    device_config_set_raster_image,
):
    device = _make_mock_device()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=_metadata(
            SetRasterImageOnAppearAction.uuid,
            provider_session_id=None,
        )
    )
    registry.provider_session_id.return_value = PROVIDER_SESSION_ID
    registry.provider_instance_provides_provider.return_value = True
    action_bus = _actions_bus()

    async with anyio.create_task_group() as tg:
        manager = DeviceManager(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=FakeHardwareCommandService(),
            config=device_config_set_raster_image,
            manager=registry,
            actions_bus=_actions_session(action_bus),
            start_soon=tg.start_soon,
        )
        async with action_bus.subscribe(PROVIDER_ADDR) as stream:
            _seed_action_availability(
                manager,
                _metadata(
                    SetRasterImageOnAppearAction.uuid,
                    provider_session_id=None,
                ),
            )
            await manager.set_page(profile="default", page=0)
            ctx = await manager.action_contexts.get("0,0")
            assert ctx is not None
            lease = next(iter(manager._binding_leases.values()))
            assert lease.attached
            assert lease.provider_session_id == PROVIDER_SESSION_ID

            created = await stream.receive()
            attached = await stream.receive()
            assert created.message_type == "actionInstanceCreated"
            assert attached.message_type == "bindingAttached"

        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_binding_activation_timeout_does_not_block_static_page_bind_loop(
    monkeypatch,
):
    blocked_action = "test.virtual.blocked"
    device = _make_mock_device()
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
                                action=blocked_action,
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
    registry = MagicMock()
    registry.get_action = AsyncMock(
        side_effect=lambda uuid, **_: _metadata(uuid),
    )
    action_bus = _actions_bus()
    original_send = device_manager_module.send_with_endpoint_identity

    async def send_or_block_action_instance_create(endpoint, message):
        body = thaw_json(message.body)
        metadata = body.get("metadata", {})
        if (
            message.message_type == ACTION_INSTANCE_CREATED
            and metadata.get("actionId") == blocked_action
        ):
            await anyio.sleep_forever()
        return await original_send(endpoint, message)

    monkeypatch.setattr(
        device_manager_module,
        "ACTION_INSTANCE_CREATE_TIMEOUT_SECONDS",
        0.01,
    )
    monkeypatch.setattr(
        device_manager_module,
        "send_with_endpoint_identity",
        send_or_block_action_instance_create,
    )
    manager = DeviceManager(
        controller_id=CONTROLLER_ID,
        device=device,
        hardware_ref=_hardware_ref(device),
        command_service=FakeHardwareCommandService(),
        config=config,
        manager=registry,
        actions_bus=_actions_session(action_bus),
        start_soon=lambda fn, *a, **k: None,
    )
    _seed_action_availability(
        manager,
        _metadata(blocked_action),
        _metadata(NoopAction.uuid),
    )

    with anyio.fail_after(1):
        await manager.set_page(profile="default", page=0)

    blocked_lease = manager._binding_lease_for_control("0,0")
    resolved_lease = manager._binding_lease_for_control("1,0")
    assert blocked_lease is not None
    assert not blocked_lease.attached
    assert await manager.action_contexts.get("0,0") is None
    assert blocked_lease.action_instance_id not in manager._action_instances
    assert resolved_lease is not None
    assert resolved_lease.attached
    assert await manager.action_contexts.get("1,0") is not None


@pytest.mark.asyncio
async def test_settings_snapshot_timeout_does_not_block_static_page_bind_loop(
    monkeypatch,
):
    class HangingSettingsService:
        async def get(self, target):
            await anyio.sleep_forever()

    device = _make_mock_device()
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
                                settings={"configured": "first"},
                            ),
                            Control(
                                selector={"control_id": "1,0"},
                                action=NoopAction.uuid,
                                settings={"configured": "second"},
                            ),
                        ]
                    )
                ],
            )
        ],
    )
    registry = MagicMock()
    registry.get_action = AsyncMock(
        side_effect=lambda uuid, **_: _metadata(uuid),
    )
    action_bus = _actions_bus()
    monkeypatch.setattr(
        device_manager_module,
        "SETTINGS_SNAPSHOT_TIMEOUT_SECONDS",
        0.01,
    )
    manager = DeviceManager(
        controller_id=CONTROLLER_ID,
        device=device,
        hardware_ref=_hardware_ref(device),
        command_service=FakeHardwareCommandService(),
        config=config,
        manager=registry,
        actions_bus=_actions_session(action_bus),
        start_soon=lambda fn, *a, **k: None,
        settings_service=HangingSettingsService(),
    )
    _seed_action_availability(
        manager,
        _metadata(ACTION_X_UUID),
        _metadata(NoopAction.uuid),
    )

    with anyio.fail_after(1):
        await manager.set_page(profile="default", page=0)

    first = await manager.action_contexts.get("0,0")
    second = await manager.action_contexts.get("1,0")
    assert first is not None
    assert first.settings == {"configured": "first"}
    assert second is not None
    assert second.settings == {"configured": "second"}


@pytest.mark.asyncio
async def test_provider_session_restart_restamps_binding_route(
    device_config_set_raster_image,
):
    device = _make_mock_device()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=_metadata(SetRasterImageOnAppearAction.uuid)
    )
    registry.provider_session_id.return_value = PROVIDER_SESSION_ID
    registry.provider_instance_provides_provider.return_value = True
    action_bus = _actions_bus()

    async with anyio.create_task_group() as tg:
        manager = DeviceManager(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=FakeHardwareCommandService(),
            config=device_config_set_raster_image,
            manager=registry,
            actions_bus=_actions_session(action_bus),
            start_soon=tg.start_soon,
        )
        _seed_action_availability(
            manager,
            _metadata(SetRasterImageOnAppearAction.uuid),
        )
        await manager.set_page(profile="default", page=0)
        ctx = await manager.action_contexts.get("0,0")
        assert ctx is not None
        lease = next(iter(manager._binding_leases.values()))
        assert lease.provider_session_id == PROVIDER_SESSION_ID

        registry.provider_session_id.return_value = "new-provider-session"
        qualified = f"{PROVIDER_INSTANCE_ID}::{SetRasterImageOnAppearAction.uuid}"
        await manager.on_action_catalog_changed(
            _catalog_event(
                successions=[
                    _provider_session_succession(
                        successor_session_id="new-provider-session",
                        actions=[qualified],
                    )
                ],
            )
        )

        assert await manager.action_contexts.get("0,0") is ctx
        assert lease.provider_session_id == "new-provider-session"
        assert lease.attached

        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_binding_stays_attached_when_beacon_session_changes(
    device_config_set_raster_image,
):
    device = _make_mock_device()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=_metadata(SetRasterImageOnAppearAction.uuid)
    )
    registry.provider_session_id.return_value = PROVIDER_SESSION_ID
    registry.provider_instance_provides_provider.return_value = True
    action_bus = _actions_bus()

    async with anyio.create_task_group() as tg:
        manager = DeviceManager(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=FakeHardwareCommandService(),
            config=device_config_set_raster_image,
            manager=registry,
            actions_bus=_actions_session(action_bus),
            start_soon=tg.start_soon,
        )
        _seed_action_availability(
            manager,
            _metadata(SetRasterImageOnAppearAction.uuid),
        )
        await manager.set_page(profile="default", page=0)
        ctx = await manager.action_contexts.get("0,0")
        assert ctx is not None
        lease = manager._binding_lease_for_control("0,0")
        assert lease is not None

        registry.provider_session_id.return_value = "new-provider-session"
        await manager.on_action_catalog_changed(
            _catalog_event(
                successions=[
                    _provider_session_succession(
                        successor_session_id="new-provider-session",
                        actions=[
                            f"{PROVIDER_INSTANCE_ID}::{SetRasterImageOnAppearAction.uuid}"
                        ],
                    )
                ]
            )
        )

        assert await manager.action_contexts.get("0,0") is ctx
        assert lease.provider_session_id == "new-provider-session"

        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_binding_stays_attached_when_beacon_session_disappears(
    device_config_set_raster_image,
):
    device = _make_mock_device()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=_metadata(SetRasterImageOnAppearAction.uuid)
    )
    registry.provider_session_id.return_value = PROVIDER_SESSION_ID
    registry.provider_instance_provides_provider.return_value = True
    action_bus = _actions_bus()

    async with anyio.create_task_group() as tg:
        manager = DeviceManager(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=FakeHardwareCommandService(),
            config=device_config_set_raster_image,
            manager=registry,
            actions_bus=_actions_session(action_bus),
            start_soon=tg.start_soon,
        )
        _seed_action_availability(
            manager,
            _metadata(SetRasterImageOnAppearAction.uuid),
        )
        await manager.set_page(profile="default", page=0)
        ctx = await manager.action_contexts.get("0,0")
        assert ctx is not None

        registry.get_action.return_value = None
        await manager.on_action_catalog_changed(
            _catalog_event(
                removed=[
                    f"{PROVIDER_INSTANCE_ID}::{SetRasterImageOnAppearAction.uuid}"
                ]
            )
        )

        assert await manager.action_contexts.get("0,0") is ctx
        assert manager._binding_leases

        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_attached_binding_survives_nonterminal_provider_session_unavailable(
    device_config_set_raster_image,
):
    device = _make_mock_device()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=_metadata(SetRasterImageOnAppearAction.uuid)
    )
    command_service = FakeHardwareCommandService()
    manager = DeviceManager(
        controller_id=CONTROLLER_ID,
        device=device,
        hardware_ref=_hardware_ref(device),
        command_service=command_service,
        config=device_config_set_raster_image,
        manager=registry,
        actions_bus=_actions_session(_actions_bus()),
        start_soon=lambda fn, *a, **k: None,
    )
    _seed_action_availability(
        manager,
        _metadata(SetRasterImageOnAppearAction.uuid),
    )
    await manager.set_page(profile="default", page=0)
    ctx = await manager.action_contexts.get("0,0")
    assert ctx is not None
    lease = manager._binding_lease_for_control("0,0")
    assert lease is not None
    assert lease.attached
    command_service.clear_raster.reset_mock()

    await manager._reconcile_binding_sessions()

    assert await manager.action_contexts.get("0,0") is ctx
    assert manager._binding_lease_for_control("0,0") is lease
    assert lease.attached
    command_service.clear_raster.assert_not_awaited()

    command = await _action_command_for_active_binding(
        manager,
        BINDING_OUTPUT,
        {
            "binding": ctx.metadata.model_dump(
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
            "commandType": "clear",
            "generation": ctx.metadata.output_generation,
        },
    )
    await manager.handle_command(
        command.model_copy(update={"sender_session_id": "stale"})
    )
    command_service.clear_raster.assert_not_awaited()

    await manager.handle_command(command)

    command_service.clear_raster.assert_awaited_once_with(
        "test-device",
        "0,0",
        "raster.bitmap",
    )


@pytest.mark.asyncio
async def test_beacon_session_reappearance_does_not_resend_binding_attached(
    device_config_set_raster_image,
):
    device = _make_mock_device()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=_metadata(SetRasterImageOnAppearAction.uuid)
    )
    registry.provider_session_id.return_value = PROVIDER_SESSION_ID
    registry.provider_instance_provides_provider.return_value = True
    action_bus = _actions_bus()

    async with anyio.create_task_group() as tg:
        manager = DeviceManager(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=FakeHardwareCommandService(),
            config=device_config_set_raster_image,
            manager=registry,
            actions_bus=_actions_session(action_bus),
            start_soon=tg.start_soon,
        )
        async with action_bus.subscribe(PROVIDER_ADDR) as stream:
            _seed_action_availability(
                manager,
                _metadata(SetRasterImageOnAppearAction.uuid),
            )
            await manager.set_page(profile="default", page=0)
            assert await manager.action_contexts.get("0,0") is not None
            await _drain_action_messages(stream)

            await manager.on_action_catalog_changed(
                _catalog_event(
                    added=[
                        f"{PROVIDER_INSTANCE_ID}::{SetRasterImageOnAppearAction.uuid}"
                    ]
                )
            )

            assert await manager.action_contexts.get("0,0") is not None
            await _assert_no_action_message(stream)

        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_binding_and_page_navigation_do_not_require_provider_session_contract():
    device = _make_mock_device()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=_metadata(SetRasterImageOnAppearAction.uuid)
    )
    registry.provider_session_id.return_value = PROVIDER_SESSION_ID
    registry.provider_instance_provides_provider.return_value = True
    action_bus = _actions_bus()
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
                                action=SetRasterImageOnAppearAction.uuid,
                                settings={},
                            )
                        ]
                    ),
                    Page(
                        controls=[
                            Control(
                                selector={"control_id": "1,0"},
                                action=SetRasterImageOnAppearAction.uuid,
                                settings={},
                            )
                        ]
                    ),
                ],
            )
        ],
    )

    async with anyio.create_task_group() as tg:
        manager = DeviceManager(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=FakeHardwareCommandService(),
            config=config,
            manager=registry,
            actions_bus=_actions_session(action_bus),
            start_soon=tg.start_soon,
        )
        _seed_action_availability(
            manager,
            _metadata(SetRasterImageOnAppearAction.uuid),
        )

        await manager.set_page(profile="default", page=0)
        owner_ctx = await manager.action_contexts.get("0,0")
        assert owner_ctx is not None

        await manager.set_page(profile="default", page=1)
        assert await manager.action_contexts.get("1,0") is not None

        await manager.set_page(profile="default", page=0)
        owner_ctx = await manager.action_contexts.get("0,0")
        assert owner_ctx is not None

        await manager.open_page(
            descriptor=_dynamic_page("dynamic-page", "1,0"),
            context_id=owner_ctx.id,
        )
        assert await manager.action_contexts.get("1,0") is not None

        page_session = manager._dynamic_page_session
        assert page_session is not None
        await manager.close_page(context_id=page_session.context_id)
        assert await manager.action_contexts.get("0,0") is not None

        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_binding_survives_provider_session_contract_invalid(
    device_config_set_raster_image,
):
    device = _make_mock_device()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=_metadata(SetRasterImageOnAppearAction.uuid)
    )
    registry.provider_session_id.return_value = None
    registry.provider_instance_provides_provider.return_value = True
    action_bus = _actions_bus()

    async with anyio.create_task_group() as tg:
        manager = DeviceManager(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=FakeHardwareCommandService(),
            config=device_config_set_raster_image,
            manager=registry,
            actions_bus=_actions_session(action_bus),
            start_soon=tg.start_soon,
        )
        _seed_action_availability(
            manager,
            _metadata(SetRasterImageOnAppearAction.uuid),
        )
        await manager.set_page(profile="default", page=0)
        ctx = await manager.action_contexts.get("0,0")
        assert ctx is not None

        await manager._reconcile_binding_sessions()

        assert await manager.action_contexts.get("0,0") is ctx

        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_provider_session_terminal_cleanup_no_longer_revokes_bindings(
    device_config_set_raster_image,
):
    device = _make_mock_device()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=_metadata(SetRasterImageOnAppearAction.uuid)
    )
    registry.provider_session_id.return_value = PROVIDER_SESSION_ID
    registry.provider_instance_provides_provider.return_value = True
    action_bus = _actions_bus()

    async with anyio.create_task_group() as tg:
        manager = DeviceManager(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=FakeHardwareCommandService(),
            config=device_config_set_raster_image,
            manager=registry,
            actions_bus=_actions_session(action_bus),
            start_soon=tg.start_soon,
        )
        async with action_bus.subscribe(PROVIDER_ADDR) as stream:
            _seed_action_availability(
                manager,
                _metadata(SetRasterImageOnAppearAction.uuid),
            )
            await manager.set_page(profile="default", page=0)
            owner_ctx = await manager.action_contexts.get("0,0")
            assert owner_ctx is not None
            await _drain_action_messages(stream)

            await manager.open_page(
                descriptor=_dynamic_page("dynamic-page", "1,0"),
                context_id=owner_ctx.id,
            )
            page_session = manager._dynamic_page_session
            assert page_session is not None
            child_ctx = await manager.action_contexts.get("1,0")
            assert child_ctx is not None
            await _drain_action_messages(stream)

            await manager.on_event(_hardware_input("1,0", "down", sequence=1))
            assert manager._held_input_bindings
            await _drain_action_messages(stream)

            await manager._reconcile_binding_sessions()

            assert manager._dynamic_page_session is page_session
            assert await manager.action_contexts.get("1,0") is child_ctx
            assert manager._binding_leases
            assert manager._held_input_bindings
            await _assert_no_action_message(stream)

        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_dynamic_page_survives_action_beacon_withdrawal_with_valid_session(
    device_config_set_raster_image,
):
    device = _make_mock_device()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=_metadata(SetRasterImageOnAppearAction.uuid)
    )
    registry.provider_session_id.return_value = PROVIDER_SESSION_ID
    registry.provider_instance_provides_provider.return_value = True
    action_bus = _actions_bus()

    async with anyio.create_task_group() as tg:
        manager = DeviceManager(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=FakeHardwareCommandService(),
            config=device_config_set_raster_image,
            manager=registry,
            actions_bus=_actions_session(action_bus),
            start_soon=tg.start_soon,
        )
        _seed_action_availability(
            manager,
            _metadata(SetRasterImageOnAppearAction.uuid),
        )
        await manager.set_page(profile="default", page=0)
        owner_ctx = await manager.action_contexts.get("0,0")
        assert owner_ctx is not None

        await manager.open_page(
            descriptor=_dynamic_page("dynamic-page", "1,0"),
            context_id=owner_ctx.id,
        )
        session = manager._dynamic_page_session
        assert session is not None
        child_ctx = await manager.action_contexts.get("1,0")
        assert child_ctx is not None

        registry.get_action.return_value = None
        await manager.on_action_catalog_changed(
            _catalog_event(
                removed=[f"{PROVIDER_INSTANCE_ID}::{SetRasterImageOnAppearAction.uuid}"]
            )
        )

        assert manager._dynamic_page_session is session
        assert await manager.action_contexts.get("1,0") is child_ctx

        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_close_dynamic_page_restores_cached_static_plan_after_beacon_withdrawal(
    device_config_set_raster_image,
):
    device = _make_mock_device()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=_metadata(SetRasterImageOnAppearAction.uuid)
    )
    registry.provider_session_id.return_value = PROVIDER_SESSION_ID
    registry.provider_instance_provides_provider.return_value = True
    action_bus = _actions_bus()

    async with anyio.create_task_group() as tg:
        manager = DeviceManager(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=FakeHardwareCommandService(),
            config=device_config_set_raster_image,
            manager=registry,
            actions_bus=_actions_session(action_bus),
            start_soon=tg.start_soon,
        )
        _seed_action_availability(
            manager,
            _metadata(SetRasterImageOnAppearAction.uuid),
        )
        await manager.set_page(profile="default", page=0)
        owner_ctx = await manager.action_contexts.get("0,0")
        assert owner_ctx is not None

        await manager.open_page(
            descriptor=_dynamic_page("dynamic-page", "1,0"),
            context_id=owner_ctx.id,
        )
        session = manager._dynamic_page_session
        assert session is not None
        child_ctx = await manager.action_contexts.get("1,0")
        assert child_ctx is not None

        registry.get_action.return_value = None
        await manager.on_action_catalog_changed(
            _catalog_event(
                removed=[
                    f"{PROVIDER_INSTANCE_ID}::{SetRasterImageOnAppearAction.uuid}"
                ]
            )
        )
        assert manager._dynamic_page_session is session
        assert await manager.action_contexts.get("1,0") is child_ctx

        registry.get_action.reset_mock()
        await manager.close_page(context_id=session.context_id)

        registry.get_action.assert_not_awaited()
        restored_ctx = await manager.action_contexts.get("0,0")
        assert restored_ctx is not None
        assert restored_ctx.provider_session_id == PROVIDER_SESSION_ID
        assert manager._dynamic_page_session is None

        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_close_dynamic_page_restores_binding_with_action_instance_context(
    device_config_set_raster_image,
):
    device = _make_mock_device()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=_metadata(SetRasterImageOnAppearAction.uuid)
    )
    registry.provider_session_id.return_value = PROVIDER_SESSION_ID
    registry.provider_instance_provides_provider.return_value = True
    action_bus = _actions_bus()

    async with anyio.create_task_group() as tg:
        manager = DeviceManager(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=FakeHardwareCommandService(),
            config=device_config_set_raster_image,
            manager=registry,
            actions_bus=_actions_session(action_bus),
            start_soon=tg.start_soon,
        )
        async with action_bus.subscribe(PROVIDER_ADDR) as stream:
            _seed_action_availability(
                manager,
                _metadata(SetRasterImageOnAppearAction.uuid),
            )
            await manager.set_page(profile="default", page=0)
            owner_ctx = await manager.action_contexts.get("0,0")
            assert owner_ctx is not None
            initial_messages = await _collect_action_messages(stream)
            initial_created = next(
                msg
                for msg in initial_messages
                if msg.message_type == ACTION_INSTANCE_CREATED
            )
            assert (
                thaw_json(initial_created.body)["metadata"]["contextId"]
                == owner_ctx.id
            )

            await manager.open_page(
                descriptor=_dynamic_page("dynamic-page", "1,0"),
                context_id=owner_ctx.id,
            )
            session = manager._dynamic_page_session
            assert session is not None
            await _drain_action_messages(stream)

            await manager.close_page(context_id=session.context_id)

            restored_ctx = await manager.action_contexts.get("0,0")
            assert restored_ctx is not None
            assert restored_ctx.id == owner_ctx.id
            close_messages = await _collect_action_messages(stream)
            restored_attached = next(
                msg
                for msg in close_messages
                if msg.message_type == "bindingAttached"
                and thaw_json(msg.body)["binding"]["controlRef"]["controlId"] == "0,0"
            )
            assert (
                thaw_json(restored_attached.body)["binding"]["contextId"]
                == owner_ctx.id
            )

        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_static_page_installs_without_provider_session_preparation():
    device = _make_mock_device()
    action_bus = _actions_bus()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=_metadata(SetRasterImageOnAppearAction.uuid)
    )
    registry.provider_session_id.return_value = PROVIDER_SESSION_ID
    provider_sessions = ReadyProviderSessions()
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
                                action=SetRasterImageOnAppearAction.uuid,
                                settings={},
                            ),
                            Control(
                                selector={"control_id": "1,0"},
                                action=SetRasterImageOnAppearAction.uuid,
                                settings={},
                            ),
                        ]
                    )
                ],
            )
        ],
    )

    manager = DeviceManager(
        controller_id=CONTROLLER_ID,
        device=device,
        hardware_ref=_hardware_ref(device),
        command_service=FakeHardwareCommandService(),
        config=config,
        manager=registry,
        actions_bus=_actions_session(action_bus),
        start_soon=lambda fn, *a, **k: None,
    )
    _seed_action_availability(
        manager,
        _metadata(SetRasterImageOnAppearAction.uuid),
    )

    await manager.set_page(profile="default", page=0)

    assert provider_sessions.prepare_calls == []
    assert await manager.action_contexts.get("0,0") is not None
    assert await manager.action_contexts.get("1,0") is not None

    await manager._reconcile_binding_sessions()

    assert provider_sessions.refresh_calls == []


@pytest.mark.asyncio
async def test_action_command_authorization_uses_cached_provider_session():
    device = _make_mock_device()
    action_bus = _actions_bus()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=_metadata(SetRasterImageOnAppearAction.uuid)
    )
    registry.provider_session_id.return_value = PROVIDER_SESSION_ID
    provider_sessions = ReadyProviderSessions()

    manager = DeviceManager(
        controller_id=CONTROLLER_ID,
        device=device,
        hardware_ref=_hardware_ref(device),
        command_service=FakeHardwareCommandService(),
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
                                    action=SetRasterImageOnAppearAction.uuid,
                                    settings={},
                                )
                            ]
                        )
                    ],
                )
            ],
        ),
        manager=registry,
        actions_bus=_actions_session(action_bus),
        start_soon=lambda fn, *a, **k: None,
    )
    _seed_action_availability(
        manager,
        _metadata(SetRasterImageOnAppearAction.uuid),
    )
    await manager.set_page(profile="default", page=0)
    ctx = await manager.action_contexts.get("0,0")
    assert ctx is not None

    authorization = await manager._authorize_action_command(
        _action_command(
            BINDING_OUTPUT,
            {
                "binding": ctx.metadata.model_dump(
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
                "commandType": "clear",
                "generation": 0,
            },
            context_id=ctx.id,
            action_instance_id=ctx.action_instance_id,
            binding_id=ctx.binding_id,
        ),
        context_id=ctx.id,
    )

    assert authorization is not None
    assert provider_sessions.refresh_calls == []


@pytest.mark.asyncio
async def test_dynamic_page_replace_preserves_rebound_control_outputs(
    device_config_set_raster_image, persistence_tmp_dir
):
    """Dynamic page replacement should not blank controls that are rebound immediately."""
    device = _make_mock_device()
    action_bus = _actions_bus()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=_metadata(SetRasterImageOnAppearAction.uuid)
    )
    command_service = FakeHardwareCommandService()

    async with anyio.create_task_group() as tg:
        manager = DeviceManager(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=command_service,
            config=device_config_set_raster_image,
            manager=registry,
            actions_bus=_actions_session(action_bus),
            start_soon=tg.start_soon,
        )
        _seed_action_availability(
            manager,
            _metadata(SetRasterImageOnAppearAction.uuid),
        )
        await manager.set_page(profile="default", page=0)
        owner_ctx = await manager.action_contexts.get("0,0")
        assert owner_ctx is not None
        registry.get_action.reset_mock()
        await manager.open_page(
            descriptor=_dynamic_page("dynamic-page", "0,0", "1,0"),
            context_id=owner_ctx.id,
        )
        assert registry.get_action.await_count == 0
        session = manager._dynamic_page_session
        assert session is not None

        command_service.clear_raster.reset_mock()
        await manager.replace_page(
            descriptor=_dynamic_page(session.page_id, "0,0", "1,0"),
            context_id=session.context_id,
        )

        command_service.clear_raster.assert_not_awaited()
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_held_input_cancelled_when_dynamic_page_rebinds_control(
    device_config_set_raster_image, persistence_tmp_dir
):
    device = _make_mock_device()
    action_bus = _actions_bus()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=_metadata(SetRasterImageOnAppearAction.uuid)
    )
    command_service = FakeHardwareCommandService()

    async with anyio.create_task_group() as tg:
        manager = DeviceManager(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=command_service,
            config=device_config_set_raster_image,
            manager=registry,
            actions_bus=_actions_session(action_bus),
            start_soon=tg.start_soon,
        )
        async with action_bus.subscribe(PROVIDER_ADDR) as stream:
            _seed_action_availability(
                manager,
                _metadata(SetRasterImageOnAppearAction.uuid),
            )
            await manager.set_page(profile="default", page=0)
            await _drain_action_messages(stream)
            owner_ctx = await manager.action_contexts.get("0,0")
            assert owner_ctx is not None

            await manager.on_event(_hardware_input("0,0", "down", sequence=1))
            body = await _next_capability_input(stream)
            assert body.event.event_type == "down"
            assert body.binding.binding_id == owner_ctx.binding_id

            await manager.open_page(
                descriptor=_dynamic_page("dynamic-page", "0,0"),
                context_id=owner_ctx.id,
            )
            child_ctx = await manager.action_contexts.get("0,0")
            assert child_ctx is not None
            assert child_ctx.binding_id != owner_ctx.binding_id
            body = await _next_capability_input(stream)
            assert body.event.event_type == "cancel"
            assert body.binding.binding_id == owner_ctx.binding_id
            await _drain_action_messages(stream)

            await manager.on_event(_hardware_input("0,0", "up", sequence=2))
            await _assert_no_action_message(stream)

            await manager.on_event(_hardware_input("0,0", "down", sequence=3))
            body = await _next_capability_input(stream)
            assert body.event.event_type == "down"
            assert body.binding.binding_id == child_ctx.binding_id

            await manager.on_event(_hardware_input("0,0", "up", sequence=4))
            body = await _next_capability_input(stream)
            assert body.event.event_type == "up"
            assert body.binding.binding_id == child_ctx.binding_id
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_held_input_cancelled_when_closing_dynamic_page_back_to_static(
    device_config_set_raster_image, persistence_tmp_dir
):
    device = _make_mock_device()
    action_bus = _actions_bus()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=_metadata(SetRasterImageOnAppearAction.uuid)
    )
    command_service = FakeHardwareCommandService()

    async with anyio.create_task_group() as tg:
        manager = DeviceManager(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=command_service,
            config=device_config_set_raster_image,
            manager=registry,
            actions_bus=_actions_session(action_bus),
            start_soon=tg.start_soon,
        )
        async with action_bus.subscribe(PROVIDER_ADDR) as stream:
            _seed_action_availability(
                manager,
                _metadata(SetRasterImageOnAppearAction.uuid),
            )
            await manager.set_page(profile="default", page=0)
            await _drain_action_messages(stream)
            owner_ctx = await manager.action_contexts.get("0,0")
            assert owner_ctx is not None

            await manager.open_page(
                descriptor=_dynamic_page("dynamic-page", "0,0"),
                context_id=owner_ctx.id,
            )
            child_ctx = await manager.action_contexts.get("0,0")
            assert child_ctx is not None
            await _drain_action_messages(stream)

            await manager.on_event(_hardware_input("0,0", "down", sequence=1))
            body = await _next_capability_input(stream)
            assert body.event.event_type == "down"
            assert body.binding.binding_id == child_ctx.binding_id

            session = manager._dynamic_page_session
            assert session is not None
            await manager.close_page(context_id=session.context_id)
            restored_ctx = await manager.action_contexts.get("0,0")
            assert restored_ctx is not None
            assert restored_ctx.binding_id != child_ctx.binding_id
            body = await _next_capability_input(stream)
            assert body.event.event_type == "cancel"
            assert body.binding.binding_id == child_ctx.binding_id
            await _drain_action_messages(stream)

            await manager.on_event(_hardware_input("0,0", "up", sequence=2))
            await _assert_no_action_message(stream)
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_held_input_cancelled_when_config_is_removed(
    device_config_set_raster_image, persistence_tmp_dir
):
    device = _make_mock_device()
    action_bus = _actions_bus()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=_metadata(SetRasterImageOnAppearAction.uuid)
    )

    async with anyio.create_task_group() as tg:
        manager = DeviceManager(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=FakeHardwareCommandService(),
            config=device_config_set_raster_image,
            manager=registry,
            actions_bus=_actions_session(action_bus),
            start_soon=tg.start_soon,
        )
        async with action_bus.subscribe(PROVIDER_ADDR) as stream:
            _seed_action_availability(
                manager,
                _metadata(SetRasterImageOnAppearAction.uuid),
            )
            await manager.set_page(profile="default", page=0)
            await _drain_action_messages(stream)
            ctx = await manager.action_contexts.get("0,0")
            assert ctx is not None

            await manager.on_event(_hardware_input("0,0", "down", sequence=1))
            body = await _next_capability_input(stream)
            assert body.event.event_type == "down"
            assert body.binding.binding_id == ctx.binding_id

            await manager._on_config_changed(None)

            body = await _next_capability_input(stream)
            assert body.event.event_type == "cancel"
            assert body.binding.binding_id == ctx.binding_id
            await _drain_action_messages(stream)
            assert not manager._config_active
            assert await manager.action_contexts.get("0,0") is None

            await manager.on_event(_hardware_input("0,0", "up", sequence=2))
            await _assert_no_action_message(stream)
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_close_dynamic_page_restores_when_close_notification_fails(
    device_config_set_raster_image,
    monkeypatch: pytest.MonkeyPatch,
):
    device = _make_mock_device()
    action_bus = _actions_bus()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=_metadata(SetRasterImageOnAppearAction.uuid)
    )
    original_send = device_manager_module.send_with_endpoint_identity

    async def fail_page_closed(endpoint, message):
        if message.message_type == PAGE_SESSION_CLOSED:
            raise RuntimeError("provider offline")
        return await original_send(endpoint, message)

    monkeypatch.setattr(
        device_manager_module,
        "send_with_endpoint_identity",
        fail_page_closed,
    )

    async with anyio.create_task_group() as tg:
        manager = DeviceManager(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=FakeHardwareCommandService(),
            config=device_config_set_raster_image,
            manager=registry,
            actions_bus=_actions_session(action_bus),
            start_soon=tg.start_soon,
        )
        _seed_action_availability(
            manager,
            _metadata(SetRasterImageOnAppearAction.uuid),
        )
        await manager.set_page(profile="default", page=0)
        owner_ctx = await manager.action_contexts.get("0,0")
        assert owner_ctx is not None

        await manager.open_page(
            descriptor=_dynamic_page("dynamic-page", "1,0"),
            context_id=owner_ctx.id,
        )
        session = manager._dynamic_page_session
        assert session is not None

        await manager.close_page(context_id=session.context_id)

        assert manager._dynamic_page_session is None
        assert await manager.action_contexts.get("0,0") is not None
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_release_on_same_binding_is_delivered(
    device_config_set_raster_image, persistence_tmp_dir
):
    device = _make_mock_device()
    action_bus = _actions_bus()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=_metadata(SetRasterImageOnAppearAction.uuid)
    )
    command_service = FakeHardwareCommandService()

    async with anyio.create_task_group() as tg:
        manager = DeviceManager(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=command_service,
            config=device_config_set_raster_image,
            manager=registry,
            actions_bus=_actions_session(action_bus),
            start_soon=tg.start_soon,
        )
        async with action_bus.subscribe(PROVIDER_ADDR) as stream:
            _seed_action_availability(
                manager,
                _metadata(SetRasterImageOnAppearAction.uuid),
            )
            await manager.set_page(profile="default", page=0)
            await _drain_action_messages(stream)
            ctx = await manager.action_contexts.get("0,0")
            assert ctx is not None

            await manager.on_event(_hardware_input("0,0", "down", sequence=1))
            body = await _next_capability_input(stream)
            assert body.event.event_type == "down"
            assert body.binding.binding_id == ctx.binding_id

            await manager.on_event(_hardware_input("0,0", "up", sequence=2))
            body = await _next_capability_input(stream)
            assert body.event.event_type == "up"
            assert body.binding.binding_id == ctx.binding_id
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_open_page_from_dynamic_child_dismisses_previous_owner(
    device_config_set_raster_image, persistence_tmp_dir
):
    device = _make_mock_device()
    action_bus = _actions_bus()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        side_effect=lambda uuid, provider_instance_id=None, **_: _metadata(
            uuid,
            provider_instance_id=provider_instance_id or PROVIDER_INSTANCE_ID,
        )
    )
    command_service = FakeHardwareCommandService()

    async with anyio.create_task_group() as tg:
        manager = DeviceManager(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=command_service,
            config=device_config_set_raster_image,
            manager=registry,
            actions_bus=_actions_session(action_bus),
            start_soon=tg.start_soon,
        )
        _seed_action_availability(
            manager,
            _metadata(SetRasterImageOnAppearAction.uuid),
            _metadata(
                "test.virtual.child",
                provider_instance_id="python-child",
            ),
        )
        await manager.set_page(profile="default", page=0)
        owner_ctx = await manager.action_contexts.get("0,0")
        assert owner_ctx is not None
        await manager.open_page(
            descriptor=_dynamic_page_with_action_child(
                "first-page",
                "1,0",
                action_id="test.virtual.child",
                provider_instance_id="python-child",
            ),
            context_id=owner_ctx.id,
        )
        first_session = manager._dynamic_page_session
        assert first_session is not None
        child_ctx = await manager.action_contexts.get("1,0")
        assert child_ctx is not None

        await manager.open_page(
            descriptor=_dynamic_page("second-page", "0,0"),
            context_id=child_ctx.id,
        )

        second_session = manager._dynamic_page_session
        assert second_session is not None
        assert second_session.page_id == "second-page"
        assert second_session.page_session_id != first_session.page_session_id
        assert second_session.owner_binding_id == child_ctx.binding_id
        assert second_session.owner_provider_instance_id == "python-child"
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_replace_page_from_non_owner_is_noop(
    device_config_set_raster_image, persistence_tmp_dir
):
    device = _make_mock_device()
    action_bus = _actions_bus()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        side_effect=lambda uuid, provider_instance_id=None, **_: _metadata(
            uuid,
            provider_instance_id=provider_instance_id or PROVIDER_INSTANCE_ID,
        )
    )
    command_service = FakeHardwareCommandService()

    async with anyio.create_task_group() as tg:
        manager = DeviceManager(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=command_service,
            config=device_config_set_raster_image,
            manager=registry,
            actions_bus=_actions_session(action_bus),
            start_soon=tg.start_soon,
        )
        _seed_action_availability(
            manager,
            _metadata(SetRasterImageOnAppearAction.uuid),
            _metadata(
                "test.virtual.child",
                provider_instance_id="python-child",
            ),
        )
        await manager.set_page(profile="default", page=0)
        owner_ctx = await manager.action_contexts.get("0,0")
        assert owner_ctx is not None
        await manager.open_page(
            descriptor=_dynamic_page_with_action_child(
                "dynamic-page",
                "1,0",
                action_id="test.virtual.child",
                provider_instance_id="python-child",
            ),
            context_id=owner_ctx.id,
        )
        session = manager._dynamic_page_session
        assert session is not None
        child_ctx = await manager.action_contexts.get("1,0")
        assert child_ctx is not None

        await manager.replace_page(
            descriptor=_dynamic_page(session.page_id, "0,0"),
            context_id=child_ctx.id,
        )

        assert manager._dynamic_page_session is session
        assert await manager.action_contexts.get("1,0") is child_ctx
        assert await manager.action_contexts.get("0,0") is None
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_close_page_from_non_owner_is_noop(
    device_config_set_raster_image, persistence_tmp_dir
):
    device = _make_mock_device()
    action_bus = _actions_bus()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        side_effect=lambda uuid, provider_instance_id=None, **_: _metadata(
            uuid,
            provider_instance_id=provider_instance_id or PROVIDER_INSTANCE_ID,
        )
    )
    command_service = FakeHardwareCommandService()

    async with anyio.create_task_group() as tg:
        manager = DeviceManager(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=command_service,
            config=device_config_set_raster_image,
            manager=registry,
            actions_bus=_actions_session(action_bus),
            start_soon=tg.start_soon,
        )
        _seed_action_availability(
            manager,
            _metadata(SetRasterImageOnAppearAction.uuid),
            _metadata(
                "test.virtual.child",
                provider_instance_id="python-child",
            ),
        )
        await manager.set_page(profile="default", page=0)
        owner_ctx = await manager.action_contexts.get("0,0")
        assert owner_ctx is not None
        await manager.open_page(
            descriptor=_dynamic_page_with_action_child(
                "dynamic-page",
                "1,0",
                action_id="test.virtual.child",
                provider_instance_id="python-child",
            ),
            context_id=owner_ctx.id,
        )
        session = manager._dynamic_page_session
        assert session is not None
        child_ctx = await manager.action_contexts.get("1,0")
        assert child_ctx is not None

        await manager.close_page(context_id=child_ctx.id)

        assert manager._dynamic_page_session is session
        assert await manager.action_contexts.get("1,0") is child_ctx
        assert await manager.action_contexts.get("0,0") is None
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_dynamic_page_replace_from_owner_during_nonterminal_unavailable(
    device_config_set_raster_image,
):
    device = _make_mock_device()
    action_bus = _actions_bus()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=_metadata(SetRasterImageOnAppearAction.uuid)
    )

    manager = DeviceManager(
        controller_id=CONTROLLER_ID,
        device=device,
        hardware_ref=_hardware_ref(device),
        command_service=FakeHardwareCommandService(),
        config=device_config_set_raster_image,
        manager=registry,
        actions_bus=_actions_session(action_bus),
        start_soon=lambda fn, *a, **k: None,
    )
    _seed_action_availability(
        manager,
        _metadata(SetRasterImageOnAppearAction.uuid),
    )
    await manager.set_page(profile="default", page=0)
    owner_ctx = await manager.action_contexts.get("0,0")
    assert owner_ctx is not None
    await manager.open_page(
        descriptor=_dynamic_page("dynamic-page", "1,0"),
        context_id=owner_ctx.id,
    )
    session = manager._dynamic_page_session
    assert session is not None
    child_ctx = await manager.action_contexts.get("1,0")
    assert child_ctx is not None

    await manager._reconcile_binding_sessions()

    replacement = _dynamic_page(session.page_id, "0,0")
    await manager.handle_command(
        _page_session_command(
            REPLACE_PAGE,
            {
                "descriptor": replacement.model_dump(
                    by_alias=True,
                    exclude_none=True,
                    mode="json",
                )
            },
            session_id=session.page_session_id,
            context_id=session.context_id,
            action_instance_id=session.action_instance_id,
        )
    )

    assert manager._dynamic_page_session is session
    assert await manager.action_contexts.get("1,0") is None
    assert await manager.action_contexts.get("0,0") is not None


@pytest.mark.asyncio
async def test_action_lifecycle_rejected_binding_detaches_only_that_binding(
    device_config_set_raster_image,
):
    device = _make_mock_device()
    action_bus = _actions_bus()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=_metadata(SetRasterImageOnAppearAction.uuid)
    )
    command_service = FakeHardwareCommandService()

    manager = DeviceManager(
        controller_id=CONTROLLER_ID,
        device=device,
        hardware_ref=_hardware_ref(device),
        command_service=command_service,
        config=device_config_set_raster_image,
        manager=registry,
        actions_bus=_actions_session(action_bus),
        start_soon=lambda fn, *a, **k: None,
    )
    _seed_action_availability(
        manager,
        _metadata(SetRasterImageOnAppearAction.uuid),
    )
    await manager.set_page(profile="default", page=0)
    ctx = await manager.action_contexts.get("0,0")
    assert ctx is not None

    msg = await _action_command_for_active_binding(
        manager,
        ACTION_LIFECYCLE_REJECTED,
        {
            "targetKind": "binding",
            "binding": ctx.metadata.model_dump(
                by_alias=True,
                exclude_none=True,
                mode="json",
            ),
            "reason": "invalid_settings",
        },
    )
    await manager.handle_command(msg)

    assert await manager.action_contexts.get("0,0") is None
    assert manager._binding_leases == {}
    assert ctx.action_instance_id in manager._action_instances


@pytest.mark.asyncio
async def test_action_lifecycle_rejected_action_instance_destroys_affected_bindings(
    device_config_set_raster_image,
):
    device = _make_mock_device()
    action_bus = _actions_bus()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=_metadata(SetRasterImageOnAppearAction.uuid)
    )

    manager = DeviceManager(
        controller_id=CONTROLLER_ID,
        device=device,
        hardware_ref=_hardware_ref(device),
        command_service=FakeHardwareCommandService(),
        config=device_config_set_raster_image,
        manager=registry,
        actions_bus=_actions_session(action_bus),
        start_soon=lambda fn, *a, **k: None,
    )
    _seed_action_availability(
        manager,
        _metadata(SetRasterImageOnAppearAction.uuid),
    )
    await manager.set_page(profile="default", page=0)
    ctx = await manager.action_contexts.get("0,0")
    assert ctx is not None
    metadata = manager._action_instances[ctx.action_instance_id]

    await manager.handle_command(
        _action_instance_command(
            ACTION_LIFECYCLE_REJECTED,
            {
                "targetKind": "action_instance",
                "actionInstance": metadata.model_dump(
                    by_alias=True,
                    exclude_none=True,
                    mode="json",
                ),
                "reason": "action_not_available",
            },
            context_id=metadata.context_id,
            action_instance_id=metadata.action_instance_id,
        )
    )

    assert await manager.action_contexts.get("0,0") is None
    assert manager._binding_leases == {}
    assert ctx.action_instance_id not in manager._action_instances
    assert ctx.action_instance_id not in manager._action_instance_provider_sessions


@pytest.mark.asyncio
async def test_action_instance_rejection_from_owner_during_nonterminal_unavailable(
    device_config_set_raster_image,
):
    device = _make_mock_device()
    action_bus = _actions_bus()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=_metadata(SetRasterImageOnAppearAction.uuid)
    )

    manager = DeviceManager(
        controller_id=CONTROLLER_ID,
        device=device,
        hardware_ref=_hardware_ref(device),
        command_service=FakeHardwareCommandService(),
        config=device_config_set_raster_image,
        manager=registry,
        actions_bus=_actions_session(action_bus),
        start_soon=lambda fn, *a, **k: None,
    )
    _seed_action_availability(
        manager,
        _metadata(SetRasterImageOnAppearAction.uuid),
    )
    await manager.set_page(profile="default", page=0)
    ctx = await manager.action_contexts.get("0,0")
    assert ctx is not None
    metadata = manager._action_instances[ctx.action_instance_id]

    await manager._reconcile_binding_sessions()

    payload = {
        "targetKind": "action_instance",
        "actionInstance": metadata.model_dump(
            by_alias=True,
            exclude_none=True,
            mode="json",
        ),
        "reason": "action_not_available",
    }
    await manager.handle_command(
        _action_instance_command(
            ACTION_LIFECYCLE_REJECTED,
            payload,
            context_id=metadata.context_id,
            action_instance_id=metadata.action_instance_id,
            sender_session_id="stale",
        )
    )

    assert await manager.action_contexts.get("0,0") is ctx
    assert ctx.action_instance_id in manager._action_instances

    await manager.handle_command(
        _action_instance_command(
            ACTION_LIFECYCLE_REJECTED,
            payload,
            context_id=metadata.context_id,
            action_instance_id=metadata.action_instance_id,
        )
    )

    assert await manager.action_contexts.get("0,0") is None
    assert manager._binding_leases == {}
    assert ctx.action_instance_id not in manager._action_instances


@pytest.mark.asyncio
async def test_action_lifecycle_rejected_page_session_closes_child_bindings(
    device_config_set_raster_image,
):
    device = _make_mock_device()
    action_bus = _actions_bus()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=_metadata(SetRasterImageOnAppearAction.uuid)
    )

    async with anyio.create_task_group() as tg:
        manager = DeviceManager(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=FakeHardwareCommandService(),
            config=device_config_set_raster_image,
            manager=registry,
            actions_bus=_actions_session(action_bus),
            start_soon=tg.start_soon,
        )
        _seed_action_availability(
            manager,
            _metadata(SetRasterImageOnAppearAction.uuid),
        )
        await manager.set_page(profile="default", page=0)
        owner_ctx = await manager.action_contexts.get("0,0")
        assert owner_ctx is not None
        await manager.open_page(
            descriptor=_dynamic_page("dynamic-page", "1,0"),
            context_id=owner_ctx.id,
        )
        session = manager._dynamic_page_session
        assert session is not None
        child_ctx = await manager.action_contexts.get("1,0")
        assert child_ctx is not None

        await manager.handle_command(
            _page_session_command(
                ACTION_LIFECYCLE_REJECTED,
                {
                    "targetKind": "page_session",
                    "pageSession": manager._page_session_metadata(session).model_dump(
                        by_alias=True,
                        exclude_none=True,
                        mode="json",
                    ),
                    "reason": "resource_unavailable",
                },
                session_id=session.page_session_id,
                context_id=session.context_id,
                action_instance_id=session.action_instance_id,
            )
        )

        assert manager._dynamic_page_session is None
        assert await manager.action_contexts.get("1,0") is None
        assert all(
            lease.page_session_id != session.page_session_id
            for lease in manager._binding_leases.values()
        )
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_action_lifecycle_rejected_from_stale_provider_session_is_ignored(
    device_config_set_raster_image,
):
    device = _make_mock_device()
    action_bus = _actions_bus()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=_metadata(SetRasterImageOnAppearAction.uuid)
    )

    manager = DeviceManager(
        controller_id=CONTROLLER_ID,
        device=device,
        hardware_ref=_hardware_ref(device),
        command_service=FakeHardwareCommandService(),
        config=device_config_set_raster_image,
        manager=registry,
        actions_bus=_actions_session(action_bus),
        start_soon=lambda fn, *a, **k: None,
    )
    _seed_action_availability(
        manager,
        _metadata(SetRasterImageOnAppearAction.uuid),
    )
    await manager.set_page(profile="default", page=0)
    ctx = await manager.action_contexts.get("0,0")
    assert ctx is not None

    msg = await _action_command_for_active_binding(
        manager,
        ACTION_LIFECYCLE_REJECTED,
        {
            "targetKind": "binding",
            "binding": ctx.metadata.model_dump(
                by_alias=True,
                exclude_none=True,
                mode="json",
            ),
            "reason": "stale_lifecycle",
        },
    )
    await manager.handle_command(msg.model_copy(update={"sender_session_id": "stale"}))

    assert await manager.action_contexts.get("0,0") is ctx
    assert manager._binding_leases


@pytest.mark.asyncio
async def test_set_raster_image_last_write_wins_same_control(
    device_config_set_raster_image, persistence_tmp_dir
):
    """bindingOutput rejects stale or mismatched output generations."""
    from deckr.actions.messages import BINDING_OUTPUT

    device = _make_mock_device()
    action_bus = _actions_bus()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=_metadata(SetRasterImageOnAppearAction.uuid)
    )
    command_service = FakeHardwareCommandService()

    async with anyio.create_task_group() as tg:
        manager = DeviceManager(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=command_service,
            config=device_config_set_raster_image,
            manager=registry,
            actions_bus=_actions_session(action_bus),
            start_soon=tg.start_soon,
        )
        _seed_action_availability(
            manager,
            _metadata(SetRasterImageOnAppearAction.uuid),
        )
        await manager.set_page(profile="default", page=0)
        ctx = await manager.action_contexts.get("0,0")
        assert ctx is not None
        binding = ctx.metadata.model_copy(update={"output_generation": 1})
        msg = await _action_command_for_active_binding(
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
async def test_settings_isolated_by_page_same_control(persistence_tmp_dir):
    """Same control on different pages keeps separate settings."""
    device = _make_mock_device()
    registry = MagicMock()
    registry.get_action = AsyncMock(return_value=_metadata(NoopAction.uuid))
    action_bus = _actions_bus()

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
    config_service = MemoryConfigService(config)
    settings_service = ConfigBackedSettingsService(
        controller_id=CONTROLLER_ID,
        config_service=config_service,
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
            actions_bus=_actions_session(action_bus),
            start_soon=start_soon,
            settings_service=settings_service,
        )
        _seed_action_availability(manager, _metadata(NoopAction.uuid))
        await manager.set_page(profile="default", page=0)
        await anyio.sleep(0.05)
        page0_ctx = await manager.action_contexts.get("0,0")
        await page0_ctx.controller_context.set_settings({"marker": "page0"})

        await manager.set_page(profile="default", page=1)
        page1_ctx = await manager.action_contexts.get("0,0")
        await page1_ctx.controller_context.set_settings({"marker": "page1"})

        await manager.set_page(profile="default", page=0)
        page0_ctx_reload = await manager.action_contexts.get("0,0")
        page0_settings = await page0_ctx_reload.controller_context.get_settings()
        assert page0_settings.marker == "page0"

        await manager.set_page(profile="default", page=1)
        page1_ctx_reload = await manager.action_contexts.get("0,0")
        page1_settings = await page1_ctx_reload.controller_context.get_settings()
        assert page1_settings.marker == "page1"


@pytest.mark.asyncio
async def test_settings_isolated_by_control_same_action(persistence_tmp_dir):
    """Same action on different controls keeps separate settings."""
    device = _make_mock_device()
    registry = MagicMock()
    registry.get_action = AsyncMock(return_value=_metadata(NoopAction.uuid))
    action_bus = _actions_bus()

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
    config_service = MemoryConfigService(config)
    settings_service = ConfigBackedSettingsService(
        controller_id=CONTROLLER_ID,
        config_service=config_service,
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
            actions_bus=_actions_session(action_bus),
            start_soon=start_soon,
            settings_service=settings_service,
        )
        _seed_action_availability(manager, _metadata(NoopAction.uuid))
        await manager.set_page(profile="default", page=0)
        await anyio.sleep(0.05)
        control_a = await manager.action_contexts.get("0,0")
        control_b = await manager.action_contexts.get("1,0")
        await control_a.controller_context.set_settings({"control_marker": "A"})
        await control_b.controller_context.set_settings({"control_marker": "B"})

        await manager.set_page(profile="default", page=0)
        control_a_reload = await manager.action_contexts.get("0,0")
        control_b_reload = await manager.action_contexts.get("1,0")
        settings_a = await control_a_reload.controller_context.get_settings()
        settings_b = await control_b_reload.controller_context.get_settings()
        assert settings_a.control_marker == "A"
        assert settings_b.control_marker == "B"


@pytest.mark.asyncio
async def test_config_reload_clears_runtime_settings_overlay(persistence_tmp_dir):
    device = _make_mock_device()
    registry = MagicMock()
    registry.get_action = AsyncMock(return_value=_metadata(NoopAction.uuid))
    action_bus = _actions_bus()
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
            actions_bus=_actions_session(action_bus),
            start_soon=start_soon,
        )
        _seed_action_availability(manager, _metadata(NoopAction.uuid))
        await manager.set_page(profile="default", page=0)
        ctx = await manager.action_contexts.get("0,0")
        assert ctx is not None
        settings = await ctx.controller_context.get_settings()
        assert settings.label == "from-config"
        assert settings.nested == {"role": {"page": "root"}}

        await ctx.controller_context.set_settings({"label": "runtime", "extra": True})
        runtime_settings = await ctx.controller_context.get_settings()
        assert runtime_settings.label == "runtime"
        assert runtime_settings.extra is True

        await manager._on_config_changed(reloaded_config)
        reloaded_ctx = await manager.action_contexts.get("0,0")
        assert reloaded_ctx is not None
        reloaded_settings = await reloaded_ctx.controller_context.get_settings()
        assert vars(reloaded_settings) == {"label": "from-reload"}


@pytest.mark.asyncio
async def test_clear_page_can_skip_hardware_output_for_disconnect(persistence_tmp_dir):
    device = _make_mock_device()
    registry = MagicMock()
    registry.get_action = AsyncMock(return_value=_metadata(NoopAction.uuid))
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
        actions_bus=_actions_session(_actions_bus()),
        start_soon=lambda fn, *a, **k: None,
    )
    _seed_action_availability(manager, _metadata(NoopAction.uuid))

    await manager.set_page(profile="default", page=0)
    assert await manager.action_contexts.get("0,0") is not None
    command_service.clear_raster.reset_mock()
    command_service.clear_raster.side_effect = LookupError("No live hardware route")

    await manager.clear_page(clear_outputs=False)

    command_service.clear_raster.assert_not_awaited()
    assert await manager.action_contexts.get("0,0") is None


class ConfigurableActionRegistry:
    """Registry that can add/remove actions for testing catalog-change handling.

    Uses qualified IDs (provider_instance_id::action_uuid) internally to match ActionRegistry.
    """

    def __init__(self):
        self._actions: dict[str, ActionMetadata] = {}

    def _qualified_id(self, provider_instance_id: str, action_uuid: str) -> str:
        return f"{provider_instance_id}::{action_uuid}"

    async def get_action(
        self,
        address: str,
        *,
        provider_instance_id: str | None = None,
        provider_labels: dict[str, str] | None = None,
    ) -> ActionMetadata | None:
        del provider_labels
        if "::" in address:
            return self._actions.get(address)
        for key, meta in self._actions.items():
            if (
                provider_instance_id is not None
                and meta.provider_instance_id != provider_instance_id
            ):
                continue
            if key.endswith(f"::{address}"):
                return meta
        return None

    def provider_instance_provides_provider(
        self,
        provider_instance_id: str,
        provider_id: str,
    ) -> bool:
        return any(
            meta.provider_instance_id == provider_instance_id
            and meta.provider_id == provider_id
            for meta in self._actions.values()
        )

    def provider_session_id(self, provider_instance_id: str) -> str | None:
        del provider_instance_id
        return None

    def add_action(self, action_uuid: str, meta: ActionMetadata) -> None:
        qualified = self._qualified_id(meta.provider_instance_id, action_uuid)
        self._actions[qualified] = meta

    def remove_action(self, action_uuid: str, provider_instance_id: str) -> None:
        qualified = self._qualified_id(provider_instance_id, action_uuid)
        self._actions.pop(qualified, None)

    def get_builtin_action(self, uuid: str):
        return None


ACTION_X_UUID = "test.action.x"


@pytest.mark.asyncio
async def test_provider_direct_availability_resolves_candidate_control(
    persistence_tmp_dir,
):
    """Beacon candidates render pending until provider-direct availability arrives."""
    device = _make_mock_device()
    action_bus = _actions_bus()
    registry = ConfigurableActionRegistry()
    # Initially no action - control will show unavailable
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
            actions_bus=_actions_session(action_bus),
            start_soon=start_soon,
        )
        await manager.set_page(profile="default", page=0)
        await anyio.sleep(0.05)

        # Control should be unavailable (no context)
        ctx_before = await manager.action_contexts.get("0,0")
        assert ctx_before is None

        # Add action and notify
        registry.add_action(
            ACTION_X_UUID,
            _metadata(
                ACTION_X_UUID,
                provider_instance_id="test-provider",
                provider_id="test",
            ),
        )
        await manager.on_action_catalog_changed(
            _catalog_event(added=[f"test-provider::{ACTION_X_UUID}"])
        )

        key = ProviderActionKey("test-provider", ACTION_X_UUID)
        record = manager._action_availability.record_for(key)
        assert record is not None
        assert record.state == ActionAvailabilityState.UNKNOWN
        assert record.source == ActionAvailabilitySource.BEACON_CANDIDATE
        assert await manager.action_contexts.get("0,0") is None
        assert (
            manager._action_availability.snapshot_for_intents(
                manager._current_plan_action_intents()
            )
            == {}
        )

        changed = manager._action_availability_service.ingest_provider_entries(
            provider_instance_id="test-provider",
            provider_id="test",
            entries=[_availability_entry(ACTION_X_UUID)],
        )
        await manager.on_action_availability_changed(changed)

        ctx_after = await manager.action_contexts.get("0,0")
        assert ctx_after is not None
        assert ctx_after.action_uuid == ACTION_X_UUID


@pytest.mark.asyncio
async def test_unrelated_availability_change_does_not_rebuild_current_page(
    persistence_tmp_dir,
    monkeypatch: pytest.MonkeyPatch,
):
    device = _make_mock_device()
    action_bus = _actions_bus()
    registry = ConfigurableActionRegistry()
    registry.add_action(
        ACTION_X_UUID,
        _metadata(
            ACTION_X_UUID,
            provider_instance_id="test-provider",
            provider_id="test",
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
            actions_bus=_actions_session(action_bus),
            start_soon=tg.start_soon,
        )
        _seed_action_availability(
            manager,
            _metadata(
                ACTION_X_UUID,
                provider_instance_id="test-provider",
                provider_id="test",
            ),
        )
        await manager.set_page(profile="default", page=0)
        ctx_before = await manager.action_contexts.get("0,0")
        assert ctx_before is not None

        build_page_plan = AsyncMock(wraps=manager._build_page_plan)
        monkeypatch.setattr(manager, "_build_page_plan", build_page_plan)
        command_service.clear_raster.reset_mock()

        await manager.on_action_availability_changed(
            {ProviderActionKey("other-provider", "test.action.unrelated")}
        )

        build_page_plan.assert_not_awaited()
        command_service.clear_raster.assert_not_awaited()
        assert await manager.action_contexts.get("0,0") is ctx_before
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_default_availability_service_skips_legacy_lookup_without_direct_record(
    persistence_tmp_dir,
):
    device = _make_mock_device()
    action_bus = _actions_bus()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=_metadata(
            ACTION_X_UUID,
            provider_instance_id="test-provider",
            provider_id="test",
        )
    )
    registry.provider_session_id.return_value = PROVIDER_SESSION_ID
    registry.provider_instance_provides_provider.return_value = True
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
                                provider_instance_id="test-provider",
                                settings={},
                            )
                        ]
                    )
                ],
            )
        ],
    )

    manager = DeviceManager(
        controller_id=CONTROLLER_ID,
        device=device,
        hardware_ref=_hardware_ref(device),
        command_service=FakeHardwareCommandService(),
        config=config,
        manager=registry,
        actions_bus=_actions_session(action_bus),
        start_soon=lambda *args, **kwargs: None,
    )

    await manager.set_page(profile="default", page=0)

    registry.get_action.assert_not_awaited()
    assert await manager.action_contexts.get("0,0") is None


@pytest.mark.asyncio
async def test_on_action_catalog_changed_removed_preserves_attached_context(
    persistence_tmp_dir,
):
    device = _make_mock_device()
    action_bus = _actions_bus()
    registry = ConfigurableActionRegistry()
    registry.add_action(
        ACTION_X_UUID,
        _metadata(
            ACTION_X_UUID,
            provider_instance_id="test-provider",
            provider_id="test",
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
            actions_bus=_actions_session(action_bus),
            start_soon=tg.start_soon,
        )
        _seed_action_availability(
            manager,
            _metadata(
                ACTION_X_UUID,
                provider_instance_id="test-provider",
                provider_id="test",
            ),
        )
        await manager.set_page(profile="default", page=0)
        await anyio.sleep(0.05)

        ctx_before = await manager.action_contexts.get("0,0")
        assert ctx_before is not None
        key = ProviderActionKey("test-provider", ACTION_X_UUID)
        assert manager._action_availability.record_for(key) is not None

        registry.remove_action(ACTION_X_UUID, "test-provider")
        await manager.on_action_catalog_changed(
            _catalog_event(removed=[f"test-provider::{ACTION_X_UUID}"])
        )

        record = manager._action_availability.record_for(key)
        assert record is not None
        assert record.source == ActionAvailabilitySource.PROVIDER_DIRECT
        assert record.state == ActionAvailabilityState.AVAILABLE
        assert await manager.action_contexts.get("0,0") is ctx_before
        assert manager._binding_leases

        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_beacon_only_candidate_state_does_not_become_authoritative_availability(
    persistence_tmp_dir,
):
    device = _make_mock_device()
    action_bus = _actions_bus()
    registry = ConfigurableActionRegistry()
    registry.add_action(
        ACTION_X_UUID,
        _metadata(
            ACTION_X_UUID,
            provider_instance_id="test-provider",
            provider_id="test",
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
        manager = DeviceManager(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=FakeHardwareCommandService(),
            config=config,
            manager=registry,
            actions_bus=_actions_session(action_bus),
            start_soon=tg.start_soon,
        )
        await manager._action_availability_service.ingest_catalog_changed(
            _catalog_event(added=[f"test-provider::{ACTION_X_UUID}"])
        )
        await manager.set_page(profile="default", page=0)

        assert await manager.action_contexts.get("0,0") is None
        key = ProviderActionKey("test-provider", ACTION_X_UUID)
        record = manager._action_availability.record_for(key)
        assert record is not None
        assert record.state == ActionAvailabilityState.UNKNOWN
        assert record.source == ActionAvailabilitySource.BEACON_CANDIDATE
        assert (
            manager._action_availability.snapshot_for_intents(
                manager._current_plan_action_intents()
            )
            == {}
        )
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_on_action_catalog_changed_same_session_update_does_not_remove_context(
    persistence_tmp_dir,
):
    device = _make_mock_device()
    action_bus = _actions_bus()
    registry = ConfigurableActionRegistry()
    registry.add_action(
        ACTION_X_UUID,
        _metadata(
            ACTION_X_UUID,
            provider_instance_id="test-provider",
            provider_id="test",
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
        manager = DeviceManager(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=FakeHardwareCommandService(),
            config=config,
            manager=registry,
            actions_bus=_actions_session(action_bus),
            start_soon=tg.start_soon,
        )
        _seed_action_availability(
            manager,
            _metadata(
                ACTION_X_UUID,
                provider_instance_id="test-provider",
                provider_id="test",
            ),
        )
        await manager.set_page(profile="default", page=0)
        ctx_before = await manager.action_contexts.get("0,0")
        assert ctx_before is not None

        qualified = f"test-provider::{ACTION_X_UUID}"
        await manager.on_action_catalog_changed(_catalog_event(updated=[qualified]))

        assert await manager.action_contexts.get("0,0") is ctx_before
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_on_action_catalog_changed_session_succession_restamps_context(
    persistence_tmp_dir,
):
    device = _make_mock_device()
    action_bus = _actions_bus()
    registry = ConfigurableActionRegistry()
    registry.add_action(
        ACTION_X_UUID,
        _metadata(
            ACTION_X_UUID,
            provider_instance_id="test-provider",
            provider_id="test",
            provider_session_id="old-session",
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
        manager = DeviceManager(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=FakeHardwareCommandService(),
            config=config,
            manager=registry,
            actions_bus=_actions_session(action_bus),
            start_soon=tg.start_soon,
        )
        _seed_action_availability(
            manager,
            _metadata(
                ACTION_X_UUID,
                provider_instance_id="test-provider",
                provider_id="test",
                provider_session_id="old-session",
            ),
        )
        await manager.set_page(profile="default", page=0)
        ctx_before = await manager.action_contexts.get("0,0")
        assert ctx_before is not None

        registry.add_action(
            ACTION_X_UUID,
            _metadata(
                ACTION_X_UUID,
                provider_instance_id="test-provider",
                provider_id="test",
                provider_session_id="new-session",
            ),
        )
        qualified = f"test-provider::{ACTION_X_UUID}"
        await manager.on_action_catalog_changed(
            _catalog_event(
                successions=[
                    _provider_session_succession(
                        provider_instance_id="test-provider",
                        provider_id="test",
                        previous_session_id="old-session",
                        successor_session_id="new-session",
                        actions=[qualified],
                    )
                ],
            )
        )

        ctx_after = await manager.action_contexts.get("0,0")
        assert ctx_after is ctx_before
        lease = next(iter(manager._binding_leases.values()))
        assert lease.provider_session_id == "new-session"
        assert lease.context is ctx_after
        assert len(manager._binding_leases) == 1
        tg.cancel_scope.cancel()
