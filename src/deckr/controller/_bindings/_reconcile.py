import logging
from collections.abc import Iterable, Mapping
from typing import Any

import anyio
from deckr.actions.messages import (
    BindingMetadata,
    MatchedCapability,
    SettingsTargetRef,
    make_binding_id,
    make_context_id,
)
from deckr.contracts.authority import ContractPointer
from deckr.contracts.models import thaw_json
from deckr.hardware.descriptors import (
    ControlRef,
)

from deckr.controller._actions import (
    ActionAvailabilitySource,
    ActionAvailabilityState,
    ActionIntentKey,
    ActionMetadata,
    ProviderActionKey,
    ProviderSessionKey,
    provider_session_key,
)
from deckr.controller._binding_planner import (
    BindingPlanStatus,
    PagePlan,
    PlannedBinding,
)
from deckr.controller._binding_resolution import ResolvedControlBinding
from deckr.controller._bindings._attachments import (
    BindingLease,
)
from deckr.controller._bindings._context import (
    ControlContext,
)
from deckr.controller._bindings._models import (
    PageCommit,
    _contract_pointer_matches,
    _find_control_surface,
    _format_provider_action_keys,
    _lease_matches_action,
    _lease_matches_action_ignoring_session,
    _selected_raster_capability_id,
)
from deckr.controller._command_router import DeviceOutput
from deckr.controller._pages import (
    PageStackEntry,
    StaticPageRef,
)
from deckr.controller._session_effects import Effect
from deckr.controller._session_state import BindingStatus
from deckr.controller.action_provider.builtin import BUILTIN_ACTION_PROVIDER_ID

logger = logging.getLogger(__name__)


ACTION_INSTANCE_CREATE_TIMEOUT_SECONDS = 1.0
BINDING_ATTACH_NOTIFY_TIMEOUT_SECONDS = 1.0
SETTINGS_SERVICE_TIMEOUT_SECONDS = 1.0
_ACTION_METADATA_UNSET = object()


