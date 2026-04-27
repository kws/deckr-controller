"""Tests for DeviceManager.handle_command: plugin host API command dispatch layer."""

from unittest.mock import AsyncMock, MagicMock

import anyio
import pytest
from deckr.contracts.messages import DeckrMessage
from deckr.hardware import messages as hw_messages
from deckr.hardware.messages import (
    HardwareCoordinates,
    HardwareDevice,
    HardwareDeviceRef,
    HardwareImageFormat,
    HardwareSlot,
)
from deckr.pluginhost.messages import (
    CLOSE_PAGE,
    COMMAND_MESSAGE_TYPES,
    OPEN_PAGE,
    PAGE_APPEAR,
    PAGE_DISAPPEAR,
    REPLACE_PAGE,
    REQUEST_SETTINGS,
    SET_IMAGE,
    SET_PAGE,
    SET_SETTINGS,
    SET_TITLE,
    SLEEP_SCREEN,
    UPDATE_PAGE,
    WAKE_SCREEN,
    DynamicPageDescriptor,
    context_subject,
    controller_address,
    host_address,
    plugin_message,
)
from deckr.transports.bus import EventBus
from pydantic import ValidationError

from deckr.controller._device_manager import DeviceManager, _descriptor_from_payload
from deckr.controller._navigation_service import StaticPageRef
from deckr.controller.config._data import Control, DeviceConfig, Page, Profile
from deckr.controller.plugin.builtin import (
    BUILTIN_ACTION_PROVIDER_ID,
    LEGACY_BUILTIN_ACTION_PROVIDER_ID,
)
from deckr.controller.plugin.provider import ActionMetadata

CONTROLLER_ID = "controller-main"
CONTROLLER_ADDR = controller_address(CONTROLLER_ID)
HOST_ID = "python"
HOST_ADDR = host_address(HOST_ID)


def _plugin_bus() -> EventBus:
    return EventBus("plugin_messages")


def _command_message(
    message_type: str,
    payload: dict | None = None,
    *,
    config_id: str = "test-device",
    sender=HOST_ADDR,
    context_id: str,
    action_instance_id: str | None = None,
    binding_id: str | None = None,
    page_session_id: str | None = None,
) -> DeckrMessage:
    return plugin_message(
        sender=sender,
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


def _make_slot(
    slot_id: str,
    has_display: bool = True,
    slot_type: str = "key",
) -> HardwareSlot:
    return HardwareSlot(
        id=slot_id,
        coordinates=HardwareCoordinates(column=0, row=0),
        image_format=HardwareImageFormat(width=72, height=72) if has_display else None,
        slot_type=slot_type,
        gestures=["key_down", "key_up"],
    )


def _make_mock_device(device_id: str = "test-device", with_buttons: bool = False):
    slots = [_make_slot("0,0"), _make_slot("1,0")]
    if with_buttons:
        slots.append(_make_slot("B2", has_display=False, slot_type="button"))
    return HardwareDevice(
        id=device_id,
        name="Test Device",
        hid=f"mock:{device_id}",
        fingerprint=f"fingerprint:{device_id}",
        slots=slots,
    )


def _hardware_ref(device: HardwareDevice):
    return HardwareDeviceRef(manager_id="manager-main", device_id=device.id)


class FakeHardwareCommandService:
    def __init__(self):
        self.set_image = AsyncMock()
        self.clear_slot = AsyncMock()
        self.sleep_screen = AsyncMock()
        self.wake_screen = AsyncMock()


class NoopAction:
    uuid: str = "test.virtual.noop"

    async def on_will_appear(self, event, context):
        pass

    async def on_will_disappear(self, event, context):
        pass


def _minimal_config(device_id: str = "test-device") -> DeviceConfig:
    return DeviceConfig(
        id=device_id,
        name="Test Device",
        match={"fingerprint": f"fingerprint:{device_id}"},
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
                    )
                ],
            )
        ],
    )


