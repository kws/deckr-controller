"""Descriptor-derived control layout helpers."""

from __future__ import annotations

from dataclasses import dataclass
from math import ceil, floor
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
from deckr.hardware.validation import validate_constraints

from deckr.controller._raster import RasterFormat


@dataclass(frozen=True)
class ControlCoordinates:
    column: int
    row: int


@dataclass(frozen=True)
class ControlLayout:
    id: str
    kind: str
    coordinates: ControlCoordinates
    input_events: tuple[str, ...]
    image_format: RasterFormat | None = None
    raster_capability_id: str | None = None


@dataclass(frozen=True)
class RasterTarget:
    """One control with a raster output capability."""

    control_id: str
    row: int
    column: int
    image_format: RasterFormat
    capability_id: str


def control_layout_by_id(
    device: DeviceDescriptor,
    control_id: str,
) -> ControlLayout | None:
    for control in device.controls:
        if control.control_id == control_id:
            return control_layout(control)
    return None


def control_layout(control: ControlDescriptor) -> ControlLayout:
    raster_capability = _first_raster_capability(control)
    return control_layout_for_raster_capability(
        control,
        raster_capability.capability_id if raster_capability is not None else None,
    )


def control_layout_for_raster_capability(
    control: ControlDescriptor,
    raster_capability_id: str | None,
) -> ControlLayout:
    raster_capability = _raster_capability_by_id(control, raster_capability_id)
    image_format = (
        _raster_image_format(raster_capability)
        if raster_capability is not None
        else None
    )
    geometry = control.geometry
    return ControlLayout(
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


def raster_controls(device: DeviceDescriptor) -> tuple[RasterTarget, ...]:
    """Return controls with raster outputs, ordered by descriptor geometry."""

    controls: list[RasterTarget] = []
    for descriptor in device.controls:
        surface = control_layout(descriptor)
        if surface.image_format is None or surface.raster_capability_id is None:
            continue
        controls.append(
            RasterTarget(
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
                input_events.extend(("down", "up"))
            elif capability.capability_type == "activation":
                input_events.append("press")
        elif capability.family == DECKR_INPUT_ENCODER:
            input_events.append("rotate")
        elif capability.family == DECKR_INPUT_TOUCH:
            if "tap" in capability.event_types:
                input_events.append("tap")
            if "swipe" in capability.event_types:
                input_events.append("swipe")
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
    constraints = tuple(c for c in capability.constraints if c.subject == subject)
    candidates = [default]
    for constraint in constraints:
        if constraint.value is not None:
            candidates.append(constraint.value)
        candidates.extend(constraint.values)
        if constraint.minimum is not None:
            candidates.append(ceil(constraint.minimum))
        if constraint.maximum is not None:
            candidates.append(floor(constraint.maximum))
    for constraint in constraints:
        if constraint.step is not None:
            base = constraint.minimum or 0
            candidates.extend(
                base + ceil((candidate - base) / constraint.step) * constraint.step
                for candidate in tuple(candidates)
                if isinstance(candidate, int | float)
            )
    for candidate in candidates:
        if subject in {"width", "height"} and (
            not isinstance(candidate, int | float)
            or candidate <= 0
            or int(candidate) != candidate
        ):
            continue
        try:
            validate_constraints(capability, {subject: candidate})
        except (TypeError, ValueError, ArithmeticError):
            continue
        return candidate
    raise ValueError(f"No supported raster {subject} satisfies the descriptor")


def _raster_image_format(capability: CapabilityDescriptor) -> RasterFormat:
    encoding = None
    for candidate in ("jpeg", "png"):
        try:
            validate_constraints(capability, {"encoding": candidate})
        except (TypeError, ValueError, ArithmeticError):
            continue
        encoding = candidate
        break
    if encoding is None:
        raise ValueError("Raster capability has no supported image encoding")
    return RasterFormat(
        width=int(_constraint_value(capability, "width", 72)),
        height=int(_constraint_value(capability, "height", 72)),
        rotation=int(_constraint_value(capability, "rotation", 0)),
        format=encoding.upper(),
    )
