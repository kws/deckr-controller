from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from deckr.controller._actions._models import BuiltinActionCatalog
    from deckr.controller._actions._requests import ActionRequestPort
    from deckr.controller._bindings._action_lifecycle import (
        ActionInstanceLifecycleService,
    )
    from deckr.controller._bindings._attachments import BindingLease
    from deckr.controller._bindings._context import RuntimeMessageSender
    from deckr.controller._bindings._ports import BindingActionService
    from deckr.controller._bindings._presentation import BindingPresentation
    from deckr.controller._bindings._rules import BindingRules
    from deckr.controller._hardware import HardwareCommandService
    from deckr.controller._session_effects import SessionEffects
    from deckr.controller._session_state import DeviceState
    from deckr.controller._state_gate import StateGate
    from deckr.controller.settings import SettingsService

import logging
from collections.abc import Mapping
from typing import Any

import anyio
from deckr.actions.messages import (
    BindingMetadata,
    make_binding_id,
    make_context_id,
)
from deckr.contracts.models import thaw_json
from deckr.hardware.descriptors import (
    ControlRef,
)

from deckr.controller._actions import (
    ActionMetadata,
    provider_session_key,
)
from deckr.controller._binding_resolution import ResolvedControlBinding
from deckr.controller._bindings._attachments import (
    BindingLease,
)
from deckr.controller._bindings._context import (
    ControlContext,
)
from deckr.controller._bindings._models import (
    _find_control_layout,
    _selected_raster_capability_id,
)
from deckr.controller._session_effects import Effect
from deckr.controller._session_state import BindingStatus
from deckr.controller.action_provider.builtin import BUILTIN_ACTION_PROVIDER_ID

logger = logging.getLogger(__name__)


ACTION_INSTANCE_CREATE_TIMEOUT_SECONDS = 1.0
BINDING_ATTACH_NOTIFY_TIMEOUT_SECONDS = 1.0
SETTINGS_SERVICE_TIMEOUT_SECONDS = 1.0


class BindingInstaller:
    """Create installed bindings and own their asynchronous preparation."""

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
        rules: BindingRules,
        revoke: Callable[..., Awaitable[BindingLease | None]],
    ):
        self.state, self.boundary, self.effects = state, boundary, effects
        self.lifecycle, self.catalog, self.registry = lifecycle, catalog, registry
        self.settings, self.runtime_sender = settings, runtime_sender
        self.scoped_requests, self.start_soon = scoped_requests, start_soon
        self.controller_id, self.hardware = controller_id, hardware
        self.presentation, self.rules, self.revoke = presentation, rules, revoke
        self._deferred_bindings: dict[str, bool] = {}

    def retire(self, binding_id: str) -> None:
        self._deferred_bindings.pop(binding_id, None)

    async def _enable_binding_authority(self, lease: BindingLease) -> None:
        self.state.attachments.enable_binding_authority(lease)
        await self.presentation._claim_binding_output_route(lease)

    def _preparation_current(self, operation, lease) -> bool:
        return (
            self.state.operation_current(operation)
            and self.state.attachments.binding_leases.get(lease.binding_id) is lease
            and self.rules._lease_uses_current_provider_session_contract(lease)
            and self.state.hardware_current(self.hardware)
        )

    async def recover_attachment(self, lease: BindingLease) -> None:
        """Recovery uses the same operation ledger and readiness as initial attach."""
        async with self.boundary:
            if (
                self.state.attachments.binding_leases.get(lease.binding_id) is lease
                and self.state.hardware_current(self.hardware)
                and self.rules._lease_uses_current_provider_session_contract(lease)
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
            action_id=lease.action_id,
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
                    and self.rules._lease_uses_current_provider_session_contract(lease)
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
                    await self.revoke(
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
                    await self.revoke(
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
        action_meta: ActionMetadata | None = None,
    ) -> bool:
        """Resolve a binding: create ControlContext and announce the binding lease.
        Returns True if context was created, False if action not found (caller should render unavailable).
        """
        raster_capability_id = _selected_raster_capability_id(binding)
        control = _find_control_layout(
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
        if action_meta is None:
            logger.info(
                "Binding unresolved on profile=%s page=%s control=%s action=%s",
                profile_id,
                page_id,
                binding.control_id,
                binding.action_id,
            )
            return False
        action_meta = self.rules._action_metadata_with_current_session(action_meta)
        try:
            matched_capabilities = self.rules._matched_capabilities(
                binding, action_meta
            )
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
                binding.action_id,
                action_meta.provider_instance_id,
                provider_session_id,
            )
            return False
        settings_target = (
            self.rules._build_settings_target_for_binding(
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
        if action_meta.provider_instance_id == BUILTIN_ACTION_PROVIDER_ID:
            builtin_action = self.registry.get_builtin_action(action_meta.action_id)
        binding_id = make_binding_id()
        existing_context_id = self.lifecycle.context_id_for_action_instance(
            action_meta=action_meta,
            action_instance_id=action_instance_id,
        )
        context_id = existing_context_id or make_context_id()
        binding_metadata = BindingMetadata(
            providerInstanceId=action_meta.provider_instance_id,
            providerId=action_meta.provider_id,
            actionId=action_meta.action_id,
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
            action_id=action_meta.action_id,
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
            action_id=action_meta.action_id,
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
                binding.action_id,
                action_meta.provider_instance_id,
                binding_id,
            )
            return True
        logger.info(
            "Binding pending provider availability on profile=%s page=%s control=%s action=%s provider=%s binding=%s",
            profile_id,
            page_id,
            binding.control_id,
            binding.action_id,
            action_meta.provider_instance_id,
            binding_id,
        )
        return False