def _registry_for_action(
    *,
    host_id: str = HOST_ID,
    action_uuid: str = NoopAction.uuid,
) -> MagicMock:
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=ActionMetadata(
            uuid=action_uuid,
            host_id=host_id,
        )
    )
    return registry


def _make_manager(
    *,
    command_service: FakeHardwareCommandService | None = None,
    plugin_bus: EventBus | None = None,
    registry: MagicMock | None = None,
    config: DeviceConfig | None = None,
    device: HardwareDevice | None = None,
) -> DeviceManager:
    device = device or _make_mock_device()
    return DeviceManager(
        controller_id=CONTROLLER_ID,
        device=device,
        hardware_ref=_hardware_ref(device),
        command_service=command_service or FakeHardwareCommandService(),
        config=config or _minimal_config(device.id),
        manager=registry or _registry_for_action(),
        plugin_bus=plugin_bus or _plugin_bus(),
        start_soon=lambda fn, *a, **k: None,
    )


async def _active_context(manager: DeviceManager, slot_id: str = "0,0"):
    ctx = await manager.action_contexts.get(slot_id)
    assert ctx is not None
    return ctx


async def _command_for_active_binding(
    manager: DeviceManager,
    message_type: str,
    payload: dict | None = None,
    *,
    slot_id: str = "0,0",
    sender=HOST_ADDR,
    config_id: str = "test-device",
) -> DeckrMessage:
    ctx = await _active_context(manager, slot_id)
    return _command_message(
        message_type,
        payload,
        sender=sender,
        config_id=config_id,
        context_id=ctx.id,
        action_instance_id=ctx.action_instance_id,
        binding_id=ctx.binding_id,
        page_session_id=ctx.page_session_id,
    )


def _command_for_page_session(
    manager: DeviceManager,
    message_type: str,
    payload: dict | None = None,
    *,
    sender=HOST_ADDR,
) -> DeckrMessage:
    session = manager._dynamic_page_session
    assert session is not None
    return _command_message(
        message_type,
        payload,
        sender=sender,
        context_id=session.context_id,
        action_instance_id=session.action_instance_id,
        page_session_id=session.page_session_id,
    )


@pytest.mark.asyncio
async def test_handle_command_sleep_screen_calls_device(persistence_tmp_dir):
    """SLEEP_SCREEN command publishes a hardware sleep command."""
    device = _make_mock_device()
    command_service = FakeHardwareCommandService()
    plugin_bus = _plugin_bus()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=ActionMetadata(
            uuid=NoopAction.uuid,
            host_id="python",
        )
    )
    manager = DeviceManager(
        controller_id=CONTROLLER_ID,
        device=device,
        hardware_ref=_hardware_ref(device),
        command_service=command_service,
        config=_minimal_config(),
        manager=registry,
        plugin_bus=plugin_bus,
        start_soon=lambda fn, *a, **k: None,
    )
    await manager.set_page(profile="default", page=0)

    msg = await _command_for_active_binding(manager, SLEEP_SCREEN)
    await manager.handle_command(msg)

    command_service.sleep_screen.assert_awaited_once_with("test-device")


@pytest.mark.asyncio
async def test_handle_command_wake_screen_calls_device(persistence_tmp_dir):
    """WAKE_SCREEN command publishes a hardware wake command."""
    device = _make_mock_device()
    command_service = FakeHardwareCommandService()
    plugin_bus = _plugin_bus()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=ActionMetadata(
            uuid=NoopAction.uuid,
            host_id="python",
        )
    )
    manager = DeviceManager(
        controller_id=CONTROLLER_ID,
        device=device,
        hardware_ref=_hardware_ref(device),
        command_service=command_service,
        config=_minimal_config(),
        manager=registry,
        plugin_bus=plugin_bus,
        start_soon=lambda fn, *a, **k: None,
    )
    await manager.set_page(profile="default", page=0)

    msg = await _command_for_active_binding(manager, WAKE_SCREEN)
    await manager.handle_command(msg)

    command_service.wake_screen.assert_awaited_once_with("test-device")


