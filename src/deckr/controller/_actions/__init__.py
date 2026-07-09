"""Private controller action-service API."""

from deckr.controller._actions._models import (
    ActionAvailabilityPolicy,
    ActionAvailabilityRecord,
    ActionAvailabilitySource,
    ActionAvailabilityState,
    ActionMetadata,
    ActionPlanningSnapshot,
    ActionProviderManager,
    ActionUnavailableCause,
    ProviderActionKey,
    ProviderSessionKey,
    SettingsActionMetadata,
    provider_session_key,
)

__all__ = [
    "PROVIDER_SESSION_INVALID_REASON",
    "SERVICE_VIEW_MISSING_REASON",
    "SERVICE_VIEW_UNAVAILABLE_REASON",
    "ActionAvailabilityCache",
    "ActionAvailabilityPolicy",
    "ActionAvailabilityRecord",
    "ActionAvailabilitySource",
    "ActionAvailabilityState",
    "ActionMetadata",
    "ActionPlanningSnapshot",
    "ActionProviderManager",
    "ActionUnavailableCause",
    "ControllerActionService",
    "ProviderActionKey",
    "ProviderSessionKey",
    "SettingsActionMetadata",
    "action_unavailable_cause",
    "provider_session_key",
    "unavailable_overlay_template",
]

_AVAILABILITY_EXPORTS = {
    "PROVIDER_SESSION_INVALID_REASON",
    "SERVICE_VIEW_MISSING_REASON",
    "SERVICE_VIEW_UNAVAILABLE_REASON",
    "ActionAvailabilityCache",
    "action_unavailable_cause",
    "unavailable_overlay_template",
}


def __getattr__(name: str):
    if name == "ControllerActionService":
        from deckr.controller._actions._service import ControllerActionService

        return ControllerActionService
    if name in _AVAILABILITY_EXPORTS:
        from deckr.controller import _action_availability

        return getattr(_action_availability, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
