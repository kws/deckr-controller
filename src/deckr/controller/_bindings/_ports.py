"""Dependency-neutral service ports consumed by controller bindings."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from deckr.actions.messages import ActionMessageBody
    from deckr.actions.rendering import BindingRenderResultBody
    from deckr.contracts.authority import ContractPointer
    from deckr.contracts.messages import DeckrMessage

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
    from deckr.controller._actions._routing import ActionRuntimeCommand


class LifecycleAvailabilityRecorder(Protocol):
    """Availability write port used by action-instance lifecycle handling."""

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
        action_id: str,
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

    async def decode_inbound_runtime_message(
        self, message: DeckrMessage
    ) -> ActionRuntimeCommand | None: ...

    async def authorize_runtime_message(
        self,
        key: ProviderSessionKey | None,
        message: DeckrMessage,
        *,
        validate_payload: bool = True,
    ) -> bool: ...

    async def send_runtime_message(
        self,
        provider_session_key: ProviderSessionKey,
        message_type: str,
        body: ActionMessageBody,
        *,
        contract: ContractPointer | None,
        causation_id: str | None = None,
    ) -> bool: ...

    async def ensure_local_builtin_availability(
        self,
        intents: Iterable[ActionIntentKey],
    ) -> None: ...

    async def render_message_key(
        self, message: DeckrMessage
    ) -> ProviderSessionKey | None: ...

    async def send_render_result(
        self, message: DeckrMessage, body: BindingRenderResultBody
    ) -> bool: ...

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
