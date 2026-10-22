import logging
import time
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import replace

import anyio
from deckr.actions.messages import (
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
    BuiltinActionCatalog,
    ProviderActionKey,
    ProviderSessionKey,
)
from deckr.controller._actions._requests import (
    BindingGrant,
)
from deckr.controller._actions._routing import ActionRuntimeCommand
from deckr.controller._bindings._attachments import (
    BindingLease,
)
from deckr.controller._bindings._context import (
    ControlContext,
    RuntimeMessageSender,
)
from deckr.controller._bindings._models import (
    BindingActionSnapshot,
)
from deckr.controller._bindings._ports import BindingActionService
from deckr.controller._bindings._requests import ScopedActionRequests
from deckr.controller._device_components import build_device_components
from deckr.controller._device_snapshot import binding_snapshot
from deckr.controller._hardware import HardwareCommandService
from deckr.controller._pages import (
    DynamicPageSession,
    PageSessionService,
)
from deckr.controller._render_workers import RenderBackend, ThreadRenderBackend
from deckr.controller._session_effects import Effect
from deckr.controller._session_state import DeviceIdentity, DeviceState
from deckr.controller._state_gate import StateGate
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
        manager: BuiltinActionCatalog,
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
            hardware_claim=(command_service.claim_identity(config.id)),
        )
        self.boundary = StateGate()
        self.components = build_device_components(
            self,
            controller_id=controller_id,
            command_service=command_service,
            manager=manager,
            actions_bus=actions_bus,
            start_soon=start_soon,
            settings_service=settings_service,
            action_service=action_service,
            page_timeout_check_interval=page_timeout_check_interval,
        )
        self.components.interests._sync_action_interest()

    def scoped_requests(self, metadata, *, may_navigate=False):
        return ScopedActionRequests(
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
        self.components.effects.submit(
            "render_results",
            Effect(
                "render result",
                lambda: self.components.actions.send_render_result(message, result),
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
        await self.components.effects.idle()

    @property
    def config_active(self) -> bool:
        return self.state.navigation.config_active

    async def start(
        self,
        tg: anyio.abc.TaskGroup,
        stopping: anyio.Event,
    ) -> None:
        self._start_soon = tg.start_soon
        self.components.effects.set_runner(tg.start_soon)
        self.components.bindings.installer.start_soon = tg.start_soon
        self.components.dispatcher.set_runner(tg.start_soon)
        self.components.rendering.ports = replace(
            self.components.rendering.ports, start_soon=tg.start_soon
        )
        tg.start_soon(self.components.navigation._page_timeout_loop, stopping)
        tg.start_soon(self._config_listener)

    async def aclose(self) -> None:
        async with self.boundary:
            if self.state.stopped:
                return
            self.state.stopped = True
            self.state.navigation.mark_config_inactive()
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
        return binding_snapshot(
            self.state, self.components.lifecycle, self.planned_intent_for_lease
        )

    def context_for_control(self, control_id: str) -> ControlContext | None:
        lease = self.state.attachments.binding_for_control(control_id)
        if (
            lease is None
            or not lease.ready
            or not self.state.attachments.binding_command_authorized(lease)
        ):
            return None
        return lease.context

    def binding_by_id(self, binding_id: str) -> BindingLease | None:
        return self.state.attachments.binding_leases.get(binding_id)

    def iter_binding_leases(self) -> Iterable[BindingLease]:
        return tuple(self.state.attachments.binding_leases.values())

    def active_page_session(self) -> DynamicPageSession | None:
        return self.state.navigation.active_dynamic_session()

    def binding_command_authorized(self, lease: BindingLease) -> bool:
        return self.state.attachments.binding_command_authorized(lease)

    def binding_output_authorized(self, lease: BindingLease) -> bool:
        return self.state.attachments.binding_output_authorized(lease)

    def _render_output_authorized(self, control_id, binding_id, context_id):
        if not self.state.hardware_current(self._command_service):
            return False
        if not self.state.attachments.output_render_authorized(
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
        async with self.boundary:
            return await self.components.bindings.revoke_binding(
                binding_id,
                clear_output=clear_output,
                notify_provider=notify_provider,
                reason=reason,
                clear_held_input=clear_held_input,
            )

    def _sync_top_frame_state(self) -> None:
        self.components.interests._sync_action_interest()

    def action_interest_snapshot(
        self,
        *,
        now: float | None = None,
    ) -> ActionInterestSnapshot:
        return self.components.interests.action_interest_snapshot(now=now)

    def current_contract(
        self,
        key: ProviderSessionKey | None,
    ) -> ContractPointer | None:
        return self.components.actions.current_contract(key)

    async def send_action_runtime_message(
        self,
        *,
        provider_session_key: ProviderSessionKey | None,
        contract: ContractPointer | None,
        causation_id: str | None = None,
        message_type: str,
        body,
    ) -> bool:
        if provider_session_key is None:
            return False
        return await self.components.actions.send_runtime_message(
            provider_session_key,
            contract=contract,
            causation_id=causation_id,
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
        return self.components.bindings.planned_intent_for_lease(lease)

    async def recover_binding_provider_session_contract(
        self,
        lease: BindingLease,
        *,
        reason: str,
    ) -> None:
        return await self.components.bindings.recover_binding_provider_session_contract(
            lease, reason=reason
        )

    async def page_expired(self, deadline, *, now: float) -> None:
        return await self.components.navigation.page_expired(deadline, now=now)

    def page_session_metadata(
        self,
        session: DynamicPageSession,
    ) -> PageSessionMetadata:
        return self.components.navigation.page_session_metadata(session)

    async def set_page(
        self,
        *,
        profile: str | None = None,
        page: int | None = None,
        descriptor: DynamicPageCommand | None = None,
        causation_id: str | None = None,
    ) -> bool:
        return await self.components.navigation.set_page(
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
        return await self.components.navigation.open_page(
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
        return await self.components.navigation.replace_page(
            descriptor=descriptor, context_id=context_id, causation_id=causation_id
        )

    async def close_page(
        self,
        *,
        context_id: str,
        reason: str = "close",
        causation_id: str | None = None,
    ) -> None:
        return await self.components.navigation.close_page(
            context_id=context_id, reason=reason, causation_id=causation_id
        )

    async def clear_page(
        self,
        *,
        clear_outputs: bool = True,
        reason: str = "clear",
    ) -> None:
        return await self.components.navigation.clear_page(
            clear_outputs=clear_outputs, reason=reason
        )

    async def on_device_descriptor_changed(self, descriptor: DeviceDescriptor) -> None:
        return await self.components.navigation.on_device_descriptor_changed(descriptor)

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
        return (
            await self.components.bindings.availability.on_action_availability_changed(
                changed_keys
            )
        )

    async def on_config_changed(self, config: DeviceConfig | None) -> None:
        return await self.components.navigation.on_config_changed(config)

    async def render_direct_output(self, lease, body, message):
        await self.components.rendering.direct_output(lease, body, message)

    async def handle_render_command(self, message: DeckrMessage) -> bool:
        return await self.components.rendering.handle(message)

    async def handle_page_command(self, message: DeckrMessage) -> bool:
        from deckr.controller._bindings._page_commands import handle_page_command

        return await handle_page_command(self.components.page_commands, message)

    async def handle_provider_command(self, msg: DeckrMessage) -> None:
        if await self.handle_render_command(msg):
            return
        if await self.handle_page_command(msg):
            return
        command = await self.components.actions.decode_inbound_runtime_message(msg)
        if command is not None:
            await self.handle_action_command(command)

    async def handle_action_command(self, command: ActionRuntimeCommand) -> None:
        msg, metadata = command.envelope, command.metadata
        if metadata is None:
            return
        key = self.provider_session_key_for_session(
            provider_instance_id=metadata.provider_instance_id,
            provider_id=metadata.provider_id,
            provider_session_id=msg.sender_session_id,
        )
        authorized = await self.components.actions.authorize_runtime_message(key, msg)
        async with self.boundary:
            if self.state.stopped:
                return
            self._admitted_command = (msg.message_id, key) if authorized else None
            try:
                await self.components.commands.handle(command)
            finally:
                self._admitted_command = None

    async def handle_action_request(self, grant: BindingGrant, request):
        return await self.components.requests.handle(grant, request)

    async def handle_hardware_input(self, message: DeckrMessage):
        async with self.boundary:
            if self.state.hardware_current(self._command_service):
                await self.components.inputs._route_hardware_input(message)
