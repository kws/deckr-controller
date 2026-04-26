"""Tests for DeviceLayout builder and ImageGrid."""

from unittest.mock import MagicMock

from deckr.hardware.messages import (
    HardwareCoordinates,
    HardwareImageFormat,
    HardwareSlot,
)

from deckr.controller._device_layout import (
    ImageGrid,
    SlotInfo,
    build_device_layout,
)


def _make_slot(
    slot_id: str, row: int, col: int, slot_type: str = "key", has_display: bool = True
):
    if slot_type == "button":
        g = ["key_down", "key_up"]
    elif slot_type == "encoder":
        g = ["encoder_down", "encoder_rotate", "encoder_up"]
    else:
        g = ["key_down", "key_up"]
    return HardwareSlot(
        id=slot_id,
        coordinates=HardwareCoordinates(column=col, row=row),
        image_format=HardwareImageFormat(width=72, height=72) if has_display else None,
        slot_type=slot_type,
        gestures=g,
    )


def test_build_device_layout_empty_device():
    device = MagicMock()
    device.id = "dev1"
    device.slots = []
    layout = build_device_layout(device)
    assert layout.device_id == "dev1"
    assert layout.image_grid.rows == 0
    assert layout.image_grid.cols == 0
    assert layout.image_grid.total_keys() == 0
    assert len(layout.buttons) == 0
    assert len(layout.encoders) == 0


def test_build_device_layout_image_grid_only():
    device = MagicMock()
    device.id = "dev1"
    device.slots = [
        _make_slot("0,0", 0, 0),
        _make_slot("1,0", 1, 0),
        _make_slot("0,1", 0, 1),
    ]
    layout = build_device_layout(device)
    assert layout.device_id == "dev1"
    assert layout.image_grid.total_keys() == 3
    assert layout.image_grid.rows == 2
    assert layout.image_grid.cols == 2
    slot_ids = [s.slot_id for s in layout.image_grid.slots]
    assert "0,0" in slot_ids
    assert "1,0" in slot_ids
    assert "0,1" in slot_ids
    assert layout.image_grid.slot_id(0, 0) is not None
    assert (
        layout.image_grid.slot_id(1, 1) is None
        or layout.image_grid.slot_id(1, 1) in slot_ids
    )


def test_build_device_layout_classifies_buttons_and_encoders():
    device = MagicMock()
    device.id = "dev1"
    device.slots = [
        HardwareSlot(
            id="0,0",
            coordinates=HardwareCoordinates(column=0, row=0),
            image_format=HardwareImageFormat(width=72, height=72),
            slot_type="key",
            gestures=["key_down", "key_up"],
        ),
        HardwareSlot(
            id="B1",
            coordinates=HardwareCoordinates(column=0, row=2),
            image_format=None,
            slot_type="button",
            gestures=["key_down", "key_up"],
        ),
        HardwareSlot(
            id="D1",
            coordinates=HardwareCoordinates(column=1, row=2),
            image_format=None,
            slot_type="encoder",
            gestures=["encoder_down", "encoder_rotate", "encoder_up"],
        ),
    ]
    layout = build_device_layout(device)
    assert layout.image_grid.total_keys() == 1
    assert len(layout.buttons) == 1
    assert layout.buttons[0].slot_id == "B1"
    assert len(layout.encoders) == 1
    assert layout.encoders[0].slot_id == "D1"
    assert layout.encoders[0].image_format is None


def test_image_grid_slot_id_row_col():
    # Slots in row-major order: (0,0), (0,1), (1,0), (1,1)
    grid = ImageGrid(
        rows=2,
        cols=2,
        slots=(
            SlotInfo("0,0", 0, 0, HardwareImageFormat(width=72, height=72)),
            SlotInfo("0,1", 0, 1, HardwareImageFormat(width=72, height=72)),
            SlotInfo("1,0", 1, 0, HardwareImageFormat(width=72, height=72)),
            SlotInfo("1,1", 1, 1, HardwareImageFormat(width=72, height=72)),
        ),
    )
    assert grid.slot_id(0, 0) == "0,0"
    assert grid.slot_id(1, 0) == "1,0"
    assert grid.slot_id(0, 1) == "0,1"
    assert grid.slot_id(1, 1) == "1,1"
    assert grid.slot_id(2, 0) is None
    assert grid.total_keys() == 4
