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

from deckr.controller._device_layout import (
    RasterImageFormat,
    control_surface,
    raster_controls,
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


def test_raster_controls_empty_device():
    assert raster_controls(_device()) == ()


def test_raster_controls_order_by_geometry_without_kind_classification():
    device = _device(
        _make_control("plain-button-with-display", 1, 0, kind="button"),
        _make_control("top-key", 0, 0, kind="key"),
        _make_control("encoder-display", 0, 1, kind="encoder"),
        _make_control("button-no-display", 2, 0, kind="button", has_display=False),
    )

    controls = raster_controls(device)

    assert [control.control_id for control in controls] == [
        "top-key",
        "encoder-display",
        "plain-button-with-display",
    ]
    assert all(control.image_format == RasterImageFormat(width=72, height=72) for control in controls)
    assert {control.capability_id for control in controls} == {"raster.bitmap"}


def test_control_surface_reports_descriptor_kind_and_input_events():
    surface = control_surface(
        _make_control(
            "D1",
            2,
            1,
            kind="encoder",
            has_display=False,
            input_capabilities=_encoder_caps(),
        )
    )

    assert surface.id == "D1"
    assert surface.kind == "encoder"
    assert surface.input_events == ("rotate",)
    assert surface.image_format is None
    assert surface.raster_capability_id is None


def test_control_surface_reports_raster_capability_metadata():
    surface = control_surface(_make_control("0,0", 0, 0))

    assert surface.id == "0,0"
    assert surface.kind == "key"
    assert surface.coordinates.row == 0
    assert surface.coordinates.column == 0
    assert surface.input_events == ("down", "press", "up")
    assert surface.image_format == RasterImageFormat(width=72, height=72)
    assert surface.raster_capability_id == "raster.bitmap"
