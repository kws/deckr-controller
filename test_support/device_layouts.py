from unittest.mock import AsyncMock

from deckr.actions.messages import (
    DynamicPageCommand,
    PageChildBindingDescriptor,
    PageChildBindingTarget,
)
from deckr.hardware.descriptors import (
    DECKR_INPUT_BUTTON,
    DECKR_INPUT_ENCODER,
    DECKR_INPUT_TOUCH,
    DECKR_OUTPUT_RASTER,
    CapabilityDescriptor,
    ControlDescriptor,
    ControlGeometry,
    DeviceDescriptor,
    DeviceRef,
)

from test_support.binding_constants import PROVIDER_INSTANCE_ID


def _make_control(
    control_id: str,
    row: int = 0,
    col: int = 0,
    kind: str = "key",
    events: list[str] | None = None,
    has_display: bool = True,
    raster_width: int = 72,
    raster_height: int = 72,
    raster_rotation: int | None = None,
) -> ControlDescriptor:
    if events is None:
        events = ["momentary", "press"]
    input_capabilities = []
    if "momentary" in events:
        input_capabilities.append(
            CapabilityDescriptor(
                capabilityId="button.momentary",
                family=DECKR_INPUT_BUTTON,
                type="momentary",
                direction="input",
                access=("emits",),
                eventTypes=("down", "up"),
            )
        )
    if "press" in events:
        input_capabilities.append(
            CapabilityDescriptor.model_validate(
                {
                    "capabilityId": "button.press",
                    "family": DECKR_INPUT_BUTTON,
                    "type": "activation",
                    "direction": "input",
                    "access": ["emits"],
                    "eventTypes": ["press"],
                }
            )
        )
    if "rotate" in events:
        input_capabilities.append(
            CapabilityDescriptor(
                capabilityId="encoder.relative",
                family=DECKR_INPUT_ENCODER,
                type="relative",
                direction="input",
                access=("emits",),
                eventTypes=("rotate",),
            )
        )
    if "tap" in events or "swipe" in events:
        event_types = []
        if "tap" in events:
            event_types.append("tap")
        if "swipe" in events:
            event_types.append("swipe")
        input_capabilities.append(
            CapabilityDescriptor(
                capabilityId="touch.gesture",
                family=DECKR_INPUT_TOUCH,
                type="gesture",
                direction="input",
                access=("emits",),
                eventTypes=tuple(event_types),
            )
        )
    output_capabilities = []
    if has_display:
        raster_constraints = [
            {
                "type": "fixed",
                "subject": "width",
                "value": raster_width,
                "unit": "pixel",
            },
            {
                "type": "fixed",
                "subject": "height",
                "value": raster_height,
                "unit": "pixel",
            },
        ]
        if raster_rotation is not None:
            raster_constraints.append(
                {
                    "type": "fixed",
                    "subject": "rotation",
                    "value": raster_rotation,
                    "unit": "degree",
                }
            )
        output_capabilities.append(
            CapabilityDescriptor.model_validate(
                {
                    "capabilityId": "raster.bitmap",
                    "family": DECKR_OUTPUT_RASTER,
                    "type": "bitmap",
                    "direction": "output",
                    "access": ["settable"],
                    "commandTypes": ["set_frame", "clear"],
                    "constraints": raster_constraints,
                }
            )
        )
    return ControlDescriptor(
        controlId=control_id,
        kind=kind,
        geometry=ControlGeometry(x=col, y=row, width=1, height=1, unit="grid"),
        inputCapabilities=tuple(input_capabilities),
        outputCapabilities=tuple(output_capabilities),
    )


def _make_mock_device(
    device_id: str = "test-device",
    controls: list[ControlDescriptor] | None = None,
) -> DeviceDescriptor:
    """Create device metadata for controller tests."""
    if controls is None:
        controls = [_make_control("0,0"), _make_control("1,0")]
    return DeviceDescriptor(
        deviceId=device_id,
        displayName="Test Device",
        fingerprint=f"fingerprint:{device_id}",
        controls=tuple(controls),
    )


def _make_mars_msd_two_device() -> DeviceDescriptor:
    key_kwargs = {
        "kind": "key",
        "events": ["momentary"],
        "raster_width": 64,
        "raster_height": 64,
        "raster_rotation": 270,
    }
    controls = [
        _make_control("0,0", row=0, col=0, **key_kwargs),
        _make_control("1,0", row=0, col=1, **key_kwargs),
        _make_control("2,0", row=0, col=2, **key_kwargs),
        _make_control("0,1", row=1, col=0, **key_kwargs),
        _make_control("1,1", row=1, col=1, **key_kwargs),
        _make_control("2,1", row=1, col=2, **key_kwargs),
        _make_control(
            "3,0",
            row=0,
            col=3,
            kind="dial",
            events=["rotate", "momentary"],
            has_display=False,
        ),
        _make_control(
            "3,1",
            row=1,
            col=3,
            kind="dial",
            events=["rotate", "momentary"],
            has_display=False,
        ),
        _make_control(
            "4,1",
            row=1,
            col=4,
            kind="dial",
            events=["rotate", "momentary"],
            has_display=False,
        ),
        _make_control(
            "B1", row=2, col=0, kind="button", events=["momentary"], has_display=False
        ),
        _make_control(
            "B2", row=2, col=1, kind="button", events=["momentary"], has_display=False
        ),
        _make_control(
            "B3", row=2, col=2, kind="button", events=["momentary"], has_display=False
        ),
    ]
    return DeviceDescriptor(
        deviceId="mars-gaming-msd-two",
        displayName="Mars Gaming MSD-TWO",
        fingerprint="0B00:1001:0300D0785616",
        manufacturer="MiraBox",
        model="MSD-TWO",
        controls=tuple(controls),
    )


def _hardware_ref(device: DeviceDescriptor) -> DeviceRef:
    return DeviceRef(
        managerId="manager-main",
        deviceId=device.device_id,
    )


class FakeHardwareCommandService:
    def claim_identity(self, config_id):
        return None

    def has_authority(self, config_id, device_ref):
        return True

    def __init__(self):
        self.set_raster_frame = AsyncMock()
        self.clear_raster = AsyncMock()


def _dynamic_page(page_id: str, *control_ids: str) -> DynamicPageCommand:
    return DynamicPageCommand(
        pageId=page_id,
        bindings=tuple(
            PageChildBindingDescriptor(
                controlId=control_id,
                target=PageChildBindingTarget(kind="self"),
                itemKey=f"item-{ix}",
            )
            for ix, control_id in enumerate(control_ids)
        ),
    )


def _dynamic_page_with_action_child(
    page_id: str,
    control_id: str,
    *,
    action_id: str,
    provider_instance_id: str,
) -> DynamicPageCommand:
    return DynamicPageCommand(
        pageId=page_id,
        bindings=(
            PageChildBindingDescriptor(
                controlId=control_id,
                target=PageChildBindingTarget(
                    kind="action",
                    actionId=action_id,
                    providerInstanceId=provider_instance_id,
                    instanceKey=control_id,
                ),
            ),
        ),
    )


def _dynamic_page_with_action_children(
    page_id: str,
    *children: tuple[str, str],
    provider_instance_id: str = PROVIDER_INSTANCE_ID,
) -> DynamicPageCommand:
    return DynamicPageCommand(
        pageId=page_id,
        bindings=tuple(
            PageChildBindingDescriptor(
                controlId=control_id,
                target=PageChildBindingTarget(
                    kind="action",
                    actionId=action_id,
                    providerInstanceId=provider_instance_id,
                    instanceKey=control_id,
                ),
            )
            for control_id, action_id in children
        ),
    )
