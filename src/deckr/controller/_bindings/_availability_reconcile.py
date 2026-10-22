from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from deckr.controller._binding_plan import PagePlan, PlannedBinding
    from deckr.controller._bindings._action_lifecycle import (
        ActionInstanceLifecycleService,
    )
    from deckr.controller._bindings._attachments import BindingLease
    from deckr.controller._bindings._installation import BindingInstaller
    from deckr.controller._bindings._planning import PagePlanning
    from deckr.controller._bindings._ports import BindingActionService
    from deckr.controller._bindings._presentation import BindingPresentation
    from deckr.controller._bindings._rules import BindingRules
    from deckr.controller._interest_diagnostics import InterestDiagnostics
    from deckr.controller._session_state import DeviceState
    from deckr.controller._state_gate import StateGate

import logging
from collections.abc import Iterable

from deckr.controller._actions import (
    ActionMetadata,
    ProviderActionKey,
    ProviderSessionKey,
)
from deckr.controller._binding_plan import BindingPlanStatus, PagePlan, PlannedBinding
from deckr.controller._bindings._attachments import (
    BindingLease,
)
from deckr.controller._bindings._decisions import (
    BindingChange,
    InstalledBindingFacts,
    binding_decision,
)
from deckr.controller._bindings._models import (
    _find_control_layout,
    _format_provider_action_keys,
    _lease_matches_action,
    _lease_matches_action_ignoring_session,
    _selected_raster_capability_id,
)
from deckr.controller.action_provider.builtin import BUILTIN_ACTION_PROVIDER_ID

logger = logging.getLogger(__name__)


