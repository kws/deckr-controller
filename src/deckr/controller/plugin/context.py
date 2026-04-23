import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from deckr.hardware import events as hw_events
from deckr.plugin.events import (
    Coordinates,
    DialRotate,
    KeyDown,
    KeyUp,
    SingleActionPayload,
    TouchSwipe,
    TouchTap,
    WillAppear,
    WillDisappear,
)
from deckr.plugin.interface import ControlContext as ControlContextProtocol
from deckr.plugin.messages import (
    DIAL_ROTATE,
    KEY_DOWN,
    KEY_UP,
    TOUCH_SWIPE,
    TOUCH_TAP,
    WILL_APPEAR,
    WILL_DISAPPEAR,
    HostMessage,
    build_context_id,
    controller_address,
    host_address,
)

from deckr.controller._command_router import CommandRouter, DeviceOutput
from deckr.controller._render import RenderService
from deckr.controller._render_dispatcher import RenderDispatcher
from deckr.controller._state_store import ControlStateStore, StateOverride, TitleOptions
from deckr.controller.plugin.builtin._context import BuiltInPluginContext
from deckr.controller.settings import SettingsService, SettingsTarget

if TYPE_CHECKING:
    from deckr.plugin.interface import PluginAction

    from deckr.controller._device_manager import DeviceManager

logger = logging.getLogger(__name__)


def _parse_manifest_defaults(
    raw: dict[str, Any] | None,
) -> dict[int, StateOverride] | None:
    """Convert serialized manifest_defaults from host to dict[int, StateOverride]."""
    if not raw:
        return None
    result: dict[int, StateOverride] = {}
    for k, v in raw.items():
        if not isinstance(v, dict):
            continue
        try:
            idx = int(k)
        except ValueError:
            continue
        to_dict = v.get("title_options")
        title_options = None
        if isinstance(to_dict, dict):
            title_options = TitleOptions(
                font_family=to_dict.get("font_family"),
                font_size=to_dict.get("font_size"),
                font_style=to_dict.get("font_style"),
                title_color=to_dict.get("title_color"),
                title_alignment=to_dict.get("title_alignment"),
            )
        result[idx] = StateOverride(
            title=v.get("title"),
            image=v.get("image"),
            title_options=title_options,
        )
    return result if result else None


def _merge_title_options(
    base: TitleOptions | None, override: TitleOptions | None
) -> TitleOptions | None:
    """Merge override into base; override fields take precedence when not None."""
    if override is None:
        return base
    if base is None:
        return override
    return TitleOptions(
        font_family=override.font_family
        if override.font_family is not None
        else base.font_family,
        font_size=override.font_size
        if override.font_size is not None
        else base.font_size,
        font_style=override.font_style
        if override.font_style is not None
        else base.font_style,
        title_color=override.title_color
        if override.title_color is not None
        else base.title_color,
        title_alignment=(
            override.title_alignment
            if override.title_alignment is not None
            else base.title_alignment
        ),
    )


class ControlContext(ControlContextProtocol):
    def __init__(
        self,
        controller_id: str,
        device: hw_events.HWDevice,
        host_id: str,
        action_uuid: str,
        slot: hw_events.HWSlot,
        settings: dict,
        manager: "DeviceManager",
        plugin_bus: Any,
        start_soon: Callable[..., None],
        render_dispatcher: RenderDispatcher,
        settings_service: SettingsService | None,
        context_settings_target: SettingsTarget | None,
        global_settings_target: SettingsTarget | None,
        *,
        profile_id: str,
        page_id: str,
        title_options: TitleOptions | None = None,
        manifest_defaults: dict[int, StateOverride] | None = None,
        manifest_defaults_raw: dict[str, Any] | None = None,
        builtin_action: "PluginAction | None" = None,
    ):
        self._controller_id = controller_id
        self.device = device
        self.host_id = host_id
        self.action_uuid = action_uuid
        self._builtin_action = builtin_action
        self.slot = slot
        self.manager = manager
        self._plugin_bus = plugin_bus
        self.profile_id = profile_id
        self.page_id = page_id
        self.settings_target = context_settings_target
        self.global_settings_target = global_settings_target

        md = manifest_defaults or _parse_manifest_defaults(manifest_defaults_raw)
        self._store = ControlStateStore(
            context_id=build_context_id(controller_id, device.id, slot.id)
        )
        self._store.settings = dict(settings)
        # Config title_options overrides manifest; partial config merges with manifest defaults
        manifest_opts = md[0].title_options if md and 0 in md else None
        self._store.title_options = _merge_title_options(manifest_opts, title_options)

        output = DeviceOutput(device, slot.id)
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
            manifest_defaults=md,
        )
        self.plugin_context = BuiltInPluginContext(
            router=self._router,
            device=device,
            manager=manager,
            context_id=self.id,
            settings_service=settings_service,
            global_settings_target=global_settings_target,
        )

    @property
    def id(self) -> str:
        return build_context_id(self._controller_id, self.device.id, self.slot.id)

    _ENRICHED_EVENT_TYPES = frozenset(
        {KEY_UP, KEY_DOWN, DIAL_ROTATE, TOUCH_TAP, TOUCH_SWIPE}
    )

    async def _send_event(self, msg_type: str, payload: dict) -> None:
        """Send an event to the plugin host via the bus, or deliver directly if builtin."""
        if self._builtin_action is not None:
            await self._deliver_to_builtin(msg_type, payload)
            return
        if msg_type in self._ENRICHED_EVENT_TYPES:
            payload = {**payload, "action": self.action_uuid, "fullscreen": False}
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
        payload = SingleActionPayload(
            controller="Keypad",
            coordinates=Coordinates(
                column=self.slot.coordinates.column, row=self.slot.coordinates.row
            ),
            resources={"icon": "path/to/icon.png"},
            settings=self._store.settings,
        )
        event = WillAppear(
            action=self.action_uuid,
            context=self.id,
            device=self.device.id,
            payload=payload,
        )
        await self._send_event(WILL_APPEAR, {"event": event.model_dump()})
        await self._router.render()

    async def on_will_disappear(self):
        event = WillDisappear(
            action=self.action_uuid, context=self.id, device=self.device.id
        )
        await self._send_event(WILL_DISAPPEAR, {"event": event.model_dump()})

    async def on_key_up(self, event: KeyUp):
        await self._send_event(KEY_UP, {"event": event.model_dump()})

    async def on_key_down(self, event: KeyDown):
        await self._send_event(KEY_DOWN, {"event": event.model_dump()})

    async def on_dial_rotate(self, event: DialRotate):
        await self._send_event(DIAL_ROTATE, {"event": event.model_dump()})

    async def on_touch_tap(self, event: TouchTap):
        await self._send_event(TOUCH_TAP, {"event": event.model_dump()})

    async def on_touch_swipe(self, event: TouchSwipe):
        await self._send_event(TOUCH_SWIPE, {"event": event.model_dump()})
