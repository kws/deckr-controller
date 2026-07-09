"""Internal controller action-service data models."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from deckr.controller._binding_planner import ActionIntentKey


@dataclass
class ActionMetadata:
    """Metadata for an action known to the controller.

    ``provider_session_id`` is live routing metadata. It is populated only from
    the contract-fenced action runtime availability view.
    """

    uuid: str
    provider_instance_id: str
    provider_id: str
    name: str | None = None
    provider_session_id: str | None = None
    provider_labels: Mapping[str, str] | None = None
    settings_schema: dict | None = None
    provider_settings_schema: dict | None = None


class ActionProviderManager(Protocol):
    """Builtin action resolver interface consumed by controller services."""

    async def get_action(
        self,
        uuid: str,
        *,
        provider_instance_id: str | None = None,
        provider_labels: Mapping[str, str] | None = None,
    ) -> ActionMetadata | None: ...


@dataclass(frozen=True, slots=True)
class ProviderActionKey:
    provider_instance_id: str
    action_uuid: str


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


@dataclass(frozen=True, slots=True)
class ActionPlanningSnapshot:
    metadata: Mapping[ActionIntentKey, ActionMetadata]
    pending: frozenset[ActionIntentKey]
    unavailable: frozenset[ActionIntentKey]


@dataclass(frozen=True, slots=True)
class SettingsActionMetadata:
    action: ActionMetadata | None
    stale: bool


AvailabilityChangedCallback = Callable[[frozenset[ProviderActionKey]], object]
