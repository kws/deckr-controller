"""Pure navigation rules for a configured route and one temporary page."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from deckr.actions.catalog import ActionDescriptor
from deckr.actions.messages import (
    DynamicPageCommand,
    SettingsTargetRef,
)
from deckr.contracts.authority import ContractPointer

from deckr.controller._actions._models import ActionMetadata

if TYPE_CHECKING:
    from deckr.controller._binding_plan import PagePlan


DEFAULT_WIDGET_TIMEOUT_MS = 60_000


@dataclass(frozen=True, slots=True)
class StaticPageRef:
    profile_name: str
    page_index: int


PageRef = StaticPageRef | DynamicPageCommand


@dataclass(slots=True)
class DynamicPageSession:
    page_id: str
    page_session_id: str
    context_id: str
    action_instance_id: str
    owner_context_id: str
    owner_binding_id: str
    owner_control_id: str
    owner_action_id: str
    owner_provider_instance_id: str
    owner_provider_id: str
    owner_provider_session_id: str | None
    owner_action_meta: ActionMetadata
    owner_profile: str
    owner_page: int
    timeout_ms: int
    last_activity: float
    settings_target: SettingsTargetRef | None
    generation: int = 0
    owner_contract: ContractPointer | None = None
    deadline_revision: int = 0
    source: Literal["provider", "configuration"] = "provider"
    presentation: Literal["temporary", "route"] = "temporary"
    lifetime_owner: Literal["action_instance", "controller"] = "action_instance"


@dataclass(slots=True)
class PageFrame:
    entry: PageRef
    page_session: DynamicPageSession | None
    committed_plan: PagePlan


@dataclass(slots=True)
class NavigationState:
    configured_route: PageFrame | None = None
    temporary_page: PageFrame | None = None
    revision: int = 0

    @property
    def frames(self) -> tuple[PageFrame, ...]:
        return tuple(
            frame
            for frame in (self.configured_route, self.temporary_page)
            if frame is not None
        )


@dataclass(frozen=True, slots=True)
class PageDeadline:
    page_session_id: str
    revision: int
    due_at: float


@dataclass(frozen=True, slots=True)
class PageOwnerBinding:
    context_id: str
    binding_id: str
    control_id: str
    action_id: str
    provider_instance_id: str
    provider_id: str
    provider_session_id: str | None
    action_instance_id: str
    profile_id: str
    page_id: str
    page_session_id: str | None
    settings_target: SettingsTargetRef | None
    action_descriptor: ActionDescriptor | None = None
    contract: ContractPointer | None = None


@dataclass(frozen=True, slots=True)
class PageStateView:
    """Borrowed navigation records, valid only while holding the device gate.

    Frames and page sessions retain identity and may change at the next commit.
    This view is never used as an independently immutable diagnostic snapshot.
    """

    config_active: bool
    frames: tuple[PageFrame, ...]
    current_frame: PageFrame | None
    current_plan: PagePlan | None
    active_dynamic_session: DynamicPageSession | None


@dataclass(frozen=True, slots=True)
class PageTransitionDraft:
    kind: Literal["set", "open", "replace", "close", "refresh"]
    entry: PageRef
    page_session: DynamicPageSession | None
    departing: PageRef | None
    retained_plan: PagePlan | None
    preserve_rebound_outputs: bool
    sessions_to_close: tuple[DynamicPageSession, ...] = ()
    previous_dynamic_plans: tuple[PagePlan, ...] = ()
    cleanup_reason: str = "navigate"
    page_session_generation: int | None = None


@dataclass(frozen=True, slots=True)
class PageTransitionEffects:
    sessions_to_close: tuple[DynamicPageSession, ...] = ()
    previous_dynamic_plans: tuple[PagePlan, ...] = ()
    cleanup_reason: str = "navigate"