@pytest.mark.asyncio
async def test_handle_command_open_page(persistence_tmp_dir):
    """OPEN_PAGE navigates to dynamic page."""
    device = _make_mock_device()
    plugin_bus = _plugin_bus()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=ActionMetadata(
            uuid=NoopAction.uuid,
            host_id="python",
        )
    )
    manager = DeviceManager(
        controller_id=CONTROLLER_ID,
        device=device,
        hardware_ref=_hardware_ref(device),
        command_service=FakeHardwareCommandService(),
        config=_minimal_config(),
        manager=registry,
        plugin_bus=plugin_bus,
        start_soon=lambda fn, *a, **k: None,
    )
    await manager.set_page(profile="default", page=0)

    current = manager._nav.current_page
    assert isinstance(current, StaticPageRef)

    descriptor_payload = {
        "pageId": "test-page-1",
        "bindings": [
            {"controlId": "0,0", "actionUuid": NoopAction.uuid, "settings": {}},
            {"controlId": "1,0", "actionUuid": NoopAction.uuid, "settings": {}},
        ],
    }
    msg = await _command_for_active_binding(
        manager, OPEN_PAGE, {"descriptor": descriptor_payload}
    )
    await manager.handle_command(msg)

    current = manager._nav.current_page
    assert isinstance(current, DynamicPageDescriptor)
    assert current.page_id == "test-page-1"


@pytest.mark.asyncio
async def test_dynamic_child_binding_can_reuse_opener_control_and_close_page(
    persistence_tmp_dir,
):
    owner_action = "test.action.owner"
    child_action = "test.action.child"
    device = _make_mock_device()
    plugin_bus = _plugin_bus()

    async def get_action(uuid: str):
        return ActionMetadata(uuid=uuid, host_id="python")

    registry = MagicMock()
    registry.get_action = AsyncMock(side_effect=get_action)
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
                            Control(slot="0,0", action=owner_action, settings={})
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
        plugin_bus=plugin_bus,
        start_soon=lambda fn, *a, **k: None,
    )
    await manager.set_page(profile="default", page=0)
    owner_ctx = await _active_context(manager)

    descriptor_payload = {
        "pageId": "child-page",
        "bindings": [
            {
                "controlId": "0,0",
                "actionUuid": child_action,
                "settings": {"seed": "descriptor"},
            },
        ],
    }
    await manager.handle_command(
        await _command_for_active_binding(
            manager, OPEN_PAGE, {"descriptor": descriptor_payload}
        )
    )

    child_ctx = await _active_context(manager)
    assert child_ctx.id != owner_ctx.id
    assert child_ctx.binding_id != owner_ctx.binding_id
    assert child_ctx.page_session_id is not None
    assert child_ctx.action_uuid == child_action
    child_settings = await child_ctx.plugin_context.get_settings()
    assert vars(child_settings) == {"seed": "descriptor"}
    await child_ctx.plugin_context.set_settings({"seed": "runtime"})
    runtime_child_settings = await child_ctx.plugin_context.get_settings()
    assert vars(runtime_child_settings) == {"seed": "runtime"}

    child_ctx.on_key_up = AsyncMock()
    await manager.on_event(
        hw_messages.hardware_input_message(
            manager_id="manager-main",
            device_id=device.id,
            body=hw_messages.KeyUpMessage(key_id="0,0"),
        )
    )
    child_ctx.on_key_up.assert_awaited_once()
    event = child_ctx.on_key_up.await_args.args[0]
    assert event.context == child_ctx.id

    await manager.handle_command(await _command_for_active_binding(manager, CLOSE_PAGE))
    assert isinstance(manager._nav.current_page, StaticPageRef)

    await manager.handle_command(
        await _command_for_active_binding(
            manager, OPEN_PAGE, {"descriptor": descriptor_payload}
        )
    )
    reopened_child_ctx = await _active_context(manager)
    reopened_settings = await reopened_child_ctx.plugin_context.get_settings()
    assert vars(reopened_settings) == {"seed": "descriptor"}


