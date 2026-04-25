"""Tests for binding validator."""

from unittest.mock import AsyncMock, MagicMock

import anyio
import pytest
from deckr.core.messaging import EventBus
from deckr.hardware.events import Coordinates, HWSImageFormat, HWSlot

from deckr.controller._binding_validator import (
    ValidationError,
    ValidationResult,
    format_validation_summary,
    validate_page_bindings,
)
from deckr.controller._navigation_service import SlotBinding
from deckr.controller._render import RenderResult
from deckr.controller.plugin.provider import ActionMetadata

CONTROLLER_ID = "controller-main"


class _ImmediateRenderBackend:
    async def render(self, request):
        return RenderResult(
            context_id=request.context_id,
            slot_id=request.slot_id,
            generation=request.generation,
            frame=b"frame",
        )

    async def aclose(self) -> None:
        return


def _make_slot(
    slot_id: str,
    row: int = 0,
    col: int = 0,
    slot_type: str = "key",
    gestures: frozenset | None = None,
    has_display: bool = True,
) -> HWSlot:
    if gestures is None:
        gestures = frozenset({"key_down", "key_up"})
    return HWSlot(
        id=slot_id,
        coordinates=Coordinates(column=col, row=row),
        image_format=HWSImageFormat(width=72, height=72) if has_display else None,
        slot_type=slot_type,
        gestures=gestures,
    )


# --- validate_page_bindings ---


def _make_key_action():
    """Action that only has key + will_appear (so required_gestures = key_down, key_up; requires_image = True)."""
    action = MagicMock(spec=["on_key_down", "on_key_up", "on_will_appear"])
    action.on_key_down = MagicMock()
    action.on_key_up = MagicMock()
    action.on_will_appear = AsyncMock()
    return action


@pytest.mark.asyncio
async def test_validate_page_bindings_all_valid():
    device = MagicMock()
    device.slots = [_make_slot("0,0"), _make_slot("0,1")]
    action = _make_key_action()

    async def get_action(uuid: str):
        return action

    bindings = [
        SlotBinding(slot_id="0,0", action_uuid="action.a", settings={}),
        SlotBinding(slot_id="0,1", action_uuid="action.b", settings={}),
    ]
    result = await validate_page_bindings(bindings, device, get_action)
    assert result.valid is True
    assert len(result.errors) == 0


@pytest.mark.asyncio
async def test_validate_page_bindings_missing_slot():
    device = MagicMock()
    device.slots = [_make_slot("0,0")]
    action = MagicMock()
    action.on_key_down = MagicMock()
    action.on_key_up = MagicMock()
    action.on_will_appear = MagicMock()

    async def get_action(uuid: str):
        return action

    bindings = [SlotBinding(slot_id="99,99", action_uuid="action.a", settings={})]
    result = await validate_page_bindings(bindings, device, get_action)
    assert result.valid is False
    assert len(result.errors) == 1
    assert result.errors[0].code == "slot_not_found"
    assert "99,99" in result.errors[0].message


@pytest.mark.asyncio
async def test_validate_page_bindings_missing_action():
    """Missing action is non-blocking; page loads with slot showing 'unavailable'."""
    device = MagicMock()
    device.slots = [_make_slot("0,0")]

    async def get_action(uuid: str):
        return None

    bindings = [SlotBinding(slot_id="0,0", action_uuid="nonexistent", settings={})]
    result = await validate_page_bindings(bindings, device, get_action)
    assert result.valid is True  # Page can load (partial activation)
    assert result.has_blocking_errors is False
    assert result.has_non_blocking_errors is True
    assert len(result.errors) == 1
    assert result.errors[0].code == "action_not_found"


# --- format_validation_summary ---


def test_format_validation_summary_passed():
    result = ValidationResult(valid=True)
    assert "passed" in format_validation_summary(result)


def test_format_validation_summary_errors():
    result = ValidationResult(valid=False)
    result.add_error("slot_not_found", "slot 'x' not found", "x", "action.a")
    result.add_error(
        "capability_mismatch", "mismatch", "y", "action.b", details=["need image"]
    )
    s = format_validation_summary(result)
    assert "2 error(s)" in s
    assert "slot_not_found" in s or "x" in s
    assert "capability_mismatch" in s or "y" in s


