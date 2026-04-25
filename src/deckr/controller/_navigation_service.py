"""Page stack and transition logic. Does not create contexts or render."""

from __future__ import annotations

from dataclasses import dataclass

from deckr.plugin.messages import (
    DynamicPageDescriptor,
    SlotBinding,
    TitleOptions,
)

from deckr.controller.config._data import (
    DeviceConfig,
)
from deckr.controller.config._data import (
    TitleOptions as ConfigTitleOptions,
)


def _config_title_options_to_store(
    opts: ConfigTitleOptions | None,
) -> TitleOptions | None:
    """Convert config TitleOptions to plugin message TitleOptions."""
    if opts is None:
        return None
    return TitleOptions(
        font_family=opts.font_family,
        font_size=opts.font_size,
        font_style=opts.font_style,
        title_color=opts.title_color,
        title_alignment=opts.title_alignment,
    )


@dataclass(frozen=True)
class StaticPageRef:
    profile_name: str
    page_index: int


PageStackEntry = StaticPageRef | DynamicPageDescriptor


@dataclass
class PageTransition:
    """Result of a navigation operation: what to tear down and what to show."""

    departing: PageStackEntry | None
    arriving: PageStackEntry


class NavigationService:
    """Owns current page and computes transitions. No stack; pages identified by (profile, page_number)."""

    def __init__(self, config: DeviceConfig):
        self._config = config
        self._current_page: PageStackEntry | None = None

    @property
    def current_page(self) -> PageStackEntry | None:
        return self._current_page

    def set_page(self, entry: PageStackEntry) -> PageTransition:
        departing = self._current_page
        self._current_page = entry
        return PageTransition(departing=departing, arriving=entry)

    def update_config(self, config: DeviceConfig) -> PageTransition:
        """Update config and reset to root. Returns transition for caller to execute."""
        departing = self._current_page
        self._config = config
        profile = config.profiles[0]
        root = StaticPageRef(profile_name=profile.name, page_index=0)
        self._current_page = root
        return PageTransition(departing=departing, arriving=root)

    def switch_profile(self, profile_name: str) -> PageTransition:
        departing = self._current_page
        profile = self._find_profile(profile_name)
        root = StaticPageRef(profile_name=profile.name, page_index=0)
        self._current_page = root
        return PageTransition(departing=departing, arriving=root)

    def _find_profile(self, profile_name: str):
        for p in self._config.profiles:
            if p.name == profile_name:
                return p
        return self._config.profiles[0]

    def resolve_static_bindings(self, ref: StaticPageRef) -> list[SlotBinding]:
        """Return slot bindings for a static page from config."""
        profile = self._find_profile(ref.profile_name)
        page = profile.pages[ref.page_index]
        return [
            SlotBinding(
                slot_id=c.slot,
                action_uuid=c.action,
                settings=dict(c.settings),
                title_options=_config_title_options_to_store(c.title_options),
            )
            for c in page.controls
        ]
