"""Local action availability cache for controller-side planning."""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

import anyio
from deckr.actions.endpoints import (
    BUILTIN_ACTION_PROVIDER_ID,
    RESERVED_BUILTIN_PROVIDER_IDS,
    action_provider_address,
    parse_action_provider_address,
)
from deckr.actions.messages import (
    ACTION_AVAILABILITY_REQUEST,
    ACTION_AVAILABILITY_SNAPSHOT,
    ACTION_INTEREST_UPDATE,
    ActionAvailabilityChangedBody,
    ActionAvailabilityEntry,
    ActionAvailabilityRequestBody,
    ActionAvailabilitySelector,
    ActionAvailabilitySnapshotBody,
    ActionInterestEntry,
    ActionInterestUpdateBody,
    action_provider_instance_subject,
)
from deckr.contracts.messages import ACTIONS_LANE, DeckrMessage
from deckr.lanes import EndpointSession

from deckr.controller._action_interest import (
    ActionInterestSnapshot,
    ActionInterestStrength,
)
from deckr.controller._binding_planner import ActionIntentKey
from deckr.controller._settings_metadata import SettingsActionMetadata
from deckr.controller.action_provider.events import ActionCatalogChangedEvent
from deckr.controller.action_provider.provider import (
    ActionMetadata,
    ActionProviderManager,
)

logger = logging.getLogger(__name__)

DEFAULT_PROVIDER_REVALIDATION_SECONDS = 60.0
DEFAULT_STALE_GRACE_SECONDS = 5 * 60.0


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
    fresh_ttl_seconds: float | None = DEFAULT_PROVIDER_REVALIDATION_SECONDS
    stale_grace_seconds: float | None = DEFAULT_STALE_GRACE_SECONDS
    candidate_ttl_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class ActionPlanningSnapshot:
    metadata: Mapping[ActionIntentKey, ActionMetadata]
    pending: frozenset[ActionIntentKey]
    unavailable: frozenset[ActionIntentKey]


class ActionProviderSessionPreparer(Protocol):
    async def prepare_many(
        self,
        actions: Iterable[ActionMetadata],
    ) -> Mapping[object, object]: ...

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
            source=ActionAvailabilitySource.PROVIDER_DIRECT,
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
            source=ActionAvailabilitySource.PROVIDER_DIRECT,
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
    ) -> dict[ActionIntentKey, ActionMetadata]:
        snapshot: dict[ActionIntentKey, ActionMetadata] = {}
        snapshot_now = self._now(now)
        stale_keys = frozenset(stale_provider_keys)
        for intent in intents:
            metadata = self._metadata_for_intent(
                intent,
                now=snapshot_now,
                stale_provider_keys=stale_keys,
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
    ) -> ActionPlanningSnapshot:
        lookup_now = self._now(now)
        stale_keys = frozenset(stale_provider_keys)
        metadata: dict[ActionIntentKey, ActionMetadata] = {}
        pending: set[ActionIntentKey] = set()
        unavailable: set[ActionIntentKey] = set()

        for intent in intents:
            selected = self._selected_planning_record(
                intent,
                now=lookup_now,
                stale_provider_keys=stale_keys,
            )
            if selected is None:
                unavailable.add(intent)
                continue

            record = selected.record
            if self._planning_record_is_usable(
                selected,
                stale_provider_keys=stale_keys,
            ):
                if record.metadata is not None:
                    metadata[intent] = record.metadata
                else:
                    pending.add(intent)
                continue
            if self._planning_record_is_pending(selected):
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
    ) -> ActionMetadata | None:
        selected = self._selected_planning_record(
            intent,
            now=now,
            stale_provider_keys=stale_provider_keys,
        )
        if selected is None or not self._planning_record_is_usable(
            selected,
            stale_provider_keys=stale_provider_keys,
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
                ):
                    return item

        for predicate in (
            self._planning_record_is_fresh_available,
            self._planning_record_is_provider_direct_pending,
            self._planning_record_is_live_candidate,
        ):
            for item in records:
                if predicate(item):
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
                if item.record.source == ActionAvailabilitySource.PROVIDER_DIRECT
                else 1,
            )
        )
        return tuple(records)

    def _planning_record_is_usable(
        self,
        item: _PlanningRecord,
        *,
        stale_provider_keys: frozenset[ProviderActionKey],
    ) -> bool:
        record = item.record
        if record.source != ActionAvailabilitySource.PROVIDER_DIRECT:
            return False
        if record.state != ActionAvailabilityState.AVAILABLE:
            return False
        if item.state == ActionAvailabilityState.AVAILABLE:
            return record.metadata is not None
        return (
            item.state == ActionAvailabilityState.STALE
            and record.key in stale_provider_keys
            and record.metadata is not None
        )

    def _planning_record_is_fresh_available(self, item: _PlanningRecord) -> bool:
        record = item.record
        return (
            record.source == ActionAvailabilitySource.PROVIDER_DIRECT
            and record.state == ActionAvailabilityState.AVAILABLE
            and item.state == ActionAvailabilityState.AVAILABLE
            and record.metadata is not None
        )

    def _planning_record_is_provider_direct_pending(
        self,
        item: _PlanningRecord,
    ) -> bool:
        record = item.record
        if record.source != ActionAvailabilitySource.PROVIDER_DIRECT:
            return False
        if item.state == ActionAvailabilityState.EXPIRED:
            return False
        if record.state == ActionAvailabilityState.UNAVAILABLE:
            return False
        return item.state in {
            ActionAvailabilityState.UNKNOWN,
            ActionAvailabilityState.PROBING,
            ActionAvailabilityState.STALE,
        }

    def _planning_record_is_live_candidate(self, item: _PlanningRecord) -> bool:
        return (
            item.record.source == ActionAvailabilitySource.BEACON_CANDIDATE
            and item.state
            in {
                ActionAvailabilityState.UNKNOWN,
                ActionAvailabilityState.PROBING,
            }
        )

    def _planning_record_is_pending(self, item: _PlanningRecord) -> bool:
        return self._planning_record_is_provider_direct_pending(
            item
        ) or self._planning_record_is_live_candidate(item)

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


