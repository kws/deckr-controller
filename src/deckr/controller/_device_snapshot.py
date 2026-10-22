import logging
from types import MappingProxyType

from deckr.controller._bindings._models import (
    BindingActionSnapshot,
    HeldInputSnapshot,
    _binding_lease_snapshot,
    _control_context_snapshot,
)
from deckr.controller._session_state import BindingStatus

logger = logging.getLogger(__name__)


def binding_snapshot(
    state, lifecycle, planned_intent_for_lease
) -> BindingActionSnapshot:
    plan = state.navigation.current_plan()
    control_status = {}
    for planned in plan.bindings if plan is not None else ():
        lease = state.attachments.binding_for_control(planned.control_id)
        control_status[planned.control_id] = (
            state.binding_status.get(lease.binding_id, BindingStatus.UNAVAILABLE)
            if lease is not None
            else BindingStatus.UNAVAILABLE
        )
    active_contexts = {
        lease.control_id: _control_context_snapshot(lease)
        for lease in state.attachments.binding_leases.values()
        if lease.ready and state.attachments.binding_command_authorized(lease)
    }
    return BindingActionSnapshot(
        binding_leases=MappingProxyType(
            {
                binding_id: _binding_lease_snapshot(
                    lease,
                    planned_intent=planned_intent_for_lease(lease),
                )
                for binding_id, lease in state.attachments.binding_leases.items()
            }
        ),
        active_contexts=MappingProxyType(active_contexts),
        action_instances=lifecycle.snapshot_action_instances(),
        provider_session_keys=lifecycle.provider_session_keys(),
        output_owners=MappingProxyType(
            dict(state.attachments.active_output_by_control)
        ),
        held_inputs=tuple(
            HeldInputSnapshot(
                binding_id=held.binding_id,
                control_id=held.control_id,
                capability_id=held.capability_id,
                context_id=held.context_id,
            )
            for held in state.attachments.held_input_bindings.values()
        ),
        control_status=MappingProxyType(control_status),
    )
