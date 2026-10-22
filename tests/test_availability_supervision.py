from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import anyio
import pytest
from deckr.action_runtime import (
    action_runtime_service_id,
)
from deckr.contracts.authority import ContractPointer
from deckr.services import ServiceUnavailable

from deckr.controller._actions import (
    SERVICE_VIEW_UNAVAILABLE_REASON,
    ActionAvailabilityState,
    ControllerActionService,
    ProviderActionKey,
    ProviderSessionKey,
)
from test_support.availability import (
    _metadata,
    _record_service_available,
    _service_descriptor,
    _ServiceViewWatchServices,
)

CONTROLLER_ID = "controller-main"

CONTROLLER_SESSION_ID = "controller-session"

PROVIDER_SESSION_ID = "provider-session"


@pytest.mark.asyncio
async def test_service_watchers_prefer_newest_duplicate_service_descriptor():
    service = ControllerActionService(
        controller_id=CONTROLLER_ID,
        controller_session_id=CONTROLLER_SESSION_ID,
        manager=MagicMock(),
    )
    newest = _service_descriptor(
        "provider-alpha",
        session_id="new-provider-session",
        updated_at=datetime(2026, 1, 2, tzinfo=UTC),
        advertisement_id="new",
    )
    stale = _service_descriptor(
        "provider-alpha",
        session_id="stale-provider-session",
        updated_at=datetime(2026, 1, 1, tzinfo=UTC),
        advertisement_id="stale",
    )
    services = _ServiceViewWatchServices(initial=(newest, stale))
    service.supervisor._services = services
    service.supervisor._start_soon = None

    async with anyio.create_task_group() as tg:
        stopping = anyio.Event()
        service.supervisor._start_soon = tg.start_soon
        await service.start(tg, stopping)
        await services.wait_use_count(1)

        assert services.leases[0].descriptor is newest
        stopping.set()
        await service.aclose()
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_descriptor_removal_preserves_active_runtime_lease():
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

        await services.publish_directory(())
        await anyio.sleep(0.05)

        assert services.lease_open is True
        assert service.current_contract(session_key) == ContractPointer(
            contractId="provider-session-contract-1",
            generation=1,
        )
        stopping.set()
        await service.aclose()
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_service_view_watch_terminal_unavailable_retries_descriptor(
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
    stopping = anyio.Event()

    async with anyio.create_task_group() as tg:
        services._initial = (descriptor,)
        service.supervisor._start_soon = tg.start_soon
        service.supervisor._start_service_watch(service_id, stopping=stopping)
        with anyio.fail_after(1):
            await services.entered.wait()

        await services.send(
            ServiceUnavailable(
                "contract_cancelled",
                "service-use contract cancelled",
            )
        )
        with anyio.fail_after(1):
            assert await notify_receive.receive() == frozenset({key})
            while services.closed_count < 1:
                await anyio.sleep(0.01)

        while services.use_count < 2 or len(services.watch_calls) < 2:
            await anyio.sleep(0.01)

        assert services.use_count == 2
        assert services.closed_count == 1
        assert services.lease_open is True
        assert services.watch_calls[0][0] is services.leases[0]
        assert services.watch_calls[1][0] is services.leases[1]
        assert len(services.watch_calls) == 2
        record = service.record_for_key(key)
        assert record is not None
        assert record.state == ActionAvailabilityState.UNAVAILABLE
        assert record.reason == SERVICE_VIEW_UNAVAILABLE_REASON

        tg.cancel_scope.cancel()
