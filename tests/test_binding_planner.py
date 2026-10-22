"""Tests for pure binding planner decisions."""

from dataclasses import replace

import pytest
from deckr.actions.catalog import ActionDescriptor
from deckr.actions.messages import (
    DynamicPageCommand,
    PageChildBindingDescriptor,
    PageChildBindingTarget,
)
from deckr.hardware.descriptors import (
    DECKR_INPUT_BUTTON,
    DECKR_OUTPUT_RASTER,
    CapabilityDescriptor,
    ControlDescriptor,
    ControlGeometry,
    DeviceDescriptor,
    DeviceRef,
)

from deckr.controller._actions import ActionIntentKey, ActionMetadata
from deckr.controller._binding_plan import BindingPlanStatus, format_validation_summary
from deckr.controller._binding_planner import BindingPlanner
from deckr.controller._binding_resolution import ConfiguredControlBinding
from deckr.controller._capability_matching import matched_capabilities
from deckr.controller._pages import DynamicPageSession, StaticPageRef
from deckr.controller.config import ControlSelector
from deckr.controller.settings import derive_static_action_instance_id

CONTROLLER_ID = "controller-main"
CONFIG_ID = "test-device"
PROVIDER_INSTANCE_ID = "python"
PROVIDER_ID = "test.provider"
PROVIDER_SESSION_ID = "provider-session"


def _planner() -> BindingPlanner:
    return BindingPlanner(CONTROLLER_ID, CONFIG_ID)


def _make_control(control_id: str) -> ControlDescriptor:
    return ControlDescriptor(
        controlId=control_id,
        kind="key",
        geometry=ControlGeometry(x=0, y=0, width=1, height=1, unit="grid"),
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
        outputCapabilities=(
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
        ),
    )


def _device(*control_ids: str) -> DeviceDescriptor:
    return DeviceDescriptor(
        deviceId="test-device",
        displayName="Test Device",
        fingerprint="fingerprint:test-device",
        controls=tuple(_make_control(control_id) for control_id in control_ids),
    )


def _binding(
    control_id: str,
    action_id: str,
    *,
    provider_instance_id: str | None = PROVIDER_INSTANCE_ID,
    stable_id: str | None = None,
) -> ConfiguredControlBinding:
    return ConfiguredControlBinding(
        selector=ControlSelector(control_id=control_id),
        action_id=action_id,
        provider_instance_id=provider_instance_id,
        provider_labels={},
        settings={},
        stable_id=stable_id,
    )


def _metadata(
    action_id: str,
    *,
    provider_instance_id: str = PROVIDER_INSTANCE_ID,
    provider_id: str = PROVIDER_ID,
) -> ActionMetadata:
    return ActionMetadata(
        action_id=action_id,
        provider_instance_id=provider_instance_id,
        provider_id=provider_id,
        provider_session_id=PROVIDER_SESSION_ID,
    )


def _intent(
    action_id: str,
    provider_instance_id: str | None = PROVIDER_INSTANCE_ID,
) -> ActionIntentKey:
    return ActionIntentKey(action_id, provider_instance_id, ())


def _dynamic_session() -> DynamicPageSession:
    return DynamicPageSession(
        page_id="dynamic-page",
        page_session_id="page-session-1",
        context_id="page-context",
        action_instance_id="owner-action-instance",
        owner_context_id="owner-context",
        owner_binding_id="owner-binding",
        owner_control_id="0,0",
        owner_action_id="action.owner",
        owner_provider_instance_id=PROVIDER_INSTANCE_ID,
        owner_provider_id=PROVIDER_ID,
        owner_provider_session_id=PROVIDER_SESSION_ID,
        owner_action_meta=_metadata("action.owner"),
        owner_profile="default",
        owner_page=0,
        timeout_ms=60_000,
        last_activity=0.0,
        settings_target=None,
    )


