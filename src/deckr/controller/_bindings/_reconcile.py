from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from deckr.controller._actions._models import BuiltinActionCatalog
    from deckr.controller._actions._requests import ActionRequestPort
    from deckr.controller._binding_plan import PagePlan, PlannedBinding
    from deckr.controller._bindings._action_lifecycle import (
        ActionInstanceLifecycleService,
    )
    from deckr.controller._bindings._attachments import BindingLease
    from deckr.controller._bindings._context import RuntimeMessageSender
    from deckr.controller._bindings._input import InputInteractions
    from deckr.controller._bindings._installation import BindingInstaller
    from deckr.controller._bindings._planning import PagePlanning
    from deckr.controller._bindings._ports import BindingActionService
    from deckr.controller._bindings._presentation import BindingPresentation
    from deckr.controller._bindings._rules import BindingRules
    from deckr.controller._hardware import HardwareCommandService
    from deckr.controller._interest_diagnostics import InterestDiagnostics
    from deckr.controller._session_effects import SessionEffects
    from deckr.controller._session_state import DeviceState
    from deckr.controller._state_gate import StateGate
    from deckr.controller.settings import SettingsService

import logging
from collections.abc import Iterable

from deckr.controller._actions import (
    ActionIntentKey,
    ProviderActionKey,
)
from deckr.controller._binding_plan import BindingPlanStatus, PagePlan, PlannedBinding
from deckr.controller._bindings._attachments import (
    BindingLease,
)
from deckr.controller._bindings._availability_reconcile import AvailabilityReconciler
from deckr.controller._bindings._installation import BindingInstaller
from deckr.controller._bindings._models import (
    PageCommit,
    _find_control_layout,
    _lease_matches_action,
    _selected_raster_capability_id,
)
from deckr.controller._bindings._rules import BindingRules
from deckr.controller._pages import (
    PageRef,
    StaticPageRef,
)
from deckr.controller._presentation import DeviceOutput
from deckr.controller._session_effects import Effect

logger = logging.getLogger(__name__)