@pytest.mark.asyncio
async def test_open_page_emits_page_events_and_close(persistence_tmp_dir):
    device = _make_mock_device()
    plugin_bus = _plugin_bus()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=ActionMetadata(
            uuid=NoopAction.uuid,
            host_id="python",
        )
    )
    manager = DeviceManager(
        controller_id=CONTROLLER_ID,
        device=device,
        hardware_ref=_hardware_ref(device),
        command_service=FakeHardwareCommandService(),
        config=_minimal_config(),
        manager=registry,
        plugin_bus=plugin_bus,
        start_soon=lambda fn, *a, **k: None,
    )
    await manager.set_page(profile="default", page=0)

    descriptor_payload = {
        "pageId": "test-page-2",
        "bindings": [
            {"controlId": "0,0", "actionUuid": NoopAction.uuid, "settings": {}},
        ],
    }

    async def _await_event(stream, event_type: str) -> DeckrMessage:
        with anyio.fail_after(1.0):
            async for event in stream:
                if isinstance(event, DeckrMessage) and event.message_type == event_type:
                    return event
        raise AssertionError(f"Timed out waiting for {event_type}")

    async with plugin_bus.subscribe() as stream:
        open_command = await _command_for_active_binding(
            manager, OPEN_PAGE, {"descriptor": descriptor_payload}
        )
        await manager.handle_command(open_command)
        event = await _await_event(stream, PAGE_APPEAR)
        assert event.message_type == PAGE_APPEAR
        assert event.causation_id == open_command.message_id

        close_command = await _command_for_active_binding(manager, CLOSE_PAGE)
        await manager.handle_command(close_command)
        event = await _await_event(stream, PAGE_DISAPPEAR)
        assert event.message_type == PAGE_DISAPPEAR
        assert event.causation_id == close_command.message_id

    current = manager._nav.current_page
    assert isinstance(current, StaticPageRef)


@pytest.mark.asyncio
async def test_open_page_replacement_events_set_causation(persistence_tmp_dir):
    device = _make_mock_device()
    plugin_bus = _plugin_bus()
    config = _minimal_config()
    config.profiles[0].pages.append(
        Page(controls=[Control(slot="0,0", action=NoopAction.uuid, settings={})])
    )
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=ActionMetadata(
            uuid=NoopAction.uuid,
            host_id="python",
        )
    )
    manager = DeviceManager(
        controller_id=CONTROLLER_ID,
        device=device,
        hardware_ref=_hardware_ref(device),
        command_service=FakeHardwareCommandService(),
        config=config,
        manager=registry,
        plugin_bus=plugin_bus,
        start_soon=lambda fn, *a, **k: None,
    )
    await manager.set_page(profile="default", page=0)

    descriptor_payload = {
        "pageId": "test-page-2",
        "bindings": [
            {"controlId": "0,0", "actionUuid": NoopAction.uuid, "settings": {}},
        ],
    }

    async def _await_event(stream, event_type: str) -> DeckrMessage:
        with anyio.fail_after(1.0):
            async for event in stream:
                if isinstance(event, DeckrMessage) and event.message_type == event_type:
                    return event
        raise AssertionError(f"Timed out waiting for {event_type}")

    async with plugin_bus.subscribe() as stream:
        await manager.handle_command(
            await _command_for_active_binding(
                manager, OPEN_PAGE, {"descriptor": descriptor_payload}
            )
        )
        await _await_event(stream, PAGE_APPEAR)

        replacement_payload = {
            "pageId": "test-page-3",
            "bindings": [
                {"controlId": "0,0", "actionUuid": NoopAction.uuid, "settings": {}},
            ],
        }
        replacement_command = await _command_for_active_binding(
            manager,
            REPLACE_PAGE,
            {"descriptor": replacement_payload},
        )
        await manager.handle_command(replacement_command)
        event = await _await_event(stream, PAGE_DISAPPEAR)
        assert event.message_type == PAGE_DISAPPEAR
        assert event.causation_id == replacement_command.message_id
        event = await _await_event(stream, PAGE_APPEAR)
        assert event.message_type == PAGE_APPEAR
        assert event.causation_id == replacement_command.message_id