def test_static_page_plan_records_bound_and_unavailable_controls():
    planner = _planner()
    entry = StaticPageRef(profile_name="default", page_index=0)
    bindings = (
        _binding("0,0", "action.bound", stable_id="bound-control"),
        _binding("0,1", "action.missing", stable_id="missing-control"),
    )
    metadata = _metadata("action.bound")

    result = planner.build_static_page_plan(
        entry,
        bindings=bindings,
        device=_device("0,0", "0,1"),
        action_metadata={_intent("action.bound"): metadata},
    )

    assert result.plan is not None
    assert [outcome.status for outcome in result.outcomes] == [
        BindingPlanStatus.BOUND,
        BindingPlanStatus.UNAVAILABLE,
    ]
    assert [binding.status for binding in result.plan.bindings] == [
        BindingPlanStatus.BOUND,
        BindingPlanStatus.UNAVAILABLE,
    ]
    assert result.plan.bindings[0].action_meta is metadata
    assert result.plan.bindings[1].action_meta is None


@pytest.mark.parametrize(
    "required, expected",
    [
        (True, BindingPlanStatus.INVALID_DEVICE_CONTROL),
        (False, BindingPlanStatus.BOUND),
    ],
)
def test_missing_action_requirements_control_attachment(required, expected):
    metadata = _metadata("action.test")
    metadata = replace(
        metadata,
        descriptor=ActionDescriptor(
            actionId="action.test",
            requirements=(
                {
                    "name": "turn",
                    "required": required,
                    "preferences": [{"family": "dev.deckr.input.encoder"}],
                },
            ),
        ),
    )
    result = _planner().build_static_page_plan(
        StaticPageRef("default", 0),
        bindings=(_binding("0,0", "action.test"),),
        device=_device("0,0"),
        action_metadata={_intent("action.test"): metadata},
    )
    assert result.plan.bindings[0].status == expected


def test_action_requirement_preference_and_named_matches_are_preserved():
    metadata = _metadata("action.test")
    metadata = replace(
        metadata,
        descriptor=ActionDescriptor(
            actionId="action.test",
            requirements=(
                {
                    "name": "activate",
                    "eventTypes": ["down"],
                    "views": ["native"],
                    "preferences": [
                        {"family": "dev.deckr.input.encoder"},
                        {"family": "dev.deckr.input.button", "type": "momentary"},
                    ],
                },
            ),
        ),
    )
    result = _planner().build_static_page_plan(
        StaticPageRef("default", 0),
        bindings=(_binding("0,0", "action.test"),),
        device=_device("0,0"),
        action_metadata={_intent("action.test"): metadata},
    )
    planned = result.plan.bindings[0]
    assert planned.status == BindingPlanStatus.BOUND
    matches = matched_capabilities(
        planned.binding,
        metadata.requirements,
        DeviceRef(managerId="manager", deviceId="test-device"),
        _device("0,0"),
    )
    assert [(m.requirement_name, m.capability.capability_id) for m in matches] == [
        ("activate", "button.momentary")
    ]
    assert planned.binding.output_capability_ids == frozenset()


def test_retained_static_plan_restores_metadata_when_snapshot_is_empty():
    planner = _planner()
    entry = StaticPageRef(profile_name="default", page_index=0)
    bindings = (_binding("0,0", "action.bound", stable_id="stable"),)
    metadata = _metadata("action.bound")
    first = planner.build_static_page_plan(
        entry,
        bindings=bindings,
        device=_device("0,0"),
        action_metadata={_intent("action.bound"): metadata},
    )
    assert first.plan is not None

    restored = planner.build_static_page_plan(
        entry,
        bindings=bindings,
        device=_device("0,0"),
        action_metadata={},
        retained_plan=first.plan,
    )

    assert restored.plan is not None
    planned = restored.plan.bindings[0]
    assert planned.status == BindingPlanStatus.BOUND
    assert planned.action_meta is metadata


def test_selector_only_static_plan_uses_config_fallback_identity():
    planner = _planner()
    entry = StaticPageRef(profile_name="default", page_index=0)
    binding = ConfiguredControlBinding(
        selector=ControlSelector(kind="key"),
        action_id="action.bound",
        provider_instance_id=PROVIDER_INSTANCE_ID,
        provider_labels={},
        settings={},
        stable_id=None,
        identity_fallback="0",
    )
    metadata = _metadata("action.bound")

    result = planner.build_static_page_plan(
        entry,
        bindings=(binding,),
        device=_device("hardware-key"),
        action_metadata={_intent("action.bound"): metadata},
    )

    assert result.plan is not None
    planned = result.plan.bindings[0]
    assert planned.control_id == "hardware-key"
    assert planned.action_instance_id == derive_static_action_instance_id(
        controller_id=CONTROLLER_ID,
        config_id=CONFIG_ID,
        action_id="action.bound",
        profile_id="default",
        page_id="0",
        identity_fallback="0",
    )


