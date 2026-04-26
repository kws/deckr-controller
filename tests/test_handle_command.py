"""Tests for DeviceManager.handle_command: plugin host API command dispatch layer."""

from unittest.mock import AsyncMock, MagicMock

import anyio
import pytest
from deckr.contracts.messages import DeckrMessage
from deckr.hardware.events import (
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
    REQUEST_SETTINGS,
    SET_IMAGE,
    SET_PAGE,
    SET_SETTINGS,
    SET_TITLE,
    SLEEP_SCREEN,
    WAKE_SCREEN,
    DynamicPageDescriptor,
    build_context_id,
    context_subject,
    controller_address,
    host_address,
    plugin_message,
)
from deckr.transports.bus import EventBus

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


def _context_id(config_id: str = "test-device", slot_id: str = "0,0") -> str:
    return build_context_id(CONTROLLER_ID, config_id, slot_id)


def _command_message(
    message_type: str,
    payload: dict | None = None,
    *,
    config_id: str = "test-device",
    slot_id: str = "0,0",
    sender=HOST_ADDR,
    context_id: str | None = None,
) -> DeckrMessage:
    context_id = context_id or _context_id(config_id, slot_id)
    return plugin_message(
        sender=sender,
        recipient=CONTROLLER_ADDR,
        message_type=message_type,
        payload={"contextId": context_id, **(payload or {})},
        subject=context_subject(context_id),
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

    msg = _command_message(SLEEP_SCREEN)
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

    msg = _command_message(WAKE_SCREEN)
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
        "slots": [
            {"slotId": "0,0", "actionUuid": NoopAction.uuid, "settings": {}},
            {"slotId": "1,0", "actionUuid": NoopAction.uuid, "settings": {}},
        ],
    }
    msg = _command_message(OPEN_PAGE, {"descriptor": descriptor_payload})
    await manager.handle_command(msg)

    current = manager._nav.current_page
    assert isinstance(current, DynamicPageDescriptor)
    assert current.page_id == "test-page-1"


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
        "slots": [
            {"slotId": "0,0", "actionUuid": NoopAction.uuid, "settings": {}},
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
            _command_message(OPEN_PAGE, {"descriptor": descriptor_payload})
        )
        event = await _await_event(stream, PAGE_APPEAR)
        assert event.message_type == PAGE_APPEAR

        await manager.handle_command(
            _command_message(CLOSE_PAGE)
        )
        event = await _await_event(stream, PAGE_DISAPPEAR)
        assert event.message_type == PAGE_DISAPPEAR

    current = manager._nav.current_page
    assert isinstance(current, StaticPageRef)


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
            "slots": [
                {"slotId": "0,0", "actionUuid": NoopAction.uuid, "settings": {}},
            ],
        }
        await manager.handle_command(
            _command_message(OPEN_PAGE, {"descriptor": descriptor_payload})
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

    msg = _command_message(SET_PAGE, {"page": 1})
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

    msg = _command_message(SLEEP_SCREEN, config_id="other-config")
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
        _command_message(
            SET_TITLE,
            {"text": "attacker title"},
            sender=host_address("attacker"),
        )
    )
    await manager.handle_command(
        _command_message(
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
            _command_message(
                SET_SETTINGS,
                {"settings": {"owner": "attacker"}},
                sender=host_address("attacker"),
            )
        )
        await manager.handle_command(
            _command_message(
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
        _command_message(
            SET_PAGE,
            {"page": 1},
            sender=host_address("attacker"),
        )
    )
    current = manager._nav.current_page
    assert isinstance(current, StaticPageRef) and current.page_index == 0

    descriptor_payload = {
        "pageId": "attacker-page",
        "slots": [
            {"slotId": "0,0", "actionUuid": NoopAction.uuid, "settings": {}},
        ],
    }
    await manager.handle_command(
        _command_message(
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
        "slots": [
            {"slotId": "0,0", "actionUuid": NoopAction.uuid, "settings": {}},
        ],
    }
    await manager.handle_command(
        _command_message(OPEN_PAGE, {"descriptor": descriptor_payload})
    )
    owner = manager._dynamic_page_owner
    assert owner is not None
    assert isinstance(manager._nav.current_page, DynamicPageDescriptor)

    replacement_payload = {
        "pageId": "attacker-replacement",
        "slots": [
            {"slotId": "0,0", "actionUuid": NoopAction.uuid, "settings": {}},
        ],
    }
    await manager.handle_command(
        _command_message(
            OPEN_PAGE,
            {"descriptor": replacement_payload},
            sender=host_address("attacker"),
            context_id=owner.page_context_id,
        )
    )
    current = manager._nav.current_page
    assert isinstance(current, DynamicPageDescriptor)
    assert current.page_id == "owner-page"

    await manager.handle_command(
        _command_message(
            CLOSE_PAGE,
            sender=host_address("attacker"),
            context_id=owner.page_context_id,
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
        "slots": [
            {"slotId": "0,0", "actionUuid": NoopAction.uuid, "settings": {}},
        ],
    }
    await manager.handle_command(
        _command_message(OPEN_PAGE, {"descriptor": descriptor_payload})
    )
    owner = manager._dynamic_page_owner
    assert owner is not None
    assert owner.settings_target is not None

    async with plugin_bus.subscribe() as stream:
        await manager.handle_command(
            _command_message(
                SET_SETTINGS,
                {"settings": {"blocked": "host"}},
                sender=host_address("attacker"),
                context_id=owner.page_context_id,
            )
        )
        await manager.handle_command(
            _command_message(
                SET_SETTINGS,
                {"settings": {"blocked": "action"}, "actionUuid": "other.action"},
                context_id=owner.page_context_id,
            )
        )
        with anyio.move_on_after(0.05) as scope:
            await stream.receive()

    assert scope.cancel_called
    assert await manager._settings_service.get(owner.settings_target) == {}


@pytest.mark.asyncio
async def test_handle_command_rejects_different_host_for_power_commands(
    persistence_tmp_dir,
):
    command_service = FakeHardwareCommandService()
    manager = _make_manager(command_service=command_service)
    await manager.set_page(profile="default", page=0)

    await manager.handle_command(
        _command_message(SLEEP_SCREEN, sender=host_address("attacker"))
    )
    await manager.handle_command(
        _command_message(WAKE_SCREEN, sender=host_address("attacker"))
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
        _command_message(SET_IMAGE, {"image": "blocked"}, sender=sender)
    )

    assert ctx._router._store.content.image is None


@pytest.mark.asyncio
async def test_handle_command_rejects_action_uuid_mismatch(persistence_tmp_dir):
    manager = _make_manager()
    await manager.set_page(profile="default", page=0)
    ctx = await _active_context(manager)

    await manager.handle_command(
        _command_message(
            SET_IMAGE,
            {"image": "blocked", "actionUuid": "other.action"},
        )
    )

    assert ctx._router._store.content.image is None


@pytest.mark.asyncio
async def test_handle_command_rejects_payload_slot_retargeting(persistence_tmp_dir):
    manager = _make_manager()
    await manager.set_page(profile="default", page=0)
    ctx = await _active_context(manager)

    await manager.handle_command(
        _command_message(
            SET_TITLE,
            {"text": "blocked", "slot": "1,0"},
        )
    )

    assert ctx._router._store.content.title is None


# --- _descriptor_from_payload unit tests ---


def test_descriptor_from_payload_requires_slots():
    """Descriptor without slots returns None."""
    data = {
        "pageId": "p1",
        "slots": None,
    }
    assert _descriptor_from_payload(data) is None


def test_descriptor_from_payload_with_slots():
    """Descriptor with slots reconstructs SlotBindings."""
    data = {
        "pageId": "p2",
        "slots": [
            {
                "slotId": "0,0",
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
    assert desc.slots is not None
    assert len(desc.slots) == 1
    assert desc.slots[0].slot_id == "0,0"
    assert desc.slots[0].action_uuid == "slot.action"
    assert desc.slots[0].settings == {"key": "val"}
    assert desc.slots[0].title_options is not None
    assert desc.slots[0].title_options.font_family == "Inter"
    assert desc.slots[0].title_options.font_size == 14


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
        payload = {}
        if msg_type == OPEN_PAGE:
            payload["descriptor"] = {
                "pageId": "p1",
                "slots": [
                    {"slotId": "0,0", "actionUuid": NoopAction.uuid, "settings": {}},
                    {"slotId": "1,0", "actionUuid": NoopAction.uuid, "settings": {}},
                ],
            }
        elif msg_type == SET_PAGE:
            payload["page"] = 0

        msg = _command_message(msg_type, payload)
        await manager.handle_command(msg)
