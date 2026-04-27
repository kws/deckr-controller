"""Builtin plugin context: thin facade for builtin actions with direct access to controller."""

from types import SimpleNamespace
from typing import TYPE_CHECKING

from deckr.pluginhost.messages import DynamicPageDescriptor, TitleOptions
from deckr.python_plugin.interface import PluginContext as PluginContextProtocol

from deckr.controller._command_router import CommandRouter
from deckr.controller.settings import SettingsService

if TYPE_CHECKING:
    from deckr.controller._device_manager import DeviceManager
    from deckr.controller._hardware_service import HardwareCommandService


class BuiltInPluginContext(PluginContextProtocol):
    """Thin facade for builtin actions: delegates to router, hardware commands, and manager."""

    def __init__(
        self,
        router: CommandRouter,
        command_service: "HardwareCommandService",
        config_id: str,
        manager: "DeviceManager",
        context_id: str,
        settings_service: SettingsService | None = None,
    ):
        self._router = router
        self._command_service = command_service
        self._config_id = config_id
        self._manager = manager
        self._context_id = context_id
        self._settings_service = settings_service

    async def set_title(
        self,
        text: str,
        *,
        title_options: TitleOptions | None = None,
    ) -> None:
        await self._router.set_title(text, title_options=title_options)

    async def set_image(self, image: str) -> None:
        await self._router.set_image(image)

    async def show_alert(self) -> None:
        await self._router.show_alert()

    async def show_ok(self) -> None:
        await self._router.show_ok()

    async def set_settings(self, settings: dict) -> SimpleNamespace:
        return await self._router.set_settings(settings)

    async def get_settings(self) -> SimpleNamespace:
        return await self._router.get_settings()

    async def sleep_screen(self) -> None:
        await self._command_service.sleep_screen(self._config_id)

    async def wake_screen(self) -> None:
        await self._command_service.wake_screen(self._config_id)

    async def set_page(
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

    async def update_page(self, descriptor: DynamicPageDescriptor) -> None:
        await self._manager.update_page(
            descriptor=descriptor, context_id=self._context_id
        )

    async def replace_page(self, descriptor: DynamicPageDescriptor) -> None:
        await self._manager.replace_page(
            descriptor=descriptor, context_id=self._context_id
        )

    async def close_page(self) -> None:
        await self._manager.close_page(context_id=self._context_id)
