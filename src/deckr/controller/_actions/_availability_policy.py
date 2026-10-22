"""Pure availability classification, matching predicates and diagnostics."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

from deckr.actions.catalog import ActionDescriptor
from deckr.actions.endpoints import (
    RESERVED_BUILTIN_PROVIDER_IDS,
)
from deckr.actions.messages import (
    ActionAvailabilityEntry,
)

from deckr.controller._actions._models import (
    ActionAvailabilityRecord,
    ActionAvailabilitySource,
    ActionAvailabilityState,
    ActionMetadata,
    ActionUnavailableCause,
    ProviderSessionKey,
)

PROVIDER_SESSION_INVALID_REASON = "provider_session_invalid"

SERVICE_VIEW_MISSING_REASON = "action_availability_view_missing"

SERVICE_VIEW_UNAVAILABLE_REASON = "action_availability_service_unavailable"

_HASH_SIZE = 12

UNAVAILABLE_OVERLAY_TEMPLATE_BY_CAUSE: Mapping[ActionUnavailableCause, str] = {
    ActionUnavailableCause.MISSING: "unavailable_missing",
    ActionUnavailableCause.SERVICE: "unavailable_service",
    ActionUnavailableCause.SESSION: "unavailable_session",
    ActionUnavailableCause.REJECTED: "unavailable_rejected",
    ActionUnavailableCause.UNKNOWN: "unavailable_unknown",
}

_SERVICE_UNAVAILABLE_REASONS = frozenset(
    {
        "service_unavailable",
        "openhab_service_unavailable",
        "sonos_service_unavailable",
    }
)

_SESSION_UNAVAILABLE_REASONS = frozenset(
    {
        PROVIDER_SESSION_INVALID_REASON,
        "provider_session_unavailable",
        "missing_provider_session_contract",
    }
)

_REJECTED_UNAVAILABLE_REASONS = frozenset(
    {
        "action_not_available",
        "resource_unavailable",
        "provider_not_ready",
        "internal_error",
    }
)


def action_unavailable_cause(
    record: ActionAvailabilityRecord | None,
    *,
    has_live_provider_session_contract: bool | None = None,
) -> ActionUnavailableCause:
    """Classify the controller fallback overlay for an unavailable action."""

    reason = record.reason if record is not None else None
    if _service_unavailable_reason(reason):
        return ActionUnavailableCause.SERVICE
    if reason in _SESSION_UNAVAILABLE_REASONS:
        return ActionUnavailableCause.SESSION
    if has_live_provider_session_contract is False:
        return ActionUnavailableCause.SESSION
    if reason in _REJECTED_UNAVAILABLE_REASONS:
        return ActionUnavailableCause.REJECTED
    if record is None:
        return ActionUnavailableCause.MISSING
    if _metadata_missing_provider_session(record.metadata):
        return ActionUnavailableCause.SESSION
    return ActionUnavailableCause.UNKNOWN


def unavailable_overlay_template(cause: ActionUnavailableCause) -> str:
    return UNAVAILABLE_OVERLAY_TEMPLATE_BY_CAUSE[cause]


def _service_unavailable_reason(reason: str | None) -> bool:
    return reason is not None and (
        reason in _SERVICE_UNAVAILABLE_REASONS
        or reason.endswith("_service_unavailable")
    )


def _metadata_missing_provider_session(metadata: ActionMetadata | None) -> bool:
    return (
        metadata is not None
        and metadata.provider_instance_id not in RESERVED_BUILTIN_PROVIDER_IDS
        and metadata.provider_session_id is None
    )


def _labels_match(
    actual: Mapping[str, str] | None,
    required: tuple[tuple[str, str], ...],
) -> bool:
    if not required:
        return True
    actual = actual or {}
    return all(actual.get(key) == value for key, value in required)


def _metadata_has_ready_provider_session(
    metadata: ActionMetadata,
    *,
    ready_provider_session_keys: frozenset[ProviderSessionKey] | None,
) -> bool:
    if metadata.provider_instance_id in RESERVED_BUILTIN_PROVIDER_IDS:
        return True
    if metadata.provider_session_id is None:
        return False
    if ready_provider_session_keys is None:
        return True
    return (
        ProviderSessionKey(
            metadata.provider_instance_id,
            metadata.provider_id,
            metadata.provider_session_id,
        )
        in ready_provider_session_keys
    )


def _metadata_requires_provider_session_revalidation(
    metadata: ActionMetadata | None,
) -> bool:
    return (
        metadata is not None
        and metadata.provider_instance_id not in RESERVED_BUILTIN_PROVIDER_IDS
        and bool(metadata.provider_session_id)
    )


def _availability_state_for_entry(
    entry: ActionAvailabilityEntry,
) -> ActionAvailabilityState:
    if entry.status == "available":
        return ActionAvailabilityState.AVAILABLE
    if entry.status == "unavailable":
        return ActionAvailabilityState.UNAVAILABLE
    return ActionAvailabilityState.PROBING


def _state_value(state: ActionAvailabilityState | None) -> str | None:
    return state.value if state is not None else None


def _descriptor_hash(descriptor: ActionDescriptor | None) -> str | None:
    if descriptor is None:
        return None
    payload = json.dumps(descriptor.to_dict(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:_HASH_SIZE]


def _entry_same_as_existing(
    existing: ActionAvailabilityRecord | None,
    new_state: ActionAvailabilityState,
    metadata: ActionMetadata | None,
    reason: str | None,
    *,
    requires_provider_lifecycle_recovery: bool = False,
) -> bool:
    return (
        existing is not None
        and existing.source == ActionAvailabilitySource.SERVICE_VIEW
        and existing.state == new_state
        and existing.metadata == metadata
        and existing.reason == reason
        and existing.requires_provider_lifecycle_recovery
        == requires_provider_lifecycle_recovery
    )
