"""Controller-owned action runtime service coordination."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from deckr.services import DeckrServices

    from deckr.controller._actions._catalog import ActionCatalog


import logging
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from inspect import isawaitable

import anyio
from deckr.action_runtime import (
    ACTION_RUNTIME_SERVICE_PROTOCOL,
    ActionRuntimeAvailabilityViewPayload,
    action_availability_view_ref,
    action_runtime_provider_instance_id,
)
from deckr.services import (
    ManagedService,
    ServiceBackendStatus,
    ServiceDescriptor,
    ServiceUnavailable,
    service_unavailable_ends_service_use,
)

from deckr.controller._actions._availability_policy import (
    SERVICE_VIEW_MISSING_REASON,
    SERVICE_VIEW_UNAVAILABLE_REASON,
    _metadata_requires_provider_session_revalidation,
)
from deckr.controller._actions._models import (
    AvailabilityChangedCallback,
    ProviderActionKey,
    ProviderSessionKey,
    provider_session_key,
)

logger = logging.getLogger(__name__)

_SERVICE_WATCH_RETRY_SECONDS = 1.0


@dataclass(frozen=True, slots=True)
class _RuntimeLeaseState:
    key: ProviderSessionKey
    lease: ManagedService
    service_id: str
    generation: int


@dataclass(slots=True)
class ServiceWatch:
    generation: int
    scope: anyio.CancelScope | None = None


class ActionRuntimeSupervisor:
    """Managed service uses, availability watches and exact provider-session leases."""

    def __init__(
        self,
        *,
        services: DeckrServices | None,
        close_services_on_aclose: bool,
        start_soon: Callable[..., None] | None,
        catalog: ActionCatalog,
        on_availability_changed: AvailabilityChangedCallback | None,
    ):
        self._services = services
        self._close_services_on_aclose = close_services_on_aclose
        self._start_soon = start_soon
        self.catalog = catalog
        self._watches: dict[str, ServiceWatch] = {}
        self._watch_sequence = 0
        self._discovered_services: set[str] | None = None
        self._runtime_leases = {}
        self._on_availability_changed = on_availability_changed
        self._stopping = None

    async def start(
        self,
        tg: anyio.abc.TaskGroup,
        stopping: anyio.Event,
    ) -> None:
        self._stopping = stopping
        self._start_soon = tg.start_soon
        if self._services is not None:
            tg.start_soon(self._service_directory_loop, stopping)

    async def aclose(self) -> None:
        for watch in tuple(self._watches.values()):
            if watch.scope is not None:
                watch.scope.cancel()
        self._watches.clear()
        self._runtime_leases.clear()
        self._stopping = None
        if self._close_services_on_aclose and self._services is not None:
            await self._services.aclose()
            self._services = None

    def set_change_callback(
        self,
        callback: AvailabilityChangedCallback | None,
    ) -> None:
        self._on_availability_changed = callback

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
            candidates_by_service.setdefault(descriptor.service_id, []).append(
                descriptor
            )
        self._discovered_services = set(candidates_by_service)
        for service_id, candidates in sorted(candidates_by_service.items()):
            if service_id in self._watches:
                # The managed use owns its winner and recovery. Beacon changes
                # only affect the selection of a subsequent contract.
                continue
            if all(
                candidate.backend_status == ServiceBackendStatus.UNAVAILABLE
                for candidate in candidates
            ):
                continue
            if self._start_soon is None:
                continue
            self._start_service_watch(service_id, stopping=stopping)

    def _start_service_watch(
        self,
        service_id: str,
        *,
        stopping: anyio.Event,
    ) -> None:
        previous = self._watches.pop(service_id, None)
        if previous is not None and previous.scope is not None:
            previous.scope.cancel()
        self._watch_sequence += 1
        generation = self._watch_sequence
        self._watches[service_id] = ServiceWatch(generation)
        self._start_soon(
            self._run_service_view_watch,
            service_id,
            generation,
            stopping,
        )

    async def _run_service_view_watch(
        self,
        service_id: str,
        generation: int,
        stopping: anyio.Event,
    ) -> None:
        services = self._services
        if services is None:
            return
        provider_instance_id = action_runtime_provider_instance_id(service_id)
        with anyio.CancelScope() as scope:
            watch = self._watches.get(service_id)
            if watch is None or watch.generation != generation:
                return
            watch.scope = scope
            try:
                view_ref = action_availability_view_ref(service_id)
                while not stopping.is_set():
                    lease: ManagedService | None = None
                    try:
                        async with services.use_matching(
                            ACTION_RUNTIME_SERVICE_PROTOCOL,
                            predicate=lambda candidate: candidate.service_id
                            == service_id,
                            timeout_seconds=10.0,
                        ) as lease:
                            descriptor = lease.descriptor
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
                                    async for payload in lease.watch_view(view_ref):
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
                            if (
                                self._discovered_services is not None
                                and service_id not in self._discovered_services
                            ):
                                return
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
                    if (
                        self._discovered_services is not None
                        and service_id not in self._discovered_services
                    ):
                        return
            finally:
                if self._watches.get(service_id) is watch:
                    self._watches.pop(service_id, None)
                await self._notify_availability_changed(frozenset())

    async def _handle_view_payload(
        self,
        payload: Mapping[str, object] | None,
        *,
        lease: ManagedService,
        service_id: str,
        provider_instance_id: str | None,
        generation: int,
    ) -> frozenset[ProviderActionKey]:
        if payload is None:
            return (
                self.catalog.mark_provider_service_unavailable(
                    provider_instance_id,
                    reason=SERVICE_VIEW_MISSING_REASON,
                )
                if provider_instance_id is not None
                else frozenset()
            )
        try:
            view = ActionRuntimeAvailabilityViewPayload.model_validate(payload)
        except (ValueError, TypeError):
            logger.warning(
                "Ignoring invalid action availability view service=%s", service_id
            )
            return frozenset()
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
        return self.catalog.ingest_service_view_payload(view, service_id=service_id)

    def _remember_runtime_lease(
        self,
        key: ProviderSessionKey,
        *,
        lease: ManagedService,
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
        if (
            self._discovered_services is not None
            and state.service_id not in self._discovered_services
        ):
            watch = self._watches.pop(state.service_id, None)
            if watch is not None and watch.scope is not None:
                watch.scope.cancel()
            return
        self._start_service_watch(state.service_id, stopping=stopping)

    def _drop_runtime_lease_for_object(
        self,
        lease: ManagedService,
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
                self.catalog.mark_provider_session_unavailable(
                    state.key,
                    reason=reason,
                )
            )
        if not states and provider_instance_id is not None:
            changed.update(
                self.catalog.mark_provider_service_unavailable(
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
        for record in self.catalog.records():
            metadata = record.metadata
            if not _metadata_requires_provider_session_revalidation(metadata):
                continue
            key = provider_session_key(metadata)
            state = self._runtime_leases.get(key)
            if state is not None:
                try:
                    if (
                        state.lease.descriptor.session_id == key.provider_session_id
                        and state.lease.state.local_authority
                    ):
                        ready.add(key)
                except ServiceUnavailable:
                    pass
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


def _provider_id_from_descriptor(descriptor: ServiceDescriptor) -> str | None:
    value = descriptor.diagnostics.get("providerId")
    return value if isinstance(value, str) and value else None
