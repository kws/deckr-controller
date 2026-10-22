"""Controller-owned action runtime service coordination."""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable, Mapping
from inspect import isawaitable
from typing import Any

import anyio
from deckr.action_runtime import (
    ActionRuntimeAvailabilityViewPayload,
)
from deckr.actions.messages import (
    ActionAvailabilityEntry,
    ActionMessageBody,
)
from deckr.actions.rendering import BindingRenderResultBody
from deckr.contracts.authority import ContractPointer
from deckr.contracts.messages import DeckrMessage
from deckr.services import (
    DeckrServices,
)

from deckr.controller._action_interest import ActionInterestSnapshot
from deckr.controller._actions._availability import ActionAvailabilityCache
from deckr.controller._actions._availability_policy import (
    SERVICE_VIEW_UNAVAILABLE_REASON,
)
from deckr.controller._actions._catalog import ActionCatalog
from deckr.controller._actions._models import (
    ActionAvailabilityPolicy,
    ActionAvailabilityState,
    ActionIntentKey,
    ActionPlanningSnapshot,
    AvailabilityChangedCallback,
    BuiltinActionCatalog,
    ProviderActionKey,
    ProviderSessionKey,
    SettingsActionMetadata,
)
from deckr.controller._actions._routing import ActionRuntimeCommand
from deckr.controller._actions._runtime_gateway import ActionRuntimeGateway
from deckr.controller._actions._supervisor import ActionRuntimeSupervisor

logger = logging.getLogger(__name__)


