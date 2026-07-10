"""Dependency-neutral service ports consumed by controller bindings."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from deckr.contracts.authority import ContractPointer

    from deckr.controller._action_interest import ActionInterestSnapshot
    from deckr.controller._actions._models import (
        ActionAvailabilityRecord,
        ActionAvailabilityState,
        ActionIntentKey,
        ActionPlanningSnapshot,
        ProviderActionKey,
        ProviderSessionKey,
        SettingsActionMetadata,
    )


class LifecycleAvailabilityRecorder(Protocol):
    """Availability write port used by action-instance lifecycle handling."""

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
    ) -> ProviderActionKey: ...


class BindingActionService(LifecycleAvailabilityRecorder, Protocol):
    """Action-service operations required by the binding orchestrator."""

    def planning_snapshot(
        self,
        intents: Iterable[ActionIntentKey],
        *,
        existing_provider_keys: Iterable[ProviderActionKey] = (),
        now: float | None = None,
    ) -> ActionPlanningSnapshot: ...

    def settings_action_metadata(
        self,
        action_uuid: str,
        *,
        provider_instance_id: str | None = None,
        provider_id: str | None = None,
        provider_labels: Mapping[str, str] | None = None,
        now: float | None = None,
    ) -> SettingsActionMetadata: ...

    def current_contract(
        self,
        provider_session_key: ProviderSessionKey | None,
    ) -> ContractPointer | None: ...

    async def send_runtime_message(
        self,
        provider_session_key: ProviderSessionKey,
        message_type: str,
        body: Any,
    ) -> bool: ...

    async def ensure_local_builtin_availability(
        self,
        intents: Iterable[ActionIntentKey],
    ) -> frozenset[ProviderActionKey]: ...

    def record_for_key(
        self,
        key: ProviderActionKey,
    ) -> ActionAvailabilityRecord | None: ...

    def record_for_intent(
        self,
        intent: ActionIntentKey,
        *,
        now: float | None = None,
    ) -> ActionAvailabilityRecord | None: ...

    def state_for_key(
        self,
        key: ProviderActionKey,
        *,
        now: float | None = None,
    ) -> ActionAvailabilityState | None: ...

    def provider_lifecycle_recovery_required(self, key: ProviderActionKey) -> bool: ...

    def consume_provider_lifecycle_recovery(self, key: ProviderActionKey) -> bool: ...

    def update_config_interest(
        self,
        config_id: str,
        snapshot: ActionInterestSnapshot,
    ) -> None: ...

    def clear_config_interest(self, config_id: str) -> None: ...
