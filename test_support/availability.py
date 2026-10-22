from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace

import anyio
from deckr.action_runtime import (
    ACTION_RUNTIME_SERVICE_PROTOCOL,
    ActionRuntimeAvailabilityViewPayload,
    action_runtime_service_id,
)
from deckr.actions.messages import (
    ActionAvailabilityEntry,
    ActionDescriptor,
)
from deckr.contracts.authority import ContractPointer
from deckr.contracts.messages import service_address
from deckr.services import ServiceBackendStatus, ServiceDescriptor, ServiceUnavailable

from deckr.controller._actions import (
    ActionAvailabilityRecord,
    ActionAvailabilitySource,
    ActionAvailabilityState,
    ActionIntentKey,
    ActionMetadata,
    ControllerActionService,
    ProviderActionKey,
)

CONTROLLER_ID = "controller-main"


CONTROLLER_SESSION_ID = "controller-session"


PROVIDER_SESSION_ID = "provider-session"


def _metadata(
    action_id: str,
    *,
    provider_instance_id: str = "provider-a",
    provider_id: str = "provider",
    provider_labels: dict[str, str] | None = None,
    provider_session_id: str | None = PROVIDER_SESSION_ID,
) -> ActionMetadata:
    return ActionMetadata(
        action_id=action_id,
        provider_instance_id=provider_instance_id,
        provider_id=provider_id,
        provider_labels=provider_labels,
        provider_session_id=provider_session_id,
    )


def _intent(
    action_id: str,
    *,
    provider_instance_id: str | None = None,
    provider_labels: dict[str, str] | None = None,
) -> ActionIntentKey:
    return ActionIntentKey(
        action_id=action_id,
        provider_instance_id=provider_instance_id,
        provider_labels=tuple(sorted((provider_labels or {}).items())),
    )


def _unavailable_record(
    *,
    reason: str | None = None,
    metadata: ActionMetadata | None = None,
) -> ActionAvailabilityRecord:
    metadata = metadata or _metadata("action.alpha", provider_instance_id="provider-a")
    return ActionAvailabilityRecord(
        key=ProviderActionKey(metadata.provider_instance_id, metadata.action_id),
        state=ActionAvailabilityState.UNAVAILABLE,
        source=ActionAvailabilitySource.SERVICE_VIEW,
        updated_at=0.0,
        metadata=metadata,
        reason=reason,
    )


def _service_descriptor(
    provider_instance_id: str,
    *,
    session_id: str,
    updated_at: datetime,
    advertisement_id: str,
    refresh_seq: int = 1,
    backend_status: ServiceBackendStatus = ServiceBackendStatus.AVAILABLE,
) -> ServiceDescriptor:
    service_id = action_runtime_service_id(provider_instance_id)
    protocol = ACTION_RUNTIME_SERVICE_PROTOCOL
    return ServiceDescriptor(
        candidate=SimpleNamespace(
            key=f"advertisements.by_feature.test.{advertisement_id}",
            advertisement=SimpleNamespace(
                created_at=updated_at,
                updated_at=updated_at,
                refresh_seq=refresh_seq,
            ),
        ),
        service_id=service_id,
        namespace=protocol.namespace,
        endpoint=service_address(service_id),
        session_id=session_id,
        advertisement_profile=protocol.advertisement_profile,
        use_profile=protocol.use_profile,
        supported_operations=frozenset(protocol.operations),
        supported_messages=protocol.messages,
        views={},
        backend_status=backend_status,
        diagnostics={"providerId": "provider.test"},
    )


class _RuntimeLease:
    def __init__(
        self,
        descriptor: ServiceDescriptor,
        services,
        *,
        contract_id: str = "provider-session-contract",
        generation: int = 1,
    ) -> None:
        self.descriptor = descriptor
        self._services = services
        self._contract = ContractPointer(
            contractId=contract_id,
            generation=generation,
        )

    async def send(self, name, params=None, *, event=None) -> None:
        await self._services.send(self, name, params=params, event=event)

    def watch_view(self, view):
        return self._services.watch_view(self, view)

    async def authorize_inbound_message(self, message, *, name=None):
        return await self._services.authorize_inbound_message(
            self,
            message,
            name=name,
        )

    @property
    def state(self):
        return SimpleNamespace(contract=self._contract, local_authority=True)