class BindingReconciler:
    """Reconcile desired controls against installed and pending resources.

    This component applies binding rules to session-owned state. Adapter work
    runs through effects and completion enters the same serialized boundary.
    """

    def __init__(
        self,
        *,
        state,
        boundary,
        effects,
        lifecycle,
        catalog,
        registry,
        settings,
        runtime_sender,
        scoped_requests,
        start_soon,
        controller_id,
        hardware,
        presentation,
        planning,
        inputs,
        interests,
        close_page,
    ):
        self.state = state
        self.boundary = boundary
        self.effects = effects
        self.lifecycle = lifecycle
        self.catalog = catalog
        self.registry = registry
        self.settings = settings
        self.runtime_sender = runtime_sender
        self.scoped_requests = scoped_requests
        self.start_soon = start_soon
        self.controller_id = controller_id
        self.hardware = hardware
        self.presentation = presentation
        self.planning = planning
        self.inputs = inputs
        self.interests = interests
        self.close_page = close_page
        self._deferred_bindings: dict[str, bool] = {}

    def _describe_page_entry(self, entry: PageStackEntry | None) -> str:
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
        self._deferred_bindings.pop(binding_id, None)
        self.state.retire_binding(binding_id)
        self.state.attachments.disable_binding_authority(lease)
        logger.info(
            "Revoking binding config=%s control=%s action=%s provider=%s "
            "binding=%s reason=%s clearOutput=%s notifyProvider=%s",
            self.state.config.id,
            lease.control_id,
            lease.action_uuid,
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
            actionId=binding.action_uuid,
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
            action_uuid=action_meta.uuid,
            provider_instance_id=action_meta.provider_instance_id,
            provider_labels=tuple(sorted((action_meta.provider_labels or {}).items())),
        )
        record = self.catalog.record_for_intent(
            intent,
            now=self.planning.clock(),
        )
        if (
            record is None
            or record.source != ActionAvailabilitySource.SERVICE_VIEW
            or record.metadata is None
            or self.catalog.state_for_key(record.key, now=self.planning.clock())
            != ActionAvailabilityState.AVAILABLE
            or record.metadata.provider_session_id is None
        ):
            return action_meta
        current = record.metadata
        if (
            current.uuid != action_meta.uuid
            or current.provider_instance_id != action_meta.provider_instance_id
            or current.provider_id != action_meta.provider_id
        ):
            return action_meta
        return current

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
            for frame in self.state.navigation.snapshot().frames
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
            action_meta.uuid,
        )
        if not self.catalog.provider_lifecycle_recovery_required(key):
            return None
        return key

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

    def planned_intent_for_lease(self, lease: BindingLease) -> ActionIntentKey:
        current_plan = self.state.navigation.current_plan()
        if current_plan is not None:
            for planned in current_plan.bindings:
                if planned.control_id == lease.control_id:
                    return self.planning.planner.resolved_action_intent_key(
                        planned.binding
                    )
        return ActionIntentKey(
            action_uuid=lease.action_uuid,
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
            self._lease_uses_current_provider_session_contract(lease)
            or self._current_provider_session_contract_for_lease(lease) is None
        ):
            return
        logger.info(
            "Recovering binding with stale provider-session contract config=%s "
            "control=%s action=%s provider=%s binding=%s reason=%s",
            self.state.config.id,
            lease.control_id,
            lease.action_uuid,
            lease.provider_instance_id,
            lease.binding_id,
            reason,
        )
        await self.on_action_availability_changed(
            (ProviderActionKey(lease.provider_instance_id, lease.action_uuid),)
        )

    def _binding_lease_for_control(self, control_id: str) -> BindingLease | None:
        return self.state.attachments.binding_for_control(control_id)

    async def _enable_binding_authority(self, lease: BindingLease) -> None:
        self.state.attachments.enable_binding_authority(lease)
        await self.presentation._claim_binding_output_route(lease)

    def _preparation_current(self, operation, lease) -> bool:
        return (
            self.state.operation_current(operation)
            and self.state.attachments.binding_leases.get(lease.binding_id) is lease
            and self._lease_uses_current_provider_session_contract(lease)
            and self.state.hardware_current(self.hardware)
        )

    async def recover_attachment(self, lease: BindingLease) -> None:
        """Recovery uses the same operation ledger and readiness as initial attach."""
        async with self.boundary:
            if (
                self.state.attachments.binding_leases.get(lease.binding_id) is lease
                and self.state.hardware_current(self.hardware)
                and self._lease_uses_current_provider_session_contract(lease)
            ):
                await self._activate_binding(lease, restart_instance=True)

    async def _activate_binding(
        self, lease: BindingLease, *, restart_instance: bool = False
    ) -> bool:
        restart_instance = restart_instance or self._deferred_bindings.get(
            lease.binding_id, False
        )
        if lease.attached and lease.ready and not restart_instance:
            await self._enable_binding_authority(lease)
            return True
        if any(
            op.binding_id == lease.binding_id for op in self.state.operations.values()
        ):
            return False
        action_meta = ActionMetadata(
            uuid=lease.action_uuid,
            provider_instance_id=lease.provider_instance_id,
            provider_id=lease.provider_id,
            provider_session_id=lease.provider_session_id,
        )
        metadata = self.lifecycle.reserve_action_instance(
            action_meta=action_meta,
            action_instance_id=lease.action_instance_id,
            context_id=lease.context_id,
        )
        if restart_instance:
            self.lifecycle.invalidate_instance_started(metadata)
        operation = self.state.begin_operation(
            lease.binding_id,
            "recover_binding" if restart_instance else "prepare_binding",
        )
        self.state.binding_status[lease.binding_id] = BindingStatus.PREPARING
        lease.ready = False
        admitted = self.effects.submit(
            lease.action_instance_id,
            Effect(
                "prepare binding",
                lambda: self._prepare_binding(
                    operation, lease, metadata, restart_instance=restart_instance
                ),
                timeout=SETTINGS_SERVICE_TIMEOUT_SECONDS
                + ACTION_INSTANCE_CREATE_TIMEOUT_SECONDS
                + BINDING_ATTACH_NOTIFY_TIMEOUT_SECONDS
                + 1,
            ),
        )
        if not admitted:
            self.state.operations.pop(operation.id, None)
            self.state.binding_status[lease.binding_id] = BindingStatus.UNAVAILABLE
            self._deferred_bindings[lease.binding_id] = restart_instance
            self.effects.when_capacity_available(self._resume_deferred_preparations)
        else:
            self._deferred_bindings.pop(lease.binding_id, None)
        return False

    async def _resume_deferred_preparations(self) -> None:
        async with self.boundary:
            pending, self._deferred_bindings = self._deferred_bindings, {}
            if not self.state.hardware_current(self.hardware):
                return
            for binding_id, restart_instance in pending.items():
                lease = self.state.attachments.binding_leases.get(binding_id)
                if (
                    lease is not None
                    and self._lease_uses_current_provider_session_contract(lease)
                ):
                    await self._activate_binding(
                        lease, restart_instance=restart_instance
                    )

    async def _prepare_binding(
        self, operation, lease, metadata, *, restart_instance=False
    ) -> None:
        """Run adapters without holding the state boundary; return each result to it."""
        try:
            async with self.boundary:
                if not self._preparation_current(operation, lease):
                    return
            settings = dict(lease.context.settings)
            if self.settings is not None and lease.settings_target is not None:
                with anyio.move_on_after(SETTINGS_SERVICE_TIMEOUT_SECONDS):
                    try:
                        snapshot = await self.settings.get(lease.settings_target)
                        settings = dict(thaw_json(snapshot.settings))
                    except KeyError:
                        pass
            async with self.boundary:
                if not self._preparation_current(operation, lease):
                    return
                started = self.lifecycle.instance_started(metadata)
            if restart_instance or not started:
                with anyio.fail_after(ACTION_INSTANCE_CREATE_TIMEOUT_SECONDS):
                    started = await self.lifecycle.start_reserved_instance(
                        metadata, lease.provider_session_key
                    )
                if not started:
                    raise RuntimeError("Action provider no longer available")
            async with self.boundary:
                self.lifecycle.accept_instance_started(
                    metadata, lease.provider_session_key
                )
                if not self._preparation_current(operation, lease):
                    return
                lease.context.replace_settings(settings)
                # The attachment grant admits responses to attach; physical input
                # remains disabled until the adapter completes attachment.
                lease.attached = True
                await self._enable_binding_authority(lease)
            with anyio.fail_after(BINDING_ATTACH_NOTIFY_TIMEOUT_SECONDS):
                await lease.context.on_binding_attached()
            async with self.boundary:
                if not self._preparation_current(operation, lease):
                    return
                lease.ready = True
                self.state.binding_status[lease.binding_id] = BindingStatus.ACTIVE
                self.state.operations.pop(operation.id, None)
                await self.presentation._refresh_binding_output(
                    lease, reason="binding_ready"
                )
        except Exception:
            logger.warning(
                "Binding preparation failed: %s", lease.binding_id, exc_info=True
            )
            async with self.boundary:
                if self.state.operation_current(operation):
                    # An uncertain attachment must never reuse its old grant on
                    # a later reconciliation. Retire it now; desired state stays.
                    await self._revoke_binding(
                        lease.binding_id,
                        clear_output=False,
                        reason="preparation_failed",
                        clear_held_input=True,
                    )
                    await self.presentation._render_status_to_control(
                        lease.control, overlay_type="unavailable_unknown"
                    )
        finally:
            async with self.boundary:
                if self.state.operation_current(operation) and not lease.ready:
                    await self._revoke_binding(
                        lease.binding_id,
                        clear_output=False,
                        reason="preparation_superseded",
                        clear_held_input=True,
                    )
                self.state.operations.pop(operation.id, None)

    async def _try_resolve_binding(
        self,
        binding: ResolvedControlBinding,
        *,
        profile_id: str,
        page_id: str,
        action_instance_id: str,
        page_session_id: str | None = None,
        settings_target_enabled: bool = True,
        item_key: str | None = None,
        handler: str | None = None,
        internal: Mapping[str, Any] | None = None,
        action_meta: ActionMetadata | None = _ACTION_METADATA_UNSET,
    ) -> bool:
        """Resolve a binding: create ControlContext and announce the binding lease.
        Returns True if context was created, False if action not found (caller should render unavailable).
        """
        raster_capability_id = _selected_raster_capability_id(binding)
        control = _find_control_surface(
            self.state.device,
            binding.control_id,
            raster_capability_id=raster_capability_id,
        )
        if control is None:
            logger.warning(
                "Resolved binding control disappeared before bind: config=%s control=%s",
                self.state.config.id,
                binding.control_id,
            )
            return False
        if action_meta is _ACTION_METADATA_UNSET:
            action_meta = None
        if action_meta is None:
            logger.info(
                "Binding unresolved on profile=%s page=%s control=%s action=%s",
                profile_id,
                page_id,
                binding.control_id,
                binding.action_uuid,
            )
            return False
        action_meta = self._action_metadata_with_current_session(action_meta)
        try:
            matched_capabilities = self._matched_capabilities(binding, action_meta)
        except ValueError as exc:
            logger.warning("Binding requirements are not satisfied: %s", exc)
            return False
        provider_session_id = action_meta.provider_session_id
        session_key = (
            None
            if action_meta.provider_instance_id == BUILTIN_ACTION_PROVIDER_ID
            else provider_session_key(action_meta)
        )
        contract = self.catalog.current_contract(session_key)
        if (
            action_meta.provider_instance_id != BUILTIN_ACTION_PROVIDER_ID
            and contract is None
        ):
            logger.warning(
                "Binding unresolved without live provider-session contract "
                "profile=%s page=%s control=%s action=%s provider=%s session=%s",
                profile_id,
                page_id,
                binding.control_id,
                binding.action_uuid,
                action_meta.provider_instance_id,
                provider_session_id,
            )
            return False
        settings_target = (
            self._build_settings_target_for_binding(
                action_instance_id=action_instance_id,
                binding=binding,
                provider_instance_id=action_meta.provider_instance_id,
                provider_id=action_meta.provider_id,
            )
            if settings_target_enabled
            else None
        )
        initial_settings = dict(binding.settings)
        builtin_action = None
        if action_meta.provider_instance_id == BUILTIN_ACTION_PROVIDER_ID and hasattr(
            self.registry, "get_builtin_action"
        ):
            builtin_action = self.registry.get_builtin_action(action_meta.uuid)
        binding_id = make_binding_id()
        existing_context_id = self.lifecycle.context_id_for_action_instance(
            action_meta=action_meta,
            action_instance_id=action_instance_id,
        )
        context_id = existing_context_id or make_context_id()
        binding_metadata = BindingMetadata(
            providerInstanceId=action_meta.provider_instance_id,
            providerId=action_meta.provider_id,
            actionId=action_meta.uuid,
            actionInstanceId=action_instance_id,
            configId=self.state.config.id,
            contextId=context_id,
            bindingId=binding_id,
            pageSessionId=page_session_id,
            deviceRef=self.state.identity.hardware,
            controlRef=ControlRef(
                deviceRef=self.state.identity.hardware,
                controlId=control.id,
            ),
            itemKey=item_key,
            handler=handler,
            matchedCapabilities=matched_capabilities,
        )
        ctx = ControlContext(
            controller_id=self.controller_id,
            device=self.state.device,
            config_id=self.state.config.id,
            command_service=self.hardware,
            provider_instance_id=action_meta.provider_instance_id,
            provider_id=action_meta.provider_id,
            action_uuid=action_meta.uuid,
            control=control,
            settings=initial_settings,
            internal=dict(internal or {}),
            runtime_sender=self.runtime_sender,
            start_soon=self.start_soon,
            render_dispatcher=self.presentation.dispatcher,
            context_settings_target=settings_target,
            provider_session_id=provider_session_id,
            contract=contract,
            profile_id=profile_id,
            page_id=page_id,
            builtin_action=builtin_action,
            metadata=binding_metadata,
            request_port=self.scoped_requests(
                binding_metadata,
                may_navigate=action_meta.provider_instance_id
                == BUILTIN_ACTION_PROVIDER_ID,
            ),
        )
        lease = BindingLease(
            binding_id=binding_id,
            context_id=context_id,
            action_instance_id=action_instance_id,
            action_uuid=action_meta.uuid,
            provider_instance_id=action_meta.provider_instance_id,
            provider_id=action_meta.provider_id,
            provider_session_id=provider_session_id,
            provider_session_key=session_key,
            attached=False,
            ready=False,
            control_id=control.id,
            control=control,
            input_capability_ids=binding.input_capability_ids,
            raster_capability_id=raster_capability_id,
            profile_id=profile_id,
            page_id=page_id,
            settings_target=settings_target,
            context=ctx,
            page_session_id=page_session_id,
            item_key=item_key,
            handler=handler,
            action_descriptor=action_meta.descriptor,
        )
        self.state.attachments.add_binding(lease)
        attached = await self._activate_binding(lease)
        if attached:
            logger.info(
                "Binding resolved on profile=%s page=%s control=%s action=%s provider=%s binding=%s",
                profile_id,
                page_id,
                binding.control_id,
                binding.action_uuid,
                action_meta.provider_instance_id,
                binding_id,
            )
            return True
        logger.info(
            "Binding pending provider availability on profile=%s page=%s control=%s action=%s provider=%s binding=%s",
            profile_id,
            page_id,
            binding.control_id,
            binding.action_uuid,
            action_meta.provider_instance_id,
            binding_id,
        )
        return False

    def _prepare_page_commit(
        self,
        plan: PagePlan,
        *,
        departing: PageStackEntry | None,
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
            lease = self._matching_existing_binding_lease(plan, planned)
            if lease is not None:
                preserved.add(lease.binding_id)
        return frozenset(preserved)

    def _preserved_output_control_ids(self, plan: PagePlan) -> frozenset[str]:
        preserved: set[str] = set()
        for planned in plan.bindings:
            lease = self._binding_lease_for_control(planned.control_id)
            if lease is None or planned.action_meta is None:
                continue
            action_meta = self._action_metadata_with_current_session(
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
            control = _find_control_surface(
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
        existing = self._matching_existing_binding_lease(plan, planned)
        if existing is not None:
            activated = await self._activate_binding(existing)
            if activated:
                await self.presentation._refresh_binding_output(
                    existing, reason="binding_reused"
                )
            return activated
        ok = await self._try_resolve_binding(
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
        control = _find_control_surface(
            self.state.device,
            planned.control_id,
            raster_capability_id=_selected_raster_capability_id(planned.binding),
        )
        if control is not None:
            await self.presentation._render_pending_to_control(control)
        return False

    def _matching_existing_binding_lease(
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
            return lease
        return None

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
                refresh_actions=True,
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
                refresh_actions=draft.refresh_actions,
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
                lease = self._binding_lease_for_control(planned.control_id)
                if planned.status == BindingPlanStatus.PENDING:
                    if (
                        lease is not None
                        and not self._lease_uses_current_provider_session_contract(
                            lease
                        )
                    ):
                        await self._revoke_binding(
                            lease.binding_id,
                            clear_output=False,
                            reason="provider_authority_lost",
                            clear_held_input=True,
                        )
                        lease = None
                    if lease is not None:
                        logger.debug(
                            "Preserving existing binding during pending action "
                            "availability config=%s page=%s control=%s action=%s "
                            "provider=%s binding=%s",
                            self.state.config.id,
                            refreshed_plan.page_id,
                            planned.control_id,
                            (
                                planned.action_meta.uuid
                                if planned.action_meta is not None
                                else planned.binding.action_uuid
                            ),
                            lease.provider_instance_id,
                            lease.binding_id,
                        )
                        continue
                    control = _find_control_surface(
                        self.state.device,
                        planned.control_id,
                        raster_capability_id=_selected_raster_capability_id(
                            planned.binding
                        ),
                    )
                    if control is not None:
                        await self.presentation._render_pending_to_control(control)
                    continue

                if planned.status == BindingPlanStatus.UNAVAILABLE:
                    if lease is not None:
                        await self._revoke_binding(
                            lease.binding_id,
                            clear_output=False,
                            reason=f"action_{planned.status}",
                            clear_held_input=True,
                        )
                    control = _find_control_surface(
                        self.state.device,
                        planned.control_id,
                        raster_capability_id=_selected_raster_capability_id(
                            planned.binding
                        ),
                    )
                    if control is not None:
                        await self.presentation._render_unavailable_to_control(
                            control,
                            planned=planned,
                            page_id=refreshed_plan.page_id,
                        )
                    continue

                if lease is None:
                    await self._install_planned_binding(refreshed_plan, planned)
                    continue

                if _lease_matches_action(lease, planned.action_meta):
                    if not self._lease_uses_current_provider_session_contract(lease):
                        if (
                            self._current_provider_session_contract_for_lease(lease)
                            is None
                        ):
                            await self._revoke_binding(
                                lease.binding_id,
                                clear_output=False,
                                reason="provider_authority_lost",
                                clear_held_input=True,
                            )
                            await self.presentation._render_pending_to_control(
                                lease.control
                            )
                            continue
                        await self._replace_binding_provider_session(
                            refreshed_plan,
                            planned,
                            lease,
                            reason="provider_session_contract_changed",
                        )
                        continue
                    recovery_key = self._provider_lifecycle_recovery_key(
                        lease,
                        planned.action_meta,
                    )
                    if recovery_key is not None:
                        await self._replace_binding_provider_session(
                            refreshed_plan,
                            planned,
                            lease,
                            reason="provider_session_recovered",
                        )
                        self.catalog.consume_provider_lifecycle_recovery(recovery_key)
                        continue
                    if await self._activate_binding(lease):
                        await self.presentation._refresh_binding_output(
                            lease,
                            reason="action_availability_changed",
                        )
                    continue

                if _lease_matches_action_ignoring_session(
                    lease,
                    planned.action_meta,
                ):
                    await self._replace_binding_provider_session(
                        refreshed_plan,
                        planned,
                        lease,
                    )
                    continue

                await self._revoke_binding(
                    lease.binding_id,
                    clear_output=False,
                    notify_provider=False,
                    reason="action_availability_changed",
                    clear_held_input=True,
                )
                await self._install_planned_binding(refreshed_plan, planned)

    async def _replace_binding_provider_session(
        self,
        plan: PagePlan,
        planned: PlannedBinding,
        lease: BindingLease,
        *,
        reason: str = "provider_session_changed",
    ) -> None:
        action_instance_id = lease.action_instance_id
        await self._revoke_binding(
            lease.binding_id,
            clear_output=False,
            notify_provider=True,
            reason=reason,
            clear_held_input=True,
        )
        await self.lifecycle.destroy_action_instance(
            action_instance_id,
            reason=reason,
            notify_provider=True,
        )
        await self._install_planned_binding(plan, planned)
