import time
from collections.abc import AsyncIterator, Callable, Iterable

import anyio
from deckr.actions.messages import DynamicPageCommand
from deckr.contracts.messages import DeckrMessage
from deckr.hardware import messages as hw_messages
from deckr.hardware.descriptors import DeviceDescriptor, DeviceRef
from deckr.lanes import EndpointSession

from deckr.controller._action_interest import ActionInterestSnapshot
from deckr.controller._actions import (
    ActionProviderManager,
    ControllerActionService,
    ProviderActionKey,
)
from deckr.controller._bindings import (
    BindingActionSnapshot,
    ControlBindingService,
    ControlContext,
)
from deckr.controller._hardware import HardwareCommandService
from deckr.controller._pages import DynamicPageSession, PageSessionService
from deckr.controller._render_dispatcher import RenderBackend
from deckr.controller.config._data import DeviceConfig
from deckr.controller.settings import SettingsService


class DeviceManager:
    """Thin per-device façade for config/device/page orchestration."""

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
        config_stream: AsyncIterator[DeviceConfig | None] | None = None,
        clock: Callable[[], float] | None = None,
        action_service: ControllerActionService | None = None,
        page_timeout_check_interval: float = 0.25,
    ):
        self._controller_id = controller_id
        self.device = device
        self.hardware_ref = hardware_ref
        self.config_id = config.id
        self.config = config
        self.manager = manager
        self._config_stream = config_stream
        self._clock = clock or time.monotonic
        self._pages = PageSessionService(config, clock=self._clock)
        self._action_service = action_service or ControllerActionService(
            controller_id=controller_id,
            controller_session_id=actions_bus.session_id,
            manager=manager,
            start_soon=None,
            clock=self._clock,
        )
        self._bindings = ControlBindingService(
            controller_id=controller_id,
            device=device,
            hardware_ref=hardware_ref,
            command_service=command_service,
            config=config,
            manager=manager,
            actions_bus=actions_bus,
            start_soon=start_soon,
            render_backend=render_backend,
            settings_service=settings_service,
            clock=self._clock,
            action_service=self._action_service,
            pages=self._pages,
            page_command_port=self,
            page_timeout_check_interval=page_timeout_check_interval,
        )

    @property
    def config_active(self) -> bool:
        return self._pages.config_active

    @property
    def bindings(self) -> ControlBindingService:
        return self._bindings

    def snapshot(self) -> BindingActionSnapshot:
        return self._bindings.snapshot()

    def context_for_control(self, control_id: str) -> ControlContext | None:
        return self._bindings.context_for_control(control_id)

    def action_interest_snapshot(
        self,
        *,
        now: float | None = None,
    ) -> ActionInterestSnapshot:
        return self._bindings.action_interest_snapshot(now=now)

    async def start(
        self,
        tg: anyio.abc.TaskGroup,
        stopping: anyio.Event,
    ) -> None:
        await self._bindings.start(tg, stopping)

    async def _config_listener(self) -> None:
        if self._config_stream is None:
            return
        async for config in self._config_stream:
            await self.on_config_changed(config)

    async def on_config_changed(self, config: DeviceConfig | None) -> None:
        await self._bindings.on_config_changed(config)
        if config is not None:
            self.config = config

    async def set_page(
        self,
        *,
        profile: str | None = None,
        page: int | None = None,
        descriptor: DynamicPageCommand | None = None,
        causation_id: str | None = None,
    ) -> bool:
        return await self._bindings.set_page(
            profile=profile,
            page=page,
            descriptor=descriptor,
            causation_id=causation_id,
        )

    async def open_page(
        self,
        *,
        descriptor: DynamicPageCommand,
        context_id: str,
        binding_id: str | None = None,
        causation_id: str | None = None,
    ) -> DynamicPageSession | None:
        return await self._bindings.open_page(
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
        await self._bindings.replace_page(
            descriptor=descriptor,
            context_id=context_id,
            causation_id=causation_id,
        )

    async def close_page(
        self,
        *,
        context_id: str,
        reason: str = "close",
        causation_id: str | None = None,
    ) -> None:
        await self._bindings.close_page(
            context_id=context_id,
            reason=reason,
            causation_id=causation_id,
        )

    async def clear_page(
        self,
        *,
        clear_outputs: bool = True,
        reason: str = "clear",
    ) -> None:
        await self._bindings.clear_page(
            clear_outputs=clear_outputs,
            reason=reason,
        )

    async def on_device_descriptor_changed(self, descriptor: DeviceDescriptor) -> None:
        self.device = descriptor
        await self._bindings.on_device_descriptor_changed(descriptor)

    async def on_capability_state_changed(
        self,
        event: hw_messages.CapabilityStateChangedMessage,
    ) -> None:
        await self._bindings.on_capability_state_changed(event)

    async def on_command_rejected(
        self,
        event: hw_messages.CommandRejectedMessage,
    ) -> None:
        await self._bindings.on_command_rejected(event)

    async def on_action_availability_changed(
        self,
        changed_keys: Iterable[ProviderActionKey] = (),
    ) -> None:
        await self._bindings.on_action_availability_changed(changed_keys)

    async def handle_provider_command(self, msg: DeckrMessage) -> None:
        await self._bindings.handle_provider_command(msg)

    async def handle_hardware_input(self, message: DeckrMessage) -> None:
        await self._bindings.handle_hardware_input(message)