class BindingReconciler:
    """Apply page commits, revoke installed resources and coordinate reconciliation."""

    def __init__(
        self,
        *,
        state: DeviceState,
        boundary: StateGate,
        effects: SessionEffects,
        lifecycle: ActionInstanceLifecycleService,
        catalog: BindingActionService,
        registry: BuiltinActionCatalog,
        settings: SettingsService | None,
        runtime_sender: RuntimeMessageSender,
        scoped_requests: Callable[..., ActionRequestPort],
        start_soon: Callable[..., None],
        controller_id: str,
        hardware: HardwareCommandService,
        presentation: BindingPresentation,
        planning: PagePlanning,
        inputs: InputInteractions,
        interests: InterestDiagnostics,
        close_page: Callable[..., Awaitable[None]],
        retire_rendering: Callable[[str], Awaitable[None]],
    ):
        self.state = state
        self.boundary = boundary
        self.effects = effects
        self.lifecycle = lifecycle
        self.catalog = catalog
        self.hardware = hardware
        self.presentation = presentation
        self.inputs = inputs
        self.retire_rendering = retire_rendering
        self.rules = BindingRules(
            state=state,
            catalog=catalog,
            controller_id=controller_id,
            clock=planning.clock,
        )
        self.installer = BindingInstaller(
            state=state,
            boundary=boundary,
            effects=effects,
            lifecycle=lifecycle,
            catalog=catalog,
            registry=registry,
            settings=settings,
            runtime_sender=runtime_sender,
            scoped_requests=scoped_requests,
            start_soon=start_soon,
            controller_id=controller_id,
            hardware=hardware,
            presentation=presentation,
            rules=self.rules,
            revoke=self._revoke_binding,
        )
        self.availability = AvailabilityReconciler(
            state=state,
            boundary=boundary,
            catalog=catalog,
            planning=planning,
            interests=interests,
            presentation=presentation,
            lifecycle=lifecycle,
            rules=self.rules,
            installer=self.installer,
            close_page=close_page,
            revoke=self._revoke_binding,
            install=self._install_planned_binding,
        )
        self.planning = planning

    def _describe_page_entry(self, entry: PageRef | None) -> str:
        if entry is None:
            return "none"
        if isinstance(entry, StaticPageRef):
            return f"static:{entry.profile_name}:{entry.page_index}"
        return f"dynamic:{entry.page_id}"

    async def revoke_binding(
        self,
        binding_id: str,
        *,
        clear_output: bool = True,
        notify_provider: bool = True,
        reason: str = "detach",
        clear_held_input: bool = False,
    ) -> BindingLease | None:
        return await self._revoke_binding(
            binding_id,
            clear_output=clear_output,
            notify_provider=notify_provider,
            reason=reason,
            clear_held_input=clear_held_input,
        )

    async def _revoke_binding(
        self,
        binding_id: str,
        *,
        clear_output: bool = True,
        notify_provider: bool = True,
        reason: str = "detach",
        clear_held_input: bool = False,
    ) -> BindingLease | None:
        lease = self.state.attachments.binding_leases.get(binding_id)
        if lease is None:
            return None
        self.installer.retire(binding_id)
        self.state.retire_binding(binding_id)
        lease.context.presentation.retire()
        await self.retire_rendering(binding_id)
        self.state.attachments.disable_binding_authority(lease)
        logger.info(
            "Revoking binding config=%s control=%s action=%s provider=%s "
            "binding=%s reason=%s clearOutput=%s notifyProvider=%s",
            self.state.config.id,
            lease.control_id,
            lease.action_id,
            lease.provider_instance_id,
            lease.binding_id,
            reason,
            clear_output,
            notify_provider,
        )
        if clear_held_input:
            await self.inputs._cancel_held_inputs_for_binding(lease)
        self.state.attachments.remove_binding(binding_id)
        if lease.attached and notify_provider:
            self.effects.submit(
                lease.action_instance_id,
                Effect(
                    "detach binding",
                    lambda: lease.context.on_binding_detached(reason),
                    cleanup=True,
                ),
            )
        output = (
            DeviceOutput(
                self.hardware,
                self.state.config.id,
                lease.control_id,
                lease.raster_capability_id,
            )
            if lease.raster_capability_id is not None
            else None
        )
        await self.presentation.dispatcher.clear_control(
            lease.control_id,
            context_id=lease.context_id,
            # This is a controller clear after revocation, not output authorized
            # by the retired binding. A successor route supersedes its revision.
            binding_id=None,
            output=output,
            clear_output=clear_output,
        )
        return lease

    async def _revoke_active_bindings(
        self,
        *,
        clear_outputs: bool = True,
        reason: str = "active_bindings",
    ) -> None:
        await self._revoke_active_bindings_except(
            clear_outputs=clear_outputs,
            preserve_output_control_ids=frozenset(),
            reason=reason,
        )

    async def _revoke_active_bindings_except(
        self,
        *,
        clear_outputs: bool = True,
        preserve_binding_ids: frozenset[str] = frozenset(),
        preserve_output_control_ids: frozenset[str],
        reason: str = "active_bindings",
        clear_held_input: bool = True,
    ) -> None:
        for binding_id in list(self.state.attachments.binding_leases):
            lease = self.state.attachments.binding_leases.get(binding_id)
            if lease is not None and binding_id in preserve_binding_ids:
                continue
            clear_output = clear_outputs
            if lease is not None and lease.control_id in preserve_output_control_ids:
                clear_output = False
            await self._revoke_binding(
                binding_id,
                clear_output=clear_output,
                reason=reason,
                clear_held_input=clear_held_input,
            )

    def _external_dynamic_page_child_action_instance_ids(
        self,
        plans: Iterable[PagePlan],
    ) -> frozenset[str]:
        action_instance_ids: set[str] = set()
        for plan in plans:
            if plan.page_session is None:
                continue
            for planned in plan.bindings:
                child = planned.child
                if child is None or child.target.kind == "self":
                    continue
                action_instance_ids.add(planned.action_instance_id)
        return frozenset(action_instance_ids)

    def _active_external_dynamic_page_child_action_instance_ids(
        self,
    ) -> frozenset[str]:
        current_plan = self.state.navigation.current_plan()
        if current_plan is None:
            return frozenset()
        return self._external_dynamic_page_child_action_instance_ids((current_plan,))

    def _active_dynamic_page_owner_action_instance_ids(self) -> frozenset[str]:
        return frozenset(
            frame.page_session.action_instance_id
            for frame in self.state.navigation.view().frames
            if frame.page_session is not None
        )

    async def _destroy_inactive_external_dynamic_page_children(
        self,
        plans: Iterable[PagePlan],
        *,
        reason: str,
    ) -> None:
        candidates = self._external_dynamic_page_child_action_instance_ids(plans)
        if not candidates:
            return
        retained = (
            self._active_external_dynamic_page_child_action_instance_ids()
            | self._active_dynamic_page_owner_action_instance_ids()
        )
        for action_instance_id in sorted(candidates - retained):
            await self.lifecycle.destroy_action_instance(
                action_instance_id,
                reason=reason,
            )

    def planned_intent_for_lease(self, lease: BindingLease) -> ActionIntentKey:
        current_plan = self.state.navigation.current_plan()
        if current_plan is not None:
            for planned in current_plan.bindings:
                if planned.control_id == lease.control_id:
                    return self.planning.planner.resolved_action_intent_key(
                        planned.binding
                    )
        return ActionIntentKey(
            action_id=lease.action_id,
            provider_instance_id=lease.provider_instance_id,
            provider_labels=(),
        )

    async def recover_binding_provider_session_contract(
        self,
        lease: BindingLease,
        *,
        reason: str,
    ) -> None:
        if (
            self.rules._lease_uses_current_provider_session_contract(lease)
            or self.rules._current_provider_session_contract_for_lease(lease) is None
        ):
            return
        logger.info(
            "Recovering binding with stale provider-session contract config=%s "
            "control=%s action=%s provider=%s binding=%s reason=%s",
            self.state.config.id,
            lease.control_id,
            lease.action_id,
            lease.provider_instance_id,
            lease.binding_id,
            reason,
        )
        await self.availability.on_action_availability_changed(
            (ProviderActionKey(lease.provider_instance_id, lease.action_id),)
        )

    def _binding_lease_for_control(self, control_id: str) -> BindingLease | None:
        return self.state.attachments.binding_for_control(control_id)

    def _prepare_page_commit(
        self,
        plan: PagePlan,
        *,
        departing: PageRef | None,
        preserve_rebound_outputs: bool,
    ) -> PageCommit:
        arriving = plan.entry
        preserve_output_control_ids = (
            self._preserved_output_control_ids(plan)
            if preserve_rebound_outputs
            else frozenset()
        )
        return PageCommit(
            plan=plan,
            departing=departing,
            preserve_binding_ids=self._preserved_binding_ids(plan),
            preserve_output_control_ids=preserve_output_control_ids,
            transition_reason=(
                "page_transition:"
                f"{self._describe_page_entry(departing)}->"
                f"{self._describe_page_entry(arriving)}"
            ),
        )

    def _preserved_binding_ids(self, plan: PagePlan) -> frozenset[str]:
        if not isinstance(plan.entry, StaticPageRef):
            return frozenset()
        preserved: set[str] = set()
        for planned in plan.bindings:
            lease = self.rules.matching_existing(plan, planned)
            if lease is not None:
                preserved.add(lease.binding_id)
        return frozenset(preserved)

    def _preserved_output_control_ids(self, plan: PagePlan) -> frozenset[str]:
        preserved: set[str] = set()
        for planned in plan.bindings:
            lease = self._binding_lease_for_control(planned.control_id)
            if lease is None or planned.action_meta is None:
                continue
            action_meta = self.rules._action_metadata_with_current_session(
                planned.action_meta
            )
            if (
                _lease_matches_action(lease, action_meta)
                and lease.action_instance_id == planned.action_instance_id
                and lease.page_session_id == planned.page_session_id
                and lease.item_key == planned.item_key
                and lease.handler == planned.handler
            ):
                preserved.add(planned.control_id)
        return frozenset(preserved)

    async def _apply_page_commit(self, commit: PageCommit) -> None:
        plan = commit.plan
        departing = commit.departing
        arriving = plan.entry
        logger.info(
            "Executing page transition config=%s departing=%s arriving=%s "
            "pageSession=%s preserveOutputs=%s",
            self.state.config.id,
            self._describe_page_entry(departing),
            self._describe_page_entry(arriving),
            plan.page_session.page_session_id
            if plan.page_session is not None
            else None,
            sorted(commit.preserve_output_control_ids),
        )

        if departing is not None:
            await self._revoke_active_bindings_except(
                preserve_binding_ids=commit.preserve_binding_ids,
                preserve_output_control_ids=commit.preserve_output_control_ids,
                reason=commit.transition_reason,
            )

        await self.presentation._clear_all_raster_controls(
            preserve_control_ids=commit.preserve_output_control_ids,
        )

        resolved_count = 0
        unavailable_count = 0
        for planned in plan.bindings:
            if await self._install_planned_binding(plan, planned):
                resolved_count += 1
            else:
                unavailable_count += 1
        logger.info(
            "Page bindings installed config=%s page=%s resolved=%s unavailable=%s",
            self.state.config.id,
            plan.page_id,
            resolved_count,
            unavailable_count,
        )

    async def _install_planned_binding(
        self,
        plan: PagePlan,
        planned: PlannedBinding,
    ) -> bool:
        if planned.status in {
            BindingPlanStatus.PENDING,
            BindingPlanStatus.UNAVAILABLE,
            BindingPlanStatus.INVALID_DEVICE_CONTROL,
        }:
            control = _find_control_layout(
                self.state.device,
                planned.control_id,
                raster_capability_id=_selected_raster_capability_id(planned.binding),
            )
            if control is not None:
                if planned.status == BindingPlanStatus.PENDING:
                    await self.presentation._render_pending_to_control(control)
                else:
                    await self.presentation._render_unavailable_to_control(
                        control,
                        planned=planned,
                        page_id=plan.page_id,
                    )
            return False
        existing = self.rules.matching_existing(plan, planned)
        if existing is not None:
            activated = await self.installer._activate_binding(existing)
            if activated:
                await self.presentation._refresh_binding_output(
                    existing, reason="binding_reused"
                )
            return activated
        ok = await self.installer._try_resolve_binding(
            planned.binding,
            profile_id=plan.profile_id,
            page_id=plan.page_id,
            action_instance_id=planned.action_instance_id,
            page_session_id=planned.page_session_id,
            settings_target_enabled=planned.settings_target_enabled,
            item_key=planned.item_key,
            handler=planned.handler,
            internal=planned.internal,
            action_meta=planned.action_meta,
        )
        if ok:
            return True
        control = _find_control_layout(
            self.state.device,
            planned.control_id,
            raster_capability_id=_selected_raster_capability_id(planned.binding),
        )
        if control is not None:
            await self.presentation._render_pending_to_control(control)
        return False
