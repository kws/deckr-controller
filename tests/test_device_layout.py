"""Tests for DeviceLayout builder and ImageGrid."""

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
    ImageGrid,
    RasterImageFormat,
    SlotInfo,
    build_device_layout,
)


def _raster_capability() -> CapabilityDescriptor:
    return CapabilityDescriptor.model_validate(
        {
            "capabilityId": "raster.bitmap",
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
        inputCapabilities=input_capabilities if input_capabilities is not None else _button_caps(),
        outputCapabilities=(_raster_capability(),) if has_display else (),
    )


def _device(*controls: ControlDescriptor, device_id: str = "dev1") -> DeviceDescriptor:
    return DeviceDescriptor(
        deviceId=device_id,
        displayName="Test Device",
        fingerprint=f"fingerprint:{device_id}",
        controls=controls,
    )


def test_build_device_layout_empty_device():
    layout = build_device_layout(_device())
    assert layout.device_id == "dev1"
    assert layout.image_grid.rows == 0
    assert layout.image_grid.cols == 0
    assert layout.image_grid.total_keys() == 0
    assert len(layout.buttons) == 0
    assert len(layout.encoders) == 0


def test_build_device_layout_image_grid_only():
    device = _device(
        _make_control("0,0", 0, 0),
        _make_control("1,0", 1, 0),
        _make_control("0,1", 0, 1),
    )
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
    encoder_caps = (
        CapabilityDescriptor(
            capabilityId="encoder.relative",
            family=DECKR_INPUT_ENCODER,
            type="relative",
            direction="input",
            access=("emits",),
            eventTypes=("rotate",),
        ),
    )
    device = _device(
        _make_control("0,0", 0, 0),
        _make_control("B1", 2, 0, kind="button", has_display=False),
        _make_control(
            "D1",
            2,
            1,
            kind="encoder",
            has_display=False,
            input_capabilities=encoder_caps,
        ),
    )
    layout = build_device_layout(device)
    assert layout.image_grid.total_keys() == 1
    assert len(layout.buttons) == 1
    assert layout.buttons[0].slot_id == "B1"
    assert len(layout.encoders) == 1
    assert layout.encoders[0].slot_id == "D1"
    assert layout.encoders[0].image_format is None


def test_image_grid_slot_id_row_col():
    grid = ImageGrid(
        rows=2,
        cols=2,
        slots=(
            SlotInfo("0,0", 0, 0, RasterImageFormat(width=72, height=72), "raster.bitmap"),
            SlotInfo("0,1", 0, 1, RasterImageFormat(width=72, height=72), "raster.bitmap"),
            SlotInfo("1,0", 1, 0, RasterImageFormat(width=72, height=72), "raster.bitmap"),
            SlotInfo("1,1", 1, 1, RasterImageFormat(width=72, height=72), "raster.bitmap"),
        ),
    )
    assert grid.slot_id(0, 0) == "0,0"
    assert grid.slot_id(1, 0) == "1,0"
    assert grid.slot_id(0, 1) == "0,1"
    assert grid.slot_id(1, 1) == "1,1"
    assert grid.slot_id(2, 0) is None
    assert grid.total_keys() == 4
