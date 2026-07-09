from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import anyio
import pytest
from deckr.action_runtime import (
    ACTION_RUNTIME_SERVICE_PROTOCOL,
    ActionRuntimeAvailabilityViewPayload,
    action_runtime_service_id,
)
from deckr.actions.messages import ActionAvailabilityEntry, ActionDescriptor
from deckr.contracts.authority import ContractPointer
from deckr.contracts.messages import service_address
from deckr.services import ServiceBackendStatus, ServiceDescriptor, ServiceUnavailable

from deckr.controller._actions import (
    ControllerActionService,
    ProviderSessionKey,
)
from deckr.controller._binding_planner import ActionIntentKey

CONTROLLER_ID = "controller-main"
CONTROLLER_SESSION_ID = "controller-session"
PROVIDER_INSTANCE_ID = "python-dev.deckr.clock"
PROVIDER_ID = "dev.deckr.clock"
ACTION_UUID = "dev.deckr.clock.action.time"


def _service_descriptor(
    *,
    provider_session_id: str,
    advertisement_id: str,
    refresh_seq: int,
) -> ServiceDescriptor:
    service_id = action_runtime_service_id(PROVIDER_INSTANCE_ID)
    protocol = ACTION_RUNTIME_SERVICE_PROTOCOL
    updated_at = datetime(2026, 1, refresh_seq, tzinfo=UTC)
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
        session_id=provider_session_id,
        advertisement_profile=protocol.advertisement_profile,
        use_profile=protocol.use_profile,
        supported_operations=frozenset(protocol.operations),
        supported_messages=protocol.messages,
        views={},
        backend_status=ServiceBackendStatus.AVAILABLE,
        diagnostics={"providerId": PROVIDER_ID},
    )


def _availability_view(
    *,
    provider_session_id: str,
) -> ActionRuntimeAvailabilityViewPayload:
    service_id = action_runtime_service_id(PROVIDER_INSTANCE_ID)
    return ActionRuntimeAvailabilityViewPayload(
        providerInstanceId=PROVIDER_INSTANCE_ID,
        serviceId=service_id,
        serviceEndpoint=service_address(service_id),
        providerId=PROVIDER_ID,
        serviceSessionId=provider_session_id,
        labels={},
        entries=(
            ActionAvailabilityEntry(
                actionId=ACTION_UUID,
                status="available",
                descriptor=ActionDescriptor(
                    actionId=ACTION_UUID,
                    name="Clock",
                ),
            ),
        ),
    )


class _RuntimeLease:
    def __init__(self, descriptor: ServiceDescriptor, generation: int) -> None:
        self.descriptor = descriptor
        self.contract = SimpleNamespace(
            contract_id=f"service-use:{descriptor.session_id}",
            generation=generation,
        )


class _DirectoryHarness:
    def __init__(self, initial: tuple[ServiceDescriptor, ...]) -> None:
        self._initial = initial
        self._send, self._receive = anyio.create_memory_object_stream[
            tuple[ServiceDescriptor, ...]
        ](10)

    async def publish(self, descriptors: tuple[ServiceDescriptor, ...]) -> None:
        await self._send.send(descriptors)

    async def watch_records(self):
        yield self._initial
        async with self._receive:
            async for descriptors in self._receive:
                yield descriptors


class _ActionRuntimeServicesHarness:
    def __init__(self, initial: tuple[ServiceDescriptor, ...]) -> None:
        self.directory_harness = _DirectoryHarness(initial)
        self._view_send, self._view_receive = anyio.create_memory_object_stream(10)
        self._use_events: dict[str, anyio.Event] = {}
        self._use_generation = 0

    def directory(self, protocol):
        assert protocol == ACTION_RUNTIME_SERVICE_PROTOCOL
        return self.directory_harness

    async def publish_directory(
        self,
        descriptors: tuple[ServiceDescriptor, ...],
    ) -> None:
        await self.directory_harness.publish(descriptors)

    async def publish_view(
        self,
        payload: ActionRuntimeAvailabilityViewPayload | BaseException,
    ) -> None:
        await self._view_send.send(
            payload if isinstance(payload, BaseException) else payload.to_dict()
        )

    async def wait_used(self, provider_session_id: str) -> None:
        event = self._use_events.setdefault(provider_session_id, anyio.Event())
        with anyio.fail_after(1):
            await event.wait()

    @asynccontextmanager
    async def use(self, descriptor: ServiceDescriptor):
        self._use_generation += 1
        event = self._use_events.setdefault(descriptor.session_id, anyio.Event())
        event.set()
        yield _RuntimeLease(descriptor, self._use_generation)

    async def watch_view(self, lease, view_ref):
        del lease, view_ref
        while True:
            payload = await self._view_receive.receive()
            if isinstance(payload, BaseException):
                raise payload
            yield payload


