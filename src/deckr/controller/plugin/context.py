import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from deckr.hardware import events as hw_events
from deckr.python_plugin.events import (
    DialRotate,
    KeyDown,
    KeyUp,
    SlotInfo,
    TouchSwipe,
    TouchTap,
    WillAppear,
    WillDisappear,
)
from deckr.python_plugin.interface import ControlContext as ControlContextProtocol
from deckr.pluginhost.messages import (
    DIAL_ROTATE,
    KEY_DOWN,
    KEY_UP,
    TOUCH_SWIPE,
    TOUCH_TAP,
    WILL_APPEAR,
    WILL_DISAPPEAR,
    HostMessage,
    TitleOptions,
    build_context_id,
    controller_address,
    host_address,
)

from deckr.controller._command_router import CommandRouter, DeviceOutput
from deckr.controller._hardware_service import HardwareCommandService
from deckr.controller._render import RenderService
from deckr.controller._render_dispatcher import RenderDispatcher
from deckr.controller._state_store import ControlStateStore
from deckr.controller.plugin.builtin._context import BuiltInPluginContext
from deckr.controller.settings import SettingsService, SettingsTarget

if TYPE_CHECKING:
    from deckr.python_plugin.interface import PluginAction

    from deckr.controller._device_manager import DeviceManager

logger = logging.getLogger(__name__)


class ControlContext(ControlContextProtocol):
    def __init__(
        self,
        controller_id: str,
        device: hw_events.HardwareDevice,
        command_service: HardwareCommandService,
        host_id: str,
        action_uuid: str,
        slot: hw_events.HardwareSlot,
        settings: dict,
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
        builtin_action: "PluginAction | None" = None,
    ):
        self._controller_id = controller_id
        self.device = device
        self._command_service = command_service
        self.host_id = host_id
        self.action_uuid = action_uuid
        self._builtin_action = builtin_action
        self.slot = slot
        self.manager = manager
        self._plugin_bus = plugin_bus
        self.profile_id = profile_id
        self.page_id = page_id
        self.settings_target = context_settings_target

        self._store = ControlStateStore(
            context_id=build_context_id(controller_id, device.id, slot.id)
        )
        self._store.settings = dict(settings)
        self._store.default_title_options = title_options

        output = DeviceOutput(command_service, device.id, slot.id)
        render_service = RenderService()
        self._router = CommandRouter(
            store=self._store,
            render_service=render_service,
            render_dispatcher=render_dispatcher,
            output=output,
            image_format=slot.image_format,
            start_soon=start_soon,
            settings_service=settings_service,
            settings_target=context_settings_target,
        )
        self.plugin_context = BuiltInPluginContext(
            router=self._router,
            command_service=command_service,
            device_id=device.id,
            manager=manager,
            context_id=self.id,
            settings_service=settings_service,
        )

    @property
    def id(self) -> str:
        return build_context_id(self._controller_id, self.device.id, self.slot.id)

    def _slot_info(self) -> SlotInfo:
        image_format = None
        if self.slot.image_format is not None:
            image_format = {
                "width": self.slot.image_format.width,
                "height": self.slot.image_format.height,
                "format": self.slot.image_format.format,
                "rotation": self.slot.image_format.rotation,
            }
        return SlotInfo.model_validate(
            {
                "slotId": self.slot.id,
                "slotType": self.slot.slot_type,
                "coordinates": {
                    "column": self.slot.coordinates.column,
                    "row": self.slot.coordinates.row,
                },
                "gestures": sorted(self.slot.gestures),
                "imageFormat": image_format,
            }
        )

    async def _send_event(self, msg_type: str, payload: dict) -> None:
        """Send an event to the plugin host via the bus, or deliver directly if builtin."""
        if self._builtin_action is not None:
            await self._deliver_to_builtin(msg_type, payload)
            return
        payload = {**payload, "actionUuid": self.action_uuid}
        msg = HostMessage(
            from_id=controller_address(self._controller_id),
            to_id=host_address(self.host_id),
            type=msg_type,
            payload=payload,
        )
        await self._plugin_bus.send(msg)

    async def _deliver_to_builtin(self, msg_type: str, payload: dict) -> None:
        """Deliver event directly to builtin action (no bus)."""
        action = self._builtin_action
        if action is None:
            return
        event_data = payload.get("event", payload)
        method_map = {
            WILL_APPEAR: ("on_will_appear", WillAppear),
            WILL_DISAPPEAR: ("on_will_disappear", WillDisappear),
            KEY_UP: ("on_key_up", KeyUp),
            KEY_DOWN: ("on_key_down", KeyDown),
            DIAL_ROTATE: ("on_dial_rotate", DialRotate),
            TOUCH_TAP: ("on_touch_tap", TouchTap),
            TOUCH_SWIPE: ("on_touch_swipe", TouchSwipe),
        }
        entry = method_map.get(msg_type)
        if entry is None:
            return
        method_name, event_cls = entry
        if not hasattr(action, method_name):
            return
        event = event_cls.model_validate(event_data)
        method = getattr(action, method_name)
        await method(event, self.plugin_context)

    async def on_will_appear(self):
        await self._router.hydrate_settings()
        logger.info(
            "Dispatching willAppear slot=%s action=%s host=%s settings=%s",
            self.slot.id,
            self.action_uuid,
            self.host_id,
            self._store.settings,
        )
        event = WillAppear(
            context=self.id,
            slot=self._slot_info(),
        )
        await self._send_event(
            WILL_APPEAR,
            {
                "event": event.model_dump(by_alias=True),
                "settings": self._store.settings,
            },
        )
        await self._router.render()

    async def on_will_disappear(self):
        event = WillDisappear(
            context=self.id,
            slot_id=self.slot.id,
        )
        await self._send_event(
            WILL_DISAPPEAR,
            {"event": event.model_dump(by_alias=True)},
        )

    async def on_key_up(self, event: KeyUp):
        await self._send_event(KEY_UP, {"event": event.model_dump(by_alias=True)})

    async def on_key_down(self, event: KeyDown):
        await self._send_event(KEY_DOWN, {"event": event.model_dump(by_alias=True)})

    async def on_dial_rotate(self, event: DialRotate):
        await self._send_event(
            DIAL_ROTATE,
            {"event": event.model_dump(by_alias=True)},
        )

    async def on_touch_tap(self, event: TouchTap):
        await self._send_event(TOUCH_TAP, {"event": event.model_dump(by_alias=True)})

    async def on_touch_swipe(self, event: TouchSwipe):
        await self._send_event(
            TOUCH_SWIPE,
            {"event": event.model_dump(by_alias=True)},
        )
