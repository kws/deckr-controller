"""Controller protocols used for action-provider lookup and dispatch metadata."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol


@dataclass
class ActionMetadata:
    """Metadata for an action.

    ``provider_session_id`` is live routing metadata. Beacon advertisements must
    not populate it; only the contract-fenced action availability service view may.
    """

    uuid: str
    provider_instance_id: str
    provider_id: str
    name: str | None = None
    provider_session_id: str | None = None
    provider_labels: Mapping[str, str] | None = None
    settings_schema: dict | None = None
    provider_settings_schema: dict | None = None


@dataclass(frozen=True, slots=True)
class ActionProviderSessionCandidate:
    """Beacon-discovered provider runtime session for Concord negotiation only."""

    provider_instance_id: str
    provider_id: str
    provider_session_id: str


class ActionProviderManager(Protocol):
    """Active manager interface consumed by DeviceManager."""

    async def get_action(
        self,
        uuid: str,
        *,
        provider_instance_id: str | None = None,
        provider_labels: Mapping[str, str] | None = None,
    ) -> ActionMetadata | None: ...

    def provider_instance_provides_provider(
        self,
        provider_instance_id: str,
        provider_id: str,
    ) -> bool: ...

    def provider_session_candidate(
        self,
        provider_instance_id: str,
        provider_id: str,
    ) -> ActionProviderSessionCandidate | None: ...
