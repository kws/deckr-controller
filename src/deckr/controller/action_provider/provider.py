"""Controller protocols used for action-provider lookup and dispatch metadata."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol


@dataclass
class ActionMetadata:
    """Metadata for an action from provider-instance current state."""

    uuid: str
    provider_instance_id: str
    provider_id: str
    name: str | None = None
    catalog_session_id: str | None = None
    provider_labels: Mapping[str, str] | None = None
    settings_schema: dict | None = None
    provider_settings_schema: dict | None = None


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

    def provider_session_id(self, provider_instance_id: str) -> str | None: ...