def test_format_validation_summary_list_of_errors():
    errors = [
        ValidationError("slot_not_found", "msg", "0,0", "a", details=[]),
    ]
    s = format_validation_summary(errors)
    assert "1 error(s)" in s


# --- Integration: DeviceManager rejects invalid static page ---


@pytest.mark.asyncio
async def test_device_manager_rejects_invalid_static_page_and_reverts_stack():
    """When static page has invalid bindings (e.g. missing slot), DeviceManager rejects transition and reverts stack."""
    from deckr.controller._device_manager import DeviceManager
    from deckr.controller.config._data import Control, DeviceConfig, Page, Profile

    device = MagicMock()
    device.id = "test-dev"
    device.hid = "test-hid"
    device.slots = [_make_slot("0,0")]  # only slot 0,0 exists
    device.set_image = AsyncMock()
    device.clear_slot = AsyncMock()
    device.sleep_screen = AsyncMock()
    device.wake_screen = AsyncMock()

    config = DeviceConfig(
        id="test-dev",
        name="Test",
        profiles=[
            Profile(
                name="default",
                pages=[
                    Page(
                        controls=[
                            Control(
                                slot="99,99",
                                action="deckr.plugin.builtin.gotopage",
                                settings={},
                            ),
                        ]
                    ),
                ],
            ),
        ],
    )

    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=ActionMetadata(
            uuid="deckr.plugin.builtin.gotopage",
            host_id="python",
        )
    )

    def start_soon(*args, **kwargs):
        pass

    plugin_bus = EventBus()
    manager = DeviceManager(
        controller_id=CONTROLLER_ID,
        device=device,
        config=config,
        manager=registry,
        plugin_bus=plugin_bus,
        start_soon=start_soon,
    )
    await manager.set_page(profile="default", page=0)

    # Validation rejected the page: no contexts were created (invalid slot 99,99 not on device).
    # Current page remains unchanged when validation fails on the first page.
    contexts = await manager.action_contexts.values()
    assert len(contexts) == 0


@pytest.mark.asyncio
async def test_device_manager_loads_page_with_missing_action_shows_unavailable():
    """When static page has missing action, page loads; slot shows 'unavailable' overlay."""
    from deckr.controller._device_manager import DeviceManager
    from deckr.controller.config._data import Control, DeviceConfig, Page, Profile

    device = MagicMock()
    device.id = "test-dev"
    device.hid = "test-hid"
    device.slots = [_make_slot("0,0"), _make_slot("0,1")]
    device.set_image = AsyncMock()
    device.clear_slot = AsyncMock()
    device.sleep_screen = AsyncMock()
    device.wake_screen = AsyncMock()

    config = DeviceConfig(
        id="test-dev",
        name="Test",
        profiles=[
            Profile(
                name="default",
                pages=[
                    Page(
                        controls=[
                            Control(
                                slot="0,0",
                                action="deckr.plugin.builtin.gotopage",
                                settings={},
                            ),
                            Control(
                                slot="0,1",
                                action="com.example.nonexistent",
                                settings={},
                            ),
                        ]
                    ),
                ],
            ),
        ],
    )

    registry = MagicMock()
    action = _make_key_action()
    action.uuid = "deckr.plugin.builtin.gotopage"

    async def get_action(uuid):
        if uuid == "deckr.plugin.builtin.gotopage":
            return ActionMetadata(
                uuid=action.uuid,
                host_id="python",
            )
        return None

    registry.get_action = get_action

    plugin_bus = EventBus()
    async with anyio.create_task_group() as tg:
        manager = DeviceManager(
            controller_id=CONTROLLER_ID,
            device=device,
            config=config,
            manager=registry,
            plugin_bus=plugin_bus,
            start_soon=tg.start_soon,
            render_backend=_ImmediateRenderBackend(),
        )
        await manager.set_page(profile="default", page=0)

        contexts = await manager.action_contexts.values()
        assert len(contexts) == 1

        with anyio.fail_after(1.0):
            while not any(c[0][0] == "0,1" for c in device.set_image.call_args_list):
                await anyio.sleep(0.01)

        tg.cancel_scope.cancel()
