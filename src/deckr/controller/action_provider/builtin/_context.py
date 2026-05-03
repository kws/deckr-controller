"""Controller action context: thin facade for builtin actions with direct access to controller."""

from types import SimpleNamespace
from typing import TYPE_CHECKING

from deckr.actions.messages import BindingMetadata, DynamicPageCommand, TitleOptions

from deckr.controller._command_router import CommandRouter
from deckr.controller.settings import SettingsService

if TYPE_CHECKING:
    from deckr.controller._device_manager import DeviceManager


class ControllerActionContext:
    """Thin facade for builtin actions: delegates to router and manager."""

    def __init__(
        self,
        router: CommandRouter,
        manager: "DeviceManager",
        context_id: str,
        binding_metadata: BindingMetadata,
        settings_service: SettingsService | None = None,
    ):
        self._router = router
        self._manager = manager
        self._context_id = context_id
        self.binding_metadata = binding_metadata
        self._settings_service = settings_service

    async def set_title(
        self,
        text: str,
        *,
        title_options: TitleOptions | None = None,
    ) -> None:
        await self._router.set_title(text, title_options=title_options)

    async def set_raster_image(self, image: str) -> None:
        await self._router.set_raster_image(image)

    async def set_settings(self, settings: dict) -> SimpleNamespace:
        return await self._router.set_settings(settings)

    async def get_settings(self) -> SimpleNamespace:
        return await self._router.get_settings()

    async def set_page(
        self,
        *,
        profile: str = "default",
        page: int = 0,
    ) -> None:
        await self._manager.set_page(profile=profile, page=page)

    async def open_page(self, descriptor: DynamicPageCommand) -> None:
        await self._manager.open_page(
            descriptor=descriptor, context_id=self._context_id
        )

    async def update_page(self, descriptor: DynamicPageCommand) -> None:
        await self._manager.update_page(
            descriptor=descriptor, context_id=self._context_id
        )

    async def replace_page(self, descriptor: DynamicPageCommand) -> None:
        await self._manager.replace_page(
            descriptor=descriptor, context_id=self._context_id
        )

    async def close_page(self) -> None:
        await self._manager.close_page(context_id=self._context_id)
