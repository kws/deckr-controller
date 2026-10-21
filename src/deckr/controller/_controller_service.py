import logging
from collections.abc import Callable

import anyio
from deckr.actions.messages import (
    CLOSE_PAGE,
    COMMAND_MESSAGE_TYPES,
    OPEN_PAGE,
    REPLACE_PAGE,
)
from deckr.components import BaseComponent, RunContext
from deckr.contracts.messages import (
    SERVICES_LANE,
    DeckrMessage,
)
from deckr.core.util.anyio import AsyncMap
from deckr.hardware import DeckrHardware
from deckr.hardware import messages as hw_messages
from deckr.hardware.descriptors import DeviceDescriptor
from deckr.lanes import EndpointSession

from deckr.controller._actions import ControllerActionService, ProviderActionKey
from deckr.controller._actions._routing import action_command_metadata
from deckr.controller._device_session import DeviceSession
from deckr.controller._hardware import (
    ControllerHardwareService,
    LiveDeviceRoute,
)
from deckr.controller._render_dispatcher import (
    ProcessPoolRenderBackend,
    RenderBackend,
)
from deckr.controller._stop_aware import cancel_on_stopping
from deckr.controller.action_provider.action_registry import ActionRegistry
from deckr.controller.config import DeviceConfigService
from deckr.controller.settings import SettingsService

logger = logging.getLogger(__name__)


