"""Immutable action identity and a narrow port for controller requests."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from deckr.actions.messages import BindingMetadata, DynamicPageCommand
from deckr.contracts.models import freeze_json

from deckr.controller._actions._requests import (
    ActionRequestPort,
    ClosePage,
    Navigate,
    OpenPage,
    PageGrant,
    SetRasterImage,
    SetTitle,
    UpdatePage,
)


@dataclass(frozen=True, slots=True)
class SettingsView:
    _values: Mapping[str, Any]

    def __getattr__(self, name):
        try:
            return self._values[name]
        except KeyError:
            raise AttributeError(name) from None


class ControllerActionContext:
    def __init__(
        self,
        request_port: ActionRequestPort,
        binding_metadata: BindingMetadata,
        settings: Mapping[str, Any],
    ):
        self._requests = request_port
        self.binding_metadata = binding_metadata
        self.replace_settings(settings)
        self._page_grant: PageGrant | None = None

    @property
    def settings(self) -> SettingsView:
        return self._settings

    def replace_settings(self, settings: Mapping[str, Any]) -> None:
        self._settings = SettingsView(freeze_json(settings))

    async def set_title(self, text: str) -> None:
        await self._requests.request(SetTitle(text))

    async def set_raster_image(self, image: str) -> None:
        await self._requests.request(SetRasterImage(image))

    async def set_page(self, *, profile: str = "default", page: int = 0) -> None:
        await self._requests.request(Navigate(profile, page))

    async def open_page(self, descriptor: DynamicPageCommand) -> None:
        grant = await self._requests.request(OpenPage(descriptor))
        if isinstance(grant, PageGrant):
            self._page_grant = grant

    async def replace_page(self, descriptor: DynamicPageCommand) -> None:
        if self._page_grant is not None:
            grant = await self._requests.request(
                UpdatePage(self._page_grant, descriptor)
            )
            if isinstance(grant, PageGrant):
                self._page_grant = grant

    async def close_page(self) -> None:
        if self._page_grant is not None:
            await self._requests.request(ClosePage(self._page_grant))
