import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from deckr.actions.messages import (
    SettingsTargetRef,
)
from deckr.contracts.authority import ContractPointer
from deckr.hardware.descriptors import (
    DECKR_OUTPUT_RASTER,
    DeviceDescriptor,
)

from deckr.controller._actions import (
    ActionAvailabilityRecord,
    ActionAvailabilityState,
    ActionIntentKey,
    ActionMetadata,
    ActionUnavailableCause,
    ProviderActionKey,
    ProviderSessionKey,
)
from deckr.controller._binding_planner import (
    PagePlan,
)
from deckr.controller._binding_resolution import ResolvedControlBinding
from deckr.controller._bindings._action_lifecycle import (
    ActionInstanceSnapshot,
)
from deckr.controller._bindings._attachments import (
    BindingLease,
)
from deckr.controller._device_layout import (
    ControlSurface,
    control_surface_for_raster_capability,
)
from deckr.controller._pages import (
    PageStackEntry,
)

logger = logging.getLogger(__name__)


def _find_control_surface(
    device: DeviceDescriptor,
    control_id: str,
    *,
    raster_capability_id: str | None = None,
) -> ControlSurface | None:
    for control in device.controls:
        if control.control_id == control_id:
            return control_surface_for_raster_capability(control, raster_capability_id)
    return None


def _selected_raster_capability_id(binding: ResolvedControlBinding) -> str | None:
    for capability in binding.control.output_capabilities:
        if capability.capability_id not in binding.output_capability_ids:
            continue
        if (
            capability.family == DECKR_OUTPUT_RASTER
            and capability.capability_type == "bitmap"
        ):
            return capability.capability_id
    return None


def _dedupe_action_intents(
    intents: Iterable[ActionIntentKey],
) -> tuple[ActionIntentKey, ...]:
    return tuple(dict.fromkeys(intents))


def _format_provider_action_keys(
    keys: Iterable[ProviderActionKey],
) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((key.provider_instance_id, key.action_uuid) for key in keys))


@dataclass(frozen=True, slots=True)
class PageCommit:
    plan: PagePlan
    departing: PageStackEntry | None
    preserve_binding_ids: frozenset[str]
    preserve_output_control_ids: frozenset[str]
    transition_reason: str


@dataclass(frozen=True, slots=True)
class BindingLeaseSnapshot:
    binding_id: str
    context_id: str
    action_instance_id: str
    planned_intent: ActionIntentKey
    action_uuid: str
    provider_instance_id: str
    provider_id: str
    provider_session_id: str | None
    provider_session_key: ProviderSessionKey | None
    attached: bool
    control_id: str
    input_capability_ids: frozenset[str]
    raster_capability_id: str | None
    profile_id: str
    page_id: str
    settings_target: SettingsTargetRef | None
    page_session_id: str | None
    item_key: str | None
    handler: str | None
    output_route_generation: int
    command_route_generation: int
    stale_lifecycle_recoveries: int
    contract: ContractPointer | None


@dataclass(frozen=True, slots=True)
class ControlContextSnapshot:
    control_id: str
    context_id: str
    binding_id: str
    action_instance_id: str
    provider_instance_id: str
    provider_id: str
    provider_session_id: str | None
    page_session_id: str | None


@dataclass(frozen=True, slots=True)
class HeldInputSnapshot:
    binding_id: str
    control_id: str
    capability_id: str
    context_id: str


@dataclass(frozen=True, slots=True)
class BindingActionSnapshot:
    binding_leases: Mapping[str, BindingLeaseSnapshot]
    active_contexts: Mapping[str, ControlContextSnapshot]
    action_instances: Mapping[str, ActionInstanceSnapshot]
    provider_session_keys: Mapping[str, ProviderSessionKey | None]
    output_owners: Mapping[str, str]
    held_inputs: tuple[HeldInputSnapshot, ...]
    control_status: Mapping[str, str]


@dataclass(frozen=True, slots=True)
class _UnavailableFallbackRender:
    template: str
    cause: ActionUnavailableCause
    record: ActionAvailabilityRecord | None
    state: ActionAvailabilityState | None


def _qualified_action_id(provider_instance_id: str, action_uuid: str) -> str:
    return f"{provider_instance_id}::{action_uuid}"


def _lease_matches_action(lease: BindingLease, action_meta: ActionMetadata) -> bool:
    return (
        lease.action_uuid == action_meta.uuid
        and lease.provider_instance_id == action_meta.provider_instance_id
        and lease.provider_id == action_meta.provider_id
        and lease.provider_session_id == action_meta.provider_session_id
    )


def _lease_matches_action_ignoring_session(
    lease: BindingLease,
    action_meta: ActionMetadata,
) -> bool:
    return (
        lease.action_uuid == action_meta.uuid
        and lease.provider_instance_id == action_meta.provider_instance_id
        and lease.provider_id == action_meta.provider_id
    )


def _contract_pointer_matches(
    left: ContractPointer | None,
    right: ContractPointer | None,
) -> bool:
    if left is None or right is None:
        return left is right
    return left.contract_id == right.contract_id and left.generation == right.generation


def _binding_lease_snapshot(
    lease: BindingLease,
    *,
    planned_intent: ActionIntentKey,
) -> BindingLeaseSnapshot:
    return BindingLeaseSnapshot(
        binding_id=lease.binding_id,
        context_id=lease.context_id,
        action_instance_id=lease.action_instance_id,
        planned_intent=planned_intent,
        action_uuid=lease.action_uuid,
        provider_instance_id=lease.provider_instance_id,
        provider_id=lease.provider_id,
        provider_session_id=lease.provider_session_id,
        provider_session_key=lease.provider_session_key,
        attached=lease.attached,
        control_id=lease.control_id,
        input_capability_ids=lease.input_capability_ids,
        raster_capability_id=lease.raster_capability_id,
        profile_id=lease.profile_id,
        page_id=lease.page_id,
        settings_target=lease.settings_target,
        page_session_id=lease.page_session_id,
        item_key=lease.item_key,
        handler=lease.handler,
        output_route_generation=lease.output_route_generation,
        command_route_generation=lease.command_route_generation,
        stale_lifecycle_recoveries=lease.stale_lifecycle_recoveries,
        contract=lease.context.contract,
    )


def _control_context_snapshot(lease: BindingLease) -> ControlContextSnapshot:
    return ControlContextSnapshot(
        control_id=lease.control_id,
        context_id=lease.context_id,
        binding_id=lease.binding_id,
        action_instance_id=lease.action_instance_id,
        provider_instance_id=lease.provider_instance_id,
        provider_id=lease.provider_id,
        provider_session_id=lease.provider_session_id,
        page_session_id=lease.page_session_id,
    )
