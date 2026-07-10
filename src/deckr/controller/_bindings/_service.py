import logging
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

import anyio
from deckr.actions.messages import (
    BindingMetadata,
    DynamicPageCommand,
    MatchedCapability,
    PageSessionMetadata,
    SettingsTargetRef,
    make_binding_id,
    make_context_id,
)
from deckr.contracts.authority import ContractPointer
from deckr.contracts.messages import DeckrMessage
from deckr.contracts.models import thaw_json
from deckr.core.util.anyio import AsyncMap
from deckr.hardware import messages as hw_messages
from deckr.hardware.descriptors import (
    DECKR_OUTPUT_RASTER,
    CapabilityRef,
    ControlRef,
    DeviceDescriptor,
    DeviceRef,
)
from deckr.lanes import EndpointSession

from deckr.controller._action_interest import (
    ActionInterestSnapshot,
    ActionInterestSource,
    ActionInterestTracker,
)
from deckr.controller._actions import (
    ActionAvailabilityRecord,
    ActionAvailabilitySource,
    ActionAvailabilityState,
    ActionIntentKey,
    ActionMetadata,
    ActionPlanningSnapshot,
    ActionProviderManager,
    ActionUnavailableCause,
    ControllerActionService,
    ProviderActionKey,
    ProviderSessionKey,
    action_unavailable_cause,
    provider_session_key,
    unavailable_overlay_template,
)
from deckr.controller._binding_planner import (
    BindingPlanner,
    BindingPlanStatus,
    PagePlan,
    PlannedBinding,
    format_validation_summary,
)
from deckr.controller._binding_resolution import ResolvedControlBinding
from deckr.controller._bindings._action_lifecycle import (
    ActionInstanceLifecycleService,
    ActionInstanceSnapshot,
)
from deckr.controller._bindings._attachments import (
    BindingLease,
    ControlAttachmentState,
    HeldInputRecord,
)
from deckr.controller._bindings._commands import ProviderCommandIngress
from deckr.controller._bindings._context import (
    ControlContext,
    PageCommandPort,
    RuntimeMessageSender,
)
from deckr.controller._bindings._ports import BindingActionService
from deckr.controller._command_router import DeviceOutput
from deckr.controller._device_layout import (
    ControlSurface,
    control_surface_for_raster_capability,
    raster_controls,
)
from deckr.controller._event_translator import EventTranslator
from deckr.controller._hardware import HardwareCommandService
from deckr.controller._pages import (
    DynamicPageSession,
    PageOwnerBinding,
    PageSessionService,
    PageStackEntry,
    PageTransitionDraft,
    PageTransitionEffects,
    StaticPageRef,
)
from deckr.controller._render import RenderModel, RenderService, RenderSource
from deckr.controller._render_dispatcher import (
    RenderBackend,
    RenderDispatcher,
    ThreadRenderBackend,
)
from deckr.controller.action_provider.builtin import BUILTIN_ACTION_PROVIDER_ID
from deckr.controller.config._data import DeviceConfig
from deckr.controller.settings import SettingsService

logger = logging.getLogger(__name__)

ACTION_INSTANCE_CREATE_TIMEOUT_SECONDS = 1.0
BINDING_ATTACH_NOTIFY_TIMEOUT_SECONDS = 1.0
SETTINGS_SERVICE_TIMEOUT_SECONDS = 1.0
DETACH_NOTIFY_TIMEOUT_SECONDS = 1.0
_ACTION_METADATA_UNSET: Any = object()


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
    park_binding_ids: frozenset[str]
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


