"""Local action availability cache for controller-side planning."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from inspect import isawaitable
from typing import Protocol

import anyio
from deckr.action_runtime import (
    ACTION_RUNTIME_SERVICE_PROTOCOL,
    ActionRuntimeAvailabilityViewPayload,
    action_availability_view_ref,
    action_instance_settings_view_ref,
    action_runtime_message_name,
    action_runtime_payload,
    action_runtime_provider_instance_id,
    provider_settings_view_ref,
    settings_view_payload,
)
from deckr.actions.endpoints import (
    RESERVED_BUILTIN_PROVIDER_IDS,
)
from deckr.actions.messages import (
    ActionAvailabilityEntry,
    ActionMessageBody,
    SettingsSnapshot,
    SettingsTargetRef,
)
from deckr.contracts.authority import ContractPointer
from deckr.lanes import EndpointSession
from deckr.services import (
    DeckrServices,
    ServiceBackendStatus,
    ServiceDescriptor,
    ServiceUnavailable,
    ServiceUseLease,
    newest_service_descriptor,
    service_unavailable_ends_service_use,
)

from deckr.controller._action_interest import ActionInterestSnapshot
from deckr.controller._action_provider_sessions import (
    ProviderSessionKey,
)
from deckr.controller._binding_planner import ActionIntentKey
from deckr.controller._settings_metadata import SettingsActionMetadata
from deckr.controller.action_provider.events import ActionCatalogChangedEvent
from deckr.controller.action_provider.provider import (
    ActionMetadata,
    ActionProviderManager,
)

logger = logging.getLogger(__name__)

PROVIDER_SESSION_INVALID_REASON = "provider_session_invalid"
SERVICE_VIEW_MISSING_REASON = "action_availability_view_missing"
SERVICE_VIEW_UNAVAILABLE_REASON = "action_availability_service_unavailable"
_HASH_SIZE = 12
_SERVICE_WATCH_RETRY_SECONDS = 1.0


@dataclass(frozen=True, slots=True)
class ProviderActionKey:
    provider_instance_id: str
    action_uuid: str


_AvailabilityChangedCallback = Callable[[frozenset[ProviderActionKey]], object]


class ActionAvailabilitySource(StrEnum):
    BEACON_CANDIDATE = "beacon_candidate"
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
    candidate_ttl_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class ActionPlanningSnapshot:
    metadata: Mapping[ActionIntentKey, ActionMetadata]
    pending: frozenset[ActionIntentKey]
    unavailable: frozenset[ActionIntentKey]


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


class ActionProviderSessionPreparer(Protocol):
    def set_change_callback(self, callback) -> None: ...

    def start(self, stopping: anyio.Event) -> None: ...

    async def prepare_many(
        self,
        actions: Iterable[ActionMetadata],
    ) -> Mapping[object, object]: ...

    async def refresh_many(
        self,
        keys: Iterable[ProviderSessionKey],
    ) -> Mapping[ProviderSessionKey, object]: ...

    def contract_pointer(self, key: ProviderSessionKey) -> ContractPointer | None: ...

    def cached_ready(self, key: ProviderSessionKey) -> bool: ...

    async def valid(
        self,
        *,
        provider_instance_id: str,
        provider_id: str,
        provider_session_id: str | None,
    ) -> bool: ...

    async def aclose(self) -> None: ...


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

    def snapshot_for_intents(
        self,
        intents: Iterable[ActionIntentKey],
        *,
        now: float | None = None,
        stale_provider_keys: Iterable[ProviderActionKey] = (),
        ready_provider_session_keys: Iterable[ProviderSessionKey] | None = None,
    ) -> dict[ActionIntentKey, ActionMetadata]:
        snapshot: dict[ActionIntentKey, ActionMetadata] = {}
        snapshot_now = self._now(now)
        stale_keys = frozenset(stale_provider_keys)
        ready_session_keys = (
            None
            if ready_provider_session_keys is None
            else frozenset(ready_provider_session_keys)
        )
        for intent in intents:
            metadata = self._metadata_for_intent(
                intent,
                now=snapshot_now,
                stale_provider_keys=stale_keys,
                ready_provider_session_keys=ready_session_keys,
            )
            if metadata is not None:
                snapshot[intent] = metadata
        return snapshot

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

    def _metadata_for_intent(
        self,
        intent: ActionIntentKey,
        *,
        now: float,
        stale_provider_keys: frozenset[ProviderActionKey],
        ready_provider_session_keys: frozenset[ProviderSessionKey] | None,
    ) -> ActionMetadata | None:
        selected = self._selected_planning_record(
            intent,
            now=now,
            stale_provider_keys=stale_provider_keys,
            ready_provider_session_keys=ready_provider_session_keys,
        )
        if selected is None or not self._planning_record_is_usable(
            selected,
            stale_provider_keys=stale_provider_keys,
            ready_provider_session_keys=ready_provider_session_keys,
        ):
            return None
        return selected.record.metadata

    def settings_metadata_for_intent(
        self,
        intent: ActionIntentKey,
        *,
        provider_id: str | None = None,
        now: float | None = None,
    ) -> SettingsActionMetadata:
        lookup_now = self._now(now)
        for records, candidate_stale in (
            (self._availability_records.values(), False),
            (self._candidate_records.values(), True),
        ):
            matches = [
                record
                for record in records
                if record.metadata is not None
                and self._record_matches_intent(record, intent, now=lookup_now)
                and (provider_id is None or record.metadata.provider_id == provider_id)
                and self._state_for_record(record, now=lookup_now)
                != ActionAvailabilityState.EXPIRED
            ]
            if not matches:
                continue
            matches.sort(key=lambda record: record.key.provider_instance_id)
            selected = matches[0]
            return SettingsActionMetadata(
                action=selected.metadata,
                stale=candidate_stale
                or self._state_for_record(selected, now=lookup_now)
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

        for predicate in (
            self._planning_record_is_service_view_pending,
            self._planning_record_is_live_candidate,
        ):
            for item in records:
                if predicate(
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
            for record in (
                *self._availability_records.values(),
                *self._candidate_records.values(),
            )
            if self._record_matches_intent(
                record,
                intent,
                now=now,
                trust_mapped_key=record.key == selected_key,
            )
        ]
        records.sort(
            key=lambda item: (
                item.record.key.provider_instance_id,
                0
                if item.record.source == ActionAvailabilitySource.SERVICE_VIEW
                else 1,
            )
        )
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

    def _planning_record_is_live_candidate(
        self,
        item: _PlanningRecord,
        *,
        ready_provider_session_keys: frozenset[ProviderSessionKey] | None = None,
    ) -> bool:
        del ready_provider_session_keys
        return (
            item.record.source == ActionAvailabilitySource.BEACON_CANDIDATE
            and item.state
            in {
                ActionAvailabilityState.UNKNOWN,
                ActionAvailabilityState.PROBING,
            }
        )

    def _planning_record_is_pending(
        self,
        item: _PlanningRecord,
        *,
        ready_provider_session_keys: frozenset[ProviderSessionKey] | None,
    ) -> bool:
        return self._planning_record_is_service_view_pending(
            item,
            ready_provider_session_keys=ready_provider_session_keys,
        ) or self._planning_record_is_live_candidate(
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
        if record.source == ActionAvailabilitySource.BEACON_CANDIDATE:
            return self._candidate_state_for_record(record, now=now)
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


def _contract_pointer_matches(
    left: ContractPointer | None,
    right: ContractPointer | None,
) -> bool:
    if left is None or right is None:
        return left is right
    return left.contract_id == right.contract_id and left.generation == right.generation


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


class ActionAvailabilityService:
    """Controller-level action availability state and service-view I/O."""

    def __init__(
        self,
        *,
        controller_id: str,
        controller_session_id: str,
        actions_bus: EndpointSession,
        manager: ActionProviderManager,
        services: DeckrServices | None = None,
        close_services_on_aclose: bool = False,
        start_soon: Callable[..., object] | None = None,
        provider_sessions: ActionProviderSessionPreparer | None = None,
        on_availability_changed: _AvailabilityChangedCallback | None = None,
        cache: ActionAvailabilityCache | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.controller_id = controller_id
        self.controller_session_id = controller_session_id
        self.actions_bus = actions_bus
        self.manager = manager
        self._services = services
        self._close_services_on_aclose = close_services_on_aclose
        self._provider_sessions = provider_sessions
        self.cache = cache or ActionAvailabilityCache(clock=clock)
        self._clock = clock or time.monotonic
        self._start_soon = start_soon
        self._interest_by_config: dict[str, ActionInterestSnapshot] = {}
        self._provider_session_reconcile_scheduled = False
        self._service_watch_scopes: dict[str, anyio.CancelScope] = {}
        self._service_descriptor_keys: dict[str, tuple[str, str, str]] = {}
        self._runtime_leases: dict[str, ServiceUseLease] = {}
        self._on_availability_changed = on_availability_changed
        if provider_sessions is not None:
            set_change_callback = getattr(provider_sessions, "set_change_callback", None)
            if callable(set_change_callback):
                set_change_callback(self._provider_session_changed)

    def set_availability_changed_callback(
        self,
        callback: _AvailabilityChangedCallback | None,
    ) -> None:
        self._on_availability_changed = callback

    async def start(
        self,
        tg,
        stopping,
    ) -> None:
        provider_sessions = self._provider_sessions
        if provider_sessions is not None:
            start = getattr(provider_sessions, "start", None)
            if callable(start):
                start(stopping)
        if self._services is not None:
            tg.start_soon(self._service_directory_loop, stopping)

    async def aclose(self) -> None:
        for scope in tuple(self._service_watch_scopes.values()):
            scope.cancel()
        self._service_watch_scopes.clear()
        self._service_descriptor_keys.clear()
        if self._close_services_on_aclose and self._services is not None:
            await self._services.aclose()
            self._services = None
        provider_sessions = self._provider_sessions
        self._provider_sessions = None
        if provider_sessions is not None:
            set_change_callback = getattr(provider_sessions, "set_change_callback", None)
            if callable(set_change_callback):
                set_change_callback(None)
            await provider_sessions.aclose()

    def planning_snapshot(
        self,
        intents: Iterable[ActionIntentKey],
        *,
        existing_provider_keys: Iterable[ProviderActionKey] = (),
        now: float | None = None,
    ) -> ActionPlanningSnapshot:
        return self.cache.planning_snapshot(
            intents,
            stale_provider_keys=existing_provider_keys,
            ready_provider_session_keys=self._ready_provider_session_keys(),
            now=self._now(now),
        )

    def settings_action_metadata(
        self,
        action_uuid: str,
        *,
        provider_instance_id: str | None = None,
        provider_id: str | None = None,
        provider_labels: Mapping[str, str] | None = None,
        now: float | None = None,
    ) -> SettingsActionMetadata:
        return self.cache.settings_metadata_for_intent(
            ActionIntentKey(
                action_uuid=action_uuid,
                provider_instance_id=provider_instance_id,
                provider_labels=tuple(sorted((provider_labels or {}).items())),
            ),
            provider_id=provider_id,
            now=self._now(now),
        )

    def record_lifecycle_unavailable(
        self,
        *,
        provider_instance_id: str,
        provider_id: str,
        action_uuid: str,
        provider_session_id: str | None = None,
        reason: str | None = None,
        intent: ActionIntentKey | None = None,
        now: float | None = None,
    ) -> ProviderActionKey:
        key = ProviderActionKey(provider_instance_id, action_uuid)
        existing = self.cache.record_for(key)
        metadata = existing.metadata if existing is not None else None
        if metadata is None:
            metadata = ActionMetadata(
                uuid=action_uuid,
                provider_instance_id=provider_instance_id,
                provider_id=provider_id,
                provider_session_id=provider_session_id,
            )
        self.cache.record_unavailable(
            key,
            metadata=metadata,
            reason=reason,
            now=self._now(now),
            intent=intent,
        )
        return key

    def update_config_interest(
        self,
        config_id: str,
        snapshot: ActionInterestSnapshot,
    ) -> None:
        self._interest_by_config[config_id] = snapshot

    def clear_config_interest(self, config_id: str) -> None:
        self._interest_by_config.pop(config_id, None)

    async def ingest_catalog_changed(
        self,
        event: ActionCatalogChangedEvent,
    ) -> frozenset[ProviderActionKey]:
        changed: set[ProviderActionKey] = set()
        now = self._clock()
        removed_keys = {
            key
            for qualified in event.catalog_removed
            if (key := _provider_action_key_from_catalog_id(qualified)) is not None
        }
        changed.update(
            removed.key for removed in self.cache.remove_candidates(removed_keys)
        )
        for qualified in (
            *event.catalog_added,
            *event.catalog_updated,
        ):
            key = _provider_action_key_from_catalog_id(qualified)
            if key is None:
                continue
            metadata = await self.manager.get_action(
                key.action_uuid,
                provider_instance_id=key.provider_instance_id,
            )
            if metadata is None:
                continue
            self.cache.record_candidate(metadata, now=now)
            changed.add(key)
            logger.debug(
                "Action catalog availability ingest provider=%s action=%s "
                "provider_session=%s changed=True",
                key.provider_instance_id,
                key.action_uuid,
                metadata.provider_session_id,
            )
        logger.debug(
            "Action catalog availability ingest complete changed_keys=%s",
            len(changed),
        )
        return frozenset(changed)

    def ingest_service_view_payload(
        self,
        payload: ActionRuntimeAvailabilityViewPayload | Mapping[str, object],
        *,
        service_id: str | None = None,
        now: float | None = None,
    ) -> frozenset[ProviderActionKey]:
        view = (
            payload
            if isinstance(payload, ActionRuntimeAvailabilityViewPayload)
            else ActionRuntimeAvailabilityViewPayload.model_validate(payload)
        )
        if service_id is not None:
            expected_provider = action_runtime_provider_instance_id(service_id)
            if (
                expected_provider is not None
                and expected_provider != view.provider_instance_id
            ):
                logger.warning(
                    "Ignoring action availability service view with mismatched "
                    "service id provider=%s payload_provider=%s",
                    expected_provider,
                    view.provider_instance_id,
                )
                return frozenset()
        logger.debug(
            "Action availability service view received service=%s provider=%s "
            "provider_id=%s service_session=%s entries=%s",
            service_id,
            view.provider_instance_id,
            view.provider_id,
            view.service_session_id,
            len(view.entries),
        )
        record_now = self._now(now)
        changed = set(
            self.ingest_provider_entries(
                provider_instance_id=view.provider_instance_id,
                provider_id=view.provider_id,
                provider_session_id=view.service_session_id,
                provider_labels=view.labels,
                entries=view.entries,
                now=record_now,
            )
        )
        changed.update(
            self._mark_service_view_entries_missing(
                provider_instance_id=view.provider_instance_id,
                seen_action_ids={entry.action_id for entry in view.entries},
                now=record_now,
            )
        )
        logger.debug(
            "Action availability service view applied service=%s provider=%s "
            "entries=%s changed_keys=%s",
            service_id,
            view.provider_instance_id,
            len(view.entries),
            len(changed),
        )
        return frozenset(changed)

    def _mark_service_view_entries_missing(
        self,
        *,
        provider_instance_id: str,
        seen_action_ids: set[str],
        now: float,
    ) -> frozenset[ProviderActionKey]:
        changed: set[ProviderActionKey] = set()
        for record in self.cache.service_view_records():
            if record.key.provider_instance_id != provider_instance_id:
                continue
            if record.key.action_uuid in seen_action_ids:
                continue
            if (
                record.state == ActionAvailabilityState.UNAVAILABLE
                and record.reason == SERVICE_VIEW_MISSING_REASON
            ):
                continue
            self.cache.record_unavailable(
                record.key,
                metadata=record.metadata,
                reason=SERVICE_VIEW_MISSING_REASON,
                now=now,
                intent=self._mapped_intent_for_key(record.key),
            )
            changed.add(record.key)
        return frozenset(changed)

    def ingest_provider_entries(
        self,
        *,
        provider_instance_id: str,
        provider_id: str,
        provider_session_id: str | None = None,
        provider_labels: Mapping[str, str] | None = None,
        entries: Iterable[ActionAvailabilityEntry],
        now: float | None = None,
    ) -> frozenset[ProviderActionKey]:
        changed: set[ProviderActionKey] = set()
        record_now = self._now(now)
        for entry in entries:
            key = ProviderActionKey(provider_instance_id, entry.action_id)
            existing = self.cache.record_for(key)
            old_state = (
                self.cache.state_for(key, now=record_now)
                if existing is not None
                else None
            )
            metadata = self._metadata_for_entry(
                key=key,
                provider_id=provider_id,
                provider_session_id=provider_session_id,
                provider_labels=provider_labels,
                entry=entry,
            )
            mapped_intent = self._mapped_intent_for_key(key)
            new_state = _availability_state_for_entry(entry)
            requires_provider_lifecycle_recovery = (
                new_state == ActionAvailabilityState.AVAILABLE
                and existing is not None
                and existing.source == ActionAvailabilitySource.SERVICE_VIEW
                and existing.state == ActionAvailabilityState.UNAVAILABLE
                and existing.reason == PROVIDER_SESSION_INVALID_REASON
            )
            same_as_existing = old_state == new_state and _entry_same_as_existing(
                existing,
                new_state,
                metadata,
                entry.reason,
                requires_provider_lifecycle_recovery=(
                    requires_provider_lifecycle_recovery
                ),
            )
            if entry.status == "available":
                if metadata is None:
                    logger.debug(
                        "Action availability entry ignored provider=%s action=%s "
                        "status=%s old_state=%s reason=missing_metadata",
                        provider_instance_id,
                        entry.action_id,
                        entry.status,
                        _state_value(old_state),
                    )
                    continue
                self.cache.record_available(
                    metadata,
                    now=record_now,
                    intent=mapped_intent,
                    requires_provider_lifecycle_recovery=(
                        requires_provider_lifecycle_recovery
                    ),
                )
            elif entry.status == "unavailable":
                self.cache.record_unavailable(
                    key,
                    metadata=metadata,
                    reason=entry.reason,
                    now=record_now,
                    intent=mapped_intent,
                )
            else:
                self.cache.record_probing(
                    key,
                    metadata=metadata,
                    reason=entry.reason,
                    now=record_now,
                    intent=mapped_intent,
                )
            if not same_as_existing:
                changed.add(key)
            logger.debug(
                "Action availability entry ingested provider=%s provider_id=%s "
                "action=%s old_state=%s new_status=%s provider_session=%s "
                "descriptor_hash=%s same_as_existing=%s mapped_intent=%s",
                provider_instance_id,
                provider_id,
                entry.action_id,
                _state_value(old_state),
                entry.status,
                metadata.provider_session_id if metadata is not None else None,
                _descriptor_hash(entry.descriptor),
                same_as_existing,
                mapped_intent is not None,
            )
        logger.debug(
            "Action availability provider ingest complete provider=%s provider_id=%s "
            "changed_keys=%s",
            provider_instance_id,
            provider_id,
            len(changed),
        )
        return frozenset(changed)

    def mark_provider_service_unavailable(
        self,
        provider_instance_id: str,
        *,
        reason: str = SERVICE_VIEW_UNAVAILABLE_REASON,
        now: float | None = None,
    ) -> frozenset[ProviderActionKey]:
        changed: set[ProviderActionKey] = set()
        record_now = self._now(now)
        records = [
            record
            for record in (*self.cache.service_view_records(),)
            if record.key.provider_instance_id == provider_instance_id
        ]
        for record in records:
            if record.state == ActionAvailabilityState.UNAVAILABLE and (
                record.reason == reason
            ):
                continue
            self.cache.record_unavailable(
                record.key,
                metadata=record.metadata,
                reason=reason,
                now=record_now,
                intent=self._mapped_intent_for_key(record.key),
            )
            changed.add(record.key)
        if changed:
            logger.debug(
                "Action availability service marked unavailable provider=%s "
                "reason=%s changed_keys=%s",
                provider_instance_id,
                reason,
                len(changed),
            )
        return frozenset(changed)

    async def send_runtime_message(
        self,
        *,
        provider_instance_id: str,
        message_type: str,
        body: ActionMessageBody,
    ) -> bool:
        services = self._services
        lease = self._runtime_leases.get(provider_instance_id)
        if services is None or lease is None:
            return False
        name = action_runtime_message_name(message_type)
        params, event = action_runtime_payload(name, body)
        try:
            await services.send(lease, name, params=params, event=event)
        except ServiceUnavailable as exc:
            if service_unavailable_ends_service_use(exc):
                self._runtime_leases.pop(provider_instance_id, None)
                changed = self.mark_provider_service_unavailable(
                    provider_instance_id,
                    reason=SERVICE_VIEW_UNAVAILABLE_REASON,
                )
                if changed:
                    await self._notify_availability_changed(changed)
            logger.warning(
                "Could not send action runtime message provider=%s name=%s "
                "code=%s message=%s diagnostics=%s",
                provider_instance_id,
                name,
                exc.code,
                exc.message,
                exc.diagnostics,
            )
            return False
        return True

    async def put_settings_view(
        self,
        *,
        provider_instance_id: str,
        target: SettingsTargetRef,
        snapshot: SettingsSnapshot,
    ) -> bool:
        services = self._services
        lease = self._runtime_leases.get(provider_instance_id)
        if services is None or lease is None:
            return False
        if target.scope == "action_provider_instance":
            view_ref = provider_settings_view_ref(lease.descriptor.service_id, target)
        else:
            view_ref = action_instance_settings_view_ref(
                lease.descriptor.service_id,
                target,
            )
        try:
            await services.put_view(
                lease,
                view_ref,
                settings_view_payload(snapshot),
            )
        except ServiceUnavailable as exc:
            logger.warning(
                "Could not write action runtime settings view provider=%s target=%s "
                "code=%s message=%s diagnostics=%s",
                provider_instance_id,
                target.key(),
                exc.code,
                exc.message,
                exc.diagnostics,
            )
            return False
        return True

    async def ensure_local_builtin_availability(
        self,
        intents: Iterable[ActionIntentKey],
    ) -> None:
        for intent in intents:
            if intent.provider_instance_id is not None and (
                intent.provider_instance_id not in RESERVED_BUILTIN_PROVIDER_IDS
            ):
                continue
            if intent.provider_labels:
                continue
            existing = self.cache.record_for_intent(intent, now=self._clock())
            if existing is not None and existing.key.provider_instance_id in (
                RESERVED_BUILTIN_PROVIDER_IDS
            ):
                continue
            metadata = await self.manager.get_action(
                intent.action_uuid,
                provider_instance_id=intent.provider_instance_id,
                provider_labels=dict(intent.provider_labels),
            )
            if metadata is None:
                continue
            if metadata.provider_instance_id not in RESERVED_BUILTIN_PROVIDER_IDS:
                continue
            self.cache.record_available(
                metadata,
                now=self._clock(),
                intent=intent,
            )

    async def _service_directory_loop(self, stopping: anyio.Event) -> None:
        services = self._services
        if services is None:
            return
        directory = services.directory(ACTION_RUNTIME_SERVICE_PROTOCOL)
        async for descriptors in directory.watch_records():
            if stopping.is_set():
                return
            self._reconcile_service_watchers(descriptors, stopping=stopping)

    def _reconcile_service_watchers(
        self,
        descriptors: Collection[ServiceDescriptor],
        *,
        stopping: anyio.Event,
    ) -> None:
        candidates_by_service: dict[str, list[ServiceDescriptor]] = {}
        for descriptor in descriptors:
            provider_instance_id = action_runtime_provider_instance_id(
                descriptor.service_id
            )
            if provider_instance_id is None:
                continue
            candidates_by_service.setdefault(descriptor.service_id, []).append(descriptor)
        active: dict[str, ServiceDescriptor] = {}
        for service_id, candidates in candidates_by_service.items():
            descriptor = newest_service_descriptor(candidates)
            if descriptor is not None:
                active[service_id] = descriptor

        for service_id in sorted(set(self._service_watch_scopes) - set(active)):
            self._cancel_service_watch(
                service_id,
                provider_instance_id=action_runtime_provider_instance_id(
                    service_id
                ),
                reason=SERVICE_VIEW_UNAVAILABLE_REASON,
            )

        for service_id, descriptor in sorted(active.items()):
            descriptor_key = _service_descriptor_key(descriptor)
            if self._service_descriptor_keys.get(service_id) == descriptor_key:
                continue
            self._cancel_service_watch(
                service_id,
                provider_instance_id=action_runtime_provider_instance_id(
                    service_id
                ),
                reason=SERVICE_VIEW_UNAVAILABLE_REASON,
            )
            if descriptor.backend_status == ServiceBackendStatus.UNAVAILABLE:
                provider_instance_id = action_runtime_provider_instance_id(
                    service_id
                )
                if provider_instance_id is not None and self._start_soon is not None:
                    self._start_soon(
                        self._mark_unavailable_and_notify,
                        provider_instance_id,
                        SERVICE_VIEW_UNAVAILABLE_REASON,
                    )
                continue
            if self._start_soon is None:
                continue
            self._service_descriptor_keys[service_id] = descriptor_key
            self._start_soon(
                self._run_service_view_watch,
                service_id,
                descriptor,
                stopping,
            )

    def _cancel_service_watch(
        self,
        service_id: str,
        *,
        provider_instance_id: str | None,
        reason: str,
    ) -> None:
        self._service_descriptor_keys.pop(service_id, None)
        provider_instance_id = provider_instance_id or action_runtime_provider_instance_id(
            service_id
        )
        if provider_instance_id is not None:
            self._runtime_leases.pop(provider_instance_id, None)
        scope = self._service_watch_scopes.pop(service_id, None)
        if scope is not None:
            scope.cancel()
        if provider_instance_id is not None and self._start_soon is not None:
            self._start_soon(
                self._mark_unavailable_and_notify,
                provider_instance_id,
                reason,
            )

    async def _run_service_view_watch(
        self,
        service_id: str,
        descriptor: ServiceDescriptor,
        stopping: anyio.Event,
    ) -> None:
        services = self._services
        if services is None:
            return
        provider_instance_id = action_runtime_provider_instance_id(service_id)
        with anyio.CancelScope() as scope:
            self._service_watch_scopes[service_id] = scope
            try:
                view_ref = action_availability_view_ref(service_id)
                while not stopping.is_set():
                    try:
                        async with services.use(descriptor) as lease:
                            if provider_instance_id is not None:
                                self._runtime_leases[provider_instance_id] = lease
                            try:
                                while not stopping.is_set():
                                    try:
                                        async for payload in services.watch_view(
                                            lease,
                                            view_ref,
                                        ):
                                            if stopping.is_set():
                                                return
                                            if payload is None:
                                                changed = (
                                                    self.mark_provider_service_unavailable(
                                                        provider_instance_id,
                                                        reason=(
                                                            SERVICE_VIEW_MISSING_REASON
                                                        ),
                                                    )
                                                    if provider_instance_id is not None
                                                    else frozenset()
                                                )
                                            else:
                                                view = (
                                                    ActionRuntimeAvailabilityViewPayload.model_validate(
                                                        payload
                                                    )
                                                )
                                                changed = set(
                                                    self.ingest_service_view_payload(
                                                        view,
                                                        service_id=service_id,
                                                    )
                                                )
                                            if changed:
                                                await self._notify_availability_changed(
                                                    frozenset(changed)
                                                )
                                    except ServiceUnavailable as exc:
                                        if service_unavailable_ends_service_use(exc):
                                            raise
                                        logger.warning(
                                            "Action availability service view "
                                            "unavailable service=%s code=%s "
                                            "message=%s diagnostics=%s",
                                            service_id,
                                            exc.code,
                                            exc.message,
                                            exc.diagnostics,
                                        )
                                        await anyio.sleep(_SERVICE_WATCH_RETRY_SECONDS)
                                    else:
                                        break
                            finally:
                                if provider_instance_id is not None and (
                                    self._runtime_leases.get(provider_instance_id)
                                    is lease
                                ):
                                    self._runtime_leases.pop(
                                        provider_instance_id,
                                        None,
                                    )
                    except ServiceUnavailable as exc:
                        provider_instance_id = action_runtime_provider_instance_id(
                            service_id
                        )
                        if service_unavailable_ends_service_use(exc):
                            if provider_instance_id is not None:
                                changed = self.mark_provider_service_unavailable(
                                    provider_instance_id,
                                    reason=SERVICE_VIEW_UNAVAILABLE_REASON,
                                )
                                if changed:
                                    await self._notify_availability_changed(changed)
                        else:
                            logger.warning(
                                "Action availability service view unavailable "
                                "service=%s code=%s message=%s diagnostics=%s",
                                service_id,
                                exc.code,
                                exc.message,
                                exc.diagnostics,
                            )
                        await anyio.sleep(_SERVICE_WATCH_RETRY_SECONDS)
            finally:
                if provider_instance_id is not None:
                    current = self._runtime_leases.get(provider_instance_id)
                    if current is not None and current.descriptor.service_id == service_id:
                        self._runtime_leases.pop(provider_instance_id, None)
                if self._service_watch_scopes.get(service_id) is scope:
                    self._service_watch_scopes.pop(service_id, None)

    async def _mark_unavailable_and_notify(
        self,
        provider_instance_id: str,
        reason: str,
    ) -> None:
        changed = self.mark_provider_service_unavailable(
            provider_instance_id,
            reason=reason,
        )
        if changed:
            await self._notify_availability_changed(changed)

    async def provider_session_valid(
        self,
        *,
        provider_instance_id: str,
        provider_id: str,
        provider_session_id: str | None,
    ) -> bool:
        provider_sessions = self._provider_sessions
        if provider_sessions is None:
            lease = self._runtime_leases.get(provider_instance_id)
            if lease is None:
                return False
            if provider_session_id is not None and (
                lease.descriptor.session_id != provider_session_id
            ):
                return False
            if lease.descriptor.diagnostics.get("providerId") not in {None, provider_id}:
                return False
            try:
                await lease.refresh()
            except ServiceUnavailable:
                return False
            return True
        try:
            return await provider_sessions.valid(
                provider_instance_id=provider_instance_id,
                provider_id=provider_id,
                provider_session_id=provider_session_id,
            )
        except Exception:
            logger.warning(
                "Could not validate action provider session for %s/%s",
                provider_instance_id,
                provider_id,
                exc_info=True,
            )
            return False

    async def _provider_session_snapshot(
        self,
        key: ProviderSessionKey,
    ) -> object | None:
        provider_sessions = self._provider_sessions
        if provider_sessions is None:
            return None
        try:
            snapshots = await provider_sessions.refresh_many((key,))
        except Exception:
            logger.warning(
                "Could not refresh action provider session for %s/%s",
                key.provider_instance_id,
                key.provider_id,
                exc_info=True,
            )
            return None
        return snapshots.get(key)

    def contract_pointer(
        self,
        key: ProviderSessionKey | None,
    ) -> ContractPointer | None:
        if key is None:
            return None
        provider_sessions = self._provider_sessions
        if provider_sessions is None:
            lease = self._runtime_leases.get(key.provider_instance_id)
            if lease is None:
                return None
            if lease.descriptor.session_id != key.provider_session_id:
                return None
            return ContractPointer(
                contractId=lease.contract.contract_id,
                generation=lease.contract.generation,
            )
        if not self._provider_session_cached_ready(key):
            return None
        contract_pointer = getattr(provider_sessions, "contract_pointer", None)
        if not callable(contract_pointer):
            return None
        return contract_pointer(key)

    def _provider_session_changed(self) -> None:
        self._schedule_provider_session_reconcile()

    async def reconcile_provider_sessions(self) -> frozenset[ProviderActionKey]:
        provider_sessions = self._provider_sessions
        if provider_sessions is None:
            return frozenset()
        now = self._clock()
        changed: set[ProviderActionKey] = set()
        for record in self.cache.service_view_records():
            metadata = record.metadata
            if not _metadata_requires_provider_session_revalidation(metadata):
                continue
            if (
                record.state == ActionAvailabilityState.UNAVAILABLE
                and record.reason == PROVIDER_SESSION_INVALID_REASON
            ):
                continue
            session_key = ProviderSessionKey(
                metadata.provider_instance_id,
                metadata.provider_id,
                metadata.provider_session_id,
            )
            was_ready = self._provider_session_cached_ready(session_key)
            snapshot = await self._provider_session_snapshot(session_key)
            if snapshot is not None and getattr(snapshot, "ready", False):
                if not was_ready:
                    changed.add(record.key)
                    logger.info(
                        "Action availability provider session ready provider=%s "
                        "provider_id=%s action=%s provider_session=%s",
                        metadata.provider_instance_id,
                        metadata.provider_id,
                        metadata.uuid,
                        metadata.provider_session_id,
                    )
                continue
            if snapshot is None or not getattr(snapshot, "terminal", False):
                if snapshot is not None and was_ready:
                    changed.add(record.key)
                    logger.info(
                        "Action availability provider session became pending "
                        "provider=%s provider_id=%s action=%s "
                        "provider_session=%s status=%s",
                        metadata.provider_instance_id,
                        metadata.provider_id,
                        metadata.uuid,
                        metadata.provider_session_id,
                        getattr(snapshot, "status", None),
                    )
                continue
            mapped_intent = self._mapped_intent_for_key(record.key)
            old_state = self.cache.state_for(record.key, now=now)
            self.cache.record_unavailable(
                record.key,
                metadata=metadata,
                reason=PROVIDER_SESSION_INVALID_REASON,
                now=now,
                intent=mapped_intent,
            )
            changed.add(record.key)
            logger.info(
                "Action availability invalidated provider=%s provider_id=%s "
                "action=%s provider_session=%s old_state=%s reason=%s",
                metadata.provider_instance_id,
                metadata.provider_id,
                metadata.uuid,
                metadata.provider_session_id,
                _state_value(old_state),
                PROVIDER_SESSION_INVALID_REASON,
            )
        return frozenset(changed)

    def _schedule_provider_session_reconcile(self) -> None:
        if (
            self._provider_sessions is None
            or self._provider_session_reconcile_scheduled
            or self._start_soon is None
        ):
            return
        self._provider_session_reconcile_scheduled = True
        self._start_soon(self._provider_session_reconcile_task)

    async def _provider_session_reconcile_task(self) -> None:
        self._provider_session_reconcile_scheduled = False
        try:
            changed = await self.reconcile_provider_sessions()
        except Exception:
            logger.exception("Error reconciling action provider sessions")
            return
        if changed:
            await self._notify_availability_changed(changed)

    def _ready_provider_session_keys(self) -> frozenset[ProviderSessionKey] | None:
        provider_sessions = self._provider_sessions
        if provider_sessions is None:
            if self._services is None:
                return None
            ready: set[ProviderSessionKey] = set()
            for record in self.cache.service_view_records():
                metadata = record.metadata
                if not _metadata_requires_provider_session_revalidation(metadata):
                    continue
                lease = self._runtime_leases.get(metadata.provider_instance_id)
                if lease is None:
                    continue
                if lease.descriptor.session_id != metadata.provider_session_id:
                    continue
                ready.add(
                    ProviderSessionKey(
                        metadata.provider_instance_id,
                        metadata.provider_id,
                        metadata.provider_session_id,
                    )
                )
            return frozenset(ready)
        ready: set[ProviderSessionKey] = set()
        for record in self.cache.service_view_records():
            metadata = record.metadata
            if not _metadata_requires_provider_session_revalidation(metadata):
                continue
            key = ProviderSessionKey(
                metadata.provider_instance_id,
                metadata.provider_id,
                metadata.provider_session_id,
            )
            if self._provider_session_cached_ready(key):
                ready.add(key)
        return frozenset(ready)

    def _provider_session_cached_ready(self, key: ProviderSessionKey) -> bool:
        provider_sessions = self._provider_sessions
        if provider_sessions is None:
            return False
        cached_ready = getattr(provider_sessions, "cached_ready", None)
        if not callable(cached_ready):
            return False
        try:
            return bool(cached_ready(key))
        except Exception:
            logger.warning(
                "Could not read cached action provider session readiness for %s/%s",
                key.provider_instance_id,
                key.provider_id,
                exc_info=True,
            )
            return False

    async def _notify_availability_changed(
        self,
        changed: frozenset[ProviderActionKey],
    ) -> None:
        callback = self._on_availability_changed
        if callback is None:
            return
        result = callback(changed)
        if isawaitable(result):
            await result

    def _metadata_for_entry(
        self,
        *,
        key: ProviderActionKey,
        provider_id: str,
        provider_session_id: str | None,
        provider_labels: Mapping[str, str] | None,
        entry: ActionAvailabilityEntry,
    ) -> ActionMetadata | None:
        descriptor = entry.descriptor
        candidate = self.cache.record_for(key)
        candidate_metadata = candidate.metadata if candidate is not None else None
        if descriptor is None and candidate_metadata is None:
            return None
        return ActionMetadata(
            uuid=key.action_uuid,
            provider_instance_id=key.provider_instance_id,
            provider_id=provider_id,
            name=descriptor.name if descriptor is not None else (
                candidate_metadata.name if candidate_metadata is not None else None
            ),
            provider_session_id=provider_session_id,
            provider_labels=(
                candidate_metadata.provider_labels
                if candidate_metadata is not None
                else dict(provider_labels or {})
            ),
            settings_schema=(
                dict(descriptor.settings_schema)
                if descriptor is not None and descriptor.settings_schema is not None
                else None
            ),
            provider_settings_schema=(
                dict(descriptor.provider_settings_schema)
                if descriptor is not None
                and descriptor.provider_settings_schema is not None
                else None
            ),
        )

    def _mapped_intent_for_key(self, key: ProviderActionKey) -> ActionIntentKey | None:
        for intent, mapped_key in self.cache._record_keys_by_intent.items():
            if mapped_key == key:
                return intent
        return None

    def _now(self, now: float | None) -> float:
        return self._clock() if now is None else now


def _provider_action_key_from_catalog_id(
    catalog_id: str,
) -> ProviderActionKey | None:
    provider_instance_id, separator, action_uuid = catalog_id.partition("::")
    if not separator or not provider_instance_id or not action_uuid:
        return None
    return ProviderActionKey(provider_instance_id, action_uuid)


def _service_descriptor_key(descriptor: ServiceDescriptor) -> tuple[str, str, str]:
    return (
        str(descriptor.endpoint),
        descriptor.session_id,
        descriptor.backend_status.value,
    )
