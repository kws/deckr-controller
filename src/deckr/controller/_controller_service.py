import logging
from collections.abc import Callable

import anyio
from deckr.core.component import BaseComponent, RunContext
from deckr.core.util.anyio import AsyncMap
from deckr.hardware import events as hw_events
from deckr.plugin.messages import (
    ALL_HOSTS,
    COMMAND_MESSAGE_TYPES,
    HOST_ONLINE,
    REQUEST_ACTIONS,
    ActionsChangedEvent,
    HostMessage,
    controller_address,
    extract_device_id,
)
from deckr.transports.bus import EventBus

from deckr.controller._device_manager import DeviceManager
from deckr.controller._hardware_service import (
    HardwareCommandService,
    HardwareDeviceRegistry,
)
from deckr.controller._render_dispatcher import (
    ProcessPoolRenderBackend,
    RenderBackend,
)
from deckr.controller.config import DeviceConfigService
from deckr.controller.plugin.action_registry import ActionRegistry
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
        self._command_service = HardwareCommandService(driver_bus)
        self._config_service = config_service
        self._settings_service = settings_service
        self._controller_id = controller_id
        self._controller_contexts = AsyncMap[str, DeviceManager]()
        self._device_disconnect_events: dict[str, anyio.Event] = {}
        self._action_registry = action_registry
        self._plugin_bus = plugin_bus
        self._start_soon: Callable | None = None
        self._render_backend = render_backend

    async def _handle_plugin_command(self, msg: HostMessage) -> None:
        """Route command messages to the appropriate DeviceManager."""
        if msg.type not in COMMAND_MESSAGE_TYPES:
            return
        payload = msg.payload
        context_id = payload.get("contextId", "")
        device_id = extract_device_id(context_id)
        ctrl_ctx = await self._controller_contexts.get(device_id)
        if ctrl_ctx is not None:
            await ctrl_ctx.handle_command(msg)

    async def _handle_host_online(self, msg: HostMessage) -> None:
        if self._plugin_bus is None:
            return
        await self._plugin_bus.send(
            HostMessage(
                from_id=controller_address(self._controller_id),
                to_id=msg.from_id,
                type=REQUEST_ACTIONS,
                payload={},
            )
        )

    async def _plugin_subscription_loop(self) -> None:
        """Subscribe to plugin bus and route command messages to DeviceManagers."""
        if self._plugin_bus is None:
            return
        async with self._plugin_bus.subscribe() as stream:
            async for envelope in stream:
                event = envelope.message
                try:
                    if isinstance(event, ActionsChangedEvent):
                        controller_contexts = await self._controller_contexts.values()
                        logger.info(
                            "Applying ActionsChangedEvent to %d device(s): +%s -%s",
                            len(controller_contexts),
                            event.registered,
                            event.unregistered,
                        )
                        for ctrl_ctx in controller_contexts:
                            await ctrl_ctx.on_actions_changed(
                                event.registered, event.unregistered
                            )
                        continue
                    if not isinstance(event, HostMessage):
                        continue
                    if not event.for_controller(self._controller_id):
                        continue
                    if event.type == HOST_ONLINE:
                        await self._handle_host_online(event)
                        continue
                    if event.type in COMMAND_MESSAGE_TYPES:
                        await self._handle_plugin_command(event)
                except Exception:
                    if isinstance(event, HostMessage):
                        logger.exception(
                            "Error handling plugin message %s from %s",
                            event.type,
                            event.from_id,
                        )
                    else:
                        logger.exception("Error handling plugin bus event")

    async def _event_loop(self):
        async with self._driver_bus.subscribe() as subscribe:
            async for envelope in subscribe:
                event = envelope.message
                if isinstance(event, hw_events.DeviceConnectedMessage):
                    device = self._device_registry.connect(event)
                    await self.on_device_connected(device)
                elif isinstance(event, hw_events.DeviceDisconnectedMessage):
                    self._device_registry.disconnect(event.device_id)
                    await self.on_device_disconnected(event.device_id)
                elif isinstance(event, hw_events.HARDWARE_INPUT_MESSAGE_TYPES):
                    device_id = event.device_id
                    ctrl_ctx = await self._controller_contexts.get(device_id)
                    if ctrl_ctx is not None:
                        await ctrl_ctx.on_event(event)

    async def start(self, ctx: RunContext):
        self._start_soon = ctx.tg.start_soon
        if self._render_backend is None:
            self._render_backend = ProcessPoolRenderBackend()
        if self._plugin_bus is not None:
            ctx.tg.start_soon(self._plugin_subscription_loop)
            request_msg = HostMessage(
                from_id=controller_address(self._controller_id),
                to_id=ALL_HOSTS,
                type=REQUEST_ACTIONS,
                payload={},
            )
            logger.info("Requesting actions from all hosts")
            await self._plugin_bus.send(request_msg)
        ctx.tg.start_soon(self._event_loop)

    async def stop(self):
        for ctrl_ctx in await self._controller_contexts.values():
            await ctrl_ctx.clear_page()
        if self._render_backend is not None:
            await self._render_backend.aclose()

    async def _device_lifecycle(self, device: hw_events.WireHWDevice) -> None:
        """Run device setup, config listener, and wait for disconnect."""
        stream = self._config_service.subscribe(device.id)
        first = await anext(stream)
        if first is None:
            logger.error("Config not found for device %s", device.id)
            return
        ctrl_ctx = DeviceManager(
            controller_id=self._controller_id,
            device=device,
            command_service=self._command_service,
            config=first,
            manager=self._action_registry,
            plugin_bus=self._plugin_bus,
            start_soon=self._start_soon,
            render_backend=self._render_backend,
            settings_service=self._settings_service,
            config_stream=stream,
        )
        await self._controller_contexts.set(device.id, ctrl_ctx)
        await ctrl_ctx.set_page()

        disconnect_event = anyio.Event()
        self._device_disconnect_events[device.id] = disconnect_event
        try:
            async with anyio.create_task_group() as device_tg:
                device_tg.start_soon(ctrl_ctx._config_listener)
                await disconnect_event.wait()
        finally:
            self._device_disconnect_events.pop(device.id, None)

    async def on_device_connected(self, device: hw_events.WireHWDevice):
        logger.info("Starting controller service for device %s", device.id)
        self._start_soon(self._device_lifecycle, device)

    async def on_device_disconnected(self, device_id: str):
        ctrl_ctx = await self._controller_contexts.get(device_id)
        try:
            if ctrl_ctx is not None:
                await ctrl_ctx.clear_page()
        finally:
            await self._controller_contexts.delete(device_id)
            disconnect_ev = self._device_disconnect_events.get(device_id)
            if disconnect_ev is not None:
                disconnect_ev.set()
            logger.info("Stopped controller service for device %s", device_id)