def _availability_view(
    provider_instance_id: str,
    *,
    provider_id: str = "provider.test",
    service_session_id: str = PROVIDER_SESSION_ID,
    labels: dict[str, str] | None = None,
    entries: tuple[ActionAvailabilityEntry, ...] = (),
) -> ActionRuntimeAvailabilityViewPayload:
    service_id = action_runtime_service_id(provider_instance_id)
    return ActionRuntimeAvailabilityViewPayload(
        providerInstanceId=provider_instance_id,
        serviceId=service_id,
        serviceEndpoint=service_address(service_id),
        providerId=provider_id,
        serviceSessionId=service_session_id,
        labels=labels or {},
        entries=entries,
    )


def _record_service_available(
    service: ControllerActionService,
    metadata: ActionMetadata,
    *,
    now: float | None = None,
) -> frozenset[ProviderActionKey]:
    return service.ingest_provider_entries(
        provider_instance_id=metadata.provider_instance_id,
        provider_id=metadata.provider_id,
        provider_session_id=metadata.provider_session_id,
        provider_labels=metadata.provider_labels,
        entries=(
            ActionAvailabilityEntry(
                actionId=metadata.action_id,
                status="available",
                descriptor=ActionDescriptor(
                    actionId=metadata.action_id,
                    name=metadata.name,
                    settingsSchema=metadata.settings_schema,
                    providerSettingsSchema=metadata.provider_settings_schema,
                ),
            ),
        ),
        now=now,
    )


class _ServiceViewWatchServices:
    def __init__(self, initial: tuple[ServiceDescriptor, ...] = ()) -> None:
        self._directory_send, self._directory_receive = (
            anyio.create_memory_object_stream[tuple[ServiceDescriptor, ...]](10)
        )
        self._initial = initial
        self._send, self._receive = anyio.create_memory_object_stream(10)
        self.entered = anyio.Event()
        self.use_count = 0
        self.closed_count = 0
        self.lease_open = False
        self.leases = []
        self.watch_calls = []
        self.send_calls = []
        self.send_error: ServiceUnavailable | None = None
        self._use_count_events: dict[int, anyio.Event] = {}

    def directory(self, protocol):
        assert protocol == ACTION_RUNTIME_SERVICE_PROTOCOL
        return self

    async def publish_directory(
        self,
        descriptors: tuple[ServiceDescriptor, ...],
    ) -> None:
        self._initial = descriptors
        await self._directory_send.send(descriptors)

    async def watch_records(self):
        yield self._initial
        async with self._directory_receive:
            async for descriptors in self._directory_receive:
                yield descriptors

    async def send(self, *args, params=None, event=None) -> None:
        if len(args) == 1:
            await self._send.send(args[0])
            return
        lease, name = args
        self.send_calls.append((lease, name, params, event))
        if self.send_error is not None:
            raise self.send_error

    @asynccontextmanager
    async def use_matching(self, protocol, *, predicate, timeout_seconds):
        assert protocol == ACTION_RUNTIME_SERVICE_PROTOCOL
        assert timeout_seconds == 10.0
        descriptor = next(item for item in self._initial if predicate(item))
        async with self.use(descriptor) as lease:
            yield lease

    @asynccontextmanager
    async def use(self, descriptor):
        self.use_count += 1
        event = self._use_count_events.setdefault(self.use_count, anyio.Event())
        event.set()
        lease = _RuntimeLease(
            descriptor,
            self,
            contract_id=f"provider-session-contract-{self.use_count}",
        )
        self.leases.append(lease)
        self.lease_open = True
        self.entered.set()
        try:
            yield lease
        finally:
            self.lease_open = False
            self.closed_count += 1

    async def wait_use_count(self, count: int) -> None:
        if self.use_count >= count:
            return
        event = self._use_count_events.setdefault(count, anyio.Event())
        with anyio.fail_after(1):
            await event.wait()

    async def watch_view(self, lease, view):
        self.watch_calls.append((lease, view))
        while True:
            payload = await self._receive.receive()
            if isinstance(payload, BaseException):
                raise payload
            yield payload

    async def authorize_inbound_message(self, lease, message, *, name=None):
        del lease, message, name
        return True
