"""Local action availability cache for controller-side planning."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum

from deckr.controller._binding_planner import ActionIntentKey
from deckr.controller.action_provider.provider import ActionMetadata


@dataclass(frozen=True, slots=True)
class ProviderActionKey:
    provider_instance_id: str
    action_uuid: str


class ActionAvailabilitySource(StrEnum):
    BEACON_CANDIDATE = "beacon_candidate"
    PROVIDER_DIRECT = "provider_direct"


class ActionAvailabilityState(StrEnum):
    UNKNOWN = "unknown"
    PROBING = "probing"
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"
    STALE = "stale"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class ActionAvailabilityRecord:
    key: ProviderActionKey
    state: ActionAvailabilityState
    source: ActionAvailabilitySource
    updated_at: float
    metadata: ActionMetadata | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class ActionAvailabilityPolicy:
    fresh_ttl_seconds: float | None = None
    stale_grace_seconds: float | None = None
    candidate_ttl_seconds: float | None = None


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
        self._candidate_records: dict[ProviderActionKey, ActionAvailabilityRecord] = {}
        self._record_keys_by_intent: dict[ActionIntentKey, ProviderActionKey] = {}

    def record(self, record: ActionAvailabilityRecord) -> None:
        if record.source == ActionAvailabilitySource.BEACON_CANDIDATE:
            self._candidate_records[record.key] = record
            return
        self._availability_records[record.key] = record

    def record_candidate(
        self,
        metadata: ActionMetadata,
        *,
        now: float | None = None,
        intent: ActionIntentKey | None = None,
        state: ActionAvailabilityState = ActionAvailabilityState.UNKNOWN,
    ) -> ActionAvailabilityRecord:
        if state not in (
            ActionAvailabilityState.UNKNOWN,
            ActionAvailabilityState.PROBING,
        ):
            raise ValueError("candidate state must be unknown or probing")
        key = ProviderActionKey(
            provider_instance_id=metadata.provider_instance_id,
            action_uuid=metadata.uuid,
        )
        record = ActionAvailabilityRecord(
            key=key,
            state=state,
            source=ActionAvailabilitySource.BEACON_CANDIDATE,
            updated_at=self._now(now),
            metadata=metadata,
        )
        self.record(record)
        if intent is not None:
            self._record_keys_by_intent[intent] = key
        return record

    def record_available(
        self,
        metadata: ActionMetadata,
        *,
        now: float | None = None,
        intent: ActionIntentKey | None = None,
    ) -> ActionAvailabilityRecord:
        key = ProviderActionKey(
            provider_instance_id=metadata.provider_instance_id,
            action_uuid=metadata.uuid,
        )
        record = ActionAvailabilityRecord(
            key=key,
            state=ActionAvailabilityState.AVAILABLE,
            source=ActionAvailabilitySource.PROVIDER_DIRECT,
            updated_at=self._now(now),
            metadata=metadata,
        )
        self.record(record)
        if intent is not None:
            self._record_keys_by_intent[intent] = key
        return record

    def remove_candidate(
        self,
        key: ProviderActionKey,
    ) -> ActionAvailabilityRecord | None:
        record = self._candidate_records.get(key)
        if record is None:
            return None
        removed = self._candidate_records.pop(key)
        self._remove_intent_mappings_for_key(key)
        return removed

    def remove_candidates(
        self,
        keys: Iterable[ProviderActionKey],
    ) -> tuple[ActionAvailabilityRecord, ...]:
        removed: list[ActionAvailabilityRecord] = []
        for key in keys:
            record = self.remove_candidate(key)
            if record is not None:
                removed.append(record)
        return tuple(removed)

    def record_for(
        self,
        key: ProviderActionKey,
    ) -> ActionAvailabilityRecord | None:
        return self._availability_records.get(key) or self._candidate_records.get(key)

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

    def snapshot_for_intents(
        self,
        intents: Iterable[ActionIntentKey],
        *,
        now: float | None = None,
    ) -> dict[ActionIntentKey, ActionMetadata]:
        snapshot: dict[ActionIntentKey, ActionMetadata] = {}
        snapshot_now = self._now(now)
        for intent in intents:
            metadata = self._metadata_for_intent(intent, now=snapshot_now)
            if metadata is not None:
                snapshot[intent] = metadata
        return snapshot

    def _metadata_for_intent(
        self,
        intent: ActionIntentKey,
        *,
        now: float,
    ) -> ActionMetadata | None:
        selected_key = self._record_keys_by_intent.get(intent)
        if selected_key is not None:
            selected_record = self._availability_records.get(selected_key)
            if (
                selected_record is not None
                and self._record_matches_intent(selected_record, intent, now=now)
            ):
                return selected_record.metadata

        candidates = [
            record
            for record in self._availability_records.values()
            if self._record_matches_intent(record, intent, now=now)
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda record: record.key.provider_instance_id)
        return candidates[0].metadata

    def _record_matches_intent(
        self,
        record: ActionAvailabilityRecord,
        intent: ActionIntentKey,
        *,
        now: float,
    ) -> bool:
        if record.key.action_uuid != intent.action_uuid:
            return False
        if (
            intent.provider_instance_id is not None
            and record.key.provider_instance_id != intent.provider_instance_id
        ):
            return False
        metadata = record.metadata
        if metadata is None:
            return False
        if not self._is_snapshot_eligible(record, now=now):
            return False
        return _labels_match(metadata.provider_labels, intent.provider_labels)

    def _is_snapshot_eligible(
        self,
        record: ActionAvailabilityRecord,
        *,
        now: float,
    ) -> bool:
        if record.source != ActionAvailabilitySource.PROVIDER_DIRECT:
            return False
        state = self._state_for_record(record, now=now)
        return state in (
            ActionAvailabilityState.AVAILABLE,
            ActionAvailabilityState.STALE,
        )

    def _state_for_record(
        self,
        record: ActionAvailabilityRecord,
        *,
        now: float,
    ) -> ActionAvailabilityState:
        if record.source == ActionAvailabilitySource.BEACON_CANDIDATE:
            return self._candidate_state_for_record(record, now=now)
        if record.state != ActionAvailabilityState.AVAILABLE:
            return record.state
        fresh_ttl = self._policy.fresh_ttl_seconds
        if fresh_ttl is None:
            return ActionAvailabilityState.AVAILABLE
        age_seconds = max(0.0, now - record.updated_at)
        if age_seconds <= fresh_ttl:
            return ActionAvailabilityState.AVAILABLE
        stale_grace = self._policy.stale_grace_seconds
        if stale_grace is not None and age_seconds <= fresh_ttl + stale_grace:
            return ActionAvailabilityState.STALE
        return ActionAvailabilityState.EXPIRED

    def _candidate_state_for_record(
        self,
        record: ActionAvailabilityRecord,
        *,
        now: float,
    ) -> ActionAvailabilityState:
        if record.state not in (
            ActionAvailabilityState.UNKNOWN,
            ActionAvailabilityState.PROBING,
        ):
            return record.state
        candidate_ttl = self._policy.candidate_ttl_seconds
        if candidate_ttl is None:
            return record.state
        age_seconds = max(0.0, now - record.updated_at)
        if age_seconds <= candidate_ttl:
            return record.state
        return ActionAvailabilityState.EXPIRED

    def _remove_intent_mappings_for_key(self, key: ProviderActionKey) -> None:
        for intent, mapped_key in tuple(self._record_keys_by_intent.items()):
            if mapped_key == key:
                self._record_keys_by_intent.pop(intent, None)

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
