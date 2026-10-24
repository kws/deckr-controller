import logging
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any, Protocol

from deckr.actions.messages import (
    BindingMetadata,
    CapabilityInputEvent,
    DynamicPageCommand,
    SettingsTargetRef,
)
from deckr.contracts.authority import ContractPointer
from deckr.contracts.models import thaw_json
from deckr.hardware.descriptors import DeviceDescriptor

from deckr.controller._actions import ProviderSessionKey
from deckr.controller._actions._gateway import (
    ActionGateway,
    LocalActionAdapter,
    RemoteActionAdapter,
)
from deckr.controller._actions._requests import ActionRequestPort
from deckr.controller._device_layout import ControlLayout
from deckr.controller._hardware import HardwareCommandService
from deckr.controller._pages import DynamicPageSession
from deckr.controller._presentation import DeviceOutput, PresentationController
from deckr.controller._render import RenderService, RenderSource
from deckr.controller._render_dispatcher import RenderDispatcher
from deckr.controller._state_store import ControlStateStore
from deckr.controller.action_provider.builtin._context import ControllerActionContext

if TYPE_CHECKING:
    from deckr.controller.action_provider.builtin import BuiltinAction

logger = logging.getLogger(__name__)


class RuntimeMessageSender(Protocol):
    async def send_action_runtime_message(
        self,
        *,
        provider_session_key: ProviderSessionKey | None,
        contract: ContractPointer | None,
        causation_id: str | None = None,
        message_type: str,
        body: Any,
    ) -> bool: ...


class PageCommandPort(Protocol):
    async def set_page(
        self,
        *,
        profile: str | None = None,
        page: int | None = None,
        descriptor: DynamicPageCommand | None = None,
        causation_id: str | None = None,
    ) -> bool: ...

    async def open_page(
        self,
        *,
        descriptor: DynamicPageCommand,
        context_id: str,
        binding_id: str | None = None,
        causation_id: str | None = None,
    ) -> DynamicPageSession | None: ...

    async def replace_page(
        self,
        *,
        descriptor: DynamicPageCommand,
        context_id: str,
        causation_id: str | None = None,
    ) -> None: ...

    async def close_page(
        self,
        *,
        context_id: str,
        reason: str = "close",
        causation_id: str | None = None,
    ) -> None: ...


