"""Internal controller action-service data models."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from deckr.controller.action_provider.builtin import BuiltinAction

from deckr.actions.bindings import CapabilityRequirement
from deckr.actions.catalog import ActionDescriptor
from deckr.contracts.models import freeze_json


@dataclass(frozen=True, slots=True)
class ActionIntentKey:
    action_id: str
    provider_instance_id: str | None
    provider_labels: tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class ActionMetadata:
    """Metadata for an action known to the controller.

    ``provider_session_id`` is live routing metadata. It is populated only from
    the contract-fenced action runtime availability view.
    """

    action_id: str
    provider_instance_id: str
    provider_id: str
    name: str | None = None
    provider_session_id: str | None = None
    provider_labels: Mapping[str, str] | None = None
    settings_schema: Mapping | None = None
    provider_settings_schema: Mapping | None = None
    descriptor: ActionDescriptor | None = None

    def __post_init__(self):
        for name in ("provider_labels", "settings_schema", "provider_settings_schema"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, freeze_json(value))

    @property
    def requirements(self) -> tuple[CapabilityRequirement, ...] | None:
        return self.descriptor.requirements if self.descriptor is not None else None


class BuiltinActionCatalog(Protocol):
    """Builtin action resolver interface consumed by controller services."""

    async def get_action(
        self,
        action_id: str,
        *,
        provider_instance_id: str | None = None,
        provider_labels: Mapping[str, str] | None = None,
    ) -> ActionMetadata | None: ...

    def catalog_snapshot(self) -> tuple[ActionMetadata, ...]: ...

    def get_builtin_action(self, action_id: str) -> BuiltinAction | None: ...


@dataclass(frozen=True, slots=True)
class ProviderActionKey:
    provider_instance_id: str
    action_id: str


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


class ActionAvailabilitySource(StrEnum):
    SERVICE_VIEW = "service_view"


class ActionAvailabilityState(StrEnum):
    UNKNOWN = "unknown"
    PROBING = "probing"
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    STALE = "stale"
    EXPIRED = "expired"


class ActionUnavailableCause(StrEnum):
    MISSING = "missing"
    SERVICE = "service"
    SESSION = "session"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ActionAvailabilityRecord:
    key: ProviderActionKey
    state: ActionAvailabilityState
    source: ActionAvailabilitySource
    updated_at: float
    metadata: ActionMetadata | None = None
    reason: str | None = None
    requires_provider_lifecycle_recovery: bool = False


@dataclass(frozen=True, slots=True)
class ActionAvailabilityPolicy:
    fresh_ttl_seconds: float | None = None
    stale_grace_seconds: float | None = None
    unreferenced_retention_seconds: float = 300
    max_unreferenced_records: int = 1024


@dataclass(frozen=True, slots=True)
class ActionPlanningSnapshot:
    metadata: Mapping[ActionIntentKey, ActionMetadata]
    pending: frozenset[ActionIntentKey]
    unavailable: frozenset[ActionIntentKey]

    def __post_init__(self):
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True, slots=True)
class SettingsActionMetadata:
    action: ActionMetadata | None
    stale: bool


AvailabilityChangedCallback = Callable[[frozenset[ProviderActionKey]], object]
