"""Pure navigation rules for a configured route and one temporary page."""

from __future__ import annotations

from typing import TYPE_CHECKING

from deckr.controller._binding_resolution import ConfiguredControlBinding
from deckr.controller.config._data import DeviceConfig
from deckr.controller.settings import static_action_identity_fallback

if TYPE_CHECKING:
    pass


from deckr.controller._pages._models import (
    StaticPageRef,
)


def configured_bindings(
    config: DeviceConfig, ref: StaticPageRef
) -> list[ConfiguredControlBinding]:
    profile = next((p for p in config.profiles if p.name == ref.profile_name), None)
    if profile is None:
        raise ValueError(f"Unknown configured profile: {ref.profile_name}")
    if not 0 <= ref.page_index < len(profile.pages):
        raise ValueError("page index is outside the configured profile")
    page = profile.pages[ref.page_index]
    return [
        ConfiguredControlBinding(
            selector=control.selector,
            action_id=control.action,
            provider_instance_id=control.provider_instance_id,
            provider_labels=dict(control.provider_labels),
            settings=dict(control.settings),
            stable_id=control.id,
            identity_fallback=static_action_identity_fallback(
                selector_control_id=control.selector.control_id,
                control_index=control_index,
            ),
            template_overrides=dict(control.template_overrides),
        )
        for control_index, control in enumerate(page.controls)
    ]
