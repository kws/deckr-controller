from __future__ import annotations

from typing import TYPE_CHECKING

from deckr.controller._binding_plan import BindingPlanStatus, PagePlan, PlannedBinding
from deckr.controller._bindings._models import (
    _find_control_layout,
    _lease_matches_action,
    _selected_raster_capability_id,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from deckr.controller._bindings._attachments import BindingLease
    from deckr.controller._bindings._ports import BindingActionService
    from deckr.controller._session_state import DeviceState

import logging

from deckr.actions.messages import (
    MatchedCapability,
    SettingsTargetRef,
)
from deckr.contracts.authority import ContractPointer

from deckr.controller._actions import (
    ActionAvailabilitySource,
    ActionAvailabilityState,
    ActionIntentKey,
    ActionMetadata,
)
from deckr.controller._binding_resolution import ResolvedControlBinding
from deckr.controller._bindings._attachments import (
    BindingLease,
)
from deckr.controller._bindings._models import (
    _contract_pointer_matches,
)
from deckr.controller.action_provider.builtin import BUILTIN_ACTION_PROVIDER_ID

logger = logging.getLogger(__name__)


class BindingRules:
    """Read-only binding identity and capability policy over current snapshots."""

    def __init__(
        self,
        *,
        state: DeviceState,
        catalog: BindingActionService,
        controller_id: str,
        clock: Callable[[], float],
    ):
        self.state = state
        self.catalog = catalog
        self.controller_id = controller_id
        self.clock = clock

    def _build_settings_target_for_binding(
        self,
        *,
        action_instance_id: str,
        binding: ResolvedControlBinding,
        provider_instance_id: str,
        provider_id: str,
    ) -> SettingsTargetRef:
        return SettingsTargetRef(
            scope="action_instance",
            controllerId=self.controller_id,
            configId=self.state.config.id,
            providerInstanceId=provider_instance_id,
            providerId=provider_id,
            actionId=binding.action_id,
            actionInstanceId=action_instance_id,
            stableId=binding.stable_id,
        )

    def _matched_capabilities(
        self,
        binding: ResolvedControlBinding,
        action_meta: ActionMetadata,
    ) -> tuple[MatchedCapability, ...]:
        from deckr.controller._capability_matching import matched_capabilities

        return matched_capabilities(
            binding,
            action_meta.requirements,
            self.state.identity.hardware,
            self.state.device,
        )

    def _action_metadata_with_current_session(
        self,
        action_meta: ActionMetadata,
    ) -> ActionMetadata:
        if action_meta.provider_instance_id == BUILTIN_ACTION_PROVIDER_ID:
            return action_meta
        intent = ActionIntentKey(
            action_id=action_meta.action_id,
            provider_instance_id=action_meta.provider_instance_id,
            provider_labels=tuple(sorted((action_meta.provider_labels or {}).items())),
        )
        record = self.catalog.record_for_intent(
            intent,
            now=self.clock(),
        )
        if (
            record is None
            or record.source != ActionAvailabilitySource.SERVICE_VIEW
            or record.metadata is None
            or self.catalog.state_for_key(record.key, now=self.clock())
            != ActionAvailabilityState.AVAILABLE
            or record.metadata.provider_session_id is None
        ):
            return action_meta
        current = record.metadata
        if (
            current.action_id != action_meta.action_id
            or current.provider_instance_id != action_meta.provider_instance_id
            or current.provider_id != action_meta.provider_id
        ):
            return action_meta
        return current

    def _lease_uses_current_provider_session_contract(
        self,
        lease: BindingLease,
    ) -> bool:
        if lease.provider_instance_id == BUILTIN_ACTION_PROVIDER_ID:
            return True
        current = self._current_provider_session_contract_for_lease(lease)
        return _contract_pointer_matches(current, lease.context.contract)

    def _current_provider_session_contract_for_lease(
        self,
        lease: BindingLease,
    ) -> ContractPointer | None:
        return self.catalog.current_contract(lease.provider_session_key)

    def matching_existing(
        self,
        plan: PagePlan,
        planned: PlannedBinding,
    ) -> BindingLease | None:
        if planned.action_meta is None or planned.status != BindingPlanStatus.BOUND:
            return None
        action_meta = self._action_metadata_with_current_session(planned.action_meta)
        raster_capability_id = _selected_raster_capability_id(planned.binding)
        for lease in tuple(self.state.attachments.binding_leases.values()):
            if not _lease_matches_action(lease, action_meta):
                continue
            if lease.control_id != planned.control_id:
                continue
            if lease.action_instance_id != planned.action_instance_id:
                continue
            if lease.profile_id != plan.profile_id or lease.page_id != plan.page_id:
                continue
            if lease.page_session_id != planned.page_session_id:
                continue
            if lease.item_key != planned.item_key or lease.handler != planned.handler:
                continue
            if lease.input_capability_ids != planned.binding.input_capability_ids:
                continue
            if lease.raster_capability_id != raster_capability_id:
                continue
            if not self.requirements_match(lease, planned.binding, action_meta):
                continue
            return lease
        return None

    def requirements_match(
        self,
        lease: BindingLease,
        binding: ResolvedControlBinding,
        action: ActionMetadata,
    ) -> bool:
        raster = _selected_raster_capability_id(binding)
        return (
            lease.action_descriptor == action.descriptor
            and lease.input_capability_ids == binding.input_capability_ids
            and lease.raster_capability_id == raster
            and lease.context.metadata.matched_capabilities
            == self._matched_capabilities(binding, action)
            and lease.control
            == _find_control_layout(
                self.state.device, binding.control_id, raster_capability_id=raster
            )
        )
