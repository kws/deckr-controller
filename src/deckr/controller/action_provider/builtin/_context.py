"""Controller action context: thin facade for builtin actions with direct access to controller."""

from collections.abc import Mapping
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from deckr.actions.messages import BindingMetadata, DynamicPageCommand

from deckr.controller._command_router import CommandRouter

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
        settings: Mapping[str, Any],
    ):
        self._router = router
        self._manager = manager
        self._context_id = context_id
        self.binding_metadata = binding_metadata
        self.settings = SimpleNamespace(**settings)
        self._page_session_context_id: str | None = None

    async def set_title(self, text: str) -> None:
        await self._router.set_title(text)

    async def set_raster_image(self, image: str) -> None:
        await self._router.set_raster_image(image)

    async def set_page(
        self,
        *,
        profile: str = "default",
        page: int = 0,
    ) -> None:
        await self._manager.set_page(profile=profile, page=page)

    async def open_page(self, descriptor: DynamicPageCommand) -> None:
        session = await self._manager.open_page(
            descriptor=descriptor,
            context_id=self._context_id,
            binding_id=self.binding_metadata.binding_id,
        )
        if session is not None:
            self._page_session_context_id = session.context_id

    async def replace_page(self, descriptor: DynamicPageCommand) -> None:
        if self._page_session_context_id is None:
            return
        await self._manager.replace_page(
            descriptor=descriptor,
            context_id=self._page_session_context_id,
        )

    async def close_page(self) -> None:
        if self._page_session_context_id is None:
            return
        await self._manager.close_page(context_id=self._page_session_context_id)
