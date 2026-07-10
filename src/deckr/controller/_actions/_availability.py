"""Local action availability cache for controller-side planning."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace

from deckr.actions.endpoints import (
    RESERVED_BUILTIN_PROVIDER_IDS,
)
from deckr.actions.messages import (
    ActionAvailabilityEntry,
)

from deckr.controller._actions._models import (
    ActionAvailabilityPolicy,
    ActionAvailabilityRecord,
    ActionAvailabilitySource,
    ActionAvailabilityState,
    ActionIntentKey,
    ActionMetadata,
    ActionPlanningSnapshot,
    ActionUnavailableCause,
    ProviderActionKey,
    ProviderSessionKey,
    SettingsActionMetadata,
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


@dataclass(frozen=True, slots=True)
class _PlanningRecord:
    record: ActionAvailabilityRecord
    state: ActionAvailabilityState


class ActionAvailabilityCache:
    """Pure local cache that answers planner metadata requests."""

    def __init__(
        self,
        *,
        policy: ActionAvailabilityPolicy | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._policy = policy or ActionAvailabilityPolicy()
        self._clock = clock or time.monotonic
        self._availability_records: dict[
            ProviderActionKey, ActionAvailabilityRecord
        ] = {}
        self._record_keys_by_intent: dict[ActionIntentKey, ProviderActionKey] = {}

    def record(self, record: ActionAvailabilityRecord) -> None:
        self._availability_records[record.key] = record

    def record_available(
        self,
        metadata: ActionMetadata,
        *,
        now: float | None = None,
        intent: ActionIntentKey | None = None,
        requires_provider_lifecycle_recovery: bool = False,
    ) -> ActionAvailabilityRecord:
        key = ProviderActionKey(
            provider_instance_id=metadata.provider_instance_id,
            action_uuid=metadata.uuid,
        )
        record = ActionAvailabilityRecord(
            key=key,
            state=ActionAvailabilityState.AVAILABLE,
            source=ActionAvailabilitySource.SERVICE_VIEW,
            updated_at=self._now(now),
            metadata=metadata,
            requires_provider_lifecycle_recovery=requires_provider_lifecycle_recovery,
        )
        self.record(record)
        if intent is not None:
            self._record_keys_by_intent[intent] = key
        return record

    def record_unavailable(
        self,
        key: ProviderActionKey,
        *,
        metadata: ActionMetadata | None = None,
        reason: str | None = None,
        now: float | None = None,
        intent: ActionIntentKey | None = None,
    ) -> ActionAvailabilityRecord:
        record = ActionAvailabilityRecord(
            key=key,
            state=ActionAvailabilityState.UNAVAILABLE,
            source=ActionAvailabilitySource.SERVICE_VIEW,
            updated_at=self._now(now),
            metadata=metadata,
            reason=reason,
        )
        self.record(record)
        if intent is not None:
            self._record_keys_by_intent[intent] = key
        return record

    def record_probing(
        self,
        key: ProviderActionKey,
        *,
        metadata: ActionMetadata | None = None,
        reason: str | None = None,
        now: float | None = None,
        intent: ActionIntentKey | None = None,
    ) -> ActionAvailabilityRecord:
        record = ActionAvailabilityRecord(
            key=key,
            state=ActionAvailabilityState.PROBING,
            source=ActionAvailabilitySource.SERVICE_VIEW,
            updated_at=self._now(now),
            metadata=metadata,
            reason=reason,
        )
        self.record(record)
        if intent is not None:
            self._record_keys_by_intent[intent] = key
        return record

    def record_for(
        self,
        key: ProviderActionKey,
    ) -> ActionAvailabilityRecord | None:
        return self._availability_records.get(key)

    def service_view_records(self) -> tuple[ActionAvailabilityRecord, ...]:
        return tuple(self._availability_records.values())

    def provider_lifecycle_recovery_required(self, key: ProviderActionKey) -> bool:
        record = self._availability_records.get(key)
        return (
            record is not None
            and record.source == ActionAvailabilitySource.SERVICE_VIEW
            and record.state == ActionAvailabilityState.AVAILABLE
            and record.requires_provider_lifecycle_recovery
        )

    def consume_provider_lifecycle_recovery(self, key: ProviderActionKey) -> bool:
        record = self._availability_records.get(key)
        if record is None or not record.requires_provider_lifecycle_recovery:
            return False
        self._availability_records[key] = replace(
            record,
            requires_provider_lifecycle_recovery=False,
        )
        return True

    def state_for(
        self,
        key: ProviderActionKey,
        *,
        now: float | None = None,
    ) -> ActionAvailabilityState | None:
        record = self.record_for(key)
        if record is None:
            return None
        return self._state_for_record(record, now=self._now(now))

    def record_for_intent(
        self,
        intent: ActionIntentKey,
        *,
        now: float | None = None,
    ) -> ActionAvailabilityRecord | None:
        lookup_now = self._now(now)
        selected = self._selected_planning_record(
            intent,
            now=lookup_now,
            stale_provider_keys=frozenset(),
        )
        if selected is None:
            return None
        return selected.record

    def intent_for_key(self, key: ProviderActionKey) -> ActionIntentKey | None:
        for intent, mapped_key in self._record_keys_by_intent.items():
            if mapped_key == key:
                return intent
        return None

    def planning_snapshot(
        self,
        intents: Iterable[ActionIntentKey],
        *,
        now: float | None = None,
        stale_provider_keys: Iterable[ProviderActionKey] = (),
        ready_provider_session_keys: Iterable[ProviderSessionKey] | None = None,
    ) -> ActionPlanningSnapshot:
        lookup_now = self._now(now)
        stale_keys = frozenset(stale_provider_keys)
        ready_session_keys = (
            None
            if ready_provider_session_keys is None
            else frozenset(ready_provider_session_keys)
        )
        metadata: dict[ActionIntentKey, ActionMetadata] = {}
        pending: set[ActionIntentKey] = set()
        unavailable: set[ActionIntentKey] = set()

        for intent in intents:
            selected = self._selected_planning_record(
                intent,
                now=lookup_now,
                stale_provider_keys=stale_keys,
                ready_provider_session_keys=ready_session_keys,
            )
            if selected is None:
                unavailable.add(intent)
                continue

            record = selected.record
            if self._planning_record_is_usable(
                selected,
                stale_provider_keys=stale_keys,
                ready_provider_session_keys=ready_session_keys,
            ):
                if record.metadata is not None:
                    metadata[intent] = record.metadata
                else:
                    pending.add(intent)
                continue
            if self._planning_record_is_pending(
                selected,
                ready_provider_session_keys=ready_session_keys,
            ):
                pending.add(intent)
                continue
            unavailable.add(intent)

        return ActionPlanningSnapshot(
            metadata=metadata,
            pending=frozenset(pending),
            unavailable=frozenset(unavailable),
        )

    def settings_metadata_for_intent(
        self,
        intent: ActionIntentKey,
        *,
        provider_id: str | None = None,
        now: float | None = None,
    ) -> SettingsActionMetadata:
        lookup_now = self._now(now)
        matches = [
            record
            for record in self._availability_records.values()
            if record.metadata is not None
            and self._record_matches_intent(record, intent, now=lookup_now)
            and (provider_id is None or record.metadata.provider_id == provider_id)
            and self._state_for_record(record, now=lookup_now)
            != ActionAvailabilityState.EXPIRED
        ]
        if matches:
            matches.sort(key=lambda record: record.key.provider_instance_id)
            selected = matches[0]
            return SettingsActionMetadata(
                action=selected.metadata,
                stale=self._state_for_record(selected, now=lookup_now)
                != ActionAvailabilityState.AVAILABLE,
            )
        return SettingsActionMetadata(action=None, stale=True)

    def _selected_planning_record(
        self,
        intent: ActionIntentKey,
        *,
        now: float,
        stale_provider_keys: frozenset[ProviderActionKey],
        ready_provider_session_keys: frozenset[ProviderSessionKey] | None = None,
    ) -> _PlanningRecord | None:
        records = self._planning_records_for_intent(intent, now=now)
        if not records:
            return None

        selected_key = self._record_keys_by_intent.get(intent)
        if selected_key is not None:
            for item in records:
                if item.record.key == selected_key and self._planning_record_is_usable(
                    item,
                    stale_provider_keys=stale_provider_keys,
                    ready_provider_session_keys=ready_provider_session_keys,
                ):
                    return item

        for item in records:
            if self._planning_record_is_fresh_available(
                item,
                ready_provider_session_keys=ready_provider_session_keys,
            ):
                self._record_keys_by_intent[intent] = item.record.key
                return item

        for item in records:
            if self._planning_record_is_service_view_pending(
                item,
                ready_provider_session_keys=ready_provider_session_keys,
            ):
                self._record_keys_by_intent[intent] = item.record.key
                return item

        selected = records[0]
        self._record_keys_by_intent[intent] = selected.record.key
        return selected

    def _planning_records_for_intent(
        self,
        intent: ActionIntentKey,
        *,
        now: float,
    ) -> tuple[_PlanningRecord, ...]:
        selected_key = self._record_keys_by_intent.get(intent)
        records = [
            _PlanningRecord(record=record, state=self._state_for_record(record, now=now))
            for record in self._availability_records.values()
            if self._record_matches_intent(
                record,
                intent,
                now=now,
                trust_mapped_key=record.key == selected_key,
            )
        ]
        records.sort(key=lambda item: item.record.key.provider_instance_id)
        return tuple(records)

    def _planning_record_is_usable(
        self,
        item: _PlanningRecord,
        *,
        stale_provider_keys: frozenset[ProviderActionKey],
        ready_provider_session_keys: frozenset[ProviderSessionKey] | None,
    ) -> bool:
        record = item.record
        metadata = record.metadata
        if record.source != ActionAvailabilitySource.SERVICE_VIEW:
            return False
        if record.state != ActionAvailabilityState.AVAILABLE:
            return False
        if item.state == ActionAvailabilityState.AVAILABLE:
            return metadata is not None and _metadata_has_ready_provider_session(
                metadata,
                ready_provider_session_keys=ready_provider_session_keys,
            )
        return (
            item.state == ActionAvailabilityState.STALE
            and record.key in stale_provider_keys
            and metadata is not None
            and _metadata_has_ready_provider_session(
                metadata,
                ready_provider_session_keys=ready_provider_session_keys,
            )
        )

    def _planning_record_is_fresh_available(
        self,
        item: _PlanningRecord,
        *,
        ready_provider_session_keys: frozenset[ProviderSessionKey] | None,
    ) -> bool:
        record = item.record
        metadata = record.metadata
        return (
            record.source == ActionAvailabilitySource.SERVICE_VIEW
            and record.state == ActionAvailabilityState.AVAILABLE
            and item.state == ActionAvailabilityState.AVAILABLE
            and metadata is not None
            and _metadata_has_ready_provider_session(
                metadata,
                ready_provider_session_keys=ready_provider_session_keys,
            )
        )

    def _planning_record_is_service_view_pending(
        self,
        item: _PlanningRecord,
        *,
        ready_provider_session_keys: frozenset[ProviderSessionKey] | None = None,
    ) -> bool:
        record = item.record
        if record.source != ActionAvailabilitySource.SERVICE_VIEW:
            return False
        if item.state == ActionAvailabilityState.EXPIRED:
            return False
        if (
            record.state == ActionAvailabilityState.AVAILABLE
            and record.metadata is not None
            and not _metadata_has_ready_provider_session(
                record.metadata,
                ready_provider_session_keys=ready_provider_session_keys,
            )
        ):
            return True
        if record.state == ActionAvailabilityState.UNAVAILABLE:
            return False
        return item.state in {
            ActionAvailabilityState.UNKNOWN,
            ActionAvailabilityState.PROBING,
            ActionAvailabilityState.STALE,
        }

    def _planning_record_is_pending(
        self,
        item: _PlanningRecord,
        *,
        ready_provider_session_keys: frozenset[ProviderSessionKey] | None,
    ) -> bool:
        return self._planning_record_is_service_view_pending(
            item,
            ready_provider_session_keys=ready_provider_session_keys,
        )

    def _record_matches_intent(
        self,
        record: ActionAvailabilityRecord,
        intent: ActionIntentKey,
        *,
        now: float,
        trust_mapped_key: bool = False,
    ) -> bool:
        if record.key.action_uuid != intent.action_uuid:
            return False
        if (
            intent.provider_instance_id is not None
            and record.key.provider_instance_id != intent.provider_instance_id
        ):
            return False
        metadata = record.metadata
        if metadata is None and trust_mapped_key:
            return True
        if metadata is None and not intent.provider_labels:
            return True
        if metadata is None:
            return False
        return _labels_match(metadata.provider_labels, intent.provider_labels)

    def _state_for_record(
        self,
        record: ActionAvailabilityRecord,
        *,
        now: float,
    ) -> ActionAvailabilityState:
        fresh_ttl = self._policy.fresh_ttl_seconds
        if fresh_ttl is None:
            return record.state
        age_seconds = max(0.0, now - record.updated_at)
        if age_seconds <= fresh_ttl:
            return record.state
        stale_grace = self._policy.stale_grace_seconds
        if stale_grace is not None and age_seconds <= fresh_ttl + stale_grace:
            return ActionAvailabilityState.STALE
        return ActionAvailabilityState.EXPIRED

    def _now(self, now: float | None) -> float:
        return self._clock() if now is None else now


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


def _json_hash(value: object) -> str | None:
    if value is None:
        return None
    if hasattr(value, "to_dict"):
        value = value.to_dict()
    elif hasattr(value, "model_dump"):
        value = value.model_dump(by_alias=True, exclude_none=True, mode="json")
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:_HASH_SIZE]


def _descriptor_hash(descriptor: object | None) -> str | None:
    return _json_hash(descriptor)


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
