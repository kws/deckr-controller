import logging
from collections.abc import Callable

import anyio
from deckr.components import BaseComponent, RunContext
from deckr.contracts.messages import (
    DeckrMessage,
    parse_hardware_manager_address,
    plugin_hosts_broadcast,
)
from deckr.core.util.anyio import AsyncMap
from deckr.hardware import messages as hw_messages
from deckr.pluginhost.messages import (
    COMMAND_MESSAGE_TYPES,
    HOST_ONLINE,
    REQUEST_ACTIONS,
    controller_address,
    plugin_actions_subject,
    plugin_message,
    plugin_message_for_controller,
    subject_config_id,
)
from deckr.transports.bus import EventBus

from deckr.controller._device_manager import DeviceManager
from deckr.controller._hardware_service import (
    HardwareCommandService,
    HardwareDeviceRegistry,
    LiveHardwareDevice,
)
from deckr.controller._render_dispatcher import (
    ProcessPoolRenderBackend,
    RenderBackend,
)
from deckr.controller.config import DeviceConfigService
from deckr.controller.plugin.action_registry import ActionRegistry
from deckr.controller.plugin.events import ActionsChangedEvent
from deckr.controller.settings import SettingsService

logger = logging.getLogger(__name__)


class ControllerService(BaseComponent):
    def __init__(
        self,
        driver_bus: EventBus,
        config_service: DeviceConfigService,
        settings_service: SettingsService,
        *,
        controller_id: str,
        action_registry: ActionRegistry | None = None,
        plugin_bus: EventBus | None = None,
        render_backend: RenderBackend | None = None,
    ):
        super().__init__()
        self._driver_bus = driver_bus
        self._device_registry = HardwareDeviceRegistry()
        self._config_service = config_service
        self._settings_service = settings_service
        self._controller_id = controller_id
        self._command_service = HardwareCommandService(
            driver_bus,
            controller_id=controller_id,
        )
        self._controller_contexts = AsyncMap[str, DeviceManager]()
        self._device_disconnect_events: dict[str, anyio.Event] = {}
        self._action_registry = action_registry
        self._plugin_bus = plugin_bus
        self._start_soon: Callable | None = None
        self._render_backend = render_backend

    async def _handle_plugin_command(self, msg: DeckrMessage) -> None:
        """Route command messages to the appropriate DeviceManager."""
        if msg.message_type not in COMMAND_MESSAGE_TYPES:
            return
        config_id = subject_config_id(msg.subject)
        if config_id is None:
            logger.warning(
                "Ignoring plugin command %s without config subject from %s",
                msg.message_type,
                msg.sender,
            )
            return
        ctrl_ctx = await self._controller_contexts.get(config_id)
        if ctrl_ctx is not None:
            await ctrl_ctx.handle_command(msg)

    async def _handle_host_online(self, msg: DeckrMessage) -> None:
        if self._plugin_bus is None:
            return
        await self._plugin_bus.send(
            plugin_message(
                sender=controller_address(self._controller_id),
                recipient=msg.sender,
                message_type=REQUEST_ACTIONS,
                body={},
                subject=plugin_actions_subject(),
            )
        )

    async def handle_actions_changed_event(self, event: ActionsChangedEvent) -> None:
        controller_contexts = await self._controller_contexts.values()
        logger.info(
            "Applying ActionsChangedEvent to %d device(s): +%s -%s",
            len(controller_contexts),
            event.registered,
            event.unregistered,
        )
        for ctrl_ctx in controller_contexts:
            await ctrl_ctx.on_actions_changed(event.registered, event.unregistered)

    async def _plugin_subscription_loop(self) -> None:
        """Subscribe to plugin bus and route command messages to DeviceManagers."""
        if self._plugin_bus is None:
            return
        async with self._plugin_bus.subscribe() as stream:
            async for event in stream:
                try:
                    if not isinstance(event, DeckrMessage):
                        continue
                    if not plugin_message_for_controller(event, self._controller_id):
                        continue
                    if event.message_type == HOST_ONLINE:
                        await self._handle_host_online(event)
                        continue
                    if event.message_type in COMMAND_MESSAGE_TYPES:
                        await self._handle_plugin_command(event)
                except Exception:
                    if isinstance(event, DeckrMessage):
                        logger.exception(
                            "Error handling plugin message %s from %s",
                            event.message_type,
                            event.sender,
                        )
                    else:
                        logger.exception("Error handling plugin bus event")

    async def _event_loop(self):
        async with self._driver_bus.subscribe() as subscribe:
            async for message in subscribe:
                event = hw_messages.hardware_body_from_message(message)
                if isinstance(event, hw_messages.DeviceConnectedMessage):
                    await self._handle_device_connected(message, event)
                elif isinstance(event, hw_messages.DeviceDisconnectedMessage):
                    ref = hw_messages.hardware_device_ref_from_message(message)
                    if ref is None:
                        continue
                    live = self._device_registry.disconnect_ref(ref)
                    if live is not None:
                        self._command_service.unregister_config(live.config_id)
                        await self.on_device_disconnected(live.config_id)
                elif isinstance(event, hw_messages.HARDWARE_INPUT_MESSAGE_TYPES):
                    ref = hw_messages.hardware_device_ref_from_message(message)
                    if ref is None:
                        continue
                    live = self._device_registry.get_by_ref(ref)
                    if live is None:
                        continue
                    ctrl_ctx = await self._controller_contexts.get(live.config_id)
                    if ctrl_ctx is not None:
                        await ctrl_ctx.on_event(message)

    async def _handle_device_connected(
        self,
        message: DeckrMessage,
        event: hw_messages.DeviceConnectedMessage,
    ) -> None:
        ref = hw_messages.hardware_device_ref_from_message(message)
        if ref is None:
            logger.warning("Ignoring deviceConnected without hardware subject ref")
            return
        try:
            config = await self._config_service.match_device(
                fingerprint=event.device.fingerprint,
                manager_id=ref.manager_id,
            )
        except ValueError:
            logger.exception(
                "Ambiguous config for hardware fingerprint=%s manager=%s",
                event.device.fingerprint,
                ref.manager_id,
            )
            return
        if config is None:
            logger.warning(
                "No controller config matched hardware fingerprint=%s manager=%s",
                event.device.fingerprint,
                ref.manager_id,
            )
            return
        live = self._device_registry.connect(
            config_id=config.id,
            ref=ref,
            device=event.device,
        )
        self._command_service.register_device(config_id=config.id, ref=ref)
        await self.on_device_connected(live, initial_config=config)

    async def _route_event_loop(self) -> None:
        async with self._driver_bus.route_table.subscribe() as stream:
            async for event in stream:
                if event.event_type != "endpointUnreachable" or event.endpoint is None:
                    continue
                if event.lane != self._driver_bus.lane:
                    continue
                manager_id = parse_hardware_manager_address(event.endpoint)
                if manager_id is None:
                    continue
                await self._handle_manager_unreachable(manager_id)

    async def _handle_manager_unreachable(self, manager_id: str) -> None:
        for live in self._device_registry.for_manager(manager_id):
            self._device_registry.disconnect_config(live.config_id)
            self._command_service.unregister_config(live.config_id)
            await self.on_device_disconnected(live.config_id)

    async def start(self, ctx: RunContext):
        self._start_soon = ctx.tg.start_soon
        if self._render_backend is None:
            self._render_backend = ProcessPoolRenderBackend()
        if self._plugin_bus is not None:
            ctx.tg.start_soon(self._plugin_subscription_loop)
            request_msg = plugin_message(
                sender=controller_address(self._controller_id),
                recipient=plugin_hosts_broadcast(),
                message_type=REQUEST_ACTIONS,
                body={},
                subject=plugin_actions_subject(),
            )
            logger.info("Requesting actions from all hosts")
            await self._plugin_bus.send(request_msg)
        ctx.tg.start_soon(self._event_loop)
        ctx.tg.start_soon(self._route_event_loop)

    async def stop(self):
        for ctrl_ctx in await self._controller_contexts.values():
            await ctrl_ctx.clear_page()
        if self._render_backend is not None:
            await self._render_backend.aclose()

    async def _device_lifecycle(
        self,
        live: LiveHardwareDevice,
        initial_config,
    ) -> None:
        """Run device setup, config listener, and wait for disconnect."""
        stream = self._config_service.subscribe(live.config_id)
        first = initial_config or await anext(stream)
        if first is None:
            logger.error("Config not found for %s", live.config_id)
            return
        ctrl_ctx = DeviceManager(
            controller_id=self._controller_id,
            device=live.device,
            hardware_ref=live.ref,
            command_service=self._command_service,
            config=first,
            manager=self._action_registry,
            plugin_bus=self._plugin_bus,
            start_soon=self._start_soon,
            render_backend=self._render_backend,
            settings_service=self._settings_service,
            config_stream=stream,
        )
        await self._controller_contexts.set(live.config_id, ctrl_ctx)
        await ctrl_ctx.set_page()

        disconnect_event = anyio.Event()
        self._device_disconnect_events[live.config_id] = disconnect_event
        try:
            async with anyio.create_task_group() as device_tg:
                device_tg.start_soon(ctrl_ctx._config_listener)
                await disconnect_event.wait()
        finally:
            self._device_disconnect_events.pop(live.config_id, None)

    async def on_device_connected(
        self,
        live: LiveHardwareDevice,
        *,
        initial_config,
    ):
        logger.info(
            "Starting controller service for config %s from %s/%s",
            live.config_id,
            live.ref.manager_id,
            live.ref.device_id,
        )
        self._start_soon(self._device_lifecycle, live, initial_config)

    async def on_device_disconnected(self, config_id: str):
        ctrl_ctx = await self._controller_contexts.get(config_id)
        try:
            if ctrl_ctx is not None:
                await ctrl_ctx.clear_page()
        finally:
            await self._controller_contexts.delete(config_id)
            disconnect_ev = self._device_disconnect_events.get(config_id)
            if disconnect_ev is not None:
                disconnect_ev.set()
            logger.info("Stopped controller service for config %s", config_id)
