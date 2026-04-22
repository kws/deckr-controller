"""Builtin plugin context: thin facade for builtin actions with direct access to controller."""

import webbrowser
from types import SimpleNamespace
from typing import TYPE_CHECKING

import anyio
from deckr.plugin.interface import PluginContext as PluginContextProtocol
from deckr.plugin.types import DynamicPageDescriptor

from deckr.controller._command_router import CommandRouter
from deckr.controller.settings import SettingsService, SettingsTarget

if TYPE_CHECKING:
    from deckr.hardware.events import HWDevice

    from deckr.controller._device_manager import DeviceManager


class BuiltInPluginContext(PluginContextProtocol):
    """Thin facade for builtin actions: delegates to CommandRouter, device, and manager."""

    def __init__(
        self,
        router: CommandRouter,
        device: "HWDevice",
        manager: "DeviceManager",
        context_id: str,
        settings_service: SettingsService | None = None,
        global_settings_target: SettingsTarget | None = None,
    ):
        self._router = router
        self._device = device
        self._manager = manager
        self._context_id = context_id
        self._settings_service = settings_service
        self._global_settings_target = global_settings_target

    async def set_title(
        self, text: str, state: int | None = None, *, slot: str | None = None
    ) -> None:
        await self._router.set_title(text, state=state)

    async def set_image(
        self, image: str, state: int | None = None, *, slot: str | None = None
    ) -> None:
        await self._router.set_image(image, state=state)

    async def set_state(self, state: int) -> None:
        await self._router.set_state(state)

    async def show_alert(self, *, slot: str | None = None) -> None:
        await self._router.show_alert()

    async def show_ok(self, *, slot: str | None = None) -> None:
        await self._router.show_ok()

    async def set_settings(self, settings: dict) -> None:
        await self._router.set_settings(settings)

    async def get_settings(self) -> SimpleNamespace:
        return await self._router.get_settings()

    async def get_global_settings(self) -> SimpleNamespace:
        if self._settings_service is None or self._global_settings_target is None:
            return SimpleNamespace()
        settings = await self._settings_service.get(self._global_settings_target)
        return SimpleNamespace(**settings)

    async def set_global_settings(self, settings: dict) -> None:
        if self._settings_service is None or self._global_settings_target is None:
            return
        await self._settings_service.merge(self._global_settings_target, settings)

    async def open_url(self, url: str) -> None:
        await anyio.to_thread.run_sync(webbrowser.open, url)

    async def sleep_screen(self) -> None:
        await self._device.sleep_screen()

    async def wake_screen(self) -> None:
        await self._device.wake_screen()

    async def switch_to_profile(
        self,
        *,
        profile: str = "default",
        page: int = 0,
    ) -> None:
        await self._manager.set_page(profile=profile, page=page)

    async def open_page(self, descriptor: DynamicPageDescriptor) -> None:
        await self._manager.open_page(
            descriptor=descriptor, context_id=self._context_id
        )

    async def close_page(self) -> None:
        await self._manager.close_page(context_id=self._context_id)