@pytest.mark.asyncio
async def test_widget_page_timeout_returns_to_owner(persistence_tmp_dir):
    device = _make_mock_device()
    plugin_bus = _plugin_bus()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=ActionMetadata(
            uuid=NoopAction.uuid,
            host_id="python",
        )
    )
    now = 0.0

    def clock() -> float:
        return now

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
                        ],
                        widget_timeout_ms=20,
                    )
                ],
            )
        ],
    )

    async with anyio.create_task_group() as tg:

        def start_soon(fn, *args, **kwargs):
            tg.start_soon(fn, *args, **kwargs)

        manager = DeviceManager(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=FakeHardwareCommandService(),
            config=config,
            manager=registry,
            plugin_bus=plugin_bus,
            start_soon=start_soon,
            clock=clock,
            page_timeout_check_interval=0.01,
        )
        await manager.set_page(profile="default", page=0)

        descriptor_payload = {
            "pageId": "timeout-page",
            "bindings": [
                {"controlId": "0,0", "actionUuid": NoopAction.uuid, "settings": {}},
            ],
        }
        await manager.handle_command(
            await _command_for_active_binding(
                manager, OPEN_PAGE, {"descriptor": descriptor_payload}
            )
        )
        assert isinstance(manager._nav.current_page, DynamicPageDescriptor)

        now += 0.05
        await anyio.sleep(0.05)

        current = manager._nav.current_page
        assert isinstance(current, StaticPageRef)

        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_handle_command_set_page(persistence_tmp_dir):
    """SET_PAGE command changes current page."""
    device = _make_mock_device()
    plugin_bus = _plugin_bus()
    config = _minimal_config()
    config.profiles[0].pages.append(
        Page(controls=[Control(slot="0,0", action=NoopAction.uuid, settings={})])
    )
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=ActionMetadata(
            uuid=NoopAction.uuid,
            host_id="python",
        )
    )
    manager = DeviceManager(
        controller_id=CONTROLLER_ID,
        device=device,
        hardware_ref=_hardware_ref(device),
        command_service=FakeHardwareCommandService(),
        config=config,
        manager=registry,
        plugin_bus=plugin_bus,
        start_soon=lambda fn, *a, **k: None,
    )
    await manager.set_page(profile="default", page=0)
    current = manager._nav.current_page
    assert isinstance(current, StaticPageRef) and current.page_index == 0

    msg = await _command_for_active_binding(manager, SET_PAGE, {"page": 1})
    await manager.handle_command(msg)

    current = manager._nav.current_page
    assert isinstance(current, StaticPageRef) and current.page_index == 1


@pytest.mark.asyncio
async def test_handle_command_ignores_wrong_config(persistence_tmp_dir):
    """Commands with contextId for another config are ignored."""
    device = _make_mock_device("test-device")
    command_service = FakeHardwareCommandService()
    plugin_bus = _plugin_bus()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=ActionMetadata(
            uuid=NoopAction.uuid,
            host_id="python",
        )
    )
    manager = DeviceManager(
        controller_id=CONTROLLER_ID,
        device=device,
        hardware_ref=_hardware_ref(device),
        command_service=command_service,
        config=_minimal_config(),
        manager=registry,
        plugin_bus=plugin_bus,
        start_soon=lambda fn, *a, **k: None,
    )
    await manager.set_page(profile="default", page=0)

    msg = await _command_for_active_binding(
        manager, SLEEP_SCREEN, config_id="other-config"
    )
    await manager.handle_command(msg)

    command_service.sleep_screen.assert_not_called()


