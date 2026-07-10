"""Controller-owned action runtime service coordination."""

from __future__ import annotations

import logging
import time
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from inspect import isawaitable
from typing import Any

import anyio
from deckr.action_runtime import (
    ACTION_RUNTIME_SERVICE_NAMESPACE,
    ACTION_RUNTIME_SERVICE_PROTOCOL,
    RUNTIME_TO_CONTROLLER_MESSAGES,
    ActionRuntimeAvailabilityViewPayload,
    action_availability_view_ref,
    action_runtime_body_from_service_message,
    action_runtime_message_name,
    action_runtime_payload,
    action_runtime_provider_instance_id,
    legacy_action_message_type,
)
from deckr.actions.endpoints import RESERVED_BUILTIN_PROVIDER_IDS
from deckr.actions.messages import ActionAvailabilityEntry, ActionMessageBody
from deckr.contracts.authority import ContractPointer
from deckr.contracts.messages import DeckrMessage, entity_subject, parse_service_address
from deckr.services import (
    DeckrServices,
    ServiceBackendStatus,
    ServiceDescriptor,
    ServiceMessageBody,
    ServiceUnavailable,
    ServiceUseLease,
    newest_service_descriptor,
    service_body,
    service_unavailable_ends_service_use,
)

from deckr.controller._action_interest import ActionInterestSnapshot
from deckr.controller._actions._availability import (
    PROVIDER_SESSION_INVALID_REASON,
    SERVICE_VIEW_MISSING_REASON,
    SERVICE_VIEW_UNAVAILABLE_REASON,
    ActionAvailabilityCache,
    _availability_state_for_entry,
    _descriptor_hash,
    _entry_same_as_existing,
    _metadata_requires_provider_session_revalidation,
    _state_value,
)
from deckr.controller._actions._models import (
    ActionAvailabilityPolicy,
    ActionAvailabilityState,
    ActionMetadata,
    ActionPlanningSnapshot,
    ActionProviderManager,
    AvailabilityChangedCallback,
    ProviderActionKey,
    ProviderSessionKey,
    SettingsActionMetadata,
    provider_session_key,
)
from deckr.controller._binding_planner import ActionIntentKey

logger = logging.getLogger(__name__)

_SERVICE_WATCH_RETRY_SECONDS = 1.0


@dataclass(frozen=True, slots=True)
class _RuntimeLeaseState:
    key: ProviderSessionKey
    lease: ServiceUseLease
    service_id: str
    generation: int


