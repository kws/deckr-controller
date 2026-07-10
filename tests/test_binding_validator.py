"""Tests for binding validator."""

from unittest.mock import AsyncMock

import pytest
from deckr.actions.messages import PageChildBindingDescriptor, PageChildBindingTarget
from deckr.hardware.descriptors import (
    DECKR_INPUT_BUTTON,
    DECKR_OUTPUT_RASTER,
    CapabilityDescriptor,
    ControlDescriptor,
    ControlGeometry,
    DeviceDescriptor,
)

from deckr.controller._actions import ActionMetadata
from deckr.controller._binding_resolution import ConfiguredControlBinding
from deckr.controller._binding_validator import (
    ValidationResult,
    format_validation_summary,
    validate_dynamic_page_bindings,
    validate_page_bindings,
)
from deckr.controller.config import CapabilitySelector, ControlSelector


def _make_device(
    device_id: str = "test-dev",
    controls: list[ControlDescriptor] | None = None,
) -> DeviceDescriptor:
    return DeviceDescriptor(
        deviceId=device_id,
        displayName="Test",
        fingerprint=f"fingerprint:{device_id}",
        controls=controls or [_make_control("0,0")],
    )


def _make_control(
    control_id: str,
    row: int = 0,
    col: int = 0,
    kind: str = "key",
    has_display: bool = True,
) -> ControlDescriptor:
    output_capabilities = ()
    if has_display:
        output_capabilities = (
            CapabilityDescriptor.model_validate(
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
            ),
        )
    return ControlDescriptor(
        controlId=control_id,
        kind=kind,
        geometry=ControlGeometry(x=col, y=row, width=1, height=1, unit="grid"),
        inputCapabilities=(
            CapabilityDescriptor(
                capabilityId="button.momentary",
                family=DECKR_INPUT_BUTTON,
                type="momentary",
                direction="input",
                access=("emits",),
                eventTypes=("down", "up"),
            ),
        ),
        outputCapabilities=output_capabilities,
    )


# --- validate_page_bindings ---


def _make_key_action():
    return ActionMetadata(
        uuid="action.a",
        provider_instance_id="python",
        provider_id="test",
    )


def _page_child(control_id: str, **kwargs) -> PageChildBindingDescriptor:
    return PageChildBindingDescriptor(
        controlId=control_id,
        target=PageChildBindingTarget(kind="self"),
        **kwargs,
    )


@pytest.mark.asyncio
async def test_validate_dynamic_page_bindings_resolves_explicit_child_action_target():
    device = _make_device(controls=[_make_control("3,0")])
    action = ActionMetadata(
        uuid="action.volume",
        provider_instance_id="sonos-bedroom",
        provider_id="dev.deckr.sonos",
    )

    get_action = AsyncMock(return_value=action)

    bindings = [
        PageChildBindingDescriptor(
            controlId="3,0",
            target=PageChildBindingTarget(
                kind="action",
                actionId="action.volume",
                providerInstanceId="sonos-bedroom",
                instanceKey="bedroom-volume",
            ),
            settings={"zoneName": "Bedroom"},
        )
    ]

    result = await validate_dynamic_page_bindings(
        bindings,
        device,
        get_action,
        owner_action_uuid="action.pager",
        owner_provider_instance_id="python-com.k-si.deckr.kaj",
    )

    assert result.valid is True
    assert result.bindings[0].action_uuid == "action.volume"
    assert result.bindings[0].provider_instance_id == "sonos-bedroom"
    assert result.bindings[0].settings["zoneName"] == "Bedroom"
    assert result.actions == []
    get_action.assert_not_awaited()


@pytest.mark.asyncio
async def test_validate_page_bindings_missing_action():
    """Missing action is non-blocking; page loads with control showing 'unavailable'."""
    device = _make_device(controls=[_make_control("0,0")])

    async def get_action(uuid: str, **kwargs):
        del kwargs
        return None

    bindings = [_page_child("0,0", settings={})]
    result = await validate_dynamic_page_bindings(
        bindings,
        device,
        get_action,
        owner_action_uuid="nonexistent",
        owner_provider_instance_id="python",
    )
    assert result.valid is True  # Page can load (partial activation)
    assert result.has_blocking_errors is False
    assert result.has_non_blocking_errors is False
    assert result.actions == []
    assert result.errors == []


@pytest.mark.asyncio
async def test_validate_page_bindings_rejects_ambiguous_selector():
    device = _make_device(controls=[_make_control("0,0"), _make_control("1,0")])
    action = _make_key_action()

    async def get_action(uuid: str, **kwargs):
        del kwargs
        return action

    result = await validate_page_bindings(
        [
            ConfiguredControlBinding(
                selector=ControlSelector(
                    kind="key",
                    output=(
                        CapabilitySelector(
                            family=DECKR_OUTPUT_RASTER,
                            type="bitmap",
                        ),
                    ),
                ),
                action_uuid="action.a",
                provider_instance_id=None,
                provider_labels={},
                settings={},
            )
        ],
        device,
        get_action,
    )

    assert result.valid is False
    assert result.errors[0].code == "control_selector_ambiguous"


@pytest.mark.asyncio
async def test_activation_requirement_does_not_match_momentary_only_control():
    device = _make_device(controls=[_make_control("0,0")])
    action = _make_key_action()

    async def get_action(uuid: str, **kwargs):
        del kwargs
        return action

    result = await validate_page_bindings(
        [
            ConfiguredControlBinding(
                selector=ControlSelector(
                    control_id="0,0",
                    input=(
                        CapabilitySelector(
                            family=DECKR_INPUT_BUTTON,
                            type="activation",
                        ),
                    ),
                ),
                action_uuid="action.a",
                provider_instance_id=None,
                provider_labels={},
                settings={},
            )
        ],
        device,
        get_action,
    )

    assert result.valid is False
    assert result.errors[0].code == "capability_not_found"


# --- format_validation_summary ---


def test_format_validation_summary_passed():
    result = ValidationResult(valid=True)
    assert "passed" in format_validation_summary(result)
