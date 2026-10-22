from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import anyio
import pytest
from deckr.action_runtime import (
    action_runtime_service_id,
)
from deckr.actions.messages import (
    ActionAvailabilityEntry,
    ActionDescriptor,
)
from deckr.contracts.authority import ContractPointer

from deckr.controller._actions import (
    ControllerActionService,
    ProviderActionKey,
    ProviderSessionKey,
)
from test_support.availability import (
    _availability_view,
    _intent,
    _metadata,
    _record_service_available,
    _service_descriptor,
    _ServiceViewWatchServices,
)

CONTROLLER_ID = "controller-main"


CONTROLLER_SESSION_ID = "controller-session"


PROVIDER_SESSION_ID = "provider-session"


def test_service_view_available_without_runtime_lease_is_pending():
    metadata = _metadata(
        "action.alpha",
        provider_instance_id="provider-alpha",
        provider_id="provider.test",
        provider_session_id="negotiating-session",
    )
    session_key = ProviderSessionKey(
        "provider-alpha",
        "provider.test",
        "negotiating-session",
    )
    intent = _intent("action.alpha", provider_instance_id="provider-alpha")
    service = ControllerActionService(
        controller_id=CONTROLLER_ID,
        controller_session_id=CONTROLLER_SESSION_ID,
        manager=MagicMock(),
        services=MagicMock(),
        start_soon=None,
    )
    _record_service_available(service, metadata, now=0.0)

    snapshot = service.planning_snapshot((intent,), now=1.0)

    assert snapshot.metadata == {}
    assert snapshot.pending == frozenset({intent})
    assert snapshot.unavailable == frozenset()
    assert service.current_contract(session_key) is None


@pytest.mark.asyncio
async def test_matching_runtime_lease_unlocks_planning_and_current_contract():
    metadata = _metadata(
        "action.alpha",
        provider_instance_id="provider-alpha",
        provider_id="provider.test",
        provider_session_id="negotiating-session",
    )
    session_key = ProviderSessionKey(
        "provider-alpha",
        "provider.test",
        "negotiating-session",
    )
    intent = _intent("action.alpha", provider_instance_id="provider-alpha")
    descriptor = _service_descriptor(
        "provider-alpha",
        session_id="negotiating-session",
        updated_at=datetime(2026, 1, 2, tzinfo=UTC),
        advertisement_id="availability",
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
        _record_service_available(service, metadata, now=0.0)

        planning = service.planning_snapshot((intent,))

        assert planning.metadata[intent].action_id == metadata.action_id
        assert (
            planning.metadata[intent].provider_instance_id
            == metadata.provider_instance_id
        )
        assert planning.metadata[intent].provider_id == metadata.provider_id
        assert (
            planning.metadata[intent].provider_session_id
            == metadata.provider_session_id
        )
        assert planning.pending == frozenset()
        assert service.current_contract(session_key) == ContractPointer(
            contractId="provider-session-contract-1",
            generation=1,
        )
        stopping.set()
        await service.aclose()
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_runtime_lease_session_mismatch_keeps_planning_pending():
    metadata = _metadata(
        "action.alpha",
        provider_instance_id="provider-alpha",
        provider_id="provider.test",
        provider_session_id="negotiating-session",
    )
    session_key = ProviderSessionKey(
        "provider-alpha",
        "provider.test",
        "negotiating-session",
    )
    intent = _intent("action.alpha", provider_instance_id="provider-alpha")
    descriptor = _service_descriptor(
        "provider-alpha",
        session_id="different-session",
        updated_at=datetime(2026, 1, 2, tzinfo=UTC),
        advertisement_id="availability",
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
        _record_service_available(service, metadata, now=0.0)

        planning = service.planning_snapshot((intent,), now=1.0)

        assert planning.metadata == {}
        assert planning.pending == frozenset({intent})
        assert service.current_contract(session_key) is None
        stopping.set()
        await service.aclose()
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_service_view_ingest_records_runtime_service_session():
    key = ProviderActionKey("provider-alpha", "action.alpha")
    service = ControllerActionService(
        controller_id=CONTROLLER_ID,
        controller_session_id=CONTROLLER_SESSION_ID,
        manager=MagicMock(),
        start_soon=None,
    )
    view = _availability_view(
        "provider-alpha",
        service_session_id="negotiating-session",
        entries=(
            ActionAvailabilityEntry(
                actionId="action.alpha",
                status="available",
                descriptor=ActionDescriptor(actionId="action.alpha"),
            ),
        ),
    )

    changed = service.ingest_service_view_payload(
        view,
        service_id=action_runtime_service_id("provider-alpha"),
    )

    assert changed == frozenset({key})
    record = service.record_for_key(key)
    assert record is not None
    assert record.metadata is not None
    assert record.metadata.provider_session_id == "negotiating-session"
