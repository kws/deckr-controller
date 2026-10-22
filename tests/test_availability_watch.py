from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import anyio
import pytest
from deckr.action_runtime import (
    action_runtime_service_id,
)
from deckr.actions.messages import (
    ACTION_INSTANCE_DESTROYED,
    ActionAvailabilityEntry,
    ActionDescriptor,
    ActionInstanceLifecycleBody,
    ActionInstanceMetadata,
)
from deckr.services import ServiceUnavailable

from deckr.controller._actions import (
    ActionAvailabilityState,
    ControllerActionService,
    ProviderActionKey,
    ProviderSessionKey,
)
from test_support.availability import (
    _availability_view,
    _metadata,
    _record_service_available,
    _service_descriptor,
    _ServiceViewWatchServices,
)

CONTROLLER_ID = "controller-main"

CONTROLLER_SESSION_ID = "controller-session"

PROVIDER_SESSION_ID = "provider-session"


@pytest.mark.asyncio
async def test_runtime_send_lease_loss_restarts_service_watch():
    descriptor = _service_descriptor(
        "provider-alpha",
        session_id="provider-session",
        updated_at=datetime(2026, 1, 2, tzinfo=UTC),
        advertisement_id="availability",
    )
    session_key = ProviderSessionKey(
        "provider-alpha",
        "provider.test",
        "provider-session",
    )
    services = _ServiceViewWatchServices(initial=(descriptor,))
    services.send_error = ServiceUnavailable(
        "contract_cancelled",
        "service-use contract cancelled",
    )
    service = ControllerActionService(
        controller_id=CONTROLLER_ID,
        controller_session_id=CONTROLLER_SESSION_ID,
        manager=MagicMock(),
        services=services,
    )

    async with anyio.create_task_group() as tg:
        stopping = anyio.Event()
        service.supervisor._start_soon = tg.start_soon
        await service.start(tg, stopping)
        await services.wait_use_count(1)

        sent = await service.send_runtime_message(
            session_key,
            ACTION_INSTANCE_DESTROYED,
            ActionInstanceLifecycleBody(
                metadata=ActionInstanceMetadata(
                    providerInstanceId="provider-alpha",
                    providerId="provider.test",
                    actionId="action.alpha",
                    actionInstanceId="action-instance",
                    configId="config",
                    contextId="context",
                ),
                reason="test",
            ),
            contract=service.current_contract(session_key),
        )

        assert sent is False
        assert services.send_calls
        assert service.current_contract(session_key) is None
        await services.wait_use_count(2)
        stopping.set()
        await service.aclose()
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_service_view_watch_missing_view_keeps_service_use_lease_open(
    monkeypatch,
):
    monkeypatch.setattr(
        "deckr.controller._actions._supervisor._SERVICE_WATCH_RETRY_SECONDS",
        0.01,
    )
    services = _ServiceViewWatchServices()
    notify_send, notify_receive = anyio.create_memory_object_stream(10)
    service = ControllerActionService(
        controller_id=CONTROLLER_ID,
        controller_session_id=CONTROLLER_SESSION_ID,
        manager=MagicMock(),
        services=services,
        start_soon=None,
        on_availability_changed=notify_send.send,
    )
    _record_service_available(
        service,
        _metadata(
            "action.alpha",
            provider_instance_id="provider-alpha",
            provider_id="provider.test",
        ),
        now=0.0,
    )
    service_id = action_runtime_service_id("provider-alpha")
    descriptor = _service_descriptor(
        "provider-alpha",
        session_id="provider-session",
        updated_at=datetime(2026, 1, 2, tzinfo=UTC),
        advertisement_id="availability",
    )
    stopping = anyio.Event()

    async with anyio.create_task_group() as tg:
        services._initial = (descriptor,)
        service.supervisor._start_soon = tg.start_soon
        service.supervisor._start_service_watch(service_id, stopping=stopping)
        with anyio.fail_after(1):
            await services.entered.wait()

        await services.send(None)
        with anyio.fail_after(1):
            changed = await notify_receive.receive()
        await anyio.sleep(0.05)

        assert changed == frozenset(
            {ProviderActionKey("provider-alpha", "action.alpha")}
        )
        assert services.use_count == 1
        assert services.closed_count == 0
        assert services.lease_open is True
        record = service.record_for_key(
            ProviderActionKey("provider-alpha", "action.alpha")
        )
        assert record is not None
        assert record.state == ActionAvailabilityState.UNAVAILABLE
        assert record.reason == "action_availability_view_missing"

        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_service_view_watch_recovers_from_missing_view_on_same_lease(
    monkeypatch,
):
    monkeypatch.setattr(
        "deckr.controller._actions._supervisor._SERVICE_WATCH_RETRY_SECONDS",
        0.01,
    )
    services = _ServiceViewWatchServices()
    notify_send, notify_receive = anyio.create_memory_object_stream(10)
    service = ControllerActionService(
        controller_id=CONTROLLER_ID,
        controller_session_id=CONTROLLER_SESSION_ID,
        manager=MagicMock(),
        services=services,
        start_soon=None,
        on_availability_changed=notify_send.send,
    )
    key = ProviderActionKey("provider-alpha", "action.alpha")
    _record_service_available(
        service,
        _metadata(
            "action.alpha",
            provider_instance_id="provider-alpha",
            provider_id="provider.test",
        ),
        now=0.0,
    )
    service_id = action_runtime_service_id("provider-alpha")
    descriptor = _service_descriptor(
        "provider-alpha",
        session_id="provider-session",
        updated_at=datetime(2026, 1, 2, tzinfo=UTC),
        advertisement_id="availability",
    )
    payload = _availability_view(
        "provider-alpha",
        entries=(
            ActionAvailabilityEntry(
                actionId="action.alpha",
                status="available",
                descriptor=ActionDescriptor(actionId="action.alpha", name="Alpha"),
            ),
        ),
    )
    stopping = anyio.Event()

    async with anyio.create_task_group() as tg:
        services._initial = (descriptor,)
        service.supervisor._start_soon = tg.start_soon
        service.supervisor._start_service_watch(service_id, stopping=stopping)
        with anyio.fail_after(1):
            await services.entered.wait()

        await services.send(None)
        with anyio.fail_after(1):
            assert await notify_receive.receive() == frozenset({key})
        await anyio.sleep(0.05)
        await services.send(payload.to_dict())
        with anyio.fail_after(1):
            assert await notify_receive.receive() == frozenset({key})

        assert services.use_count == 1
        assert services.closed_count == 0
        assert services.lease_open is True
        assert len(services.watch_calls) == 1
        assert services.watch_calls[0][0] is services.leases[0]
        record = service.record_for_key(key)
        assert record is not None
        assert record.state == ActionAvailabilityState.AVAILABLE
        assert record.reason is None
        assert record.metadata is not None
        assert record.metadata.name == "Alpha"

        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_service_view_watch_unavailable_fails_closed_and_negotiates_successor(
    monkeypatch,
):
    monkeypatch.setattr(
        "deckr.controller._actions._supervisor._SERVICE_WATCH_RETRY_SECONDS",
        0.01,
    )
    services = _ServiceViewWatchServices()
    notify_send, notify_receive = anyio.create_memory_object_stream(10)
    service = ControllerActionService(
        controller_id=CONTROLLER_ID,
        controller_session_id=CONTROLLER_SESSION_ID,
        manager=MagicMock(),
        services=services,
        start_soon=None,
        on_availability_changed=notify_send.send,
    )
    key = ProviderActionKey("provider-alpha", "action.alpha")
    _record_service_available(
        service,
        _metadata(
            "action.alpha",
            provider_instance_id="provider-alpha",
            provider_id="provider.test",
        ),
        now=0.0,
    )
    service_id = action_runtime_service_id("provider-alpha")
    descriptor = _service_descriptor(
        "provider-alpha",
        session_id="provider-session",
        updated_at=datetime(2026, 1, 2, tzinfo=UTC),
        advertisement_id="availability",
    )
    payload = _availability_view(
        "provider-alpha",
        entries=(
            ActionAvailabilityEntry(
                actionId="action.alpha",
                status="available",
                descriptor=ActionDescriptor(actionId="action.alpha", name="Alpha"),
            ),
        ),
    )
    stopping = anyio.Event()

    async with anyio.create_task_group() as tg:
        services._initial = (descriptor,)
        service.supervisor._start_soon = tg.start_soon
        service.supervisor._start_service_watch(service_id, stopping=stopping)
        with anyio.fail_after(1):
            await services.entered.wait()

        await services.send(
            ServiceUnavailable(
                "contract_unavailable",
                "contract materialized view unavailable",
            )
        )
        await services.wait_use_count(2)
        with anyio.fail_after(1):
            assert await notify_receive.receive() == frozenset({key})
        assert services.use_count == 2
        assert services.closed_count == 1
        assert services.lease_open is True
        assert services.watch_calls[0][0] is services.leases[0]
        record = service.record_for_key(key)
        assert record is not None
        assert record.state == ActionAvailabilityState.UNAVAILABLE

        await services.send(payload.to_dict())
        with anyio.fail_after(1):
            assert await notify_receive.receive() == frozenset({key})
        assert services.use_count == 2
        assert services.closed_count == 1
        assert services.lease_open is True

        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_service_view_watch_rejects_mismatched_payload_provider(
    monkeypatch,
):
    monkeypatch.setattr(
        "deckr.controller._actions._supervisor._SERVICE_WATCH_RETRY_SECONDS",
        0.01,
    )
    services = _ServiceViewWatchServices()
    notify_send, notify_receive = anyio.create_memory_object_stream(10)
    service = ControllerActionService(
        controller_id=CONTROLLER_ID,
        controller_session_id=CONTROLLER_SESSION_ID,
        manager=MagicMock(),
        services=services,
        start_soon=None,
        on_availability_changed=notify_send.send,
    )
    service_id = action_runtime_service_id("provider-alpha")
    descriptor = _service_descriptor(
        "provider-alpha",
        session_id="provider-session",
        updated_at=datetime(2026, 1, 2, tzinfo=UTC),
        advertisement_id="availability",
    )
    stopping = anyio.Event()
    mismatched_key = ProviderSessionKey(
        "provider-beta",
        "provider.test",
        "provider-session",
    )
    payload = _availability_view(
        "provider-beta",
        entries=(
            ActionAvailabilityEntry(
                actionId="action.beta",
                status="available",
                descriptor=ActionDescriptor(actionId="action.beta", name="Beta"),
            ),
        ),
    )

    async with anyio.create_task_group() as tg:
        services._initial = (descriptor,)
        service.supervisor._start_soon = tg.start_soon
        service.supervisor._start_service_watch(service_id, stopping=stopping)
        with anyio.fail_after(1):
            await services.entered.wait()

        await services.send(payload.to_dict())
        await anyio.sleep(0.05)

        assert service.current_contract(mismatched_key) is None
        assert (
            service.record_for_key(ProviderActionKey("provider-beta", "action.beta"))
            is None
        )
        with anyio.move_on_after(0.05) as scope:
            await notify_receive.receive()
        assert scope.cancel_called

        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_disappeared_candidate_watch_is_disposed_only_after_contract_end():
    descriptor = _service_descriptor(
        "provider-alpha",
        session_id="provider-session",
        updated_at=datetime(2026, 1, 2, tzinfo=UTC),
        advertisement_id="retire",
    )
    services = _ServiceViewWatchServices(initial=(descriptor,))
    service = ControllerActionService(
        controller_id=CONTROLLER_ID,
        controller_session_id=CONTROLLER_SESSION_ID,
        manager=MagicMock(),
        services=services,
    )
    async with anyio.create_task_group() as tg:
        stopping = anyio.Event()
        await service.start(tg, stopping)
        await services.wait_use_count(1)
        await services.publish_directory(())
        with anyio.fail_after(1):
            while service.supervisor._discovered_services != set():
                await anyio.sleep(0)
        assert services.lease_open
        await services.send(ServiceUnavailable("contract_cancelled", "ended"))
        with anyio.fail_after(1):
            while service.supervisor._watches:
                await anyio.sleep(0)
        assert not services.lease_open
        assert not service.supervisor._runtime_leases
        assert services.use_count == 1
        stopping.set()
        await service.aclose()
        tg.cancel_scope.cancel()
