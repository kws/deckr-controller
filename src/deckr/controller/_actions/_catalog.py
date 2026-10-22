"""Controller-owned action runtime service coordination."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from deckr.controller._actions._availability import ActionAvailabilityCache
    from deckr.controller._actions._models import BuiltinActionCatalog


import logging
from collections.abc import Iterable, Mapping

from deckr.action_runtime import (
    ActionRuntimeAvailabilityViewPayload,
    action_runtime_provider_instance_id,
)
from deckr.actions.endpoints import RESERVED_BUILTIN_PROVIDER_IDS
from deckr.actions.messages import (
    ActionAvailabilityEntry,
)

from deckr.controller._actions._availability_policy import (
    PROVIDER_SESSION_INVALID_REASON,
    SERVICE_VIEW_MISSING_REASON,
    SERVICE_VIEW_UNAVAILABLE_REASON,
    _availability_state_for_entry,
    _descriptor_hash,
    _entry_same_as_existing,
    _state_value,
)
from deckr.controller._actions._models import (
    ActionAvailabilityState,
    ActionIntentKey,
    ActionMetadata,
    ActionPlanningSnapshot,
    ProviderActionKey,
    ProviderSessionKey,
    SettingsActionMetadata,
)
from deckr.controller._actions._projection import metadata_for_entry

logger = logging.getLogger(__name__)


class ActionCatalog:
    """Local selection policy and accepted provider availability projections."""

    def __init__(
        self,
        *,
        manager: BuiltinActionCatalog,
        cache: ActionAvailabilityCache,
        clock: Callable[[], float],
    ):
        self.manager = manager
        self._cache = cache
        self._clock = clock

    def records(self):
        return self._cache.service_view_records()

    def prune(self, *, live_sessions, interests):
        return self._cache.prune(
            live_sessions=live_sessions, interests=interests, now=self._clock()
        )

    def _sync_local_catalog(self) -> None:
        for metadata in self.manager.catalog_snapshot():
            self._cache.record_available(metadata, now=self._clock())

    def planning_snapshot(
        self,
        intents: Iterable[ActionIntentKey],
        *,
        existing_provider_keys: Iterable[ProviderActionKey] = (),
        ready_provider_session_keys: frozenset[ProviderSessionKey] | None = None,
        now: float | None = None,
    ) -> ActionPlanningSnapshot:
        self._sync_local_catalog()
        return self._cache.planning_snapshot(
            intents,
            stale_provider_keys=existing_provider_keys,
            ready_provider_session_keys=ready_provider_session_keys,
            now=self._now(now),
        )

    def settings_action_metadata(
        self,
        action_id: str,
        *,
        provider_instance_id: str | None = None,
        provider_id: str | None = None,
        provider_labels: Mapping[str, str] | None = None,
        now: float | None = None,
    ) -> SettingsActionMetadata:
        self._sync_local_catalog()
        return self._cache.settings_metadata_for_intent(
            ActionIntentKey(
                action_id=action_id,
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
        action_id: str,
        provider_session_id: str | None = None,
        reason: str | None = None,
        intent: ActionIntentKey | None = None,
        now: float | None = None,
    ) -> ProviderActionKey:
        key = ProviderActionKey(provider_instance_id, action_id)
        existing = self._cache.record_for(key)
        metadata = existing.metadata if existing is not None else None
        if metadata is None:
            metadata = ActionMetadata(
                action_id=action_id,
                provider_instance_id=provider_instance_id,
                provider_id=provider_id,
                provider_session_id=provider_session_id,
            )
        self._cache.record_unavailable(
            key,
            metadata=metadata,
            reason=reason,
            now=self._now(now),
            intent=intent,
        )
        return key

    def record_for_intent(
        self,
        intent: ActionIntentKey,
        *,
        now: float | None = None,
    ):
        return self._cache.record_for_intent(intent, now=self._now(now))

    def record_for_key(self, key: ProviderActionKey):
        return self._cache.record_for(key)

    def state_for_key(
        self,
        key: ProviderActionKey,
        *,
        now: float | None = None,
    ) -> ActionAvailabilityState | None:
        return self._cache.state_for(key, now=self._now(now))

    def provider_lifecycle_recovery_required(self, key: ProviderActionKey) -> bool:
        return self._cache.provider_lifecycle_recovery_required(key)

    def consume_provider_lifecycle_recovery(self, key: ProviderActionKey) -> bool:
        return self._cache.consume_provider_lifecycle_recovery(key)

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
                provider_id=view.provider_id,
                provider_session_id=view.service_session_id,
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
            existing = self._cache.record_for(key)
            old_state = (
                self._cache.state_for(key, now=record_now)
                if existing is not None
                else None
            )
            metadata = metadata_for_entry(
                self._cache,
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
                self._cache.record_available(
                    metadata,
                    now=record_now,
                    intent=mapped_intent,
                    requires_provider_lifecycle_recovery=(
                        requires_provider_lifecycle_recovery
                    ),
                )
            elif entry.status == "unavailable":
                self._cache.record_unavailable(
                    key,
                    metadata=metadata,
                    reason=entry.reason,
                    now=record_now,
                    intent=mapped_intent,
                )
            else:
                self._cache.record_probing(
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
            for record in self._cache.service_view_records()
            if record.key.provider_instance_id == provider_instance_id
        ]
        for record in records:
            if record.state == ActionAvailabilityState.UNAVAILABLE and (
                record.reason == reason
            ):
                continue
            self._cache.record_unavailable(
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

    def mark_provider_session_unavailable(
        self,
        key: ProviderSessionKey,
        *,
        reason: str = SERVICE_VIEW_UNAVAILABLE_REASON,
        now: float | None = None,
    ) -> frozenset[ProviderActionKey]:
        changed: set[ProviderActionKey] = set()
        record_now = self._now(now)
        for record in self._cache.service_view_records():
            metadata = record.metadata
            if metadata is None:
                continue
            if (
                metadata.provider_instance_id != key.provider_instance_id
                or metadata.provider_id != key.provider_id
                or metadata.provider_session_id != key.provider_session_id
            ):
                continue
            if record.state == ActionAvailabilityState.UNAVAILABLE and (
                record.reason == reason
            ):
                continue
            self._cache.record_unavailable(
                record.key,
                metadata=metadata,
                reason=reason,
                now=record_now,
                intent=self._mapped_intent_for_key(record.key),
            )
            changed.add(record.key)
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
            existing = self._cache.record_for_intent(intent, now=self._clock())
            if existing is not None and existing.key.provider_instance_id in (
                RESERVED_BUILTIN_PROVIDER_IDS
            ):
                continue
            metadata = await self.manager.get_action(
                intent.action_id,
                provider_instance_id=intent.provider_instance_id,
                provider_labels=dict(intent.provider_labels),
            )
            if metadata is None:
                continue
            if metadata.provider_instance_id not in RESERVED_BUILTIN_PROVIDER_IDS:
                continue
            self._cache.record_available(
                metadata,
                now=self._clock(),
                intent=intent,
            )

    def _mark_service_view_entries_missing(
        self,
        *,
        provider_instance_id: str,
        provider_id: str,
        provider_session_id: str,
        seen_action_ids: set[str],
        now: float,
    ) -> frozenset[ProviderActionKey]:
        changed: set[ProviderActionKey] = set()
        for record in self._cache.service_view_records():
            if record.key.provider_instance_id != provider_instance_id:
                continue
            if record.key.action_id in seen_action_ids:
                continue
            metadata = record.metadata
            if metadata is not None and (
                metadata.provider_id != provider_id
                or metadata.provider_session_id != provider_session_id
            ):
                continue
            if (
                record.state == ActionAvailabilityState.UNAVAILABLE
                and record.reason == SERVICE_VIEW_MISSING_REASON
            ):
                continue
            self._cache.record_unavailable(
                record.key,
                metadata=record.metadata,
                reason=SERVICE_VIEW_MISSING_REASON,
                now=now,
                intent=self._mapped_intent_for_key(record.key),
            )
            changed.add(record.key)
        return frozenset(changed)

    def _mapped_intent_for_key(self, key: ProviderActionKey) -> ActionIntentKey | None:
        return self._cache.intent_for_key(key)

    def _now(self, now: float | None) -> float:
        return self._clock() if now is None else now
