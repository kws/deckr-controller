import time
from collections.abc import Callable, Iterable, Mapping
from types import SimpleNamespace
from typing import Any

from deckr.actions.endpoints import (
    BUILTIN_ACTION_PROVIDER_ID,
    RESERVED_BUILTIN_PROVIDER_IDS,
)
from deckr.actions.messages import (
    ActionAvailabilityEntry,
    ActionDescriptor,
)
from deckr.contracts.authority import ContractPointer

from deckr.controller._actions import (
    PROVIDER_SESSION_INVALID_REASON,
    ActionAvailabilityRecord,
    ActionAvailabilityState,
    ActionIntentKey,
    ActionMetadata,
    ActionPlanningSnapshot,
    BuiltinActionCatalog,
    ProviderActionKey,
    ProviderSessionKey,
    SettingsActionMetadata,
)
from deckr.controller._actions._availability import ActionAvailabilityCache
from test_support.binding_builders import _contract_pointer_for_provider_session
from test_support.binding_constants import (
    PROVIDER_ID,
    PROVIDER_INSTANCE_ID,
    PROVIDER_SESSION_ID,
)


class _BindingActionServiceHarness:
    def __init__(
        self,
        *,
        manager: BuiltinActionCatalog,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.manager = manager
        self._clock = clock or time.monotonic
        self._cache = ActionAvailabilityCache(clock=self._clock)
        self._contracts: dict[ProviderSessionKey, ContractPointer] = {}
        self._interest_by_config: dict[str, object] = {}
        self.sent_runtime_messages: list[SimpleNamespace] = []

    def planning_snapshot(
        self,
        intents: Iterable[ActionIntentKey],
        *,
        existing_provider_keys: Iterable[ProviderActionKey] = (),
        now: float | None = None,
    ) -> ActionPlanningSnapshot:
        return self._cache.planning_snapshot(
            intents,
            stale_provider_keys=existing_provider_keys,
            ready_provider_session_keys=frozenset(self._contracts),
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
        return self._cache.settings_metadata_for_intent(
            ActionIntentKey(
                action_id=action_id,
                provider_instance_id=provider_instance_id,
                provider_labels=tuple(sorted((provider_labels or {}).items())),
            ),
            provider_id=provider_id,
            now=self._now(now),
        )

    def current_contract(
        self,
        provider_session_key: ProviderSessionKey | None,
    ) -> ContractPointer | None:
        if provider_session_key is None:
            return None
        return self._contracts.get(provider_session_key)

    async def decode_inbound_runtime_message(self, message):
        from deckr.controller._actions._routing import decode_action_command

        try:
            return decode_action_command(message)
        except (ValueError, TypeError):
            return None

    async def authorize_runtime_message(self, key, message):
        from deckr.action_runtime import validate_action_runtime_message

        try:
            validate_action_runtime_message(message)
        except ValueError:
            return False
        return (
            key is not None
            and self.current_contract(key) == message.contract
            and key.provider_session_id == message.sender_session_id
        )

    async def send_runtime_message(
        self,
        provider_session_key: ProviderSessionKey,
        message_type: str,
        body: Any,
        *,
        contract,
        causation_id=None,
    ) -> bool:
        if contract is None or contract != self.current_contract(provider_session_key):
            return False
        self.sent_runtime_messages.append(
            SimpleNamespace(
                provider_session_key=provider_session_key,
                message_type=message_type,
                body=body,
                contract=contract,
            )
        )
        return True

    async def ensure_local_builtin_availability(
        self,
        intents: Iterable[ActionIntentKey],
    ) -> frozenset[ProviderActionKey]:
        changed: set[ProviderActionKey] = set()
        for intent in intents:
            if intent.provider_instance_id is not None and (
                intent.provider_instance_id not in RESERVED_BUILTIN_PROVIDER_IDS
            ):
                continue
            if intent.provider_labels:
                continue
            action = await self.manager.get_action(
                intent.action_id,
                provider_instance_id=intent.provider_instance_id,
                provider_labels=dict(intent.provider_labels),
            )
            if action is None:
                continue
            if action.provider_instance_id not in RESERVED_BUILTIN_PROVIDER_IDS:
                continue
            record = self._cache.record_available(
                action,
                now=self._clock(),
                intent=intent,
            )
            changed.add(record.key)
        return frozenset(changed)

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

    def record_for_key(
        self,
        key: ProviderActionKey,
    ) -> ActionAvailabilityRecord | None:
        return self._cache.record_for(key)

    def record_for_intent(
        self,
        intent: ActionIntentKey,
        *,
        now: float | None = None,
    ) -> ActionAvailabilityRecord | None:
        return self._cache.record_for_intent(intent, now=self._now(now))

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

    def update_config_interest(self, config_id: str, snapshot: object) -> None:
        self._interest_by_config[config_id] = snapshot

    def clear_config_interest(self, config_id: str) -> None:
        self._interest_by_config.pop(config_id, None)

    def has_config_interest(self, config_id: str) -> bool:
        return config_id in self._interest_by_config

    def record_runtime_contract(
        self,
        metadata: ActionMetadata,
        *,
        contract: ContractPointer | None = None,
    ) -> None:
        if (
            metadata.provider_instance_id == BUILTIN_ACTION_PROVIDER_ID
            or metadata.provider_session_id is None
        ):
            return
        pointer = contract or _contract_pointer_for_provider_session(
            metadata.provider_session_id,
            provider_instance_id=metadata.provider_instance_id,
            provider_id=metadata.provider_id,
        )
        self._contracts[
            ProviderSessionKey(
                metadata.provider_instance_id,
                metadata.provider_id,
                metadata.provider_session_id,
            )
        ] = pointer

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
            metadata = self._metadata_for_entry(
                key=key,
                provider_id=provider_id,
                provider_session_id=provider_session_id,
                provider_labels=provider_labels,
                entry=entry,
            )
            if entry.status == "available":
                if metadata is None:
                    continue
                existing = self._cache.record_for(key)
                requires_recovery = (
                    existing is not None
                    and existing.state == ActionAvailabilityState.UNAVAILABLE
                    and existing.reason == PROVIDER_SESSION_INVALID_REASON
                )
                self._cache.record_available(
                    metadata,
                    now=record_now,
                    requires_provider_lifecycle_recovery=requires_recovery,
                )
            elif entry.status == "unavailable":
                self._cache.record_unavailable(
                    key,
                    metadata=metadata,
                    reason=entry.reason,
                    now=record_now,
                )
            else:
                self._cache.record_probing(
                    key,
                    metadata=metadata,
                    reason=entry.reason,
                    now=record_now,
                )
            changed.add(key)
        return frozenset(changed)

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
        if descriptor is None and entry.status == "available":
            return None
        return ActionMetadata(
            action_id=entry.action_id,
            provider_instance_id=key.provider_instance_id,
            provider_id=provider_id,
            name=descriptor.name if descriptor is not None else None,
            descriptor=descriptor,
            provider_session_id=provider_session_id,
            provider_labels=provider_labels,
            settings_schema=(
                descriptor.settings_schema if descriptor is not None else None
            ),
            provider_settings_schema=(
                descriptor.provider_settings_schema if descriptor is not None else None
            ),
        )

    def _now(self, now: float | None) -> float:
        return self._clock() if now is None else now


def _requirements():
    return tuple(
        {"name": name, "required": False, "preferences": [{"direction": direction}]}
        for name, direction in (("input", "input"), ("output", "output"))
    )


def _metadata(
    action_id: str,
    *,
    provider_instance_id: str = PROVIDER_INSTANCE_ID,
    provider_id: str = PROVIDER_ID,
    provider_session_id: str | None = PROVIDER_SESSION_ID,
) -> ActionMetadata:
    return ActionMetadata(
        action_id=action_id,
        provider_instance_id=provider_instance_id,
        provider_id=provider_id,
        provider_session_id=provider_session_id,
        descriptor=ActionDescriptor(actionId=action_id, requirements=_requirements()),
    )


def _builtin_metadata(action_id: str) -> ActionMetadata:
    return _metadata(
        action_id,
        provider_instance_id=BUILTIN_ACTION_PROVIDER_ID,
        provider_id=BUILTIN_ACTION_PROVIDER_ID,
        provider_session_id=None,
    )


def _availability_entry(action_id: str) -> ActionAvailabilityEntry:
    return ActionAvailabilityEntry(
        actionId=action_id,
        status="available",
        descriptor=ActionDescriptor(
            actionId=action_id,
            providerId=PROVIDER_ID,
            requirements=_requirements(),
        ),
    )
