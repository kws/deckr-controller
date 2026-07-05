"""Tests for pure binding planner decisions."""

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
from deckr.controller.settings import derive_action_instance_id

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


def test_static_structural_validation_failure_has_invalid_outcome_and_no_plan():
    planner = _planner()
    entry = StaticPageRef(profile_name="default", page_index=0)

    result = planner.build_static_page_plan(
        entry,
        bindings=(_binding("9,9", "action.bound"),),
        device=_device("0,0"),
        action_metadata={_intent("action.bound"): _metadata("action.bound")},
    )

    assert result.plan is None
    assert len(result.outcomes) == 1
    assert result.outcomes[0].status == BindingPlanStatus.INVALID_DEVICE_CONTROL
    assert result.validation_errors[0].code == "control_not_found"


def test_dynamic_self_child_uses_owner_action_and_instance_id():
    planner = _planner()
    session = _dynamic_session()
    entry = DynamicPageCommand(
        pageId="dynamic-page",
        bindings=(
            PageChildBindingDescriptor(
                controlId="0,0",
                target=PageChildBindingTarget(kind="self"),
                itemKey="item-0",
                handler="open",
            ),
        ),
    )

    result = planner.build_dynamic_page_plan(
        entry,
        device=_device("0,0"),
        page_session=session,
        action_metadata={_intent("action.owner"): _metadata("action.owner")},
    )

    assert result.plan is not None
    planned = result.plan.bindings[0]
    assert planned.status == BindingPlanStatus.BOUND
    assert planned.binding.action_uuid == session.owner_action_uuid
    assert planned.binding.provider_instance_id == session.owner_provider_instance_id
    assert planned.action_instance_id == session.action_instance_id
    assert planned.action_meta is session.owner_action_meta
    assert planned.item_key == "item-0"
    assert planned.handler == "open"


def test_dynamic_self_child_uses_owner_session_when_catalog_is_pending():
    planner = _planner()
    session = _dynamic_session()
    entry = DynamicPageCommand(
        pageId="dynamic-page",
        bindings=(
            PageChildBindingDescriptor(
                controlId="0,0",
                target=PageChildBindingTarget(kind="self"),
            ),
        ),
    )

    result = planner.build_dynamic_page_plan(
        entry,
        device=_device("0,0"),
        page_session=session,
        action_metadata={},
        action_status={
            _intent(session.owner_action_uuid): BindingPlanStatus.PENDING,
        },
    )

    assert result.plan is not None
    planned = result.plan.bindings[0]
    assert planned.status == BindingPlanStatus.BOUND
    assert planned.action_meta is session.owner_action_meta


def test_explicit_dynamic_child_uses_child_metadata_and_stable_instance_id():
    planner = _planner()
    session = _dynamic_session()
    child_metadata = _metadata(
        "action.child",
        provider_instance_id="child-provider",
        provider_id="child.provider",
    )
    entry = DynamicPageCommand(
        pageId="dynamic-page",
        bindings=(
            PageChildBindingDescriptor(
                controlId="0,1",
                target=PageChildBindingTarget(
                    kind="action",
                    actionId="action.child",
                    providerInstanceId="child-provider",
                    instanceKey="child-key",
                ),
            ),
        ),
    )

    result = planner.build_dynamic_page_plan(
        entry,
        device=_device("0,1"),
        page_session=session,
        action_metadata={_intent("action.child", "child-provider"): child_metadata},
    )

    assert result.plan is not None
    planned = result.plan.bindings[0]
    expected_stable_id = "\x1f".join(
        (
            "dynamic-page",
            session.page_session_id,
            "child-provider",
            "child-key",
        )
    )
    expected_action_instance_id = derive_action_instance_id(
        controller_id=CONTROLLER_ID,
        config_id=CONFIG_ID,
        action_id="action.child",
        stable_id=expected_stable_id,
    )
    assert planned.status == BindingPlanStatus.BOUND
    assert planned.binding.action_uuid == "action.child"
    assert planned.binding.provider_instance_id == "child-provider"
    assert planned.action_instance_id == expected_action_instance_id
    assert planned.action_meta is child_metadata


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


def test_planner_uses_plain_metadata_snapshots_without_async_lookup():
    planner = _planner()
    entry = StaticPageRef(profile_name="default", page_index=0)

    result = planner.build_static_page_plan(
        entry,
        bindings=(_binding("0,0", "action.bound"),),
        device=_device("0,0"),
        action_metadata={_intent("action.bound"): _metadata("action.bound")},
    )

    assert result.plan is not None
    assert result.plan.bindings[0].status == BindingPlanStatus.BOUND