@pytest.mark.asyncio
async def test_handle_command_rejects_different_host_for_title_and_image(
    persistence_tmp_dir,
):
    manager = _make_manager()
    await manager.set_page(profile="default", page=0)
    ctx = await _active_context(manager)

    await manager.handle_command(
        await _command_for_active_binding(
            manager,
            SET_TITLE,
            {"text": "attacker title"},
            sender=host_address("attacker"),
        )
    )
    await manager.handle_command(
        await _command_for_active_binding(
            manager,
            SET_IMAGE,
            {"image": "attacker image"},
            sender=host_address("attacker"),
        )
    )

    assert ctx._router._store.content.title is None
    assert ctx._router._store.content.image is None


@pytest.mark.asyncio
async def test_handle_command_rejects_different_host_for_settings(
    persistence_tmp_dir,
):
    plugin_bus = _plugin_bus()
    manager = _make_manager(plugin_bus=plugin_bus)
    await manager.set_page(profile="default", page=0)
    ctx = await _active_context(manager)

    async with plugin_bus.subscribe() as stream:
        await manager.handle_command(
            await _command_for_active_binding(
                manager,
                SET_SETTINGS,
                {"settings": {"owner": "attacker"}},
                sender=host_address("attacker"),
            )
        )
        await manager.handle_command(
            await _command_for_active_binding(
                manager,
                REQUEST_SETTINGS,
                sender=host_address("attacker"),
            )
        )
        with anyio.move_on_after(0.05) as scope:
            await stream.receive()

    assert scope.cancel_called
    settings = await ctx._router.get_settings()
    assert vars(settings) == {}


@pytest.mark.asyncio
async def test_handle_command_rejects_different_host_for_page_commands(
    persistence_tmp_dir,
):
    config = _minimal_config()
    config.profiles[0].pages.append(
        Page(controls=[Control(slot="0,0", action=NoopAction.uuid, settings={})])
    )
    manager = _make_manager(config=config)
    await manager.set_page(profile="default", page=0)

    await manager.handle_command(
        await _command_for_active_binding(
            manager,
            SET_PAGE,
            {"page": 1},
            sender=host_address("attacker"),
        )
    )
    current = manager._nav.current_page
    assert isinstance(current, StaticPageRef) and current.page_index == 0

    descriptor_payload = {
        "pageId": "attacker-page",
        "bindings": [
            {"controlId": "0,0", "actionUuid": NoopAction.uuid, "settings": {}},
        ],
    }
    await manager.handle_command(
        await _command_for_active_binding(
            manager,
            OPEN_PAGE,
            {"descriptor": descriptor_payload},
            sender=host_address("attacker"),
        )
    )
    assert isinstance(manager._nav.current_page, StaticPageRef)


@pytest.mark.asyncio
async def test_handle_command_rejects_different_host_for_dynamic_page_close_and_replace(
    persistence_tmp_dir,
):
    manager = _make_manager()
    await manager.set_page(profile="default", page=0)
    descriptor_payload = {
        "pageId": "owner-page",
        "bindings": [
            {"controlId": "0,0", "actionUuid": NoopAction.uuid, "settings": {}},
        ],
    }
    await manager.handle_command(
        await _command_for_active_binding(
            manager, OPEN_PAGE, {"descriptor": descriptor_payload}
        )
    )
    session = manager._dynamic_page_session
    assert session is not None
    assert isinstance(manager._nav.current_page, DynamicPageDescriptor)

    replacement_payload = {
        "pageId": "attacker-replacement",
        "bindings": [
            {"controlId": "0,0", "actionUuid": NoopAction.uuid, "settings": {}},
        ],
    }
    await manager.handle_command(
        _command_message(
            REPLACE_PAGE,
            {"descriptor": replacement_payload},
            sender=host_address("attacker"),
            context_id=session.context_id,
            action_instance_id=session.action_instance_id,
            page_session_id=session.page_session_id,
        )
    )
    current = manager._nav.current_page
    assert isinstance(current, DynamicPageDescriptor)
    assert current.page_id == "owner-page"

    await manager.handle_command(
        _command_message(
            CLOSE_PAGE,
            sender=host_address("attacker"),
            context_id=session.context_id,
            action_instance_id=session.action_instance_id,
            page_session_id=session.page_session_id,
        )
    )
    assert isinstance(manager._nav.current_page, DynamicPageDescriptor)