class AvailabilityReconciler:
    """Apply availability changes to the current desired and installed bindings."""

    def __init__(
        self,
        *,
        state: DeviceState,
        boundary: StateGate,
        catalog: BindingActionService,
        planning: PagePlanning,
        interests: InterestDiagnostics,
        presentation: BindingPresentation,
        lifecycle: ActionInstanceLifecycleService,
        rules: BindingRules,
        installer: BindingInstaller,
        close_page: Callable[..., Awaitable[None]],
        revoke: Callable[..., Awaitable[BindingLease | None]],
        install: Callable[[PagePlan, PlannedBinding], Awaitable[bool]],
    ):
        self.state, self.boundary, self.catalog = state, boundary, catalog
        self.planning, self.interests, self.presentation = (
            planning,
            interests,
            presentation,
        )
        self.lifecycle, self.rules, self.installer = lifecycle, rules, installer
        self.close_page, self.revoke, self.install = close_page, revoke, install

    def _provider_lifecycle_recovery_key(
        self,
        lease: BindingLease,
        action_meta: ActionMetadata,
    ) -> ProviderActionKey | None:
        if action_meta.provider_instance_id == BUILTIN_ACTION_PROVIDER_ID:
            return None
        if not _lease_matches_action(lease, action_meta):
            return None
        key = ProviderActionKey(
            action_meta.provider_instance_id,
            action_meta.action_id,
        )
        if not self.catalog.provider_lifecycle_recovery_required(key):
            return None
        return key

    async def on_action_availability_changed(
        self,
        changed_keys: Iterable[ProviderActionKey] = (),
    ) -> None:
        """Refresh the current page availability overlay after provider availability changes."""
        changed_key_set = frozenset(changed_keys)
        async with self.boundary:
            if self.state.stopped:
                return
            session = self.state.navigation.active_dynamic_session()
            if session is not None and session.owner_contract is not None:
                key = (
                    ProviderSessionKey(
                        session.owner_provider_instance_id,
                        session.owner_provider_id,
                        session.owner_provider_session_id,
                    )
                    if session.owner_provider_session_id is not None
                    else None
                )
                if self.catalog.current_contract(key) != session.owner_contract:
                    await self.close_page(
                        context_id=session.context_id,
                        reason="owner_agreement_lost",
                    )
                    return
            draft = self.state.navigation.begin_refresh_current(
                preserve_rebound_outputs=False,
            )
            if draft is None or draft.retained_plan is None:
                logger.debug(
                    "Action availability page refresh skipped config=%s "
                    "changed_keys=%s reason=no_current_page",
                    self.state.config.id,
                    len(changed_key_set),
                )
                return
            if (
                changed_key_set
                and not self.planning._action_availability_change_affects_plan(
                    changed_key_set,
                    draft.retained_plan,
                )
            ):
                logger.debug(
                    "Action availability page refresh decision config=%s page=%s "
                    "changed_keys=%s affected=False keys=%s",
                    self.state.config.id,
                    draft.retained_plan.page_id,
                    len(changed_key_set),
                    _format_provider_action_keys(changed_key_set),
                )
                return
            logger.debug(
                "Action availability page refresh decision config=%s page=%s "
                "changed_keys=%s affected=True keys=%s",
                self.state.config.id,
                draft.retained_plan.page_id,
                len(changed_key_set),
                _format_provider_action_keys(changed_key_set),
            )

            refreshed_plan = await self.planning._build_page_plan(
                draft.entry,
                page_session=draft.page_session,
                page_session_generation=draft.page_session_generation,
                retained_plan=draft.retained_plan,
            )
            if refreshed_plan is None:
                logger.warning(
                    "Skipping action-change rebind for invalid page bindings"
                )
                return

            logger.info(
                "Re-evaluating page bindings for config=%s page=%s after action availability change",
                self.state.config.id,
                refreshed_plan.page_id,
            )

            self.state.navigation.commit(draft, refreshed_plan)
            self.interests._sync_action_interest()

            for planned in refreshed_plan.bindings:
                await self._reconcile_binding(refreshed_plan, planned)

    async def _reconcile_binding(self, plan: PagePlan, planned: PlannedBinding) -> None:
        lease = self.state.attachments.binding_for_control(planned.control_id)
        action = planned.action_meta
        recovery_key = (
            self._provider_lifecycle_recovery_key(lease, action)
            if lease and action
            else None
        )
        facts = None
        if lease is not None:
            same_action = action is not None and _lease_matches_action_ignoring_session(
                lease, action
            )
            same_session = same_action and _lease_matches_action(lease, action)
            same_requirements = (
                same_session
                and planned.status == BindingPlanStatus.BOUND
                and self.rules.requirements_match(lease, planned.binding, action)
            )
            facts = InstalledBindingFacts(
                same_action,
                same_session,
                same_requirements,
                self.rules._lease_uses_current_provider_session_contract(lease),
                self.rules._current_provider_session_contract_for_lease(lease)
                is not None,
                recovery_key is not None,
            )
        decision = binding_decision(planned.status, facts)
        if decision.retire:
            await self.revoke(
                lease.binding_id,
                clear_output=decision.change == BindingChange.INVALID,
                notify_provider=decision.notify_provider,
                reason=decision.reason,
                clear_held_input=True,
            )
        if decision.destroy_instance:
            await self.lifecycle.destroy_action_instance(
                lease.action_instance_id,
                reason=decision.reason,
                notify_provider=decision.notify_provider,
            )
        if decision.change in {BindingChange.INSTALL, BindingChange.REPLACE}:
            await self.install(plan, planned)
            if recovery_key is not None:
                self.catalog.consume_provider_lifecycle_recovery(recovery_key)
        elif decision.change == BindingChange.REFRESH:
            if await self.installer._activate_binding(lease):
                await self.presentation._refresh_binding_output(
                    lease, reason="action_availability_changed"
                )
        elif decision.change in {BindingChange.PENDING, BindingChange.UNAVAILABLE}:
            control = _find_control_layout(
                self.state.device,
                planned.control_id,
                raster_capability_id=_selected_raster_capability_id(planned.binding),
            )
            if control is not None:
                if decision.change == BindingChange.PENDING:
                    await self.presentation._render_pending_to_control(control)
                else:
                    await self.presentation._render_unavailable_to_control(
                        control, planned=planned, page_id=plan.page_id
                    )
