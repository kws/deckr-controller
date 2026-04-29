"""Descriptor-derived control layout helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from deckr.hardware.descriptors import (
    DECKR_INPUT_BUTTON,
    DECKR_INPUT_ENCODER,
    DECKR_INPUT_TOUCH,
    DECKR_OUTPUT_RASTER,
    CapabilityDescriptor,
    ControlDescriptor,
    DeviceDescriptor,
)


@dataclass(frozen=True)
class ControlCoordinates:
    column: int
    row: int


@dataclass(frozen=True)
class RasterImageFormat:
    width: int
    height: int
    format: str = "JPEG"
    rotation: int = 0


@dataclass(frozen=True)
class ControlSurface:
    id: str
    slot_type: str
    coordinates: ControlCoordinates
    gestures: tuple[str, ...]
    image_format: RasterImageFormat | None = None
    raster_capability_id: str | None = None


@dataclass(frozen=True)
class SlotInfo:
    """One image-capable control in the grid."""

    slot_id: str
    row: int
    col: int
    image_format: RasterImageFormat
    capability_id: str


@dataclass(frozen=True)
class ImageGrid:
    """Image controls arranged in rows x cols (row-major)."""

    rows: int
    cols: int
    slots: tuple[SlotInfo, ...]

    def slot_id(self, row: int, col: int) -> str | None:
        """Return control id at (row, col), or None if out of range."""
        if 0 <= row < self.rows and 0 <= col < self.cols:
            idx = row * self.cols + col
            if idx < len(self.slots):
                return self.slots[idx].slot_id
        return None

    def total_keys(self) -> int:
        return len(self.slots)


@dataclass(frozen=True)
class ButtonInfo:
    """Non-image button."""

    slot_id: str
    gestures: tuple[str, ...]


@dataclass(frozen=True)
class EncoderInfo:
    """Rotary encoder / dial; may have an optional display."""

    slot_id: str
    gestures: tuple[str, ...]
    image_format: RasterImageFormat | None


@dataclass(frozen=True)
class DeviceLayout:
    """Structured view of a device's controls for plugins and navigation."""

    device_id: str
    image_grid: ImageGrid
    buttons: tuple[ButtonInfo, ...]
    encoders: tuple[EncoderInfo, ...]


_IMAGE_GRID_TYPES = frozenset({"key", "bitmap_key", "touch_dial", "touch_strip", "screen"})
_ENCODER_TYPES = frozenset({"encoder", "dial", "touch_dial"})


def control_surface_by_id(
    device: DeviceDescriptor,
    control_id: str,
) -> ControlSurface | None:
    for control in device.controls:
        if control.control_id == control_id:
            return control_surface(control)
    return None


def control_surface(control: ControlDescriptor) -> ControlSurface:
    raster_capability = _first_raster_capability(control)
    image_format = (
        _raster_image_format(raster_capability)
        if raster_capability is not None
        else None
    )
    geometry = control.geometry
    return ControlSurface(
        id=control.control_id,
        slot_type=control.kind,
        coordinates=ControlCoordinates(
            column=int(geometry.x) if geometry is not None else 0,
            row=int(geometry.y) if geometry is not None else 0,
        ),
        gestures=tuple(_gestures_for_control(control)),
        image_format=image_format,
        raster_capability_id=(
            raster_capability.capability_id if raster_capability is not None else None
        ),
    )


def build_device_layout(device: DeviceDescriptor) -> DeviceLayout:
    """Classify descriptor controls into image grid, buttons, and encoders."""
    image_slots: list[SlotInfo] = []
    button_infos: list[ButtonInfo] = []
    encoder_infos: list[EncoderInfo] = []

    for descriptor in device.controls:
        surface = control_surface(descriptor)
        if surface.slot_type == "button":
            button_infos.append(
                ButtonInfo(slot_id=surface.id, gestures=surface.gestures)
            )
        elif surface.slot_type in _ENCODER_TYPES:
            encoder_infos.append(
                EncoderInfo(
                    slot_id=surface.id,
                    gestures=surface.gestures,
                    image_format=surface.image_format,
                )
            )
        if surface.slot_type in _IMAGE_GRID_TYPES and surface.image_format is not None:
            image_slots.append(
                SlotInfo(
                    slot_id=surface.id,
                    row=surface.coordinates.row,
                    col=surface.coordinates.column,
                    image_format=surface.image_format,
                    capability_id=surface.raster_capability_id or "raster.bitmap",
                )
            )

    image_slots.sort(key=lambda s: (s.row, s.col))
    rows = max((s.row for s in image_slots), default=-1) + 1
    cols = max((s.col for s in image_slots), default=-1) + 1

    return DeviceLayout(
        device_id=device.device_id,
        image_grid=ImageGrid(
            rows=rows,
            cols=cols,
            slots=tuple(image_slots),
        ),
        buttons=tuple(button_infos),
        encoders=tuple(encoder_infos),
    )


def _gestures_for_control(control: ControlDescriptor) -> list[str]:
    gestures: list[str] = []
    for capability in control.input_capabilities:
        if capability.family == DECKR_INPUT_BUTTON:
            if capability.capability_type == "momentary":
                gestures.extend(("key_down", "key_up"))
            elif capability.capability_type == "activation":
                gestures.append("press")
        elif capability.family == DECKR_INPUT_ENCODER:
            gestures.append("encoder_rotate")
        elif capability.family == DECKR_INPUT_TOUCH:
            if "tap" in capability.event_types:
                gestures.append("touch_tap")
            if "swipe" in capability.event_types:
                gestures.append("touch_swipe")
    return sorted(set(gestures))


def _first_raster_capability(
    control: ControlDescriptor,
) -> CapabilityDescriptor | None:
    for capability in control.output_capabilities:
        if (
            capability.family == DECKR_OUTPUT_RASTER
            and capability.capability_type == "bitmap"
        ):
            return capability
    return None


def _constraint_value(
    capability: CapabilityDescriptor,
    subject: str,
    default: Any,
) -> Any:
    for constraint in capability.constraints:
        if constraint.subject == subject and constraint.value is not None:
            return constraint.value
    return default


def _raster_image_format(capability: CapabilityDescriptor) -> RasterImageFormat:
    return RasterImageFormat(
        width=int(_constraint_value(capability, "width", 72)),
        height=int(_constraint_value(capability, "height", 72)),
        rotation=int(_constraint_value(capability, "rotation", 0)),
    )