def test_dynamic_page_plan_resolves_explicit_child_action_target():
    planner = _planner()
    child = PageChildBindingDescriptor(
        controlId="3,0",
        target=PageChildBindingTarget(
            kind="action",
            actionId="action.volume",
            providerInstanceId="sonos-bedroom",
            instanceKey="bedroom-volume",
        ),
        settings={"zoneName": "Bedroom"},
    )
    descriptor = DynamicPageCommand(pageId="dynamic-page", bindings=(child,))
    metadata = _metadata(
        "action.volume",
        provider_instance_id="sonos-bedroom",
        provider_id="dev.deckr.sonos",
    )

    result = planner.build_dynamic_page_plan(
        descriptor,
        device=_device("3,0"),
        page_session=_dynamic_session(),
        action_metadata={_intent("action.volume", "sonos-bedroom"): metadata},
    )

    assert result.plan is not None
    planned = result.plan.bindings[0]
    assert planned.status == BindingPlanStatus.BOUND
    assert planned.binding.action_id == "action.volume"
    assert planned.binding.provider_instance_id == "sonos-bedroom"
    assert planned.binding.settings["zoneName"] == "Bedroom"


def test_format_validation_summary_handles_empty_and_truncated_errors():
    planner = _planner()
    entry = StaticPageRef(profile_name="default", page_index=0)
    result = planner.build_static_page_plan(
        entry,
        bindings=tuple(
            _binding(f"missing-{index}", f"action.{index}") for index in range(4)
        ),
        device=_device("0,0"),
        action_metadata={},
    )

    assert format_validation_summary(()) == "validation passed"
    summary = format_validation_summary(result.validation_errors)
    assert "4 error(s)" in summary
    assert "... and 1 more" in summary


@pytest.mark.parametrize("select_one", [False, True])
def test_multiple_raster_targets_require_explicit_single_output_selection(select_one):
    control = _make_control("0,0")
    first = control.output_capabilities[0]
    second = first.model_copy(update={"capability_id": "raster.second"})
    control = control.model_copy(update={"output_capabilities": (first, second)})
    device = _device("0,0").model_copy(update={"controls": (control,)})
    binding = _binding("0,0", "action.test")
    if select_one:
        binding = replace(
            binding,
            selector=ControlSelector(
                control_id="0,0", output=[{"capability_id": "raster.second"}]
            ),
        )
    metadata = replace(
        _metadata("action.test"),
        descriptor=ActionDescriptor(
            actionId="action.test",
            requirements=[
                {
                    "name": "front",
                    "required": False,
                    "preferences": [{"capabilityId": "raster.bitmap"}],
                },
                {
                    "name": "back",
                    "required": False,
                    "preferences": [{"capabilityId": "raster.second"}],
                },
            ],
        ),
    )
    result = _planner().build_static_page_plan(
        StaticPageRef("default", 0),
        bindings=(binding,),
        device=device,
        action_metadata={_intent("action.test"): metadata},
    )
    planned = result.plan.bindings[0]
    assert planned.status == (
        BindingPlanStatus.BOUND
        if select_one
        else BindingPlanStatus.INVALID_DEVICE_CONTROL
    )
    if select_one:
        assert planned.binding.output_capability_ids == frozenset({"raster.second"})


def test_action_snapshot_copies_and_freezes_nested_provider_metadata():
    from dataclasses import FrozenInstanceError

    labels = {"room": "office"}
    schema = {"properties": {"text": {"type": "string"}}}
    metadata = ActionMetadata(
        action_id="action.test",
        provider_instance_id="provider",
        provider_id="plugin",
        provider_labels=labels,
        settings_schema=schema,
    )
    labels["room"] = "kitchen"
    schema["properties"]["text"]["type"] = "number"
    assert metadata.provider_labels["room"] == "office"
    assert metadata.settings_schema["properties"]["text"]["type"] == "string"
    with pytest.raises(TypeError):
        metadata.provider_labels["room"] = "other"
    with pytest.raises(FrozenInstanceError):
        metadata.name = "changed"