@pytest.mark.asyncio
async def test_handle_command_rejects_dynamic_page_settings_for_wrong_host_or_action(
    persistence_tmp_dir,
):
    plugin_bus = _plugin_bus()
    manager = _make_manager(plugin_bus=plugin_bus)
    await manager.set_page(profile="default", page=0)
    descriptor_payload = {
        "pageId": "settings-page",
        "bindings": [
            {"controlId": "0,0", "actionUuid": NoopAction.uuid, "settings": {}},
        ],
    }
    await manager.handle_command(
        await _command_for_active_binding(
            manager, OPEN_PAGE, {"descriptor": descriptor_payload}
        )
    )
    session = manager._dynamic_page_session
    assert session is not None
    assert session.settings_target is not None

    async with plugin_bus.subscribe() as stream:
        await manager.handle_command(
            _command_message(
                SET_SETTINGS,
                {"settings": {"blocked": "host"}},
                sender=host_address("attacker"),
                context_id=session.context_id,
                action_instance_id=session.action_instance_id,
                page_session_id=session.page_session_id,
            )
        )
        with pytest.raises(ValidationError):
            _command_message(
                SET_SETTINGS,
                {"settings": {"blocked": "action"}, "actionUuid": "other.action"},
                context_id=session.context_id,
                action_instance_id=session.action_instance_id,
                page_session_id=session.page_session_id,
            )
        with anyio.move_on_after(0.05) as scope:
            await stream.receive()

    assert scope.cancel_called
    assert await manager._settings_service.get(session.settings_target) == {}


@pytest.mark.asyncio
async def test_handle_command_rejects_different_host_for_power_commands(
    persistence_tmp_dir,
):
    command_service = FakeHardwareCommandService()
    manager = _make_manager(command_service=command_service)
    await manager.set_page(profile="default", page=0)

    await manager.handle_command(
        await _command_for_active_binding(
            manager, SLEEP_SCREEN, sender=host_address("attacker")
        )
    )
    await manager.handle_command(
        await _command_for_active_binding(
            manager, WAKE_SCREEN, sender=host_address("attacker")
        )
    )

    command_service.sleep_screen.assert_not_called()
    command_service.wake_screen.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sender",
    [
        CONTROLLER_ADDR,
        host_address(BUILTIN_ACTION_PROVIDER_ID),
        host_address(LEGACY_BUILTIN_ACTION_PROVIDER_ID),
    ],
)
async def test_handle_command_rejects_non_host_and_reserved_senders(
    sender,
    persistence_tmp_dir,
):
    manager = _make_manager()
    await manager.set_page(profile="default", page=0)
    ctx = await _active_context(manager)

    await manager.handle_command(
        await _command_for_active_binding(
            manager, SET_IMAGE, {"image": "blocked"}, sender=sender
        )
    )

    assert ctx._router._store.content.image is None


@pytest.mark.asyncio
async def test_command_body_rejects_action_uuid_retargeting(persistence_tmp_dir):
    manager = _make_manager()
    await manager.set_page(profile="default", page=0)
    ctx = await _active_context(manager)

    with pytest.raises(ValidationError):
        _command_message(
            SET_IMAGE,
            {"image": "blocked", "actionUuid": "other.action"},
            context_id=ctx.id,
            action_instance_id=ctx.action_instance_id,
            binding_id=ctx.binding_id,
        )

    assert ctx._router._store.content.image is None