class ControllerService(BaseComponent):
    def __init__(
        self,
        endpoint: EndpointSession,
        hardware: DeckrHardware,
        config_service: DeviceConfigService,
        settings_service: SettingsService,
        *,
        controller_id: str,
        action_registry: ActionRegistry | None = None,
        action_service: ControllerActionService | None = None,
        render_backend: RenderBackend | None = None,
    ):
        super().__init__()
        self._endpoint = endpoint
        self._config_service = config_service
        self._settings_service = settings_service
        self._controller_id = controller_id
        self._session_id = endpoint.session_id
        self._hardware = ControllerHardwareService(
            hardware=hardware,
            config_service=config_service,
            callbacks=self,
        )
        self._command_service = self._hardware.command_service
        self._controller_contexts = AsyncMap[str, DeviceSession]()
        self._device_disconnect_events: dict[str, anyio.Event] = {}
        self._action_registry = action_registry
        self._action_service = action_service
        if self._action_service is not None:
            self._action_service.set_change_callback(
                self._handle_internal_action_availability_changed
            )
        self._start_soon: Callable | None = None
        self._render_backend = render_backend
        self._stopping: anyio.Event | None = None

    async def _handle_action_command(
        self, msg: DeckrMessage, *, service_message: DeckrMessage | None = None
    ) -> None:
        """Route command messages to the appropriate DeviceSession."""
        if msg.message_type not in COMMAND_MESSAGE_TYPES:
            return
        if msg.message_type in {OPEN_PAGE, REPLACE_PAGE, CLOSE_PAGE}:
            if service_message is not None:
                for context in await self._controller_contexts.values():
                    if await context.handle_page_command(service_message):
                        break
            return
        metadata = action_command_metadata(msg)
        config_id = metadata.config_id if metadata is not None else None
        if config_id is None:
            logger.warning(
                "Ignoring action command %s without routing metadata from %s",
                msg.message_type,
                msg.sender,
            )
            return
        ctrl_ctx = await self._controller_contexts.get(config_id)
        if ctrl_ctx is not None:
            await ctrl_ctx.handle_provider_command(msg)

    async def _handle_internal_action_availability_changed(
        self,
        changed_keys: frozenset[ProviderActionKey],
    ) -> None:
        if not changed_keys:
            return
        controller_contexts = await self._controller_contexts.values()
        logger.debug(
            "Internal action availability handoff changed_keys=%s devices=%s",
            len(changed_keys),
            len(controller_contexts),
        )
        for ctrl_ctx in controller_contexts:
            await ctrl_ctx.on_action_availability_changed(changed_keys)

    async def _handle_render_command(self, message):
        from deckr.actions.pages import BindingCommandTarget
        from deckr.actions.rendering import BindingRenderResultBody
        from deckr.services import ServiceMessageBody

        body = ServiceMessageBody.from_wire(message.body)
        if body.name != "binding_render":
            return False
        if (
            self._action_service is None
            or await self._action_service.render_message_key(message) is None
        ):
            return True
        for context in await self._controller_contexts.values():
            if await context.handle_render_command(message):
                return True
        try:
            target = BindingCommandTarget.from_wire(body.params["target"])
            requirement = body.params["requirementName"]
            result = BindingRenderResultBody(
                requestMessageId=message.message_id,
                target=target,
                requirementName=requirement,
                stage="controller",
                status="rejected",
                code="target_not_current",
            )
        except (KeyError, TypeError, ValueError):
            return True
        await self._action_service.send_render_result(message, result)
        return True

    async def _actions_subscription_loop(self, stopping: anyio.Event) -> None:
        """Subscribe to Action Runtime service messages and route provider commands."""
        async with (
            self._endpoint.subscribe(SERVICES_LANE) as stream,
            cancel_on_stopping(stopping),
        ):
            async for event in stream:
                try:
                    if not isinstance(event, DeckrMessage):
                        continue
                    if await self._handle_render_command(event):
                        continue
                    action_runtime_message = (
                        await self._action_service.decode_inbound_runtime_message(event)
                        if self._action_service is not None
                        else None
                    )
                    if action_runtime_message is None:
                        continue
                    if action_runtime_message.message_type in COMMAND_MESSAGE_TYPES:
                        await self._handle_action_command(
                            action_runtime_message, service_message=event
                        )
                except Exception:
                    if isinstance(event, DeckrMessage):
                        logger.exception(
                            "Error handling action runtime message %s from %s",
                            event.message_type,
                            event.sender,
                        )
                    else:
                        logger.exception("Error handling action runtime event")

    async def on_hardware_connected(
        self,
        live: LiveDeviceRoute,
        *,
        initial_config,
    ) -> None:
        if await self._controller_contexts.get(live.config_id) is not None:
            await self.on_hardware_disconnected(
                live.config_id,
                reason="replacement live device connected",
            )
        logger.info(
            "Starting controller service for config %s from %s/%s",
            live.config_id,
            live.ref.manager_id,
            live.ref.device_id,
        )
        self._start_soon(self._device_lifecycle, live, initial_config)

    async def on_hardware_disconnected(
        self,
        config_id: str,
        *,
        reason: str = "unknown",
    ) -> None:
        ctrl_ctx = await self._controller_contexts.pop(config_id)
        disconnect_ev = self._device_disconnect_events.get(config_id)
        try:
            if ctrl_ctx is not None:
                await ctrl_ctx.aclose()
        finally:
            if disconnect_ev is not None:
                disconnect_ev.set()
            logger.info(
                "Stopped controller service for config %s (reason: %s)",
                config_id,
                reason,
            )

    async def on_hardware_descriptor_changed(
        self,
        config_id: str,
        device: DeviceDescriptor,
    ) -> None:
        ctrl_ctx = await self._controller_contexts.get(config_id)
        if ctrl_ctx is not None:
            await ctrl_ctx.on_device_descriptor_changed(device)

    async def on_hardware_control_input(
        self,
        live: LiveDeviceRoute,
        message: DeckrMessage,
    ) -> None:
        ctrl_ctx = await self._controller_contexts.get(live.config_id)
        if ctrl_ctx is not None:
            await ctrl_ctx.handle_hardware_input(message)

    async def on_hardware_capability_state_changed(
        self,
        live: LiveDeviceRoute,
        event: hw_messages.CapabilityStateChangedMessage,
    ) -> None:
        ctrl_ctx = await self._controller_contexts.get(live.config_id)
        if ctrl_ctx is not None:
            await ctrl_ctx.on_capability_state_changed(event)

    async def on_hardware_command_rejected(
        self,
        live: LiveDeviceRoute,
        event: hw_messages.CommandRejectedMessage,
    ) -> None:
        ctrl_ctx = await self._controller_contexts.get(live.config_id)
        if ctrl_ctx is not None:
            await ctrl_ctx.on_command_rejected(event)

    async def start(self, ctx: RunContext):
        self._stopping = ctx.stopping
        self._start_soon = ctx.tg.start_soon
        if self._action_service is None and self._action_registry is not None:
            self._action_service = ControllerActionService(
                controller_id=self._controller_id,
                controller_session_id=self._session_id,
                manager=self._action_registry,
                start_soon=ctx.tg.start_soon,
            )
            self._action_service.set_change_callback(
                self._handle_internal_action_availability_changed
            )
        if self._action_service is not None:
            await self._action_service.start(ctx.tg, ctx.stopping)
        if self._render_backend is None:
            self._render_backend = ProcessPoolRenderBackend()
        ctx.tg.start_soon(self._actions_subscription_loop, ctx.stopping)
        await self._hardware.start(ctx.tg, ctx.stopping)

    async def stop(self):
        if self._stopping is not None:
            self._stopping.set()
        await self._hardware.aclose()
        for ctrl_ctx in await self._controller_contexts.values():
            await ctrl_ctx.aclose()
        await self._controller_contexts.clear()
        if self._action_service is not None:
            await self._action_service.aclose()
        if self._render_backend is not None:
            await self._render_backend.aclose()

    async def _device_lifecycle(
        self,
        live: LiveDeviceRoute,
        initial_config,
    ) -> None:
        """Run device setup, config listener, and wait for disconnect."""
        stream = self._config_service.subscribe(live.config_id)
        disconnect_event = anyio.Event()
        self._device_disconnect_events[live.config_id] = disconnect_event
        try:
            try:
                first = await anext(stream)
            except StopAsyncIteration:
                first = initial_config
            initial_config_removed = first is None
            if first is None:
                logger.info(
                    "Config %s is currently removed; keeping device claimed but idle",
                    live.config_id,
                )
                first = initial_config
            if (
                disconnect_event.is_set()
                or self._hardware.route_for_config(live.config_id) is not live
            ):
                return

            ctrl_ctx = DeviceSession(
                controller_id=self._controller_id,
                device=live.device,
                hardware_ref=live.ref,
                command_service=self._command_service,
                config=first,
                manager=self._action_registry,
                actions_bus=self._endpoint,
                start_soon=self._start_soon,
                action_service=self._action_service,
                render_backend=self._render_backend,
                settings_service=self._settings_service,
                config_stream=stream,
            )
            await self._controller_contexts.set(live.config_id, ctrl_ctx)
            async with anyio.create_task_group() as device_tg:
                await ctrl_ctx.start(device_tg, disconnect_event)
                if initial_config_removed:
                    await ctrl_ctx.on_config_changed(None)
                    await disconnect_event.wait()
                    device_tg.cancel_scope.cancel()
                    return

                page_ready = anyio.Event()

                async def set_initial_page() -> None:
                    try:
                        await ctrl_ctx.set_page()
                    except Exception:
                        if disconnect_event.is_set():
                            return
                        raise
                    finally:
                        page_ready.set()

                device_tg.start_soon(set_initial_page)
                while not page_ready.is_set():
                    if disconnect_event.is_set():
                        device_tg.cancel_scope.cancel()
                        return
                    await anyio.sleep(0.01)
                if disconnect_event.is_set():
                    device_tg.cancel_scope.cancel()
                    return
                await disconnect_event.wait()
                device_tg.cancel_scope.cancel()
        finally:
            if self._device_disconnect_events.get(live.config_id) is disconnect_event:
                self._device_disconnect_events.pop(live.config_id, None)
