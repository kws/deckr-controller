"""Internal controller events for action-provider catalog state."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderSessionSuccession:
    """Catalog observation that a provider advertised a successor endpoint session."""

    provider_instance_id: str
    provider_id: str
    previous_session_id: str
    successor_session_id: str
    actions: list[str]  # qualified provider_instance::action ids affected


@dataclass(frozen=True)
class ActionCatalogChangedEvent:
    """Emitted by ActionRegistry when Beacon catalog snapshots change."""

    catalog_added: list[str]
    catalog_removed: list[str]
    catalog_updated: list[str]
    provider_session_successions: list[ProviderSessionSuccession]
