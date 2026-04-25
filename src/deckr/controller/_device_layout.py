"""Device layout descriptor: classifies hardware metadata slots."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from deckr.hardware.events import HardwareDevice, HardwareImageFormat


@dataclass(frozen=True)
class SlotInfo:
    """One image-capable slot in the grid."""

    slot_id: str
    row: int
    col: int
    image_format: HardwareImageFormat


@dataclass(frozen=True)
class ImageGrid:
    """Image keys arranged in rows × cols (row-major)."""

    rows: int
    cols: int
    slots: tuple[SlotInfo, ...]

    def slot_id(self, row: int, col: int) -> str | None:
        """Return slot_id at (row, col), or None if out of range."""
        if 0 <= row < self.rows and 0 <= col < self.cols:
            idx = row * self.cols + col
            if idx < len(self.slots):
                return self.slots[idx].slot_id
        return None

    def total_keys(self) -> int:
        return len(self.slots)


@dataclass(frozen=True)
class ButtonInfo:
    """Non-image button (e.g. B1, B2, B3 on Mirabox N3)."""

    slot_id: str
    gestures: list[str]


@dataclass(frozen=True)
class EncoderInfo:
    """Rotary encoder / dial; may have an optional display."""

    slot_id: str
    gestures: list[str]
    image_format: HardwareImageFormat | None


@dataclass(frozen=True)
class DeviceLayout:
    """Structured view of a device's controls for plugins and navigation."""

    device_id: str
    image_grid: ImageGrid
    buttons: tuple[ButtonInfo, ...]
    encoders: tuple[EncoderInfo, ...]


# Slot types that have an image surface (included in image_grid)
_IMAGE_GRID_TYPES = frozenset({"key", "touch_dial", "touch_strip", "screen"})

# Slot types that are encoders (included in encoders list)
_ENCODER_TYPES = frozenset({"encoder", "touch_dial"})


def build_device_layout(device: HardwareDevice) -> DeviceLayout:
    """Classify device slots into image grid, buttons, and encoders. Pure function of slot list."""
    image_slots: list[SlotInfo] = []
    button_infos: list[ButtonInfo] = []
    encoder_infos: list[EncoderInfo] = []

    for slot in device.slots:
        if slot.slot_type == "button":
            button_infos.append(ButtonInfo(slot_id=slot.id, gestures=slot.gestures))
        elif slot.slot_type in _ENCODER_TYPES:
            encoder_infos.append(
                EncoderInfo(
                    slot_id=slot.id,
                    gestures=slot.gestures,
                    image_format=slot.image_format,
                )
            )
        if slot.slot_type in _IMAGE_GRID_TYPES and slot.image_format is not None:
            image_slots.append(
                SlotInfo(
                    slot_id=slot.id,
                    row=slot.coordinates.row,
                    col=slot.coordinates.column,
                    image_format=slot.image_format,
                )
            )

    # Sort image slots row-major (row, then col) and compute grid dimensions
    image_slots.sort(key=lambda s: (s.row, s.col))
    rows = max((s.row for s in image_slots), default=-1) + 1
    cols = max((s.col for s in image_slots), default=-1) + 1

    return DeviceLayout(
        device_id=device.id,
        image_grid=ImageGrid(
            rows=rows,
            cols=cols,
            slots=tuple(image_slots),
        ),
        buttons=tuple(button_infos),
        encoders=tuple(encoder_infos),
    )
