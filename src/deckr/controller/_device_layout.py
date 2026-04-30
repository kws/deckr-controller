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
    kind: str
    coordinates: ControlCoordinates
    input_events: tuple[str, ...]
    image_format: RasterImageFormat | None = None
    raster_capability_id: str | None = None


@dataclass(frozen=True)
class RasterControl:
    """One control with a raster output capability."""

    control_id: str
    row: int
    column: int
    image_format: RasterImageFormat
    capability_id: str


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
    return control_surface_for_raster_capability(
        control,
        raster_capability.capability_id if raster_capability is not None else None,
    )


def control_surface_for_raster_capability(
    control: ControlDescriptor,
    raster_capability_id: str | None,
) -> ControlSurface:
    raster_capability = _raster_capability_by_id(control, raster_capability_id)
    image_format = (
        _raster_image_format(raster_capability)
        if raster_capability is not None
        else None
    )
    geometry = control.geometry
    return ControlSurface(
        id=control.control_id,
        kind=control.kind,
        coordinates=ControlCoordinates(
            column=int(geometry.x) if geometry is not None else 0,
            row=int(geometry.y) if geometry is not None else 0,
        ),
        input_events=tuple(_input_events_for_control(control)),
        image_format=image_format,
        raster_capability_id=(
            raster_capability.capability_id if raster_capability is not None else None
        ),
    )


def raster_controls(device: DeviceDescriptor) -> tuple[RasterControl, ...]:
    """Return controls with raster outputs, ordered by descriptor geometry."""

    controls: list[RasterControl] = []
    for descriptor in device.controls:
        surface = control_surface(descriptor)
        if surface.image_format is None or surface.raster_capability_id is None:
            continue
        controls.append(
            RasterControl(
                control_id=surface.id,
                row=surface.coordinates.row,
                column=surface.coordinates.column,
                image_format=surface.image_format,
                capability_id=surface.raster_capability_id,
            )
        )
    controls.sort(key=lambda control: (control.row, control.column, control.control_id))
    return tuple(controls)


def _input_events_for_control(control: ControlDescriptor) -> list[str]:
    input_events: list[str] = []
    for capability in control.input_capabilities:
        if capability.family == DECKR_INPUT_BUTTON:
            if capability.capability_type == "momentary":
                input_events.extend(("key_down", "key_up"))
            elif capability.capability_type == "activation":
                input_events.append("press")
        elif capability.family == DECKR_INPUT_ENCODER:
            input_events.append("encoder_rotate")
        elif capability.family == DECKR_INPUT_TOUCH:
            if "tap" in capability.event_types:
                input_events.append("touch_tap")
            if "swipe" in capability.event_types:
                input_events.append("touch_swipe")
    return sorted(set(input_events))


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


def _raster_capability_by_id(
    control: ControlDescriptor,
    capability_id: str | None,
) -> CapabilityDescriptor | None:
    if capability_id is None:
        return _first_raster_capability(control)
    for capability in control.output_capabilities:
        if (
            capability.capability_id == capability_id
            and capability.family == DECKR_OUTPUT_RASTER
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
