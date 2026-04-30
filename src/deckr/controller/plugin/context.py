import logging
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from deckr.contracts.models import thaw_json
from deckr.hardware.descriptors import DeviceDescriptor
from deckr.pluginhost.messages import (
    BINDING_ATTACHED,
    BINDING_DETACHED,
    CAPABILITY_INPUT,
    BindingAttachedBody,
    BindingDetachedBody,
    BindingMetadata,
    CapabilityInputBody,
    CapabilityInputEvent,
    TitleOptions,
    context_subject,
    controller_address,
    host_address,
    plugin_message,
)

from deckr.controller._command_router import CommandRouter, DeviceOutput
from deckr.controller._device_layout import ControlSurface
from deckr.controller._hardware_service import HardwareCommandService
from deckr.controller._render import RenderService
from deckr.controller._render_dispatcher import RenderDispatcher
from deckr.controller._state_store import ControlStateStore
from deckr.controller.plugin.builtin._context import BuiltInPluginContext
from deckr.controller.settings import SettingsService, SettingsTarget

if TYPE_CHECKING:
    from deckr.controller._device_manager import DeviceManager
    from deckr.controller.plugin.builtin import BuiltinAction

logger = logging.getLogger(__name__)


class ControlContext:
    """Controller-owned lease context for one visible binding."""

    def __init__(
        self,
        controller_id: str,
        device: DeviceDescriptor,
        config_id: str,
        command_service: HardwareCommandService,
        host_id: str,
        action_uuid: str,
        control: ControlSurface,
        settings: Mapping[str, Any],
        manager: "DeviceManager",
        plugin_bus: Any,
        start_soon: Callable[..., None],
        render_dispatcher: RenderDispatcher,
        settings_service: SettingsService | None,
        context_settings_target: SettingsTarget | None,
        *,
        profile_id: str,
        page_id: str,
        title_options: TitleOptions | None = None,
        builtin_action: "BuiltinAction | None" = None,
        metadata: BindingMetadata,
    ):
        self._controller_id = controller_id
        self.device = device
        self.config_id = config_id
        self._command_service = command_service
        self.host_id = host_id
        self.action_uuid = action_uuid
        self.action_instance_id = metadata.action_instance_id
        self.binding_id = metadata.binding_id
        self._context_id = metadata.context_id
        self.page_session_id = metadata.page_session_id
        self._builtin_action = builtin_action
        self.metadata = metadata
        self.control = control
        self.manager = manager
        self._plugin_bus = plugin_bus
        self.profile_id = profile_id
        self.page_id = page_id
        self.settings_target = context_settings_target

        self._store = ControlStateStore(
            context_id=metadata.context_id,
            binding_id=metadata.binding_id,
        )
        self._store.settings = dict(thaw_json(settings))
        self._store.default_title_options = title_options

        output = (
            DeviceOutput(
                command_service,
                config_id,
                control.id,
                control.raster_capability_id,
            )
            if control.raster_capability_id is not None
            else None
        )
        self._router = CommandRouter(
            store=self._store,
            render_service=RenderService(),
            render_dispatcher=render_dispatcher,
            output=output,
            image_format=control.image_format,
            start_soon=start_soon,
            settings_service=settings_service,
            settings_target=context_settings_target,
        )
        self.plugin_context = BuiltInPluginContext(
            router=self._router,
            manager=manager,
            context_id=self.id,
            binding_metadata=metadata,
            settings_service=settings_service,
        )

    @property
    def id(self) -> str:
        return self._context_id

    @property
    def settings(self) -> Mapping[str, Any]:
        return self._store.settings

    async def _publish(self, message_type: str, body: Mapping[str, Any] | Any) -> None:
        msg = plugin_message(
            sender=controller_address(self._controller_id),
            recipient=host_address(self.host_id),
            message_type=message_type,
            body=body,
            subject=context_subject(
                self.id,
                config_id=self.config_id,
                action_instance_id=self.action_instance_id,
                binding_id=self.binding_id,
                page_session_id=self.page_session_id,
                action_uuid=self.action_uuid,
            ),
        )
        await self._plugin_bus.publish(msg)

    async def on_binding_attached(self) -> None:
        await self._router.hydrate_settings()
        if self._builtin_action is not None:
            await self._builtin_action.on_bind(self.plugin_context)
            return
        await self._publish(
            BINDING_ATTACHED,
            BindingAttachedBody(
                binding=self.metadata,
                settings=self._store.settings,
            ),
        )

    async def on_binding_detached(self, reason: str) -> None:
        if self._builtin_action is not None:
            await self._builtin_action.on_unbind(self.plugin_context, reason)
            return
        await self._publish(
            BINDING_DETACHED,
            BindingDetachedBody(binding=self.metadata, reason=reason),
        )

    async def on_input(self, event: CapabilityInputEvent) -> None:
        if self._builtin_action is not None:
            await self._builtin_action.on_input(self.plugin_context, event)
            return
        await self._publish(
            CAPABILITY_INPUT,
            CapabilityInputBody(binding=self.metadata, event=event),
        )
