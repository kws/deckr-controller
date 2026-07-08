"""Tests for pure binding planner decisions."""

from deckr.hardware.descriptors import (
    DECKR_INPUT_BUTTON,
    DECKR_OUTPUT_RASTER,
    CapabilityDescriptor,
    ControlDescriptor,
    ControlGeometry,
    DeviceDescriptor,
)

from deckr.controller._binding_planner import (
    ActionIntentKey,
    BindingPlanner,
    BindingPlanStatus,
    DynamicPageSession,
)
from deckr.controller._binding_resolution import ConfiguredControlBinding
from deckr.controller._navigation_service import StaticPageRef
from deckr.controller.action_provider.provider import ActionMetadata
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
    action_uuid: str,
    *,
    provider_instance_id: str | None = PROVIDER_INSTANCE_ID,
    stable_id: str | None = None,
) -> ConfiguredControlBinding:
    return ConfiguredControlBinding(
        selector=ControlSelector(control_id=control_id),
        action_uuid=action_uuid,
        provider_instance_id=provider_instance_id,
        provider_labels={},
        settings={},
        stable_id=stable_id,
    )


def _metadata(
    action_uuid: str,
    *,
    provider_instance_id: str = PROVIDER_INSTANCE_ID,
    provider_id: str = PROVIDER_ID,
) -> ActionMetadata:
    return ActionMetadata(
        uuid=action_uuid,
        provider_instance_id=provider_instance_id,
        provider_id=provider_id,
        provider_session_id=PROVIDER_SESSION_ID,
    )


def _intent(
    action_uuid: str,
    provider_instance_id: str | None = PROVIDER_INSTANCE_ID,
) -> ActionIntentKey:
    return ActionIntentKey(action_uuid, provider_instance_id, ())


def _dynamic_session() -> DynamicPageSession:
    return DynamicPageSession(
        page_id="dynamic-page",
        page_session_id="page-session-1",
        context_id="page-context",
        action_instance_id="owner-action-instance",
        owner_context_id="owner-context",
        owner_binding_id="owner-binding",
        owner_control_id="0,0",
        owner_action_uuid="action.owner",
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
        action_uuid="action.bound",
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

