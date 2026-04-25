"""Tests for DeviceManager.handle_command: plugin host API command dispatch layer."""

import pytest
import anyio
from unittest.mock import MagicMock, AsyncMock

from deckr.controller._device_manager import DeviceManager, _descriptor_from_payload
from deckr.controller._navigation_service import StaticPageRef
from deckr.pluginhost.messages import DynamicPageDescriptor
from deckr.transports.bus import EventBus
from deckr.pluginhost.messages import (
    CLOSE_PAGE,
    COMMAND_MESSAGE_TYPES,
    HostMessage,
    OPEN_PAGE,
    PAGE_APPEAR,
    PAGE_DISAPPEAR,
    SET_PAGE,
    SLEEP_SCREEN,
    WAKE_SCREEN,
    build_context_id,
    controller_address,
    host_address,
)
from deckr.controller.plugin.provider import ActionMetadata
from deckr.controller.config._data import DeviceConfig, Profile, Page, Control
from deckr.hardware.events import (
    HardwareCoordinates,
    HardwareImageFormat,
    HardwareDevice,
    HardwareSlot,
)

CONTROLLER_ID = "controller-main"
CONTROLLER_ADDR = controller_address(CONTROLLER_ID)
HOST_ID = "python"
HOST_ADDR = host_address(HOST_ID)


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
        slots=slots,
    )


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


@pytest.mark.asyncio
async def test_handle_command_sleep_screen_calls_device(persistence_tmp_dir):
    """SLEEP_SCREEN command publishes a hardware sleep command."""
    device = _make_mock_device()
    command_service = FakeHardwareCommandService()
    plugin_bus = EventBus()
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
        command_service=command_service,
        config=_minimal_config(),
        manager=registry,
        plugin_bus=plugin_bus,
        start_soon=lambda fn, *a, **k: None,
    )
    await manager.set_page(profile="default", page=0)

    msg = HostMessage(
        from_id=HOST_ADDR,
        to_id=CONTROLLER_ADDR,
        type=SLEEP_SCREEN,
        payload={"contextId": build_context_id(CONTROLLER_ID, "test-device", "0,0")},
    )
    await manager.handle_command(msg)

    command_service.sleep_screen.assert_awaited_once_with("test-device")


@pytest.mark.asyncio
async def test_handle_command_wake_screen_calls_device(persistence_tmp_dir):
    """WAKE_SCREEN command publishes a hardware wake command."""
    device = _make_mock_device()
    command_service = FakeHardwareCommandService()
    plugin_bus = EventBus()
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
        command_service=command_service,
        config=_minimal_config(),
        manager=registry,
        plugin_bus=plugin_bus,
        start_soon=lambda fn, *a, **k: None,
    )
    await manager.set_page(profile="default", page=0)

    msg = HostMessage(
        from_id=HOST_ADDR,
        to_id=CONTROLLER_ADDR,
        type=WAKE_SCREEN,
        payload={"contextId": build_context_id(CONTROLLER_ID, "test-device", "0,0")},
    )
    await manager.handle_command(msg)

    command_service.wake_screen.assert_awaited_once_with("test-device")


@pytest.mark.asyncio
async def test_handle_command_open_page(persistence_tmp_dir):
    """OPEN_PAGE navigates to dynamic page."""
    device = _make_mock_device()
    plugin_bus = EventBus()
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
    msg = HostMessage(
        from_id=HOST_ADDR,
        to_id=CONTROLLER_ADDR,
        type=OPEN_PAGE,
        payload={
            "contextId": build_context_id(CONTROLLER_ID, "test-device", "0,0"),
            "descriptor": descriptor_payload,
        },
    )
    await manager.handle_command(msg)

    current = manager._nav.current_page
    assert isinstance(current, DynamicPageDescriptor)
    assert current.page_id == "test-page-1"


