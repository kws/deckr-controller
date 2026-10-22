"""Pure page binding planning for the controller runtime."""

from __future__ import annotations

from deckr.actions.messages import (
    PageChildBindingDescriptor,
)

from deckr.controller._binding_resolution import (
    ResolvedControlBinding,
)
from deckr.controller._pages import DynamicPageSession, StaticPageRef
from deckr.controller.settings import (
    derive_action_instance_id,
    derive_static_action_instance_id,
)


def static_action_instance_id(
    controller_id: str,
    config_id: str,
    entry: StaticPageRef,
    binding: ResolvedControlBinding,
) -> str:
    return derive_static_action_instance_id(
        controller_id=controller_id,
        config_id=config_id,
        action_id=binding.action_id,
        stable_id=binding.stable_id,
        profile_id=entry.profile_name,
        page_id=str(entry.page_index),
        selector_control_id=binding.selector.control_id,
        identity_fallback=binding.identity_fallback,
    )


def dynamic_child_action_instance_id(
    controller_id: str,
    config_id: str,
    *,
    page_session: DynamicPageSession,
    page_session_generation: int | None,
    child: PageChildBindingDescriptor,
    binding: ResolvedControlBinding,
) -> str:
    if child.target.kind == "self":
        return page_session.action_instance_id

    provider_key = binding.provider_instance_id or ""
    if binding.provider_labels:
        provider_key = "|".join(
            (
                provider_key,
                *(
                    f"{key}={value}"
                    for key, value in sorted(binding.provider_labels.items())
                ),
            )
        )
    target_key = child.target.instance_key or child.control_id
    stable_id = "\x1f".join(
        (
            "dynamic-page",
            page_session.page_session_id,
            str(
                page_session.generation
                if page_session_generation is None
                else page_session_generation
            ),
            provider_key,
            target_key,
        )
    )
    return derive_action_instance_id(
        controller_id=controller_id,
        config_id=config_id,
        action_id=binding.action_id,
        stable_id=stable_id,
    )