class ActionAvailabilityService:
    """Controller-level action availability state and provider-direct I/O."""

    def __init__(
        self,
        *,
        controller_id: str,
        controller_session_id: str,
        actions_bus: EndpointSession,
        manager: ActionProviderManager,
        start_soon: Callable[..., object] | None = None,
        provider_sessions: ActionProviderSessionPreparer | None = None,
        cache: ActionAvailabilityCache | None = None,
        clock: Callable[[], float] | None = None,
        revalidation_interval_seconds: float = DEFAULT_PROVIDER_REVALIDATION_SECONDS,
    ) -> None:
        self.controller_id = controller_id
        self.controller_session_id = controller_session_id
        self.actions_bus = actions_bus
        self.manager = manager
        self._provider_sessions = provider_sessions
        self.cache = cache or ActionAvailabilityCache(clock=clock)
        self._clock = clock or time.monotonic
        self._start_soon = start_soon
        self._revalidation_interval_seconds = revalidation_interval_seconds
        self._interest_by_config: dict[str, ActionInterestSnapshot] = {}
        self._last_interest_wire_by_provider: dict[
            str,
            tuple[tuple[str, str], ...],
        ] = {}
        self._last_request_at_by_provider: dict[str, float] = {}
        self._flush_scheduled = False

    async def start(
        self,
        tg,
        stopping,
    ) -> None:
        tg.start_soon(self._revalidation_loop, stopping)

    async def aclose(self) -> None:
        provider_sessions = self._provider_sessions
        self._provider_sessions = None
        if provider_sessions is not None:
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
        self._schedule_interest_flush()

    def clear_config_interest(self, config_id: str) -> None:
        self._interest_by_config.pop(config_id, None)
        self._schedule_interest_flush()

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
            *(
                action
                for succession in event.provider_session_successions
                for action in succession.actions
            ),
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
            metadata = self._with_current_provider_session(metadata)
            existing = self.cache._availability_records.get(key)
            if (
                existing is not None
                and existing.state == ActionAvailabilityState.AVAILABLE
            ):
                self.cache.record_available(
                    metadata,
                    now=now,
                    intent=self._mapped_intent_for_key(key),
                )
            else:
                self.cache.record_candidate(metadata, now=now)
            changed.add(key)
        self._schedule_interest_flush()
        return frozenset(changed)

    async def handle_availability_message(
        self,
        msg: DeckrMessage,
    ) -> frozenset[ProviderActionKey]:
        provider_instance_id = parse_action_provider_address(msg.sender)
        if provider_instance_id is None:
            logger.warning(
                "Ignoring availability message %s from non-provider sender %s",
                msg.message_type,
                msg.sender,
            )
            return frozenset()
        current_session_id = self.manager.provider_session_id(provider_instance_id)
        if (
            current_session_id is not None
            and msg.sender_session_id != current_session_id
        ):
            logger.warning(
                "Ignoring availability message %s from stale provider session %s",
                msg.message_type,
                msg.sender_session_id,
            )
            return frozenset()
        try:
            if msg.message_type == ACTION_AVAILABILITY_SNAPSHOT:
                body = ActionAvailabilitySnapshotBody.model_validate(msg.body)
            else:
                body = ActionAvailabilityChangedBody.model_validate(msg.body)
        except ValueError:
            logger.warning(
                "Ignoring invalid availability message %s from %s",
                msg.message_type,
                msg.sender,
                exc_info=True,
            )
            return frozenset()
        if body.provider_instance_id != provider_instance_id:
            logger.warning(
                "Ignoring availability message with mismatched providerInstanceId %s from %s",
                body.provider_instance_id,
                msg.sender,
            )
            return frozenset()
        return self.ingest_provider_entries(
            provider_instance_id=body.provider_instance_id,
            provider_id=body.provider_id,
            entries=body.entries,
            now=self._clock(),
        )

    def ingest_provider_entries(
        self,
        *,
        provider_instance_id: str,
        provider_id: str,
        entries: Iterable[ActionAvailabilityEntry],
        now: float | None = None,
    ) -> frozenset[ProviderActionKey]:
        changed: set[ProviderActionKey] = set()
        record_now = self._now(now)
        for entry in entries:
            key = ProviderActionKey(provider_instance_id, entry.action_id)
            metadata = self._metadata_for_entry(
                key=key,
                provider_id=provider_id,
                entry=entry,
            )
            mapped_intent = self._mapped_intent_for_key(key)
            if entry.status == "available":
                if metadata is None:
                    continue
                self.cache.record_available(
                    metadata,
                    now=record_now,
                    intent=mapped_intent,
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
            changed.add(key)
        return frozenset(changed)

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

    async def request_provider_availability(
        self,
        provider_instance_id: str,
        provider_id: str,
        action_ids: Iterable[str],
        *,
        force: bool = False,
        prepare_session: bool = True,
    ) -> None:
        if provider_instance_id == BUILTIN_ACTION_PROVIDER_ID:
            return
        action_ids = tuple(dict.fromkeys(action_ids))
        if not action_ids:
            return
        now = self._clock()
        last_request_at = self._last_request_at_by_provider.get(provider_instance_id)
        if (
            not force
            and last_request_at is not None
            and now - last_request_at < self._revalidation_interval_seconds
        ):
            return
        if prepare_session and not await self._prepare_provider_session(
            provider_instance_id,
            provider_id=provider_id,
            action_ids=action_ids,
        ):
            return
        self._last_request_at_by_provider[provider_instance_id] = now
        selectors = tuple(
            ActionAvailabilitySelector(actionId=action_id)
            for action_id in sorted(action_ids)
        )
        await self.actions_bus.send(
            lane=ACTIONS_LANE,
            recipient=action_provider_address(provider_instance_id),
            recipient_session_id=self.manager.provider_session_id(provider_instance_id),
            message_type=ACTION_AVAILABILITY_REQUEST,
            body=ActionAvailabilityRequestBody(
                requestId=str(uuid.uuid4()),
                selectors=selectors,
            ).to_dict(),
            subject=action_provider_instance_subject(
                provider_instance_id,
                provider_id=provider_id,
            ),
        )

    async def flush_interest(self, *, force_requests: bool = False) -> None:
        self._flush_scheduled = False
        provider_interests = self._interests_by_provider()
        for provider_instance_id, provider_id, entries in provider_interests:
            if not await self._prepare_provider_session(
                provider_instance_id,
                provider_id=provider_id,
                action_ids=(entry.action_id for entry in entries),
            ):
                continue
            wire = tuple((entry.action_id, entry.level) for entry in entries)
            if self._last_interest_wire_by_provider.get(provider_instance_id) != wire:
                self._last_interest_wire_by_provider[provider_instance_id] = wire
                await self.actions_bus.send(
                    lane=ACTIONS_LANE,
                    recipient=action_provider_address(provider_instance_id),
                    recipient_session_id=self.manager.provider_session_id(
                        provider_instance_id
                    ),
                    message_type=ACTION_INTEREST_UPDATE,
                    body=ActionInterestUpdateBody(
                        providerInstanceId=provider_instance_id,
                        providerId=provider_id,
                        entries=entries,
                    ).to_dict(),
                    subject=action_provider_instance_subject(
                        provider_instance_id,
                        provider_id=provider_id,
                    ),
                )
            await self.request_provider_availability(
                provider_instance_id,
                provider_id,
                (entry.action_id for entry in entries),
                force=force_requests,
                prepare_session=False,
            )

    async def _prepare_provider_session(
        self,
        provider_instance_id: str,
        *,
        provider_id: str,
        action_ids: Iterable[str],
    ) -> bool:
        provider_sessions = self._provider_sessions
        if provider_sessions is None:
            return True
        actions = tuple(
            self._metadata_for_session_prepare(
                provider_instance_id,
                provider_id=provider_id,
                action_id=action_id,
            )
            for action_id in dict.fromkeys(action_ids)
        )
        actions = tuple(action for action in actions if action is not None)
        if not actions:
            return True
        try:
            snapshots = await provider_sessions.prepare_many(actions)
        except Exception:
            logger.warning(
                "Could not prepare action provider session for %s",
                provider_instance_id,
                exc_info=True,
            )
            return False
        return not snapshots or any(
            not bool(getattr(snapshot, "terminal", False))
            for snapshot in snapshots.values()
        )

    def _metadata_for_session_prepare(
        self,
        provider_instance_id: str,
        *,
        provider_id: str,
        action_id: str,
    ) -> ActionMetadata | None:
        record = self.cache.record_for(ProviderActionKey(provider_instance_id, action_id))
        metadata = record.metadata if record is not None else None
        if metadata is not None:
            return self._with_current_provider_session(metadata)
        provider_session_id = self.manager.provider_session_id(provider_instance_id)
        if provider_session_id is None:
            return None
        return ActionMetadata(
            uuid=action_id,
            provider_instance_id=provider_instance_id,
            provider_id=provider_id,
            provider_session_id=provider_session_id,
        )

    async def _revalidation_loop(self, stopping) -> None:
        while not stopping.is_set():
            with anyio.move_on_after(self._revalidation_interval_seconds):
                await stopping.wait()
            if stopping.is_set():
                return
            try:
                await self.flush_interest(force_requests=True)
            except Exception:
                logger.exception("Error refreshing provider action availability")

    def _interests_by_provider(
        self,
    ) -> tuple[tuple[str, str, tuple[ActionInterestEntry, ...]], ...]:
        aggregated: dict[tuple[str, str], dict[str, str]] = {}
        for snapshot in self._interest_by_config.values():
            for record in snapshot.records:
                if record.intent.provider_instance_id in RESERVED_BUILTIN_PROVIDER_IDS:
                    continue
                candidates = self._candidate_records_for_intent(record.intent)
                for candidate in candidates:
                    metadata = candidate.metadata
                    if metadata is None:
                        continue
                    key = (metadata.provider_instance_id, metadata.provider_id)
                    action_levels = aggregated.setdefault(key, {})
                    previous = action_levels.get(metadata.uuid)
                    level = (
                        "strong"
                        if record.strength == ActionInterestStrength.STRONG
                        else "warm"
                    )
                    if previous != "strong":
                        action_levels[metadata.uuid] = level
        return tuple(
            (
                provider_instance_id,
                provider_id,
                tuple(
                    ActionInterestEntry(actionId=action_id, level=level)
                    for action_id, level in sorted(action_levels.items())
                ),
            )
            for (provider_instance_id, provider_id), action_levels in sorted(
                aggregated.items()
            )
        )

    def _candidate_records_for_intent(
        self,
        intent: ActionIntentKey,
    ) -> tuple[ActionAvailabilityRecord, ...]:
        records = [
            record
            for record in self.cache._candidate_records.values()
            if self.cache._record_matches_intent(
                record,
                intent,
                now=self._clock(),
            )
        ]
        records.sort(key=lambda record: record.key.provider_instance_id)
        return tuple(records)

    def _metadata_for_entry(
        self,
        *,
        key: ProviderActionKey,
        provider_id: str,
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
            provider_session_id=self.manager.provider_session_id(
                key.provider_instance_id
            ),
            provider_labels=(
                candidate_metadata.provider_labels
                if candidate_metadata is not None
                else None
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

    def _with_current_provider_session(
        self,
        metadata: ActionMetadata,
    ) -> ActionMetadata:
        provider_session_id = self.manager.provider_session_id(
            metadata.provider_instance_id
        )
        if provider_session_id == metadata.provider_session_id:
            return metadata
        return ActionMetadata(
            uuid=metadata.uuid,
            provider_instance_id=metadata.provider_instance_id,
            provider_id=metadata.provider_id,
            name=metadata.name,
            provider_session_id=provider_session_id or metadata.provider_session_id,
            provider_labels=metadata.provider_labels,
            settings_schema=metadata.settings_schema,
            provider_settings_schema=metadata.provider_settings_schema,
        )

    def _schedule_interest_flush(self) -> None:
        if self._flush_scheduled or self._start_soon is None:
            return
        self._flush_scheduled = True
        self._start_soon(self.flush_interest)

    def _now(self, now: float | None) -> float:
        return self._clock() if now is None else now


def _provider_action_key_from_catalog_id(
    catalog_id: str,
) -> ProviderActionKey | None:
    provider_instance_id, separator, action_uuid = catalog_id.partition("::")
    if not separator or not provider_instance_id or not action_uuid:
        return None
    return ProviderActionKey(provider_instance_id, action_uuid)
