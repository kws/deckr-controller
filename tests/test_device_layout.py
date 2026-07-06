"""Tests for descriptor-derived control surface helpers."""

from deckr.hardware.descriptors import (
    DECKR_INPUT_BUTTON,
    DECKR_INPUT_ENCODER,
    DECKR_OUTPUT_RASTER,
    CapabilityDescriptor,
    ControlDescriptor,
    ControlGeometry,
    DeviceDescriptor,
)


def _raster_capability(capability_id: str = "raster.bitmap") -> CapabilityDescriptor:
    return CapabilityDescriptor.model_validate(
        {
            "capabilityId": capability_id,
            "family": DECKR_OUTPUT_RASTER,
            "type": "bitmap",
            "direction": "output",
            "access": ["settable"],
            "commandTypes": ["set_frame", "clear"],
            "constraints": [
                {"type": "fixed", "subject": "width", "value": 72},
                {"type": "fixed", "subject": "height", "value": 72},
            ],
        }
    )


def _button_caps() -> tuple[CapabilityDescriptor, ...]:
    return (
        CapabilityDescriptor(
            capabilityId="button.momentary",
            family=DECKR_INPUT_BUTTON,
            type="momentary",
            direction="input",
            access=("emits",),
            eventTypes=("down", "up"),
        ),
        CapabilityDescriptor(
            capabilityId="button.press",
            family=DECKR_INPUT_BUTTON,
            type="activation",
            direction="input",
            access=("emits",),
            eventTypes=("press",),
        ),
    )


def _encoder_caps() -> tuple[CapabilityDescriptor, ...]:
    return (
        CapabilityDescriptor(
            capabilityId="encoder.relative",
            family=DECKR_INPUT_ENCODER,
            type="relative",
            direction="input",
            access=("emits",),
            eventTypes=("rotate",),
        ),
    )


def _make_control(
    control_id: str,
    row: int,
    col: int,
    kind: str = "key",
    *,
    has_display: bool = True,
    input_capabilities: tuple[CapabilityDescriptor, ...] | None = None,
) -> ControlDescriptor:
    return ControlDescriptor(
        controlId=control_id,
        kind=kind,
        geometry=ControlGeometry(x=col, y=row, width=1, height=1, unit="grid"),
        inputCapabilities=(
            input_capabilities if input_capabilities is not None else _button_caps()
        ),
        outputCapabilities=(_raster_capability(),) if has_display else (),
    )


def _device(*controls: ControlDescriptor, device_id: str = "dev1") -> DeviceDescriptor:
    return DeviceDescriptor(
        deviceId=device_id,
        displayName="Test Device",
        fingerprint=f"fingerprint:{device_id}",
        controls=controls,
    )