class ControllerActionService:
    """Internal controller facade for action runtime discovery and messages."""

    def __init__(
        self,
        *,
        controller_id: str,
        controller_session_id: str,
        manager: ActionProviderManager,
        services: DeckrServices | None = None,
        close_services_on_aclose: bool = False,
        start_soon: Any | None = None,
        on_availability_changed: AvailabilityChangedCallback | None = None,
        cache: ActionAvailabilityCache | None = None,
        clock: Any | None = None,
    ) -> None:
        self.controller_id = controller_id
        self.controller_session_id = controller_session_id
        self.manager = manager
        self._services = services
        self._close_services_on_aclose = close_services_on_aclose
        self._cache = cache or ActionAvailabilityCache(
            policy=ActionAvailabilityPolicy(),
            clock=clock,
        )
        self._clock = clock or time.monotonic
        self._start_soon = start_soon
        self._interest_by_config: dict[str, ActionInterestSnapshot] = {}
        self._service_watch_scopes: dict[str, anyio.CancelScope] = {}
        self._service_descriptor_keys: dict[str, tuple[str, str, str]] = {}
        self._service_watch_generations: dict[str, int] = {}
        self._runtime_leases: dict[ProviderSessionKey, _RuntimeLeaseState] = {}
        self._on_availability_changed = on_availability_changed
        self._stopping: anyio.Event | None = None

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
        self._stopping = stopping
        if self._services is not None:
            tg.start_soon(self._service_directory_loop, stopping)

    async def aclose(self) -> None:
        for scope in tuple(self._service_watch_scopes.values()):
            scope.cancel()
        self._service_watch_scopes.clear()
        self._service_descriptor_keys.clear()
        self._runtime_leases.clear()
        self._stopping = None
        if self._close_services_on_aclose and self._services is not None:
            await self._services.aclose()
            self._services = None

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
        return self._cache.settings_metadata_for_intent(
            ActionIntentKey(
                action_uuid=action_uuid,
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
        state = self._runtime_leases.get(provider_session_key)
        if state is None:
            return None
        if state.lease.descriptor.session_id != provider_session_key.provider_session_id:
            return None
        return ContractPointer(
            contractId=state.lease.contract.contract_id,
            generation=state.lease.contract.generation,
        )

    async def send_runtime_message(
        self,
        provider_session_key: ProviderSessionKey,
        message_type: str,
        body: ActionMessageBody,
    ) -> bool:
        services = self._services
        state = self._runtime_leases.get(provider_session_key)
        if services is None or state is None:
            return False
        name = action_runtime_message_name(message_type)
        params, event = action_runtime_payload(name, body)
        try:
            await services.send(state.lease, name, params=params, event=event)
        except ServiceUnavailable as exc:
            if service_unavailable_ends_service_use(exc):
                self._drop_runtime_lease(provider_session_key, state)
                self._restart_service_watch_after_lease_loss(state)
                changed = self.mark_provider_session_unavailable(
                    provider_session_key,
                    reason=SERVICE_VIEW_UNAVAILABLE_REASON,
                )
                if changed:
                    await self._notify_availability_changed(changed)
            logger.warning(
                "Could not send action runtime message provider=%s provider_id=%s "
                "session=%s name=%s code=%s message=%s diagnostics=%s",
                provider_session_key.provider_instance_id,
                provider_session_key.provider_id,
                provider_session_key.provider_session_id,
                name,
                exc.code,
                exc.message,
                exc.diagnostics,
            )
            return False
        return True

    async def decode_inbound_runtime_message(
        self,
        deckr_message: DeckrMessage,
    ) -> DeckrMessage | None:
        services = self._services
        if services is None:
            return None
        if deckr_message.message_type != "serviceMessage":
            return None
        try:
            body = service_body(deckr_message)
        except (TypeError, ValueError):
            logger.warning("Ignoring invalid Action Runtime service message", exc_info=True)
            return None
        if body.service_namespace != ACTION_RUNTIME_SERVICE_NAMESPACE:
            return None
        if body.name not in RUNTIME_TO_CONTROLLER_MESSAGES:
            return None
        provider_instance_id = _runtime_provider_instance_id_from_message(deckr_message)
        if provider_instance_id is None:
            logger.warning(
                "Ignoring Action Runtime service message %s from non-runtime sender %s",
                body.name,
                deckr_message.sender,
            )
            return None

        states = self._runtime_lease_states_for_inbound(
            provider_instance_id=provider_instance_id,
            provider_session_id=deckr_message.sender_session_id,
        )
        for state in states:
            try:
                authorized = await _authorize_inbound_action_runtime_message(
                    services,
                    state,
                    deckr_message,
                    body,
                )
            except ServiceUnavailable as exc:
                if service_unavailable_ends_service_use(exc):
                    self._drop_runtime_lease(state.key, state)
                    self._restart_service_watch_after_lease_loss(state)
                    changed = self.mark_provider_session_unavailable(
                        state.key,
                        reason=SERVICE_VIEW_UNAVAILABLE_REASON,
                    )
                    if changed:
                        await self._notify_availability_changed(changed)
                logger.warning(
                    "Ignoring unauthorized Action Runtime service message "
                    "provider=%s provider_id=%s session=%s name=%s code=%s "
                    "message=%s diagnostics=%s",
                    state.key.provider_instance_id,
                    state.key.provider_id,
                    state.key.provider_session_id,
                    body.name,
                    exc.code,
                    exc.message,
                    exc.diagnostics,
                )
                continue
            try:
                legacy_type = legacy_action_message_type(authorized.name)
                legacy_body = action_runtime_body_from_service_message(
                    authorized
                ).to_dict()
            except (TypeError, ValueError):
                logger.warning(
                    "Ignoring invalid Action Runtime payload name=%s",
                    authorized.name,
                    exc_info=True,
                )
                return None
            return deckr_message.model_copy(
                update={
                    "message_type": legacy_type,
                    "body": legacy_body,
                }
            )
        logger.warning(
            "Ignoring Action Runtime service message without active provider-session "
            "lease provider=%s session=%s name=%s",
            provider_instance_id,
            deckr_message.sender_session_id,
            body.name,
        )
        return None

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
        existing = self._cache.record_for(key)
        metadata = existing.metadata if existing is not None else None
        if metadata is None:
            metadata = ActionMetadata(
                uuid=action_uuid,
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

    def update_config_interest(
        self,
        config_id: str,
        snapshot: ActionInterestSnapshot,
    ) -> None:
        self._interest_by_config[config_id] = snapshot

    def clear_config_interest(self, config_id: str) -> None:
        self._interest_by_config.pop(config_id, None)

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
                intent.action_uuid,
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
        self._stopping = stopping
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

        for service_id, descriptor in sorted(active.items()):
            descriptor_key = _service_descriptor_key(descriptor)
            if self._service_descriptor_keys.get(service_id) == descriptor_key:
                continue
            self._service_descriptor_keys[service_id] = descriptor_key
            if descriptor.backend_status == ServiceBackendStatus.UNAVAILABLE:
                continue
            if self._start_soon is None:
                continue
            self._start_service_watch(service_id, descriptor, stopping=stopping)

    def _start_service_watch(
        self,
        service_id: str,
        descriptor: ServiceDescriptor,
        *,
        stopping: anyio.Event,
    ) -> None:
        previous = self._service_watch_scopes.pop(service_id, None)
        if previous is not None:
            previous.cancel()
        generation = self._service_watch_generations.get(service_id, 0) + 1
        self._service_watch_generations[service_id] = generation
        self._start_soon(
            self._run_service_view_watch,
            service_id,
            descriptor,
            generation,
            stopping,
        )

    async def _run_service_view_watch(
        self,
        service_id: str,
        descriptor: ServiceDescriptor,
        generation: int,
        stopping: anyio.Event,
    ) -> None:
        services = self._services
        if services is None:
            return
        provider_instance_id = action_runtime_provider_instance_id(service_id)
        with anyio.CancelScope() as scope:
            if self._service_watch_generations.get(service_id) == generation:
                self._service_watch_scopes[service_id] = scope
            try:
                view_ref = action_availability_view_ref(service_id)
                while not stopping.is_set():
                    lease: ServiceUseLease | None = None
                    try:
                        async with services.use(descriptor) as lease:
                            descriptor_provider_id = _provider_id_from_descriptor(
                                descriptor
                            )
                            if (
                                provider_instance_id is not None
                                and descriptor_provider_id is not None
                            ):
                                self._remember_runtime_lease(
                                    ProviderSessionKey(
                                        provider_instance_id,
                                        descriptor_provider_id,
                                        descriptor.session_id,
                                    ),
                                    lease=lease,
                                    service_id=service_id,
                                    generation=generation,
                                )
                            while not stopping.is_set():
                                try:
                                    async for payload in services.watch_view(
                                        lease,
                                        view_ref,
                                    ):
                                        if stopping.is_set():
                                            return
                                        changed = await self._handle_view_payload(
                                            payload,
                                            lease=lease,
                                            service_id=service_id,
                                            provider_instance_id=provider_instance_id,
                                            generation=generation,
                                        )
                                        if changed:
                                            await self._notify_availability_changed(
                                                changed
                                            )
                                except ServiceUnavailable as exc:
                                    if service_unavailable_ends_service_use(exc):
                                        raise
                                    logger.warning(
                                        "Action availability service view unavailable "
                                        "service=%s code=%s message=%s diagnostics=%s",
                                        service_id,
                                        exc.code,
                                        exc.message,
                                        exc.diagnostics,
                                    )
                                    await anyio.sleep(_SERVICE_WATCH_RETRY_SECONDS)
                                else:
                                    break
                    except ServiceUnavailable as exc:
                        if service_unavailable_ends_service_use(exc):
                            changed = self._mark_generation_unavailable(
                                service_id=service_id,
                                generation=generation,
                                provider_instance_id=provider_instance_id,
                                reason=SERVICE_VIEW_UNAVAILABLE_REASON,
                            )
                            if changed:
                                await self._notify_availability_changed(changed)
                            await anyio.sleep(_SERVICE_WATCH_RETRY_SECONDS)
                            continue
                        logger.warning(
                            "Action availability service use unavailable "
                            "service=%s code=%s message=%s diagnostics=%s",
                            service_id,
                            exc.code,
                            exc.message,
                            exc.diagnostics,
                        )
                        await anyio.sleep(_SERVICE_WATCH_RETRY_SECONDS)
                    finally:
                        if lease is not None:
                            self._drop_runtime_lease_for_object(
                                lease,
                                service_id=service_id,
                                generation=generation,
                            )
            finally:
                if self._service_watch_scopes.get(service_id) is scope:
                    self._service_watch_scopes.pop(service_id, None)

    async def _handle_view_payload(
        self,
        payload: Mapping[str, object] | None,
        *,
        lease: ServiceUseLease,
        service_id: str,
        provider_instance_id: str | None,
        generation: int,
    ) -> frozenset[ProviderActionKey]:
        if payload is None:
            return (
                self.mark_provider_service_unavailable(
                    provider_instance_id,
                    reason=SERVICE_VIEW_MISSING_REASON,
                )
                if provider_instance_id is not None
                else frozenset()
            )
        view = ActionRuntimeAvailabilityViewPayload.model_validate(payload)
        if provider_instance_id is not None and (
            view.provider_instance_id != provider_instance_id
        ):
            logger.warning(
                "Ignoring action availability view with mismatched service provider "
                "service=%s provider=%s payload_provider=%s",
                service_id,
                provider_instance_id,
                view.provider_instance_id,
            )
            return frozenset()
        descriptor_provider_id = _provider_id_from_descriptor(lease.descriptor)
        if descriptor_provider_id is not None and (
            view.provider_id != descriptor_provider_id
        ):
            logger.warning(
                "Ignoring action availability view with mismatched provider id "
                "service=%s descriptor_provider_id=%s payload_provider_id=%s",
                service_id,
                descriptor_provider_id,
                view.provider_id,
            )
            return frozenset()
        session_key = ProviderSessionKey(
            view.provider_instance_id,
            view.provider_id,
            view.service_session_id,
        )
        if view.service_session_id != lease.descriptor.session_id:
            logger.warning(
                "Ignoring action availability view with stale service session "
                "service=%s descriptor_session=%s payload_session=%s",
                service_id,
                lease.descriptor.session_id,
                view.service_session_id,
            )
            return frozenset()
        self._remember_runtime_lease(
            session_key,
            lease=lease,
            service_id=service_id,
            generation=generation,
        )
        return self.ingest_service_view_payload(view, service_id=service_id)

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
            if record.key.action_uuid in seen_action_ids:
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

    def _remember_runtime_lease(
        self,
        key: ProviderSessionKey,
        *,
        lease: ServiceUseLease,
        service_id: str,
        generation: int,
    ) -> None:
        self._runtime_leases[key] = _RuntimeLeaseState(
            key=key,
            lease=lease,
            service_id=service_id,
            generation=generation,
        )

    def _drop_runtime_lease(
        self,
        key: ProviderSessionKey,
        state: _RuntimeLeaseState,
    ) -> None:
        if self._runtime_leases.get(key) is state:
            self._runtime_leases.pop(key, None)

    def _restart_service_watch_after_lease_loss(
        self,
        state: _RuntimeLeaseState,
    ) -> None:
        stopping = self._stopping
        if self._start_soon is None or stopping is None or stopping.is_set():
            return
        descriptor = state.lease.descriptor
        if descriptor.backend_status == ServiceBackendStatus.UNAVAILABLE:
            return
        self._start_service_watch(state.service_id, descriptor, stopping=stopping)

    def _drop_runtime_lease_for_object(
        self,
        lease: ServiceUseLease,
        *,
        service_id: str,
        generation: int,
    ) -> None:
        for key, state in tuple(self._runtime_leases.items()):
            if (
                state.lease is lease
                and state.service_id == service_id
                and state.generation == generation
            ):
                self._runtime_leases.pop(key, None)

    def _mark_generation_unavailable(
        self,
        *,
        service_id: str,
        generation: int,
        provider_instance_id: str | None,
        reason: str,
    ) -> frozenset[ProviderActionKey]:
        changed: set[ProviderActionKey] = set()
        states = [
            state
            for state in self._runtime_leases.values()
            if state.service_id == service_id and state.generation == generation
        ]
        for state in states:
            changed.update(
                self.mark_provider_session_unavailable(
                    state.key,
                    reason=reason,
                )
            )
        if not states and provider_instance_id is not None:
            changed.update(
                self.mark_provider_service_unavailable(
                    provider_instance_id,
                    reason=reason,
                )
            )
        return frozenset(changed)

    def _runtime_lease_states_for_inbound(
        self,
        *,
        provider_instance_id: str,
        provider_session_id: str,
    ) -> tuple[_RuntimeLeaseState, ...]:
        return tuple(
            state
            for key, state in self._runtime_leases.items()
            if key.provider_instance_id == provider_instance_id
            and key.provider_session_id == provider_session_id
        )

    def _ready_provider_session_keys(self) -> frozenset[ProviderSessionKey] | None:
        if self._services is None:
            return None
        ready: set[ProviderSessionKey] = set()
        for record in self._cache.service_view_records():
            metadata = record.metadata
            if not _metadata_requires_provider_session_revalidation(metadata):
                continue
            key = provider_session_key(metadata)
            if key is not None and key in self._runtime_leases:
                ready.add(key)
        return frozenset(ready)

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
        existing = self._cache.record_for(key)
        existing_metadata = existing.metadata if existing is not None else None
        if descriptor is None and existing_metadata is None:
            return None
        return ActionMetadata(
            uuid=key.action_uuid,
            provider_instance_id=key.provider_instance_id,
            provider_id=provider_id,
            name=descriptor.name if descriptor is not None else (
                existing_metadata.name if existing_metadata is not None else None
            ),
            provider_session_id=provider_session_id,
            provider_labels=(
                existing_metadata.provider_labels
                if existing_metadata is not None
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
        return self._cache.intent_for_key(key)

    def _now(self, now: float | None) -> float:
        return self._clock() if now is None else now


def _runtime_provider_instance_id_from_message(message: DeckrMessage) -> str | None:
    service_id = parse_service_address(message.sender)
    if service_id is None:
        return None
    return action_runtime_provider_instance_id(service_id)


async def _authorize_inbound_action_runtime_message(
    services: DeckrServices,
    state: _RuntimeLeaseState,
    message: DeckrMessage,
    body: ServiceMessageBody,
) -> ServiceMessageBody:
    try:
        return await services.authorize_inbound_message(
            state.lease,
            message,
            name=body.name,
        )
    except ServiceUnavailable as exc:
        if exc.code != "invalid_service_response" or message.subject.kind != "context":
            raise

    # Runtime commands keep their action context subject for downstream routing;
    # validate the service-use envelope by temporarily applying its service subject.
    descriptor = state.lease.descriptor
    service_scoped_message = message.model_copy(
        update={
            "subject": entity_subject(
                "service",
                serviceId=descriptor.service_id,
                namespace=descriptor.namespace,
                name=body.name,
            )
        }
    )
    return await services.authorize_inbound_message(
        state.lease,
        service_scoped_message,
        name=body.name,
    )


def _provider_id_from_descriptor(descriptor: ServiceDescriptor) -> str | None:
    value = descriptor.diagnostics.get("providerId")
    return value if isinstance(value, str) and value else None


def _service_descriptor_key(descriptor: ServiceDescriptor) -> tuple[str, str, str]:
    return (
        str(descriptor.endpoint),
        descriptor.session_id,
        descriptor.backend_status.value,
    )