class ControlBindingService:
    def __init__(
        self,
        *,
        controller_id: str,
        device: DeviceDescriptor,
        hardware_ref: DeviceRef,
        command_service: HardwareCommandService,
        config: DeviceConfig,
        manager: ActionProviderManager,
        actions_bus: EndpointSession,
        start_soon: Callable,
        render_backend: RenderBackend | None = None,
        settings_service: SettingsService | None = None,
        clock: Callable[[], float] | None = None,
        action_service: BindingActionService | None = None,
        pages: PageSessionService | None = None,
        page_command_port: PageCommandPort | None = None,
        runtime_sender: RuntimeMessageSender | None = None,
        page_timeout_check_interval: float = 0.25,
    ):
        self._controller_id = controller_id
        self.device = device
        self.hardware_ref = hardware_ref
        self.config_id = config.id
        self._command_service = command_service
        self.config = config
        self.manager = manager
        self._start_soon = start_soon
        self._render_backend = render_backend or ThreadRenderBackend()
        self._clock = clock or time.monotonic
        self._page_command_port = page_command_port or self
        self._runtime_sender = runtime_sender or self
        self._attachments = ControlAttachmentState()
        self._render_dispatcher = RenderDispatcher(
            command_service=command_service,
            config_id=self.config_id,
            backend=self._render_backend,
            start_soon=start_soon,
            result_authorizer=self._attachments.output_render_authorized,
        )
        self._settings_service = settings_service
        self.action_contexts = AsyncMap[str, ControlContext]()
        self._translator = EventTranslator(controller_id=controller_id)
        self._pages = pages or PageSessionService(config, clock=self._clock)
        self._binding_planner = BindingPlanner(
            controller_id=controller_id,
            config_id=self.config_id,
        )
        self._action_service = action_service or (
            ControllerActionService(
                controller_id=controller_id,
                controller_session_id=actions_bus.session_id,
                manager=manager,
                start_soon=None,
                clock=self._clock,
            )
        )
        self._action_interest = ActionInterestTracker(clock=self._clock)
        self._binding_leases = self._attachments.binding_leases
        self._lifecycle = ActionInstanceLifecycleService(
            config_id=self.config_id,
            runtime_sender=self._runtime_sender,
            availability_recorder=self._action_service,
            host=self,
            clock=self._clock,
        )
        self._command_ingress = ProviderCommandIngress(
            host=self,
            lifecycle=self._lifecycle,
        )
        self._page_timeout_check_interval = page_timeout_check_interval
        self._nav_lock = anyio.Lock()
        self._sync_action_interest()

    @property
    def config_active(self) -> bool:
        return self._pages.config_active

    async def start(
        self,
        tg: anyio.abc.TaskGroup,
        stopping: anyio.Event,
    ) -> None:
        tg.start_soon(self._page_timeout_loop, stopping)

    def snapshot(self) -> BindingActionSnapshot:
        active_contexts = {
            lease.control_id: _control_context_snapshot(lease)
            for lease in self._binding_leases.values()
            if self._attachments.binding_command_authorized(lease)
        }
        return BindingActionSnapshot(
            binding_leases=MappingProxyType(
                {
                    binding_id: _binding_lease_snapshot(
                        lease,
                        planned_intent=self.planned_intent_for_lease(lease),
                    )
                    for binding_id, lease in self._binding_leases.items()
                }
            ),
            active_contexts=MappingProxyType(active_contexts),
            action_instances=self._lifecycle.snapshot_action_instances(),
            provider_session_keys=self._lifecycle.provider_session_keys(),
            output_owners=MappingProxyType(
                dict(self._attachments.active_output_by_control)
            ),
            held_inputs=tuple(
                HeldInputSnapshot(
                    binding_id=held.binding_id,
                    control_id=held.control_id,
                    capability_id=held.capability_id,
                    context_id=held.context_id,
                )
                for held in self._attachments.held_input_bindings.values()
            ),
        )

    def context_for_control(self, control_id: str) -> ControlContext | None:
        lease = self._attachments.binding_for_control(control_id)
        if lease is None or not self._attachments.binding_command_authorized(lease):
            return None
        return lease.context

    def binding_by_id(self, binding_id: str) -> BindingLease | None:
        return self._binding_leases.get(binding_id)

    def iter_binding_leases(self) -> Iterable[BindingLease]:
        return tuple(self._binding_leases.values())

    def active_page_session(self) -> DynamicPageSession | None:
        return self._pages.active_dynamic_session()

    def binding_command_authorized(self, lease: BindingLease) -> bool:
        return self._attachments.binding_command_authorized(lease)

    def binding_output_authorized(self, lease: BindingLease) -> bool:
        return self._attachments.binding_output_authorized(lease)

    async def _render_unavailable_to_control(
        self,
        control: ControlSurface,
        *,
        planned: PlannedBinding,
        page_id: str,
    ) -> None:
        """Render a not-available overlay to an output-capable control."""
        fallback = self._unavailable_fallback_render(planned)
        self._log_unavailable_fallback_render(
            control,
            planned=planned,
            page_id=page_id,
            fallback=fallback,
        )
        await self._render_status_to_control(
            control,
            overlay_type=fallback.template,
            source=self._unavailable_fallback_render_source(planned, fallback),
        )

    async def _render_pending_to_control(self, control: ControlSurface) -> None:
        """Render a pending overlay to an output-capable control."""
        await self._render_status_to_control(control, overlay_type="pending")

    async def _render_status_to_control(
        self,
        control: ControlSurface,
        *,
        overlay_type: str,
        source: RenderSource | None = None,
    ) -> None:
        if control.image_format is None or control.raster_capability_id is None:
            return
        model = RenderModel(overlay_type=overlay_type)
        render_service = RenderService()
        output = DeviceOutput(
            self._command_service,
            self.config_id,
            control.id,
            control.raster_capability_id,
        )
        context_id = make_context_id()
        request = render_service.build_request(
            model,
            control.image_format,
            config_id=self.config_id,
            context_id=context_id,
            control_id=control.id,
            source=source,
        )
        await self._render_dispatcher.submit_request(
            control_id=control.id,
            context_id=context_id,
            request=request,
            output=output,
        )

    def _unavailable_fallback_render(
        self,
        planned: PlannedBinding,
    ) -> _UnavailableFallbackRender:
        intent = self._binding_planner.resolved_action_intent_key(planned.binding)
        now = self._clock()
        record = self._action_service.record_for_intent(intent, now=now)
        state = (
            self._action_service.state_for_key(record.key, now=now)
            if record is not None
            else None
        )
        cause = action_unavailable_cause(
            record,
            has_live_provider_session_contract=(
                self._planned_binding_has_live_provider_session_contract(planned)
            ),
        )
        return _UnavailableFallbackRender(
            template=unavailable_overlay_template(cause),
            cause=cause,
            record=record,
            state=state,
        )

    def _planned_binding_has_live_provider_session_contract(
        self,
        planned: PlannedBinding,
    ) -> bool | None:
        action_meta = planned.action_meta
        if action_meta is None:
            return None
        if action_meta.provider_instance_id == BUILTIN_ACTION_PROVIDER_ID:
            return True
        session_key = provider_session_key(action_meta)
        return self.current_contract(session_key) is not None

    def _log_unavailable_fallback_render(
        self,
        control: ControlSurface,
        *,
        planned: PlannedBinding,
        page_id: str,
        fallback: _UnavailableFallbackRender,
    ) -> None:
        record = fallback.record
        selected_provider_key = (
            (
                record.key.provider_instance_id,
                record.key.action_uuid,
            )
            if record is not None
            else None
        )
        logger.info(
            "Controller unavailable fallback render config=%s page=%s control=%s "
            "action=%s provider=%s selected_provider_key=%s "
            "availability_state=%s availability_source=%s reason=%s cause=%s "
            "template=%s",
            self.config_id,
            page_id,
            control.id,
            planned.binding.action_uuid,
            self._unavailable_fallback_provider_instance(planned, record),
            selected_provider_key,
            fallback.state.value if fallback.state is not None else None,
            record.source.value if record is not None else None,
            record.reason if record is not None else None,
            fallback.cause.value,
            fallback.template,
        )

    def _unavailable_fallback_render_source(
        self,
        planned: PlannedBinding,
        fallback: _UnavailableFallbackRender,
    ) -> RenderSource:
        record = fallback.record
        metadata = record.metadata if record is not None else None
        if metadata is None:
            metadata = planned.action_meta
        return RenderSource(
            provider_instance_id=self._unavailable_fallback_provider_instance(
                planned,
                record,
            ),
            provider_id=metadata.provider_id if metadata is not None else None,
            action_id=planned.binding.action_uuid,
            action_instance_id=planned.action_instance_id,
            command_type="controller_fallback",
            content_kind=f"overlay:{fallback.template}",
            availability_cause=fallback.cause.value,
            availability_state=(
                fallback.state.value if fallback.state is not None else None
            ),
            availability_source=record.source.value if record is not None else None,
            availability_reason=record.reason if record is not None else None,
        )

    def _unavailable_fallback_provider_instance(
        self,
        planned: PlannedBinding,
        record: ActionAvailabilityRecord | None,
    ) -> str | None:
        if record is not None:
            return record.key.provider_instance_id
        if planned.action_meta is not None:
            return planned.action_meta.provider_instance_id
        return planned.binding.provider_instance_id

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
        lease = self._binding_leases.get(binding_id)
        if lease is None:
            return None
        was_active = (
            self._attachments.active_input_lease(lease.control_id) is lease
            or self._attachments.active_output_lease(lease.control_id) is lease
        )
        self._attachments.disable_binding_authority(lease)
        logger.info(
            "Revoking binding config=%s control=%s action=%s provider=%s "
            "binding=%s reason=%s clearOutput=%s notifyProvider=%s",
            self.config_id,
            lease.control_id,
            lease.action_uuid,
            lease.provider_instance_id,
            lease.binding_id,
            reason,
            clear_output,
            notify_provider,
        )
        if clear_held_input:
            await self._cancel_held_inputs_for_binding(lease)
        if was_active:
            await self.action_contexts.delete(lease.control_id)
        self._attachments.remove_binding(binding_id)
        if lease.attached and notify_provider:
            with anyio.move_on_after(DETACH_NOTIFY_TIMEOUT_SECONDS) as scope:
                await lease.context.on_binding_detached(reason)
            if scope.cancel_called:
                logger.warning(
                    "Timed out notifying provider of binding detach config=%s "
                    "control=%s action=%s provider=%s binding=%s reason=%s",
                    self.config_id,
                    lease.control_id,
                    lease.action_uuid,
                    lease.provider_instance_id,
                    lease.binding_id,
                    reason,
                )
        output = (
            DeviceOutput(
                self._command_service,
                self.config_id,
                lease.control_id,
                lease.raster_capability_id,
            )
            if lease.raster_capability_id is not None
            else None
        )
        await self._render_dispatcher.clear_control(
            lease.control_id,
            context_id=lease.context_id,
            binding_id=lease.binding_id,
            output=output,
            clear_output=clear_output,
        )
        return lease

    async def _cancel_held_inputs_for_binding(self, lease: BindingLease) -> None:
        for held in self._attachments.cancel_held_inputs_for_binding(
            lease.binding_id,
        ):
            if not lease.attached or held.context_id != lease.context_id:
                continue
            cancel_event = held.down_event.model_copy(
                update={"event_type": "cancel"}
            )
            try:
                await lease.context.on_input(cancel_event)
            except Exception:
                logger.exception(
                    "Error delivering cancel to action %s binding=%s",
                    lease.action_uuid,
                    lease.binding_id,
                )

    async def _cancel_all_held_inputs(self) -> None:
        for held in self._attachments.cancel_all_held_inputs():
            lease = self._binding_leases.get(held.binding_id)
            if lease is not None:
                await self._deliver_cancelled_input(lease, held)

    async def _deliver_cancelled_input(
        self,
        lease: BindingLease,
        held: HeldInputRecord,
    ) -> None:
        if not lease.attached or held.context_id != lease.context_id:
            return
        cancel_event = held.down_event.model_copy(update={"event_type": "cancel"})
        try:
            await lease.context.on_input(cancel_event)
        except Exception:
            logger.exception(
                "Error delivering cancel to action %s binding=%s",
                lease.action_uuid,
                lease.binding_id,
            )

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
        park_binding_ids: frozenset[str] = frozenset(),
        preserve_output_control_ids: frozenset[str],
        reason: str = "active_bindings",
        clear_held_input: bool = True,
    ) -> None:
        for binding_id in list(self._binding_leases):
            lease = self._binding_leases.get(binding_id)
            if lease is not None and binding_id in preserve_binding_ids:
                continue
            if lease is not None and binding_id in park_binding_ids:
                await self._park_binding(
                    lease,
                    reason=reason,
                    clear_held_input=clear_held_input,
                )
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

    async def _park_binding(
        self,
        lease: BindingLease,
        *,
        reason: str,
        clear_held_input: bool = True,
    ) -> None:
        was_active = (
            self._attachments.active_input_lease(lease.control_id) is lease
            or self._attachments.active_output_lease(lease.control_id) is lease
        )
        self._attachments.disable_binding_authority(lease)
        logger.info(
            "Parking binding config=%s control=%s action=%s provider=%s "
            "binding=%s reason=%s",
            self.config_id,
            lease.control_id,
            lease.action_uuid,
            lease.provider_instance_id,
            lease.binding_id,
            reason,
        )
        if clear_held_input:
            await self._cancel_held_inputs_for_binding(lease)
        if was_active:
            await self.action_contexts.delete(lease.control_id)

    async def _refresh_binding_output(self, lease: BindingLease, *, reason: str) -> None:
        base_output_generation = lease.context.base_output_generation
        metadata_output_generation = getattr(
            lease.context.metadata,
            "output_generation",
            None,
        )
        content_kind = lease.context.content_kind
        if not lease.attached:
            logger.debug(
                "Skipping cached binding output refresh for detached lease "
                "config=%s control=%s action=%s provider=%s "
                "provider_session=%s binding=%s context=%s "
                "action_instance=%s output_route_generation=%s "
                "base_output_generation=%s metadata_output_generation=%s "
                "content_kind=%s attached=%s reason=%s",
                self.config_id,
                lease.control_id,
                lease.action_uuid,
                lease.provider_instance_id,
                lease.provider_session_id,
                lease.binding_id,
                lease.context_id,
                lease.action_instance_id,
                lease.output_route_generation,
                base_output_generation,
                metadata_output_generation,
                content_kind,
                lease.attached,
                reason,
            )
            return
        logger.debug(
            "Refreshing cached binding output config=%s control=%s action=%s "
            "provider=%s provider_session=%s binding=%s context=%s "
            "action_instance=%s output_route_generation=%s "
            "base_output_generation=%s metadata_output_generation=%s "
            "content_kind=%s attached=%s reason=%s",
            self.config_id,
            lease.control_id,
            lease.action_uuid,
            lease.provider_instance_id,
            lease.provider_session_id,
            lease.binding_id,
            lease.context_id,
            lease.action_instance_id,
            lease.output_route_generation,
            base_output_generation,
            metadata_output_generation,
            content_kind,
            lease.attached,
            reason,
        )
        try:
            await lease.context.refresh_raster()
        except Exception:
            logger.exception(
                "Error refreshing binding output config=%s control=%s action=%s "
                "provider=%s binding=%s reason=%s",
                self.config_id,
                lease.control_id,
                lease.action_uuid,
                lease.provider_instance_id,
                lease.binding_id,
                reason,
            )

    async def _clear_all_raster_controls(
        self,
        *,
        preserve_control_ids: frozenset[str] = frozenset(),
    ) -> None:
        """Clear raster-capable controls before rendering a new page."""
        for control in raster_controls(self.device):
            if control.control_id in preserve_control_ids:
                continue
            await self._render_dispatcher.clear_control(
                control.control_id,
                output=DeviceOutput(
                    self._command_service,
                    self.config_id,
                    control.control_id,
                    control.capability_id,
                ),
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
            controllerId=self._controller_id,
            configId=self.config_id,
            providerInstanceId=provider_instance_id,
            providerId=provider_id,
            actionId=binding.action_uuid,
            actionInstanceId=action_instance_id,
            stableId=binding.stable_id,
        )

    def _matched_capabilities(
        self,
        binding: ResolvedControlBinding,
    ) -> tuple[MatchedCapability, ...]:
        selected = {
            "input": binding.input_capability_ids,
            "output": binding.output_capability_ids,
            "state": binding.state_capability_ids,
            "config": binding.config_capability_ids,
            "diagnostic": binding.diagnostic_capability_ids,
        }
        matches: list[MatchedCapability] = []
        for direction, capability_ids in selected.items():
            for capability in binding.control.capabilities:
                if capability.capability_id not in capability_ids:
                    continue
                if direction != "diagnostic" and capability.direction != direction:
                    continue
                matches.append(
                    MatchedCapability(
                        requirementName=direction,
                        capability=CapabilityRef(
                            deviceRef=self.hardware_ref,
                            controlId=binding.control_id,
                            capabilityId=capability.capability_id,
                        ),
                        family=capability.family,
                        type=capability.capability_type,
                        direction=capability.direction,
                        eventTypes=capability.event_types,
                        commandTypes=capability.command_types,
                        provenance="native",
                    )
                )
        return tuple(matches)

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
        record = self._action_service.record_for_intent(
            intent,
            now=self._clock(),
        )
        if (
            record is None
            or record.source != ActionAvailabilitySource.SERVICE_VIEW
            or record.metadata is None
            or self._action_service.state_for_key(record.key, now=self._clock())
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

    def _sync_top_frame_state(self) -> None:
        self._sync_action_interest()

    def action_interest_snapshot(
        self,
        *,
        now: float | None = None,
    ) -> ActionInterestSnapshot:
        return self._action_interest.snapshot(now=now)

    def _sync_action_interest(self) -> None:
        now = self._clock()
        if self._pages.config_active:
            self._action_interest.replace_strong_interests(
                ActionInterestSource.CONNECTED_CONFIG,
                self._configured_action_intents(),
                now=now,
            )
        else:
            self._action_interest.clear_source(
                ActionInterestSource.CONNECTED_CONFIG,
                now=now,
            )

        visible_source = self._visible_action_interest_source()
        visible_intents = self._current_plan_action_intents()
        if visible_source == ActionInterestSource.DYNAMIC_PAGE:
            self._action_interest.clear_source(
                ActionInterestSource.VISIBLE_BINDING,
                now=now,
            )
            self._action_interest.replace_strong_interests(
                ActionInterestSource.DYNAMIC_PAGE,
                visible_intents,
                now=now,
            )
            self._publish_action_interest_snapshot(now=now)
            return

        self._action_interest.replace_strong_interests(
            ActionInterestSource.VISIBLE_BINDING,
            visible_intents,
            now=now,
        )
        self._action_interest.clear_source(
            ActionInterestSource.DYNAMIC_PAGE,
            now=now,
        )
        self._publish_action_interest_snapshot(now=now)

    def _publish_action_interest_snapshot(self, *, now: float) -> None:
        if self._pages.config_active:
            self._action_service.update_config_interest(
                self.config_id,
                self._action_interest.snapshot(now=now),
            )
        else:
            self._action_service.clear_config_interest(self.config_id)

    def _visible_action_interest_source(self) -> ActionInterestSource:
        current_plan = self._pages.current_plan()
        if current_plan is not None and current_plan.page_session is not None:
            return ActionInterestSource.DYNAMIC_PAGE
        return ActionInterestSource.VISIBLE_BINDING

    def _configured_action_intents(self) -> tuple[ActionIntentKey, ...]:
        intents: list[ActionIntentKey] = []
        for profile in self.config.profiles:
            for page_index in range(len(profile.pages)):
                bindings = self._pages.resolve_static_bindings(
                    StaticPageRef(
                        profile_name=profile.name,
                        page_index=page_index,
                    )
                )
                intents.extend(self._binding_planner.static_action_intents(bindings))
        return _dedupe_action_intents(intents)

    def _current_plan_action_intents(self) -> tuple[ActionIntentKey, ...]:
        current_plan = self._pages.current_plan()
        if current_plan is None:
            return ()
        return _dedupe_action_intents(
            self._binding_planner.resolved_action_intent_key(planned.binding)
            for planned in current_plan.bindings
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
        current_plan = self._pages.current_plan()
        if current_plan is None:
            return frozenset()
        return self._external_dynamic_page_child_action_instance_ids(
            (current_plan,)
        )

    def _active_dynamic_page_owner_action_instance_ids(self) -> frozenset[str]:
        return frozenset(
            frame.page_session.action_instance_id
            for frame in self._pages.snapshot().frames
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
            await self._lifecycle.destroy_action_instance(
                action_instance_id,
                reason=reason,
            )

    def _action_availability_change_affects_plan(
        self,
        changed_keys: frozenset[ProviderActionKey],
        plan: PagePlan,
    ) -> bool:
        if not changed_keys:
            return True
        exact_keys = set(self._existing_provider_action_keys())
        for planned in plan.bindings:
            if planned.action_meta is None:
                continue
            exact_keys.add(
                ProviderActionKey(
                    planned.action_meta.provider_instance_id,
                    planned.action_meta.uuid,
                )
            )
        if changed_keys & exact_keys:
            return True

        intents = tuple(
            self._binding_planner.resolved_action_intent_key(planned.binding)
            for planned in plan.bindings
        )
        return any(
            self._provider_action_key_matches_intent(key, intent)
            for key in changed_keys
            for intent in intents
        )

    def _provider_action_key_matches_intent(
        self,
        key: ProviderActionKey,
        intent: ActionIntentKey,
    ) -> bool:
        if key.action_uuid != intent.action_uuid:
            return False
        if intent.provider_instance_id is not None:
            return key.provider_instance_id == intent.provider_instance_id
        if not intent.provider_labels:
            return True
        record = self._action_service.record_for_key(key)
        if record is None or record.metadata is None:
            return True
        labels = record.metadata.provider_labels or {}
        return all(labels.get(name) == value for name, value in intent.provider_labels)

    async def _action_metadata_snapshot_for_plan(
        self,
        intents: tuple[ActionIntentKey, ...],
        *,
        refresh_actions: bool,
    ) -> ActionPlanningSnapshot:
        if refresh_actions:
            await self._action_service.ensure_local_builtin_availability(
                intents
            )
        snapshot = self._action_service.planning_snapshot(
            intents,
            existing_provider_keys=self._existing_provider_action_keys(),
            now=self._clock(),
        )
        if not refresh_actions:
            return snapshot
        return snapshot

    def _existing_provider_action_keys(self) -> frozenset[ProviderActionKey]:
        return frozenset(
            ProviderActionKey(lease.provider_instance_id, lease.action_uuid)
            for lease in self._binding_leases.values()
        )

    def current_contract(
        self,
        key: ProviderSessionKey | None,
    ) -> ContractPointer | None:
        return self._action_service.current_contract(key)

    async def send_action_runtime_message(
        self,
        *,
        provider_session_key: ProviderSessionKey | None,
        message_type: str,
        body,
    ) -> bool:
        if provider_session_key is None:
            return False
        return await self._action_service.send_runtime_message(
            provider_session_key,
            message_type=message_type,
            body=body,
        )

    def provider_session_key_for_session(
        self,
        *,
        provider_instance_id: str,
        provider_id: str,
        provider_session_id: str | None,
    ) -> ProviderSessionKey | None:
        if provider_session_id is None:
            return None
        return ProviderSessionKey(
            provider_instance_id=provider_instance_id,
            provider_id=provider_id,
            provider_session_id=provider_session_id,
        )

    async def message_contract_authorized(
        self,
        msg: DeckrMessage,
        key: ProviderSessionKey | None,
    ) -> bool:
        expected = self.current_contract(key)
        return expected is not None and msg.contract == expected

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
        if not self._action_service.provider_lifecycle_recovery_required(key):
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
        return self.current_contract(lease.provider_session_key)

    def planned_intent_for_lease(self, lease: BindingLease) -> ActionIntentKey:
        current_plan = self._pages.current_plan()
        if current_plan is not None:
            for planned in current_plan.bindings:
                if planned.control_id == lease.control_id:
                    return self._binding_planner.resolved_action_intent_key(
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
            self.config_id,
            lease.control_id,
            lease.action_uuid,
            lease.provider_instance_id,
            lease.binding_id,
            reason,
        )
        await self.on_action_availability_changed(
            (ProviderActionKey(lease.provider_instance_id, lease.action_uuid),)
        )

    def _log_static_page_plan_rejection(
        self,
        result_errors,
    ) -> None:
        logger.error(
            "Page transition rejected (capability validation): %s",
            format_validation_summary(list(result_errors)),
        )
        for err in result_errors:
            logger.error(
                "Binding validation failed [%s]: %s (control=%s action=%s) %s",
                err.code,
                err.message,
                err.control_ref,
                err.action_uuid,
                err.details,
            )

    def _log_dynamic_page_plan_rejection(
        self,
        result_errors,
    ) -> None:
        logger.error(
            "Dynamic page transition rejected (capability validation): %s",
            format_validation_summary(list(result_errors)),
        )
        for err in result_errors:
            logger.error(
                "Dynamic page binding validation failed [%s]: %s (control=%s action=%s) %s",
                err.code,
                err.message,
                err.control_ref,
                err.action_uuid,
                err.details,
            )

    async def _build_static_page_plan(
        self,
        entry: StaticPageRef,
        *,
        retained_plan: PagePlan | None = None,
        refresh_actions: bool = True,
    ) -> PagePlan | None:
        bindings = self._pages.resolve_static_bindings(entry)
        action_metadata = await self._action_metadata_snapshot_for_plan(
            self._binding_planner.static_action_intents(bindings),
            refresh_actions=refresh_actions,
        )
        action_status = self._binding_plan_status(action_metadata)
        result = self._binding_planner.build_static_page_plan(
            entry,
            bindings=bindings,
            device=self.device,
            action_metadata=action_metadata.metadata,
            action_status=action_status,
            retained_plan=retained_plan,
        )
        if result.plan is None:
            self._log_static_page_plan_rejection(result.validation_errors)
        return result.plan

    async def _build_dynamic_page_plan(
        self,
        entry: DynamicPageCommand,
        *,
        page_session: DynamicPageSession,
        page_session_generation: int | None = None,
        retained_plan: PagePlan | None = None,
        refresh_actions: bool = True,
    ) -> PagePlan | None:
        action_metadata = await self._action_metadata_snapshot_for_plan(
            self._binding_planner.dynamic_action_intents(
                entry.bindings,
                owner_action_uuid=page_session.owner_action_uuid,
                owner_provider_instance_id=page_session.owner_provider_instance_id,
            ),
            refresh_actions=refresh_actions,
        )
        action_status = self._binding_plan_status(action_metadata)
        result = self._binding_planner.build_dynamic_page_plan(
            entry,
            device=self.device,
            page_session=page_session,
            page_session_generation=page_session_generation,
            action_metadata=action_metadata.metadata,
            action_status=action_status,
            retained_plan=retained_plan,
        )
        if result.plan is None:
            self._log_dynamic_page_plan_rejection(result.validation_errors)
        return result.plan

    async def _build_page_plan(
        self,
        entry: PageStackEntry,
        *,
        page_session: DynamicPageSession | None = None,
        page_session_generation: int | None = None,
        retained_plan: PagePlan | None = None,
        refresh_actions: bool = True,
    ) -> PagePlan | None:
        if isinstance(entry, StaticPageRef):
            return await self._build_static_page_plan(
                entry,
                retained_plan=retained_plan,
                refresh_actions=refresh_actions,
            )
        if page_session is None:
            logger.error("Dynamic page planning missing page session")
            return None
        return await self._build_dynamic_page_plan(
            entry,
            page_session=page_session,
            page_session_generation=page_session_generation,
            retained_plan=retained_plan,
            refresh_actions=refresh_actions,
        )

    def _binding_plan_status(
        self,
        snapshot: ActionPlanningSnapshot,
    ) -> dict[ActionIntentKey, BindingPlanStatus]:
        status: dict[ActionIntentKey, BindingPlanStatus] = {
            intent: BindingPlanStatus.PENDING for intent in snapshot.pending
        }
        status.update(
            {
                intent: BindingPlanStatus.UNAVAILABLE
                for intent in snapshot.unavailable
            }
        )
        return status

    def _binding_lease_for_control(self, control_id: str) -> BindingLease | None:
        return self._attachments.binding_for_control(control_id)

    async def _claim_binding_output_route(self, lease: BindingLease) -> None:
        if lease.raster_capability_id is None:
            return
        await self._render_dispatcher.submit_request(
            control_id=lease.control_id,
            context_id=lease.context_id,
            binding_id=lease.binding_id,
            request=None,
            output=DeviceOutput(
                self._command_service,
                self.config_id,
                lease.control_id,
                lease.raster_capability_id,
            ),
        )

    async def _enable_binding_authority(self, lease: BindingLease) -> None:
        self._attachments.enable_binding_authority(lease)
        await self.action_contexts.set(lease.control_id, lease.context)
        await self._claim_binding_output_route(lease)

    async def _activate_binding(
        self,
        lease: BindingLease,
    ) -> bool:
        if lease.attached:
            await self._enable_binding_authority(lease)
            return True
        action_meta = ActionMetadata(
            uuid=lease.action_uuid,
            provider_instance_id=lease.provider_instance_id,
            provider_id=lease.provider_id,
            provider_session_id=lease.provider_session_id,
        )
        logger.info(
            "Binding activation starting config=%s control=%s action=%s "
            "provider=%s binding=%s",
            self.config_id,
            lease.control_id,
            lease.action_uuid,
            lease.provider_instance_id,
            lease.binding_id,
        )
        with anyio.move_on_after(ACTION_INSTANCE_CREATE_TIMEOUT_SECONDS) as scope:
            await self._lifecycle.ensure_action_instance(
                action_meta=action_meta,
                action_instance_id=lease.action_instance_id,
                context_id=lease.context_id,
            )
        if scope.cancel_called:
            logger.warning(
                "Binding activation timed out config=%s control=%s action=%s "
                "provider=%s binding=%s stage=actionInstanceCreated timeout=%ss",
                self.config_id,
                lease.control_id,
                lease.action_uuid,
                lease.provider_instance_id,
                lease.binding_id,
                ACTION_INSTANCE_CREATE_TIMEOUT_SECONDS,
            )
            return False
        logger.info(
            "Binding activation action instance ready config=%s control=%s "
            "action=%s provider=%s binding=%s",
            self.config_id,
            lease.control_id,
            lease.action_uuid,
            lease.provider_instance_id,
            lease.binding_id,
        )
        lease.attached = True
        await self._enable_binding_authority(lease)
        with anyio.move_on_after(BINDING_ATTACH_NOTIFY_TIMEOUT_SECONDS) as scope:
            await lease.context.on_binding_attached()
        if scope.cancel_called:
            logger.warning(
                "Binding activation timed out config=%s control=%s action=%s "
                "provider=%s binding=%s stage=bindingAttached timeout=%ss",
                self.config_id,
                lease.control_id,
                lease.action_uuid,
                lease.provider_instance_id,
                lease.binding_id,
                BINDING_ATTACH_NOTIFY_TIMEOUT_SECONDS,
            )
        else:
            logger.info(
                "Binding activation attached config=%s control=%s action=%s "
                "provider=%s binding=%s",
                self.config_id,
                lease.control_id,
                lease.action_uuid,
                lease.provider_instance_id,
                lease.binding_id,
            )
        return True

    async def _reconcile_binding_sessions(self) -> None:
        for lease in tuple(self._binding_leases.values()):
            if not lease.attached:
                await self._activate_binding(lease)

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
            self.device,
            binding.control_id,
            raster_capability_id=raster_capability_id,
        )
        if control is None:
            logger.warning(
                "Resolved binding control disappeared before bind: config=%s control=%s",
                self.config_id,
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
        provider_session_id = action_meta.provider_session_id
        session_key = (
            None
            if action_meta.provider_instance_id == BUILTIN_ACTION_PROVIDER_ID
            else provider_session_key(action_meta)
        )
        contract = self.current_contract(session_key)
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
        if self._settings_service is not None and settings_target is not None:
            with anyio.move_on_after(SETTINGS_SERVICE_TIMEOUT_SECONDS) as scope:
                try:
                    snapshot = await self._settings_service.get(settings_target)
                    initial_settings = dict(thaw_json(snapshot.settings))
                except KeyError:
                    initial_settings = dict(binding.settings)
            if scope.cancel_called:
                logger.warning(
                    "Binding settings snapshot timed out config=%s control=%s "
                    "action=%s provider=%s target=%s timeout=%ss",
                    self.config_id,
                    binding.control_id,
                    binding.action_uuid,
                    action_meta.provider_instance_id,
                    settings_target.key(),
                    SETTINGS_SERVICE_TIMEOUT_SECONDS,
                )
        builtin_action = None
        if action_meta.provider_instance_id == BUILTIN_ACTION_PROVIDER_ID and hasattr(
            self.manager, "get_builtin_action"
        ):
            builtin_action = self.manager.get_builtin_action(action_meta.uuid)
        binding_id = make_binding_id()
        existing_context_id = self._lifecycle.context_id_for_action_instance(
            action_meta=action_meta,
            action_instance_id=action_instance_id,
        )
        context_id = existing_context_id or make_context_id()
        matched_capabilities = self._matched_capabilities(binding)
        binding_metadata = BindingMetadata(
            providerInstanceId=action_meta.provider_instance_id,
            providerId=action_meta.provider_id,
            actionId=action_meta.uuid,
            actionInstanceId=action_instance_id,
            configId=self.config_id,
            contextId=context_id,
            bindingId=binding_id,
            pageSessionId=page_session_id,
            deviceRef=self.hardware_ref,
            controlRef=ControlRef(
                deviceRef=self.hardware_ref,
                controlId=control.id,
            ),
            itemKey=item_key,
            handler=handler,
            matchedCapabilities=matched_capabilities,
        )
        ctx = ControlContext(
            controller_id=self._controller_id,
            device=self.device,
            config_id=self.config_id,
            command_service=self._command_service,
            provider_instance_id=action_meta.provider_instance_id,
            provider_id=action_meta.provider_id,
            action_uuid=action_meta.uuid,
            control=control,
            settings=initial_settings,
            internal=dict(internal or {}),
            runtime_sender=self._runtime_sender,
            page_command_port=self._page_command_port,
            start_soon=self._start_soon,
            render_dispatcher=self._render_dispatcher,
            context_settings_target=settings_target,
            provider_session_id=provider_session_id,
            contract=contract,
            profile_id=profile_id,
            page_id=page_id,
            builtin_action=builtin_action,
            metadata=binding_metadata,
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
        )
        self._attachments.add_binding(lease)
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

    async def _page_timeout_loop(self, stopping: anyio.Event) -> None:
        while not stopping.is_set():
            await anyio.sleep(self._page_timeout_check_interval)
            if stopping.is_set():
                return
            session = self._pages.expired_session()
            if session is not None:
                await self.close_page(context_id=session.context_id, reason="timeout")

    def page_session_metadata(
        self,
        session: DynamicPageSession,
    ) -> PageSessionMetadata:
        bindings = tuple(
            lease.context.metadata
            for lease in self._binding_leases.values()
            if lease.attached and lease.page_session_id == session.page_session_id
        )
        return PageSessionMetadata(
            providerInstanceId=session.owner_provider_instance_id,
            providerId=session.owner_provider_id,
            actionInstanceId=session.action_instance_id,
            configId=self.config_id,
            pageId=session.page_id,
            pageSessionId=session.page_session_id,
            contextId=session.context_id,
            ownerBindingId=session.owner_binding_id,
            bindings=bindings,
        )

    async def _apply_page_transition_effects(
        self,
        effects: PageTransitionEffects,
        *,
        causation_id: str | None = None,
    ) -> None:
        for session in effects.sessions_to_close:
            await self._lifecycle.emit_page_closed(
                session,
                effects.cleanup_reason,
                causation_id=causation_id,
            )
        await self._destroy_inactive_external_dynamic_page_children(
            effects.previous_dynamic_plans,
            reason=effects.cleanup_reason,
        )

    async def _finalize_dynamic_page(
        self,
        reason: str,
        *,
        causation_id: str | None = None,
    ) -> tuple[PagePlan, ...]:
        effects = self._pages.clear(reason=reason, dynamic_only=True)
        self._sync_top_frame_state()
        for session in effects.sessions_to_close:
            await self._lifecycle.emit_page_closed(
                session,
                reason,
                causation_id=causation_id,
            )
        return effects.previous_dynamic_plans

    async def _execute_page_draft(
        self,
        draft: PageTransitionDraft,
        *,
        causation_id: str | None = None,
        apply_effects: bool = True,
    ) -> tuple[bool, PageTransitionEffects | None]:
        plan = await self._build_page_plan(
            draft.entry,
            page_session=draft.page_session,
            page_session_generation=draft.page_session_generation,
            retained_plan=draft.retained_plan,
            refresh_actions=draft.refresh_actions,
        )
        if plan is None:
            return False, None
        await self._commit_page_plan(
            plan,
            departing=draft.departing,
            preserve_rebound_outputs=draft.preserve_rebound_outputs,
        )
        effects = self._pages.commit(draft, plan)
        self._sync_top_frame_state()
        if apply_effects:
            await self._apply_page_transition_effects(
                effects,
                causation_id=causation_id,
            )
        return True, effects

    async def _commit_page_plan(
        self,
        plan: PagePlan,
        *,
        departing: PageStackEntry | None,
        preserve_rebound_outputs: bool = False,
    ) -> None:
        commit = self._prepare_page_commit(
            plan,
            departing=departing,
            preserve_rebound_outputs=preserve_rebound_outputs,
        )
        await self._apply_page_commit(commit)

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
            park_binding_ids=self._parked_binding_ids(plan),
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

    def _parked_binding_ids(self, plan: PagePlan) -> frozenset[str]:
        session = plan.page_session
        if session is None:
            return frozenset()
        if session.owner_binding_id not in self._binding_leases:
            return frozenset()
        return frozenset({session.owner_binding_id})

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
            self.config_id,
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
                park_binding_ids=commit.park_binding_ids,
                preserve_output_control_ids=commit.preserve_output_control_ids,
                reason=commit.transition_reason,
            )

        await self._clear_all_raster_controls(
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
            self.config_id,
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
        }:
            control = _find_control_surface(
                self.device,
                planned.control_id,
                raster_capability_id=_selected_raster_capability_id(planned.binding),
            )
            if control is not None:
                if planned.status == BindingPlanStatus.PENDING:
                    await self._render_pending_to_control(control)
                else:
                    await self._render_unavailable_to_control(
                        control,
                        planned=planned,
                        page_id=plan.page_id,
                )
            return False
        existing = self._matching_existing_binding_lease(plan, planned)
        if existing is not None:
            activated = await self._activate_binding(existing)
            if activated:
                await self._refresh_binding_output(existing, reason="binding_reused")
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
            self.device,
            planned.control_id,
            raster_capability_id=_selected_raster_capability_id(planned.binding),
        )
        if control is not None:
            await self._render_unavailable_to_control(
                control,
                planned=planned,
                page_id=plan.page_id,
            )
        return False

    def _matching_existing_binding_lease(
        self,
        plan: PagePlan,
        planned: PlannedBinding,
    ) -> BindingLease | None:
        if planned.action_meta is None:
            return None
        action_meta = self._action_metadata_with_current_session(planned.action_meta)
        raster_capability_id = _selected_raster_capability_id(planned.binding)
        for lease in tuple(self._binding_leases.values()):
            if not lease.attached:
                continue
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

    async def set_page(
        self,
        *,
        profile: str | None = None,
        page: int | None = None,
        descriptor: DynamicPageCommand | None = None,
        causation_id: str | None = None,
    ) -> bool:
        """Navigate to a static page (profile, page) or dynamic page (descriptor)."""
        async with self._nav_lock:
            draft = self._pages.begin_set_page(
                profile=profile,
                page=page,
                descriptor=descriptor,
                close_dynamic=True,
                close_reason="navigate",
            )
            if draft is None:
                if not self._pages.config_active:
                    logger.info(
                        "Ignoring page transition while config %s is inactive",
                        self.config_id,
                    )
                return False
            ok, _ = await self._execute_page_draft(
                draft,
                causation_id=causation_id,
            )
            return ok

    async def open_page(
        self,
        *,
        descriptor: DynamicPageCommand,
        context_id: str,
        binding_id: str | None = None,
        causation_id: str | None = None,
    ) -> DynamicPageSession | None:
        """Open or claim the dynamic page context for the sending action context."""
        if not descriptor or not descriptor.bindings:
            return None

        async with self._nav_lock:
            if binding_id is not None:
                owner_lease = self._binding_leases.get(binding_id)
                if owner_lease is not None and (
                    owner_lease.context_id != context_id
                    or not self._attachments.binding_command_authorized(owner_lease)
                ):
                    owner_lease = None
            else:
                owner_lease = self._attachments.binding_for_context(context_id)

            if owner_lease is None:
                logger.warning(
                    "open_page ignored: no active context for %s", context_id
                )
                return None

            draft = self._pages.begin_open_page(
                descriptor=descriptor,
                owner=PageOwnerBinding(
                    context_id=context_id,
                    binding_id=owner_lease.binding_id,
                    control_id=owner_lease.control_id,
                    action_uuid=owner_lease.action_uuid,
                    provider_instance_id=owner_lease.provider_instance_id,
                    provider_id=owner_lease.provider_id,
                    provider_session_id=owner_lease.provider_session_id,
                    action_instance_id=owner_lease.action_instance_id,
                    profile_id=owner_lease.profile_id,
                    page_id=owner_lease.page_id,
                    page_session_id=owner_lease.page_session_id,
                    settings_target=owner_lease.settings_target,
                ),
            )
            if draft is None or draft.page_session is None:
                return None
            ok, _ = await self._execute_page_draft(
                draft,
                causation_id=causation_id,
            )
            if ok:
                session = draft.page_session
                await self._lifecycle.emit_page_opened(
                    session,
                    causation_id=causation_id,
                )
                return session
            return None

    async def replace_page(
        self,
        *,
        descriptor: DynamicPageCommand,
        context_id: str,
        causation_id: str | None = None,
    ) -> None:
        """Replace the active page session with a new concrete session."""
        if not descriptor or not descriptor.bindings:
            return
        async with self._nav_lock:
            draft = self._pages.begin_replace_page(
                descriptor=descriptor,
                context_id=context_id,
            )
            if draft is None:
                logger.warning(
                    "replace_page ignored: no active page for %s", context_id
                )
                return
            ok, _ = await self._execute_page_draft(
                draft,
                causation_id=causation_id,
            )
            if ok:
                self._pages.record_activity()

    async def close_page(
        self,
        *,
        context_id: str,
        reason: str = "close",
        causation_id: str | None = None,
    ) -> None:
        """Close the active widget page and return to its owner profile page."""
        async with self._nav_lock:
            await self._close_page_locked(
                context_id=context_id,
                reason=reason,
                causation_id=causation_id,
            )

    async def _close_page_locked(
        self,
        *,
        context_id: str,
        reason: str,
        causation_id: str | None = None,
    ) -> bool:
        draft = self._pages.begin_close_page(
            context_id=context_id,
            reason=reason,
        )
        if draft is None:
            logger.info("No owner for dynamic page")
            return False
        ok, _ = await self._execute_page_draft(
            draft,
            causation_id=causation_id,
        )
        if not ok:
            logger.warning("Dynamic page close rejected because restore frame is invalid")
        return ok

    async def clear_page(
        self,
        *,
        clear_outputs: bool = True,
        reason: str = "clear",
    ) -> None:
        async with self._nav_lock:
            await self._cancel_all_held_inputs()
            effects = self._pages.clear(reason=reason)
            self._sync_top_frame_state()
            for session in effects.sessions_to_close:
                await self._lifecycle.emit_page_closed(session, reason)
            await self._revoke_active_bindings(
                clear_outputs=clear_outputs,
                reason=f"{reason}_page",
            )
            await self._destroy_inactive_external_dynamic_page_children(
                effects.previous_dynamic_plans,
                reason=reason,
            )
            await self._lifecycle.destroy_all_action_instances(reason=reason)
            if clear_outputs:
                await self._clear_all_raster_controls()
            self._sync_top_frame_state()

    async def on_device_descriptor_changed(self, descriptor: DeviceDescriptor) -> None:
        """Re-resolve the active page against a changed device descriptor."""

        async with self._nav_lock:
            self.device = descriptor
            draft = self._pages.begin_refresh_current(
                refresh_actions=False,
                preserve_rebound_outputs=True,
            )
            if draft is None:
                return
            ok, _ = await self._execute_page_draft(
                draft,
                apply_effects=False,
            )
            if not ok:
                finalized_plans = await self._finalize_dynamic_page(
                    reason="device_descriptor_changed"
                )
                await self._revoke_active_bindings(
                    clear_outputs=False,
                    reason="device_descriptor_changed_failed",
                )
                await self._destroy_inactive_external_dynamic_page_children(
                    finalized_plans,
                    reason="device_descriptor_changed",
                )
                return

    async def on_capability_state_changed(
        self,
        event: hw_messages.CapabilityStateChangedMessage,
    ) -> None:
        logger.info(
            "Observed capability state change config=%s control=%s capability=%s state=%s",
            self.config_id,
            event.control_id,
            event.capability_id,
            event.state_type,
        )

    async def on_command_rejected(
        self,
        event: hw_messages.CommandRejectedMessage,
    ) -> None:
        logger.warning(
            "Hardware command rejected config=%s control=%s capability=%s command=%s reason=%s message=%s",
            self.config_id,
            event.control_id,
            event.capability_id,
            event.command_type,
            event.reason,
            event.message,
        )

    async def on_action_availability_changed(
        self,
        changed_keys: Iterable[ProviderActionKey] = (),
    ) -> None:
        """Refresh the current page availability overlay after provider availability changes."""
        changed_key_set = frozenset(changed_keys)
        async with self._nav_lock:
            draft = self._pages.begin_refresh_current(
                refresh_actions=True,
                preserve_rebound_outputs=False,
            )
            if draft is None or draft.retained_plan is None:
                logger.debug(
                    "Action availability page refresh skipped config=%s "
                    "changed_keys=%s reason=no_current_page",
                    self.config_id,
                    len(changed_key_set),
                )
                return
            if changed_key_set and not self._action_availability_change_affects_plan(
                changed_key_set,
                draft.retained_plan,
            ):
                logger.debug(
                    "Action availability page refresh decision config=%s page=%s "
                    "changed_keys=%s affected=False keys=%s",
                    self.config_id,
                    draft.retained_plan.page_id,
                    len(changed_key_set),
                    _format_provider_action_keys(changed_key_set),
                )
                return
            logger.debug(
                "Action availability page refresh decision config=%s page=%s "
                "changed_keys=%s affected=True keys=%s",
                self.config_id,
                draft.retained_plan.page_id,
                len(changed_key_set),
                _format_provider_action_keys(changed_key_set),
            )

            refreshed_plan = await self._build_page_plan(
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
                self.config_id,
                refreshed_plan.page_id,
            )

            self._pages.commit(draft, refreshed_plan)
            self._sync_top_frame_state()

            for planned in refreshed_plan.bindings:
                lease = self._binding_lease_for_control(planned.control_id)
                if planned.status == BindingPlanStatus.PENDING:
                    if lease is not None:
                        logger.debug(
                            "Preserving existing binding during pending action "
                            "availability config=%s page=%s control=%s action=%s "
                            "provider=%s binding=%s",
                            self.config_id,
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
                        self.device,
                        planned.control_id,
                        raster_capability_id=_selected_raster_capability_id(
                            planned.binding
                        ),
                    )
                    if control is not None:
                        await self._render_pending_to_control(control)
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
                        self.device,
                        planned.control_id,
                        raster_capability_id=_selected_raster_capability_id(
                            planned.binding
                        ),
                    )
                    if control is not None:
                        await self._render_unavailable_to_control(
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
                        self._action_service.consume_provider_lifecycle_recovery(
                            recovery_key
                        )
                        continue
                    if await self._activate_binding(lease):
                        await self._refresh_binding_output(
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
        preserve_page_owner = self._lease_is_active_dynamic_page_owner(
            lease,
            planned.action_meta,
        )
        await self._revoke_binding(
            lease.binding_id,
            clear_output=False,
            notify_provider=True,
            reason=reason,
            clear_held_input=True,
        )
        if preserve_page_owner and planned.action_meta is not None:
            self._move_dynamic_page_owner_provider_session(planned.action_meta)
            self._lifecycle.move_action_instance_provider_session(
                action_instance_id,
                planned.action_meta,
            )
        else:
            await self._lifecycle.destroy_action_instance(
                action_instance_id,
                reason=reason,
                notify_provider=True,
            )
        await self._install_planned_binding(plan, planned)

    def _lease_is_active_dynamic_page_owner(
        self,
        lease: BindingLease,
        action_meta: ActionMetadata | None,
    ) -> bool:
        session = self._pages.active_dynamic_session()
        return (
            session is not None
            and action_meta is not None
            and lease.page_session_id == session.page_session_id
            and lease.action_instance_id == session.action_instance_id
            and action_meta.uuid == session.owner_action_uuid
            and action_meta.provider_instance_id == session.owner_provider_instance_id
        )

    def _move_dynamic_page_owner_provider_session(
        self,
        action_meta: ActionMetadata,
    ) -> None:
        session = self._pages.active_dynamic_session()
        if session is None:
            return
        old_provider_session_id = session.owner_provider_session_id
        if not self._pages.move_owner_provider_session(action_meta):
            return
        logger.info(
            "Moving dynamic page owner provider session config=%s pageSession=%s "
            "action=%s provider=%s oldSession=%s newSession=%s",
            self.config_id,
            session.page_session_id,
            session.owner_action_uuid,
            session.owner_provider_instance_id,
            old_provider_session_id,
            action_meta.provider_session_id,
        )

    async def on_config_changed(self, config: DeviceConfig | None) -> None:
        """Handle config update or removal."""
        if config is None:
            self._pages.mark_config_inactive()
            await self.clear_page()
            return
        if config == self.config and self._pages.config_active:
            return
        async with self._nav_lock:
            await self._cancel_all_held_inputs()
            self.config = config
            draft = self._pages.update_config(config)
            for session in draft.sessions_to_close:
                await self._lifecycle.emit_page_closed(session, "config_change")
            await self._revoke_active_bindings(
                clear_outputs=False,
                reason="config_change",
            )
            await self._destroy_inactive_external_dynamic_page_children(
                draft.previous_dynamic_plans,
                reason="config_change",
            )
            await self._lifecycle.destroy_all_action_instances(
                reason="config_change"
            )
            ok, _ = await self._execute_page_draft(
                draft,
                apply_effects=False,
            )
            if not ok:
                self._pages.clear(reason="config_change")
                self._sync_top_frame_state()
                return

    async def handle_provider_command(self, msg: DeckrMessage) -> None:
        """Handle a canonical command message from an action provider."""
        await self._command_ingress.handle(msg)

    async def handle_hardware_input(self, message: DeckrMessage):
        event = hw_messages.hardware_body_from_message(message)
        translated = self._translator.translate(event, self.config_id)
        if translated is None:
            return
        if self._pages.active_dynamic_session() is not None:
            self._pages.record_activity()

        control_id = translated.control_id
        route_owner = self._attachments.active_input_for_event(
            control_id,
            translated.capability_id,
            translated.action_event.event_type,
        )
        if isinstance(route_owner, HeldInputRecord):
            lease = self._binding_leases.get(route_owner.binding_id)
            if lease is None:
                return
            await self._deliver_input_to_lease(lease, translated)
            return

        lease = route_owner
        if lease is None:
            if translated.action_event.event_type == "up":
                logger.info(
                    "Ignoring release without held owner config=%s control=%s capability=%s",
                    self.config_id,
                    control_id,
                    translated.capability_id,
                )
                return
            logger.info(
                "Ignoring input from unbound control config=%s control=%s capability=%s",
                self.config_id,
                control_id,
                translated.capability_id,
            )
            return
        if translated.capability_id not in lease.input_capability_ids:
            logger.info(
                "Ignoring input for unselected capability config=%s control=%s capability=%s",
                self.config_id,
                control_id,
                translated.capability_id,
            )
            return

        self._record_held_input_binding(translated, lease)
        await self._deliver_input_to_lease(lease, translated)

    async def _deliver_input_to_lease(self, lease: BindingLease, translated) -> None:
        if translated.capability_id not in lease.input_capability_ids:
            logger.info(
                "Ignoring input for unselected capability config=%s control=%s capability=%s",
                self.config_id,
                translated.control_id,
                translated.capability_id,
            )
            return
        try:
            await lease.context.on_input(translated.action_event)
        except Exception as e:
            logger.error(
                "Error delivering input to action %s: %s",
                lease.action_uuid,
                e,
                exc_info=True,
            )

    def _record_held_input_binding(self, translated, lease: BindingLease) -> None:
        if translated.action_event.event_type != "down":
            return
        self._attachments.record_held_input(
            lease=lease,
            control_id=translated.control_id,
            capability_id=translated.capability_id,
            down_event=translated.action_event,
        )