@pytest.mark.asyncio
async def test_command_body_rejects_slot_retargeting(persistence_tmp_dir):
    manager = _make_manager()
    await manager.set_page(profile="default", page=0)
    ctx = await _active_context(manager)

    with pytest.raises(ValidationError):
        _command_message(
            SET_TITLE,
            {"text": "blocked", "slot": "1,0"},
            context_id=ctx.id,
            action_instance_id=ctx.action_instance_id,
            binding_id=ctx.binding_id,
        )

    assert ctx._router._store.content.title is None


# --- _descriptor_from_payload unit tests ---


def test_descriptor_from_payload_requires_bindings():
    """Descriptor without bindings returns None."""
    data = {
        "pageId": "p1",
        "bindings": None,
    }
    assert _descriptor_from_payload(data) is None


def test_descriptor_from_payload_with_bindings():
    """Descriptor with bindings reconstructs control bindings."""
    data = {
        "pageId": "p2",
        "bindings": [
            {
                "controlId": "0,0",
                "actionUuid": "slot.action",
                "settings": {"key": "val"},
                "titleOptions": {
                    "fontFamily": "Inter",
                    "fontSize": 14,
                    "fontStyle": "Bold",
                    "titleColor": "#FFFFFF",
                    "titleAlignment": "middle",
                },
            }
        ],
    }
    desc = _descriptor_from_payload(data)
    assert desc is not None
    assert desc.bindings is not None
    assert len(desc.bindings) == 1
    assert desc.bindings[0].control_id == "0,0"
    assert desc.bindings[0].action_uuid == "slot.action"
    assert desc.bindings[0].settings == {"key": "val"}
    assert desc.bindings[0].title_options is not None
    assert desc.bindings[0].title_options.font_family == "Inter"
    assert desc.bindings[0].title_options.font_size == 14


def test_descriptor_from_payload_empty_returns_none():
    """Empty or None payload returns None."""
    assert _descriptor_from_payload({}) is None
    assert _descriptor_from_payload(None) is None


@pytest.mark.asyncio
async def test_handle_command_all_command_types_handled(persistence_tmp_dir):
    """All COMMAND_MESSAGE_TYPES in handle_command are handled (no silent pass)."""
    device = _make_mock_device(with_buttons=True)
    plugin_bus = _plugin_bus()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=ActionMetadata(
            uuid=NoopAction.uuid,
            host_id="python",
        )
    )
    manager = DeviceManager(
        controller_id=CONTROLLER_ID,
        device=device,
        hardware_ref=_hardware_ref(device),
        command_service=FakeHardwareCommandService(),
        config=_minimal_config(),
        manager=registry,
        plugin_bus=plugin_bus,
        start_soon=lambda fn, *a, **k: None,
    )
    await manager.set_page(profile="default", page=0)

    for msg_type in COMMAND_MESSAGE_TYPES:
        await manager.set_page(profile="default", page=0)
        payload = {}
        if msg_type in {OPEN_PAGE, UPDATE_PAGE, REPLACE_PAGE}:
            payload["descriptor"] = {
                "pageId": "p1",
                "bindings": [
                    {"controlId": "0,0", "actionUuid": NoopAction.uuid, "settings": {}},
                    {"controlId": "1,0", "actionUuid": NoopAction.uuid, "settings": {}},
                ],
            }
        elif msg_type == SET_PAGE:
            payload["page"] = 0

        if msg_type in {UPDATE_PAGE, REPLACE_PAGE, CLOSE_PAGE}:
            await manager.handle_command(
                await _command_for_active_binding(
                    manager, OPEN_PAGE, {"descriptor": payload.get("descriptor") or {
                        "pageId": "p1",
                        "bindings": [
                            {
                                "controlId": "0,0",
                                "actionUuid": NoopAction.uuid,
                                "settings": {},
                            }
                        ],
                    }}
                )
            )

        msg = await _command_for_active_binding(manager, msg_type, payload)
        await manager.handle_command(msg)