async def _wait_for_registered_session(
    service: ControllerActionService,
    intent: ActionIntentKey,
    *,
    provider_session_id: str,
):
    with anyio.fail_after(1):
        while True:
            snapshot = service.planning_snapshot((intent,))
            metadata = snapshot.metadata.get(intent)
            if metadata is not None and (
                metadata.provider_session_id == provider_session_id
            ):
                return metadata
            await anyio.sleep(0.01)


async def _wait_for_unavailable(
    service: ControllerActionService,
    intent: ActionIntentKey,
) -> None:
    with anyio.fail_after(1):
        while True:
            snapshot = service.planning_snapshot((intent,))
            if intent in snapshot.unavailable:
                return
            await anyio.sleep(0.01)


async def _wait_for_no_contract(
    service: ControllerActionService,
    key: ProviderSessionKey,
) -> None:
    with anyio.fail_after(1):
        while True:
            if service.current_contract(key) is None:
                return
            await anyio.sleep(0.01)


@pytest.mark.asyncio
async def test_controller_action_service_registers_stops_and_restarts_runtime_actions():
    first_descriptor = _service_descriptor(
        provider_session_id="provider-session-1",
        advertisement_id="first",
        refresh_seq=1,
    )
    second_descriptor = _service_descriptor(
        provider_session_id="provider-session-2",
        advertisement_id="second",
        refresh_seq=2,
    )
    services = _ActionRuntimeServicesHarness(initial=(first_descriptor,))
    intent = ActionIntentKey(
        action_uuid=ACTION_UUID,
        provider_instance_id=PROVIDER_INSTANCE_ID,
        provider_labels=(),
    )
    first_key = ProviderSessionKey(
        PROVIDER_INSTANCE_ID,
        PROVIDER_ID,
        "provider-session-1",
    )
    second_key = ProviderSessionKey(
        PROVIDER_INSTANCE_ID,
        PROVIDER_ID,
        "provider-session-2",
    )
    stopping = anyio.Event()

    async with anyio.create_task_group() as tg:
        service = ControllerActionService(
            controller_id=CONTROLLER_ID,
            controller_session_id=CONTROLLER_SESSION_ID,
            manager=MagicMock(),
            services=services,
            start_soon=tg.start_soon,
        )
        await service.start(tg, stopping)

        await services.wait_used("provider-session-1")
        await services.publish_view(
            _availability_view(provider_session_id="provider-session-1")
        )
        first_metadata = await _wait_for_registered_session(
            service,
            intent,
            provider_session_id="provider-session-1",
        )

        assert first_metadata.name == "Clock"
        assert service.current_contract(first_key) == ContractPointer(
            contractId="service-use:provider-session-1",
            generation=1,
        )

        await services.publish_view(
            ServiceUnavailable(
                "contract_cancelled",
                "service-use contract cancelled",
            )
        )
        await _wait_for_unavailable(service, intent)
        await _wait_for_no_contract(service, first_key)

        await services.publish_directory((second_descriptor,))
        await services.wait_used("provider-session-2")
        await services.publish_view(
            _availability_view(provider_session_id="provider-session-2")
        )
        second_metadata = await _wait_for_registered_session(
            service,
            intent,
            provider_session_id="provider-session-2",
        )

        assert second_metadata.name == "Clock"
        assert service.current_contract(second_key) == ContractPointer(
            contractId="service-use:provider-session-2",
            generation=2,
        )

        stopping.set()
        await service.aclose()
        tg.cancel_scope.cancel()