class ControlContext:
    """Controller-owned lease context for one visible binding."""

    def __init__(
        self,
        controller_id: str,
        device: DeviceDescriptor,
        config_id: str,
        command_service: HardwareCommandService,
        provider_instance_id: str,
        provider_id: str,
        action_id: str,
        control: ControlLayout,
        settings: Mapping[str, Any],
        internal: Mapping[str, Any],
        runtime_sender: RuntimeMessageSender,
        start_soon: Callable[..., None],
        render_dispatcher: RenderDispatcher,
        context_settings_target: SettingsTargetRef | None,
        provider_session_id: str | None,
        contract: ContractPointer | None,
        *,
        profile_id: str,
        page_id: str,
        builtin_action: "BuiltinAction | None" = None,
        metadata: BindingMetadata,
        request_port: ActionRequestPort,
    ):
        self._controller_id = controller_id
        self.device = device
        self.config_id = config_id
        self._command_service = command_service
        self.provider_instance_id = provider_instance_id
        self.provider_id = provider_id
        self.provider_session_id = provider_session_id
        self.contract = contract
        self.action_id = action_id
        self.action_instance_id = metadata.action_instance_id
        self.binding_id = metadata.binding_id
        self._context_id = metadata.context_id
        self.page_session_id = metadata.page_session_id
        self._builtin_action = builtin_action
        self.metadata = metadata
        self.control = control
        self.runtime_sender = runtime_sender
        self.profile_id = profile_id
        self.page_id = page_id
        self.settings_target = context_settings_target

        self._store = ControlStateStore(
            context_id=metadata.context_id,
            binding_id=metadata.binding_id,
        )
        self._store.settings = dict(thaw_json(settings))
        self._internal = dict(thaw_json(internal))

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
        self._router = PresentationController(
            store=self._store,
            render_service=RenderService(),
            render_dispatcher=render_dispatcher,
            output=output,
            image_format=control.image_format,
            start_soon=start_soon,
        )
        from deckr.controller._bindings._render_policy import rendering_support

        self.rendering = rendering_support(self)
        self.controller_context = ControllerActionContext(
            request_port=request_port,
            binding_metadata=metadata,
            settings=self._store.settings,
        )

        self.gateway = ActionGateway(
            LocalActionAdapter(builtin_action, self.controller_context)
            if builtin_action is not None
            else RemoteActionAdapter(
                metadata,
                self._publish,
                lambda: dict(
                    binding=self.metadata,
                    rendering=self.rendering,
                    settings=self.settings,
                    internal=self.internal,
                ),
            )
        )

    @property
    def presentation(self) -> PresentationController:
        return self._router

    def replace_settings(self, settings: Mapping[str, Any]) -> None:
        self._store.settings = dict(thaw_json(settings))
        self.controller_context.replace_settings(settings)

    @property
    def id(self) -> str:
        return self._context_id

    @property
    def settings(self) -> Mapping[str, Any]:
        return self._store.settings

    @property
    def internal(self) -> Mapping[str, Any]:
        return self._internal

    @property
    def base_output_generation(self) -> int:
        return self._store.base_output_generation

    @property
    def content_kind(self) -> str:
        overlay = self._store.overlay
        if overlay is not None:
            return f"overlay:{overlay.template}"
        if self._store.content.raster is not None:
            return "data_image"
        image = self._store.content.image
        if image is not None:
            if image.startswith("data:application/vnd.invariant.graph"):
                return "invariant_graph"
            if image.startswith("data:"):
                return "data_image"
            if image.startswith(("http://", "https://")):
                return "remote_image"
            return "image"
        if self._store.content.title is not None:
            return "title"
        return "empty"

    async def _publish(self, message_type: str, body: Mapping[str, Any] | Any) -> None:
        provider_session_key = (
            None
            if self.provider_session_id is None
            else ProviderSessionKey(
                self.provider_instance_id,
                self.provider_id,
                self.provider_session_id,
            )
        )
        sent = await self.runtime_sender.send_action_runtime_message(
            provider_session_key=provider_session_key,
            contract=self.contract,
            message_type=message_type,
            body=body,
        )
        if sent:
            return
        logger.warning(
            "Skipping action runtime message without live lease config=%s "
            "control=%s action=%s provider=%s binding=%s message=%s",
            self.config_id,
            self.control.id,
            self.action_id,
            self.provider_instance_id,
            self.binding_id,
            message_type,
        )
        raise RuntimeError(f"Action runtime delivery refused: {message_type}")

    async def on_binding_attached(self) -> None:
        await self.gateway.attach()

    async def on_binding_detached(self, reason: str) -> None:
        await self.gateway.detach(reason)

    async def on_input(self, event: CapabilityInputEvent) -> None:
        await self.gateway.input(event)

    async def set_title(self, text: str) -> None:
        await self._router.set_title(text)

    async def set_raster_image(
        self,
        image: str,
        *,
        generation: int | None = None,
        source: RenderSource | None = None,
    ) -> None:
        await self._router.set_raster_image(
            image,
            generation=generation,
            source=source,
        )

    async def clear_raster(self, *, generation: int | None = None) -> None:
        await self._router.clear(generation=generation)

    async def refresh_raster(self) -> None:
        await self._router.render(source=self._render_source("controller_refresh"))

    async def show_overlay(
        self,
        *,
        template: str,
        title: str | None,
        params: dict,
        duration_seconds: float | None,
        overlay_id: str | None,
        generation: int,
        binding_output_generation: int,
        source: RenderSource | None = None,
    ) -> bool:
        return await self._router.show_overlay(
            template=template,
            title=title,
            params=params,
            duration_seconds=duration_seconds,
            overlay_id=overlay_id,
            generation=generation,
            binding_output_generation=binding_output_generation,
            source=source,
        )

    async def clear_overlay(
        self,
        *,
        overlay_id: str | None,
        generation: int,
        binding_output_generation: int,
        source: RenderSource | None = None,
    ) -> bool:
        return await self._router.clear_overlay(
            overlay_id=overlay_id,
            generation=generation,
            binding_output_generation=binding_output_generation,
            source=source,
        )

    def _render_source(self, command_type: str) -> RenderSource:
        return RenderSource(
            provider_instance_id=self.provider_instance_id,
            provider_id=self.provider_id,
            provider_session_id=self.provider_session_id,
            action_id=self.action_id,
            action_instance_id=self.action_instance_id,
            command_type=command_type,
        )
