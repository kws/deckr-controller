"""Local action-interest tracking for controller-owned action need."""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum

from deckr.controller._binding_planner import ActionIntentKey

DEFAULT_WARM_RETENTION_SECONDS = 4 * 60 * 60


class ActionInterestSource(StrEnum):
    VISIBLE_BINDING = "visible_binding"
    DYNAMIC_PAGE = "dynamic_page"
    CONNECTED_CONFIG = "connected_config"
    RECENT_USE = "recent_use"
    SETTINGS = "settings"
    PREWARM = "prewarm"


class ActionInterestStrength(StrEnum):
    STRONG = "strong"
    WARM = "warm"


@dataclass(frozen=True, slots=True)
class ActionInterestSourceKey:
    source: ActionInterestSource
    scope_id: str | None = None


@dataclass(frozen=True, slots=True)
class ActionInterestRecord:
    intent: ActionIntentKey
    source: ActionInterestSource
    strength: ActionInterestStrength
    first_needed_at: float
    last_needed_at: float
    retain_until: float | None
    scope_id: str | None = None


@dataclass(frozen=True, slots=True)
class ActionInterestPolicy:
    warm_retention_seconds: float | None = DEFAULT_WARM_RETENTION_SECONDS


@dataclass(frozen=True, slots=True)
class ActionInterestSnapshot:
    records: tuple[ActionInterestRecord, ...]

    @property
    def strong_intents(self) -> tuple[ActionIntentKey, ...]:
        return _sorted_intents(
            {
                record.intent
                for record in self.records
                if record.strength == ActionInterestStrength.STRONG
            }
        )

    @property
    def warm_intents(self) -> tuple[ActionIntentKey, ...]:
        strong = set(self.strong_intents)
        return _sorted_intents(
            {
                record.intent
                for record in self.records
                if record.strength == ActionInterestStrength.WARM
                and record.intent not in strong
            }
        )

    @property
    def all_intents(self) -> tuple[ActionIntentKey, ...]:
        return _sorted_intents({record.intent for record in self.records})


class ActionInterestTracker:
    """Tracks strong and warm action interest without provider I/O."""

    def __init__(
        self,
        *,
        policy: ActionInterestPolicy | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._policy = policy or ActionInterestPolicy()
        self._clock = clock or time.monotonic
        self._records: dict[
            tuple[ActionInterestSourceKey, ActionIntentKey],
            ActionInterestRecord,
        ] = {}

    def replace_strong_interests(
        self,
        source: ActionInterestSource,
        intents: Iterable[ActionIntentKey],
        *,
        now: float | None = None,
        scope_id: str | None = None,
    ) -> None:
        self._replace_interests(
            source,
            intents,
            strength=ActionInterestStrength.STRONG,
            now=self._now(now),
            scope_id=scope_id,
        )

    def replace_warm_interests(
        self,
        source: ActionInterestSource,
        intents: Iterable[ActionIntentKey],
        *,
        now: float | None = None,
        scope_id: str | None = None,
    ) -> None:
        self._replace_interests(
            source,
            intents,
            strength=ActionInterestStrength.WARM,
            now=self._now(now),
            scope_id=scope_id,
        )

    def clear_source(
        self,
        source: ActionInterestSource,
        *,
        now: float | None = None,
        scope_id: str | None = None,
    ) -> None:
        self.replace_strong_interests(
            source,
            (),
            now=now,
            scope_id=scope_id,
        )

    def snapshot(self, *, now: float | None = None) -> ActionInterestSnapshot:
        snapshot_now = self._now(now)
        records = tuple(
            sorted(
                (
                    record
                    for record in self._records.values()
                    if not self._is_expired(record, now=snapshot_now)
                ),
                key=_record_sort_key,
            )
        )
        return ActionInterestSnapshot(records=records)

    def expire(self, *, now: float | None = None) -> None:
        expire_now = self._now(now)
        for key, record in tuple(self._records.items()):
            if self._is_expired(record, now=expire_now):
                self._records.pop(key, None)

    def _replace_interests(
        self,
        source: ActionInterestSource,
        intents: Iterable[ActionIntentKey],
        *,
        strength: ActionInterestStrength,
        now: float,
        scope_id: str | None,
    ) -> None:
        source_key = ActionInterestSourceKey(source=source, scope_id=scope_id)
        next_intents = set(intents)
        previous_intents = {
            intent
            for key, intent in self._records
            if key == source_key
            and not self._is_expired(self._records[(key, intent)], now=now)
            and (
                strength == ActionInterestStrength.WARM
                or self._records[(key, intent)].strength == ActionInterestStrength.STRONG
            )
        }

        for removed_intent in previous_intents - next_intents:
            if strength == ActionInterestStrength.WARM:
                self._records.pop((source_key, removed_intent), None)
            else:
                self._demote_or_remove(source_key, removed_intent, now=now)

        for intent in next_intents:
            key = (source_key, intent)
            previous = self._records.get(key)
            first_needed_at = (
                now
                if previous is None or self._is_expired(previous, now=now)
                else previous.first_needed_at
            )
            retain_until = (
                None
                if strength == ActionInterestStrength.STRONG
                else self._retain_until(now)
            )
            self._records[key] = ActionInterestRecord(
                intent=intent,
                source=source,
                strength=strength,
                first_needed_at=first_needed_at,
                last_needed_at=now,
                retain_until=retain_until,
                scope_id=scope_id,
            )

        self.expire(now=now)

    def _demote_or_remove(
        self,
        source_key: ActionInterestSourceKey,
        intent: ActionIntentKey,
        *,
        now: float,
    ) -> None:
        key = (source_key, intent)
        previous = self._records.get(key)
        if previous is None:
            return
        retain_until = self._retain_until(now)
        if retain_until is None:
            self._records.pop(key, None)
            return
        self._records[key] = ActionInterestRecord(
            intent=previous.intent,
            source=previous.source,
            strength=ActionInterestStrength.WARM,
            first_needed_at=previous.first_needed_at,
            last_needed_at=now,
            retain_until=retain_until,
            scope_id=previous.scope_id,
        )

    def _retain_until(self, now: float) -> float | None:
        warm_retention = self._policy.warm_retention_seconds
        if warm_retention is None:
            return None
        if warm_retention <= 0:
            return None
        return now + warm_retention

    def _is_expired(self, record: ActionInterestRecord, *, now: float) -> bool:
        return (
            record.strength == ActionInterestStrength.WARM
            and record.retain_until is not None
            and now > record.retain_until
        )

    def _now(self, now: float | None) -> float:
        return self._clock() if now is None else now


def _sorted_intents(intents: set[ActionIntentKey]) -> tuple[ActionIntentKey, ...]:
    return tuple(sorted(intents, key=_intent_sort_key))


def _intent_sort_key(
    intent: ActionIntentKey,
) -> tuple[str, str, tuple[tuple[str, str], ...]]:
    return (
        intent.action_uuid,
        intent.provider_instance_id or "",
        intent.provider_labels,
    )


def _record_sort_key(
    record: ActionInterestRecord,
) -> tuple[str, str, str, str, tuple[tuple[str, str], ...]]:
    intent_key = _intent_sort_key(record.intent)
    return (
        record.source,
        record.scope_id or "",
        intent_key[0],
        intent_key[1],
        intent_key[2],
    )
