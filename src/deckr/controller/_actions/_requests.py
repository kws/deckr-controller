"""Application requests shared by action adapters; no transport or UI access."""

from dataclasses import dataclass
from typing import Protocol

from deckr.actions.messages import DynamicPageCommand


@dataclass(frozen=True, slots=True)
class BindingGrant:
    device_session_id: str
    binding_id: str
    context_id: str
    action_instance_id: str
    may_navigate: bool = False


@dataclass(frozen=True, slots=True)
class PageGrant:
    device_session_id: str
    page_session_id: str
    context_id: str
    action_instance_id: str
    revision: int


@dataclass(frozen=True, slots=True)
class Navigate:
    profile: str = "default"
    page: int = 0


@dataclass(frozen=True, slots=True)
class OpenPage:
    definition: DynamicPageCommand


@dataclass(frozen=True, slots=True)
class UpdatePage:
    grant: PageGrant
    definition: DynamicPageCommand


@dataclass(frozen=True, slots=True)
class ClosePage:
    grant: PageGrant


@dataclass(frozen=True, slots=True)
class SetTitle:
    text: str


@dataclass(frozen=True, slots=True)
class SetRasterImage:
    image: str


ActionRequest = Navigate | OpenPage | UpdatePage | ClosePage | SetTitle | SetRasterImage


class ActionRequestPort(Protocol):
    async def request(self, request: ActionRequest) -> PageGrant | bool | None: ...
