import logging
import time
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import replace
from types import MappingProxyType

import anyio
from deckr.actions.messages import (
    ACTION_INSTANCE_DESTROYED,
    PAGE_SESSION_CLOSED,
    DynamicPageCommand,
    PageSessionMetadata,
)
from deckr.contracts.authority import ContractPointer
from deckr.contracts.messages import DeckrMessage
from deckr.hardware import messages as hw_messages
from deckr.hardware.descriptors import (
    DeviceDescriptor,
    DeviceRef,
)
from deckr.lanes import EndpointSession

from deckr.controller._action_interest import (
    ActionInterestSnapshot,
)
from deckr.controller._actions import (
    ActionIntentKey,
    ActionProviderManager,
    ControllerActionService,
    ProviderActionKey,
    ProviderSessionKey,
)
from deckr.controller._actions._requests import (
    BindingGrant,
    ClosePage,
    Navigate,
    OpenPage,
    PageGrant,
    SetRasterImage,
    SetTitle,
    UpdatePage,
)
from deckr.controller._actions._routing import action_command_metadata
from deckr.controller._binding_planner import (
    BindingPlanner,
)
from deckr.controller._bindings._action_lifecycle import (
    ActionInstanceLifecycleService,
)
from deckr.controller._bindings._attachments import (
    BindingLease,
)
from deckr.controller._bindings._commands import ProviderCommandIngress
from deckr.controller._bindings._context import (
    ControlContext,
    RuntimeMessageSender,
)
from deckr.controller._bindings._input import InputInteractions
from deckr.controller._bindings._models import (
    BindingActionSnapshot,
    HeldInputSnapshot,
    _binding_lease_snapshot,
    _control_context_snapshot,
)
from deckr.controller._bindings._planning import PagePlanning
from deckr.controller._bindings._ports import BindingActionService
from deckr.controller._bindings._presentation import BindingPresentation
from deckr.controller._bindings._reconcile import BindingReconciler
from deckr.controller._event_translator import EventTranslator
from deckr.controller._hardware import HardwareCommandService
from deckr.controller._interest_diagnostics import InterestDiagnostics
from deckr.controller._pages import (
    DynamicPageSession,
    PageSessionService,
)
from deckr.controller._pages._navigation import DeviceNavigation, NavigationBindings
from deckr.controller._render_dispatcher import (
    RenderBackend,
    RenderDispatcher,
    ThreadRenderBackend,
)
from deckr.controller._session_boundary import SessionBoundary, SessionEvent
from deckr.controller._session_effects import Effect, SessionEffects
from deckr.controller._session_state import BindingStatus, DeviceIdentity, DeviceState
from deckr.controller.action_provider.builtin import BUILTIN_ACTION_PROVIDER_ID
from deckr.controller.config._data import DeviceConfig
from deckr.controller.settings import SettingsService

logger = logging.getLogger(__name__)