class ControllerActionService:
    """Composition of catalog policy, managed supervision and message gateway."""

    def __init__(
        self,
        *,
        controller_id: str,
        controller_session_id: str,
        manager: BuiltinActionCatalog,
        services: DeckrServices | None = None,
        close_services_on_aclose: bool = False,
        start_soon: Any | None = None,
        on_availability_changed: AvailabilityChangedCallback | None = None,
        cache: ActionAvailabilityCache | None = None,
        clock: Any | None = None,
    ) -> None:
        self._on_availability_changed = on_availability_changed
        self._clock = clock or time.monotonic
        self.controller_id = controller_id
        self.controller_session_id = controller_session_id
        self.catalog = ActionCatalog(
            manager=manager,
            cache=cache
            or ActionAvailabilityCache(policy=ActionAvailabilityPolicy(), clock=clock),
            clock=clock or time.monotonic,
        )
        self.supervisor = ActionRuntimeSupervisor(
            services=services,
            close_services_on_aclose=close_services_on_aclose,
            start_soon=start_soon,
            catalog=self.catalog,
            on_availability_changed=self._forward_change,
        )
        self.gateway = ActionRuntimeGateway(
            supervisor=self.supervisor, catalog=self.catalog
        )
        self._interest_by_config: dict[str, ActionInterestSnapshot] = {}

    def set_change_callback(
        self,
        callback: AvailabilityChangedCallback | None,
    ) -> None:
        self._on_availability_changed = callback

    async def start(
        self,
        tg: anyio.abc.TaskGroup,
        stopping: anyio.Event,
    ) -> None:
        return await self.supervisor.start(tg, stopping)

    async def aclose(self) -> None:
        return await self.supervisor.aclose()

    def planning_snapshot(
        self,
        intents: Iterable[ActionIntentKey],
        *,
        existing_provider_keys: Iterable[ProviderActionKey] = (),
        now: float | None = None,
    ) -> ActionPlanningSnapshot:
        return self.catalog.planning_snapshot(
            intents,
            existing_provider_keys=existing_provider_keys,
            now=now,
            ready_provider_session_keys=self.supervisor._ready_provider_session_keys(),
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
        return self.catalog.settings_action_metadata(
            action_id,
            provider_instance_id=provider_instance_id,
            provider_id=provider_id,
            provider_labels=provider_labels,
            now=now,
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
        return self.catalog.record_lifecycle_unavailable(
            provider_instance_id=provider_instance_id,
            provider_id=provider_id,
            action_id=action_id,
            provider_session_id=provider_session_id,
            reason=reason,
            intent=intent,
            now=now,
        )

    def record_for_intent(
        self,
        intent: ActionIntentKey,
        *,
        now: float | None = None,
    ):
        return self.catalog.record_for_intent(intent, now=now)

    def record_for_key(self, key: ProviderActionKey):
        return self.catalog.record_for_key(key)

    def state_for_key(
        self,
        key: ProviderActionKey,
        *,
        now: float | None = None,
    ) -> ActionAvailabilityState | None:
        return self.catalog.state_for_key(key, now=now)

    def provider_lifecycle_recovery_required(self, key: ProviderActionKey) -> bool:
        return self.catalog.provider_lifecycle_recovery_required(key)

    def consume_provider_lifecycle_recovery(self, key: ProviderActionKey) -> bool:
        return self.catalog.consume_provider_lifecycle_recovery(key)

    def ingest_service_view_payload(
        self,
        payload: ActionRuntimeAvailabilityViewPayload | Mapping[str, object],
        *,
        service_id: str | None = None,
        now: float | None = None,
    ) -> frozenset[ProviderActionKey]:
        return self.catalog.ingest_service_view_payload(
            payload, service_id=service_id, now=now
        )

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
        return self.catalog.ingest_provider_entries(
            provider_instance_id=provider_instance_id,
            provider_id=provider_id,
            provider_session_id=provider_session_id,
            provider_labels=provider_labels,
            entries=entries,
            now=now,
        )

    def mark_provider_service_unavailable(
        self,
        provider_instance_id: str,
        *,
        reason: str = SERVICE_VIEW_UNAVAILABLE_REASON,
        now: float | None = None,
    ) -> frozenset[ProviderActionKey]:
        return self.catalog.mark_provider_service_unavailable(
            provider_instance_id, reason=reason, now=now
        )

    def mark_provider_session_unavailable(
        self,
        key: ProviderSessionKey,
        *,
        reason: str = SERVICE_VIEW_UNAVAILABLE_REASON,
        now: float | None = None,
    ) -> frozenset[ProviderActionKey]:
        return self.catalog.mark_provider_session_unavailable(
            key, reason=reason, now=now
        )

    async def ensure_local_builtin_availability(
        self,
        intents: Iterable[ActionIntentKey],
    ) -> None:
        return await self.catalog.ensure_local_builtin_availability(intents)

    def current_contract(
        self,
        provider_session_key: ProviderSessionKey | None,
    ) -> ContractPointer | None:
        return self.gateway.current_contract(provider_session_key)

    async def authorize_runtime_message(
        self,
        key: ProviderSessionKey | None,
        message: DeckrMessage,
        *,
        validate_payload: bool = True,
    ) -> bool:
        return await self.gateway.authorize_runtime_message(
            key, message, validate_payload=validate_payload
        )

    async def send_runtime_message(
        self,
        provider_session_key: ProviderSessionKey,
        message_type: str,
        body: ActionMessageBody,
        *,
        contract: ContractPointer | None,
        causation_id: str | None = None,
    ) -> bool:
        return await self.gateway.send_runtime_message(
            provider_session_key,
            message_type,
            body,
            contract=contract,
            causation_id=causation_id,
        )

    async def render_message_key(
        self, message: DeckrMessage
    ) -> ProviderSessionKey | None:
        return await self.gateway.render_message_key(message)

    async def send_render_result(
        self, message: DeckrMessage, body: BindingRenderResultBody
    ) -> bool:
        return await self.gateway.send_render_result(message, body)

    async def decode_inbound_runtime_message(
        self,
        deckr_message: DeckrMessage,
    ) -> ActionRuntimeCommand | None:
        return await self.gateway.decode_inbound_runtime_message(deckr_message)

    def update_config_interest(
        self,
        config_id: str,
        snapshot: ActionInterestSnapshot,
    ) -> None:
        self._interest_by_config[config_id] = snapshot
        self._prune()

    def clear_config_interest(self, config_id: str) -> None:
        self._interest_by_config.pop(config_id, None)
        self._prune()

    def _prune(self):
        live = self.supervisor._ready_provider_session_keys()
        if live is None:
            return
        now = self._clock()
        self.catalog.prune(
            live_sessions=live,
            interests=(
                record.intent
                for snapshot in self._interest_by_config.values()
                for record in snapshot.records
                if record.retain_until is None or record.retain_until > now
            ),
        )

    async def _forward_change(self, changed):
        self._prune()
        if self._on_availability_changed is not None and changed:
            result = self._on_availability_changed(changed)
            if isawaitable(result):
                await result
