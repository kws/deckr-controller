"""Controller action context: thin facade for builtin action page/output commands."""

from collections.abc import Mapping
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from deckr.actions.messages import BindingMetadata, DynamicPageCommand

from deckr.controller._command_router import CommandRouter

if TYPE_CHECKING:
    from deckr.controller._bindings import PageCommandPort


class ControllerActionContext:
    """Thin facade for builtin actions: delegates to router and page commands."""

    def __init__(
        self,
        router: CommandRouter,
        page_command_port: "PageCommandPort",
        context_id: str,
        binding_metadata: BindingMetadata,
        settings: Mapping[str, Any],
    ):
        self._router = router
        self._page_command_port = page_command_port
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
        await self._page_command_port.set_page(profile=profile, page=page)

    async def open_page(self, descriptor: DynamicPageCommand) -> None:
        session = await self._page_command_port.open_page(
            descriptor=descriptor,
            context_id=self._context_id,
            binding_id=self.binding_metadata.binding_id,
        )
        if session is not None:
            self._page_session_context_id = session.context_id

    async def replace_page(self, descriptor: DynamicPageCommand) -> None:
        if self._page_session_context_id is None:
            return
        await self._page_command_port.replace_page(
            descriptor=descriptor,
            context_id=self._page_session_context_id,
        )

    async def close_page(self) -> None:
        if self._page_session_context_id is None:
            return
        await self._page_command_port.close_page(
            context_id=self._page_session_context_id
        )
