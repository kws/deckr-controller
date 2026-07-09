from __future__ import annotations

from dataclasses import dataclass

from deckr.controller.action_provider.provider import ActionMetadata


@dataclass(frozen=True, slots=True)
class ProviderSessionKey:
    provider_instance_id: str
    provider_id: str
    provider_session_id: str


def provider_session_key(action: ActionMetadata) -> ProviderSessionKey | None:
    provider_session_id = action.provider_session_id
    if provider_session_id is None:
        return None
    return ProviderSessionKey(
        provider_instance_id=action.provider_instance_id,
        provider_id=action.provider_id,
        provider_session_id=provider_session_id,
    )