@pytest.mark.asyncio
async def test_open_page_emits_page_events_and_close(persistence_tmp_dir):
    device = _make_mock_device()
    plugin_bus = EventBus()
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

    async def _await_event(stream, event_type: str) -> HostMessage:
        with anyio.fail_after(1.0):
            async for envelope in stream:
                event = envelope.message
                if isinstance(event, HostMessage) and event.type == event_type:
                    return event
        raise AssertionError(f"Timed out waiting for {event_type}")

    async with plugin_bus.subscribe() as stream:
        await manager.handle_command(
            HostMessage(
                from_id=HOST_ADDR,
                to_id=CONTROLLER_ADDR,
                type=OPEN_PAGE,
                payload={
                    "contextId": build_context_id(CONTROLLER_ID, "test-device", "0,0"),
                    "descriptor": descriptor_payload,
                },
            )
        )
        event = await _await_event(stream, PAGE_APPEAR)
        assert event.type == PAGE_APPEAR

        await manager.handle_command(
            HostMessage(
                from_id=HOST_ADDR,
                to_id=CONTROLLER_ADDR,
                type=CLOSE_PAGE,
                payload={
                    "contextId": build_context_id(CONTROLLER_ID, "test-device", "0,0"),
                },
            )
        )
        event = await _await_event(stream, PAGE_DISAPPEAR)
        assert event.type == PAGE_DISAPPEAR

    current = manager._nav.current_page
    assert isinstance(current, StaticPageRef)


@pytest.mark.asyncio
async def test_widget_page_timeout_returns_to_owner(persistence_tmp_dir):
    device = _make_mock_device()
    plugin_bus = EventBus()
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
            HostMessage(
                from_id=HOST_ADDR,
                to_id=CONTROLLER_ADDR,
                type=OPEN_PAGE,
                payload={
                    "contextId": build_context_id(CONTROLLER_ID, "test-device", "0,0"),
                    "descriptor": descriptor_payload,
                },
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
    plugin_bus = EventBus()
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
        command_service=FakeHardwareCommandService(),
        config=config,
        manager=registry,
        plugin_bus=plugin_bus,
        start_soon=lambda fn, *a, **k: None,
    )
    await manager.set_page(profile="default", page=0)
    current = manager._nav.current_page
    assert isinstance(current, StaticPageRef) and current.page_index == 0

    msg = HostMessage(
        from_id=HOST_ADDR,
        to_id=CONTROLLER_ADDR,
        type=SET_PAGE,
        payload={
            "contextId": build_context_id(CONTROLLER_ID, "test-device", "0,0"),
            "page": 1,
        },
    )
    await manager.handle_command(msg)

    current = manager._nav.current_page
    assert isinstance(current, StaticPageRef) and current.page_index == 1


@pytest.mark.asyncio
async def test_handle_command_ignores_wrong_device(persistence_tmp_dir):
    """Commands with contextId for another device are ignored."""
    device = _make_mock_device("test-device")
    command_service = FakeHardwareCommandService()
    plugin_bus = EventBus()
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
        command_service=command_service,
        config=_minimal_config(),
        manager=registry,
        plugin_bus=plugin_bus,
        start_soon=lambda fn, *a, **k: None,
    )
    await manager.set_page(profile="default", page=0)

    msg = HostMessage(
        from_id=HOST_ADDR,
        to_id=CONTROLLER_ADDR,
        type=SLEEP_SCREEN,
        payload={"contextId": build_context_id(CONTROLLER_ID, "other-device", "0,0")},
    )
    await manager.handle_command(msg)

    command_service.sleep_screen.assert_not_called()


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
    plugin_bus = EventBus()
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
        command_service=FakeHardwareCommandService(),
        config=_minimal_config(),
        manager=registry,
        plugin_bus=plugin_bus,
        start_soon=lambda fn, *a, **k: None,
    )
    await manager.set_page(profile="default", page=0)

    for msg_type in COMMAND_MESSAGE_TYPES:
        payload = {"contextId": build_context_id(CONTROLLER_ID, "test-device", "0,0")}
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

        msg = HostMessage(
            from_id=HOST_ADDR,
            to_id=CONTROLLER_ADDR,
            type=msg_type,
            payload=payload,
        )
        await manager.handle_command(msg)