class DeviceSession:
    """One device's application boundary and state owner.

    Navigation, binding reconciliation, input rules and presentation are focused
    collaborators over this state. Adapters run as effects; their results enter
    the same boundary as hardware, configuration and action requests.
    """

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
        runtime_sender: RuntimeMessageSender | None = None,
        page_timeout_check_interval: float = 0.25,
        config_stream: AsyncIterator[DeviceConfig | None] | None = None,
    ):
        self._config_stream = config_stream
        self._controller_id = controller_id
        self._endpoint = actions_bus
        self.hardware_ref = hardware_ref
        self.config_id = config.id
        self._command_service = command_service
        self.manager = manager
        self._start_soon = start_soon
        self._render_backend = render_backend or ThreadRenderBackend()
        self._clock = clock or time.monotonic
        self._runtime_sender = runtime_sender or self
        self._admitted_command = None
        self.state = DeviceState(
            identity=DeviceIdentity(hardware_ref),
            device=device,
            config=config,
            navigation=pages or PageSessionService(config, clock=self._clock),
            hardware_claim=(
                command_service.claim_identity(config.id)
                if hasattr(command_service, "claim_identity")
                else None
            ),
        )
        self._effects = SessionEffects(start_soon)
        self._attachments = self.state.attachments
        self.boundary = SessionBoundary()
        self._render_dispatcher = RenderDispatcher(
            command_service=command_service,
            config_id=self.config_id,
            backend=self._render_backend,
            start_soon=start_soon,
            result_authorizer=self._render_output_authorized,
            completion_transaction=lambda: self.boundary.event(
                SessionEvent("presentation")
            ),
        )
        self._settings_service = settings_service
        self._translator = EventTranslator(controller_id=controller_id)
        self._pages = self.state.navigation
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
        self.presentation = BindingPresentation(
            state=self.state,
            hardware=self._command_service,
            catalog=self._action_service,
            dispatcher=self._render_dispatcher,
            clock=self._clock,
            planner=self._binding_planner,
        )
        self.inputs = InputInteractions(
            state=self.state,
            effects=self._effects,
            translator=self._translator,
            catalog=self._action_service,
            hardware=self._command_service,
        )
        self.planning = PagePlanning(
            state=self.state,
            catalog=self._action_service,
            planner=self._binding_planner,
            clock=self._clock,
        )
        self.interests = InterestDiagnostics(
            state=self.state,
            catalog=self._action_service,
            planner=self._binding_planner,
            clock=self._clock,
        )
        self._binding_leases = self._attachments.binding_leases
        self._lifecycle = ActionInstanceLifecycleService(
            config_id=self.config_id,
            runtime_sender=self._runtime_sender,
            notification_sender=_LifecycleNotifications(
                self._effects, self._runtime_sender
            ),
            effects=self._effects,
            authority_current=lambda: self.state.hardware_current(
                self._command_service
            ),
            schedule_recovery=lambda lease: self.bindings.recover_attachment(lease),
            availability_recorder=self._action_service,
            host=self,
            clock=self._clock,
        )
        self.bindings = BindingReconciler(
            state=self.state,
            boundary=self.boundary,
            effects=self._effects,
            lifecycle=self._lifecycle,
            catalog=self._action_service,
            registry=manager,
            settings=settings_service,
            runtime_sender=self._runtime_sender,
            scoped_requests=self.scoped_requests,
            start_soon=start_soon,
            controller_id=controller_id,
            hardware=command_service,
            presentation=self.presentation,
            planning=self.planning,
            inputs=self.inputs,
            interests=self.interests,
            close_page=self.close_page,
        )
        self.navigation = DeviceNavigation(
            state=self.state,
            boundary=self.boundary,
            lifecycle=self._lifecycle,
            clock=self._clock,
            interval=page_timeout_check_interval,
            bindings=NavigationBindings(
                build_plan=self.planning._build_page_plan,
                prepare_commit=self.bindings._prepare_page_commit,
                apply_commit=self.bindings._apply_page_commit,
                release_children=self.bindings._destroy_inactive_external_dynamic_page_children,
                sync_interest=self._sync_top_frame_state,
                cancel_inputs=self.inputs._cancel_all_held_inputs,
                revoke_bindings=self.bindings._revoke_active_bindings,
                clear_outputs=self.presentation._clear_all_raster_controls,
            ),
        )
        self._command_ingress = ProviderCommandIngress(
            host=self,
            lifecycle=self._lifecycle,
        )
        self._page_timeout_check_interval = page_timeout_check_interval
        from deckr.controller._bindings._rendering import (
            BindingRenderer,
            RenderingPorts,
        )

        self._rendering = BindingRenderer(
            RenderingPorts(
                endpoint=self._endpoint,
                hardware=self._command_service,
                actions=self._action_service,
                dispatcher=self._render_dispatcher,
                backend=self._render_backend,
                boundary=self.boundary,
                start_soon=start_soon,
                config_id=self.config_id,
                hardware_ref=self.hardware_ref,
                notify=self._queue_render_result,
                is_active=lambda: self.config_active
                and self.state.hardware_current(self._command_service),
                binding_by_id=self.binding_by_id,
                binding_output_authorized=self.binding_output_authorized,
                current_contract=self.current_contract,
            )
        )
        from deckr.controller._bindings._page_commands import PageCommandPorts

        self._page_commands = PageCommandPorts(
            endpoint=self._endpoint,
            hardware=self._command_service,
            actions=self._action_service,
            navigation=self.navigation,
            boundary=self.boundary,
            state=self.state,
            binding_by_id=self.binding_by_id,
            binding_command_authorized=self.binding_command_authorized,
            provider_session_key_for_session=self.provider_session_key_for_session,
            current_contract=self.current_contract,
            emit_page_opened=self._lifecycle.emit_page_opened,
        )
        self.interests._sync_action_interest()

    def scoped_requests(self, metadata, *, may_navigate=False):
        return _ScopedActionRequests(
            self,
            BindingGrant(
                self.identity.session_id,
                metadata.binding_id,
                metadata.context_id,
                metadata.action_instance_id,
                may_navigate=may_navigate,
            ),
        )

    def _queue_render_result(self, message, result) -> None:
        self._effects.submit(
            "render_results",
            Effect(
                "render result",
                lambda: self._action_service.send_render_result(message, result),
            ),
        )

    @property
    def device(self):
        return self.state.device

    @property
    def config(self):
        return self.state.config

    @property
    def identity(self) -> DeviceIdentity:
        return self.state.identity

    async def wait_idle(self) -> None:
        await self._effects.idle()

    @property
    def config_active(self) -> bool:
        return self._pages.config_active

    async def start(
        self,
        tg: anyio.abc.TaskGroup,
        stopping: anyio.Event,
    ) -> None:
        self._start_soon = tg.start_soon
        self._effects.set_runner(tg.start_soon)
        self.bindings.start_soon = tg.start_soon
        self._render_dispatcher.set_runner(tg.start_soon)
        self._rendering.ports = replace(self._rendering.ports, start_soon=tg.start_soon)
        tg.start_soon(self.navigation._page_timeout_loop, stopping)
        tg.start_soon(self._config_listener)

    async def aclose(self) -> None:
        async with self.boundary.event(SessionEvent("hardware_lost")):
            if self.state.stopped:
                return
            self.state.stopped = True
            self._pages.mark_config_inactive()
            await self.clear_page(clear_outputs=False, reason="device_session_ended")
            self.state.operations.clear()
        # Remote cleanup is bounded and never holds local authority open.
        with anyio.move_on_after(1):
            await self.wait_idle()

    async def _config_listener(self) -> None:
        if self._config_stream is not None:
            async for config in self._config_stream:
                await self.on_config_changed(config)

    def snapshot(self) -> BindingActionSnapshot:
        plan = self._pages.current_plan()
        control_status = {}
        for planned in plan.bindings if plan is not None else ():
            lease = self._attachments.binding_for_control(planned.control_id)
            control_status[planned.control_id] = (
                self.state.binding_status.get(
                    lease.binding_id, BindingStatus.UNAVAILABLE
                )
                if lease is not None
                else BindingStatus.UNAVAILABLE
            )
        active_contexts = {
            lease.control_id: _control_context_snapshot(lease)
            for lease in self._binding_leases.values()
            if lease.ready and self._attachments.binding_command_authorized(lease)
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
            control_status=MappingProxyType(control_status),
        )

    def context_for_control(self, control_id: str) -> ControlContext | None:
        lease = self._attachments.binding_for_control(control_id)
        if (
            lease is None
            or not lease.ready
            or not self._attachments.binding_command_authorized(lease)
        ):
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

    def _render_output_authorized(self, control_id, binding_id, context_id):
        if not self.state.hardware_current(self._command_service):
            return False
        if not self._attachments.output_render_authorized(
            control_id, binding_id, context_id
        ):
            return False
        if binding_id is None:
            return True
        lease = self.binding_by_id(binding_id)
        if lease is None:
            return False
        if lease.provider_instance_id == BUILTIN_ACTION_PROVIDER_ID:
            return True
        return (
            lease.context.contract is not None
            and self.current_contract(lease.provider_session_key)
            == lease.context.contract
            and self._command_service.has_authority(self.config_id, self.hardware_ref)
        )

    async def revoke_binding(
        self,
        binding_id: str,
        *,
        clear_output: bool = True,
        notify_provider: bool = True,
        reason: str = "detach",
        clear_held_input: bool = False,
    ) -> BindingLease | None:
        async with self.boundary.event(SessionEvent("binding_revoked", binding_id)):
            return await self.bindings.revoke_binding(
                binding_id,
                clear_output=clear_output,
                notify_provider=notify_provider,
                reason=reason,
                clear_held_input=clear_held_input,
            )

    def _sync_top_frame_state(self) -> None:
        self.interests._sync_action_interest()

    def action_interest_snapshot(
        self,
        *,
        now: float | None = None,
    ) -> ActionInterestSnapshot:
        return self.interests.action_interest_snapshot(now=now)

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
        # Managed authorization is performed by the ingress adapter before this
        # local transition. Recheck the exact agreement and admitted message.
        return (
            self._admitted_command == (msg.message_id, key)
            and msg.contract is not None
            and self.current_contract(key) == msg.contract
        )

    def planned_intent_for_lease(self, lease: BindingLease) -> ActionIntentKey:
        return self.bindings.planned_intent_for_lease(lease)

    async def recover_binding_provider_session_contract(
        self,
        lease: BindingLease,
        *,
        reason: str,
    ) -> None:
        return await self.bindings.recover_binding_provider_session_contract(
            lease, reason=reason
        )

    async def page_expired(self, deadline, *, now: float) -> None:
        return await self.navigation.page_expired(deadline, now=now)

    def page_session_metadata(
        self,
        session: DynamicPageSession,
    ) -> PageSessionMetadata:
        return self.navigation.page_session_metadata(session)

    async def set_page(
        self,
        *,
        profile: str | None = None,
        page: int | None = None,
        descriptor: DynamicPageCommand | None = None,
        causation_id: str | None = None,
    ) -> bool:
        return await self.navigation.set_page(
            profile=profile, page=page, descriptor=descriptor, causation_id=causation_id
        )

    async def open_page(
        self,
        *,
        descriptor: DynamicPageCommand,
        context_id: str,
        binding_id: str | None = None,
        causation_id: str | None = None,
    ) -> DynamicPageSession | None:
        return await self.navigation.open_page(
            descriptor=descriptor,
            context_id=context_id,
            binding_id=binding_id,
            causation_id=causation_id,
        )

    async def replace_page(
        self,
        *,
        descriptor: DynamicPageCommand,
        context_id: str,
        causation_id: str | None = None,
    ) -> None:
        return await self.navigation.replace_page(
            descriptor=descriptor, context_id=context_id, causation_id=causation_id
        )

    async def close_page(
        self,
        *,
        context_id: str,
        reason: str = "close",
        causation_id: str | None = None,
    ) -> None:
        return await self.navigation.close_page(
            context_id=context_id, reason=reason, causation_id=causation_id
        )

    async def clear_page(
        self,
        *,
        clear_outputs: bool = True,
        reason: str = "clear",
    ) -> None:
        return await self.navigation.clear_page(
            clear_outputs=clear_outputs, reason=reason
        )

    async def on_device_descriptor_changed(self, descriptor: DeviceDescriptor) -> None:
        return await self.navigation.on_device_descriptor_changed(descriptor)

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
        return await self.bindings.on_action_availability_changed(changed_keys)

    async def on_config_changed(self, config: DeviceConfig | None) -> None:
        return await self.navigation.on_config_changed(config)

    async def render_direct_output(self, lease, body, message):
        await self._rendering.direct_output(lease, body, message)

    async def handle_render_command(self, message: DeckrMessage) -> bool:
        return await self._rendering.handle(message)

    async def handle_page_command(self, message: DeckrMessage) -> bool:
        from deckr.controller._bindings._page_commands import handle_page_command

        return await handle_page_command(self._page_commands, message)

    async def handle_provider_command(self, msg: DeckrMessage) -> None:
        """Handle a canonical command message from an action provider."""
        if msg.lane == "services" and msg.message_type == "serviceMessage":
            if await self.handle_render_command(msg):
                return
            await self.handle_page_command(msg)
            return
        try:
            metadata = action_command_metadata(msg)
        except (ValueError, TypeError):
            return
        if metadata is None:
            return
        key = self.provider_session_key_for_session(
            provider_instance_id=metadata.provider_instance_id,
            provider_id=metadata.provider_id,
            provider_session_id=msg.sender_session_id,
        )
        authorized = await self._action_service.authorize_decoded_message(key, msg)
        async with self.boundary.event(SessionEvent("action_request", msg)):
            if self.state.stopped:
                return
            self._admitted_command = (msg.message_id, key) if authorized else None
            try:
                await self._command_ingress.handle(msg)
            finally:
                self._admitted_command = None

    def _page_grant(self, session) -> PageGrant:
        return PageGrant(
            self.identity.session_id,
            session.page_session_id,
            session.context_id,
            session.action_instance_id,
            session.generation,
        )

    async def handle_action_request(self, grant: BindingGrant, request):
        async with self.boundary.event(SessionEvent("action_request", request)):
            if (
                self.state.stopped
                or grant.device_session_id != self.identity.session_id
                or not self.config_active
                or not self.state.hardware_current(self._command_service)
            ):
                return None
            if isinstance(request, UpdatePage | ClosePage):
                session = self.active_page_session()
                if (
                    session is None
                    or request.grant != self._page_grant(session)
                    or grant.action_instance_id != session.action_instance_id
                ):
                    return None
                key = self.provider_session_key_for_session(
                    provider_instance_id=session.owner_provider_instance_id,
                    provider_id=session.owner_provider_id,
                    provider_session_id=session.owner_provider_session_id,
                )
                if (
                    session.owner_contract is not None
                    and self.current_contract(key) != session.owner_contract
                ):
                    return None
                draft = self._pages.begin_action_request(request)
                if draft is None:
                    return None
                ok, _ = await self.navigation._execute_page_draft(draft)
                if not ok:
                    return None
                if isinstance(request, ClosePage):
                    return True
                await self._lifecycle.emit_page_opened(session)
                return self._page_grant(session)
            lease = self.binding_by_id(grant.binding_id)
            if (
                lease is None
                or lease.context_id != grant.context_id
                or not self.binding_command_authorized(lease)
                or not self.bindings._lease_uses_current_provider_session_contract(
                    lease
                )
            ):
                return None
            if isinstance(request, Navigate):
                if grant.may_navigate:
                    return await self.set_page(
                        profile=request.profile, page=request.page
                    )
            elif isinstance(request, OpenPage):
                session = await self.open_page(
                    descriptor=request.definition,
                    context_id=grant.context_id,
                    binding_id=grant.binding_id,
                )
                return self._page_grant(session) if session is not None else None
            elif isinstance(request, SetTitle):
                if self.binding_output_authorized(lease):
                    await lease.context.set_title(request.text)
            elif isinstance(request, SetRasterImage):
                if self.binding_output_authorized(lease):
                    await lease.context.set_raster_image(request.image)
            return None

    async def handle_hardware_input(self, message: DeckrMessage):
        async with self.boundary.event(SessionEvent("hardware_input", message)):
            if self.state.hardware_current(self._command_service):
                await self.inputs._route_hardware_input(message)


class _LifecycleNotifications:
    """Snapshot notifications are effects, ordered with the instance's other work."""

    def __init__(self, effects, sender):
        self.effects = effects
        self.sender = sender

    async def send_action_runtime_message(
        self, *, provider_session_key, message_type, body
    ):
        metadata = getattr(body, "metadata", None) or getattr(
            body, "page_session", None
        )
        resource = metadata.action_instance_id
        return self.effects.submit(
            resource,
            Effect(
                message_type,
                lambda: self.sender.send_action_runtime_message(
                    provider_session_key=provider_session_key,
                    message_type=message_type,
                    body=body,
                ),
                cleanup=message_type
                in {ACTION_INSTANCE_DESTROYED, PAGE_SESSION_CLOSED},
            ),
        )


class _ScopedActionRequests:
    def __init__(self, session, grant):
        self._submit = session.handle_action_request
        self._grant = grant

    async def request(self, request):
        return await self._submit(self._grant, request)
