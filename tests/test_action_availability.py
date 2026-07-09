from __future__ import annotations

import logging
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
from deckr.actions.messages import (
    ACTION_INSTANCE_DESTROYED,
    ActionAvailabilityEntry,
    ActionDescriptor,
    ActionInstanceLifecycleBody,
    ActionInstanceMetadata,
)
from deckr.contracts.authority import ContractPointer
from deckr.contracts.messages import service_address
from deckr.services import ServiceBackendStatus, ServiceDescriptor, ServiceUnavailable

from deckr.controller._actions import (
    PROVIDER_SESSION_INVALID_REASON,
    SERVICE_VIEW_UNAVAILABLE_REASON,
    ActionAvailabilityCache,
    ActionAvailabilityPolicy,
    ActionAvailabilityRecord,
    ActionAvailabilitySource,
    ActionAvailabilityState,
    ActionMetadata,
    ActionUnavailableCause,
    ControllerActionService,
    ProviderActionKey,
    ProviderSessionKey,
    action_unavailable_cause,
    unavailable_overlay_template,
)
from deckr.controller._binding_planner import ActionIntentKey

CONTROLLER_ID = "controller-main"
CONTROLLER_SESSION_ID = "controller-session"
PROVIDER_SESSION_ID = "provider-session"


def _metadata(
    action_uuid: str,
    *,
    provider_instance_id: str = "provider-a",
    provider_id: str = "provider",
    provider_labels: dict[str, str] | None = None,
    provider_session_id: str | None = PROVIDER_SESSION_ID,
) -> ActionMetadata:
    return ActionMetadata(
        uuid=action_uuid,
        provider_instance_id=provider_instance_id,
        provider_id=provider_id,
        provider_labels=provider_labels,
        provider_session_id=provider_session_id,
    )


def _intent(
    action_uuid: str,
    *,
    provider_instance_id: str | None = None,
    provider_labels: dict[str, str] | None = None,
) -> ActionIntentKey:
    return ActionIntentKey(
        action_uuid=action_uuid,
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
        key=ProviderActionKey(metadata.provider_instance_id, metadata.uuid),
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
        diagnostics={},
    )


class _RuntimeLease:
    def __init__(
        self,
        descriptor: ServiceDescriptor,
        *,
        contract_id: str = "provider-session-contract",
        generation: int = 1,
        refresh_error: ServiceUnavailable | None = None,
    ) -> None:
        self.descriptor = descriptor
        self.contract = SimpleNamespace(contract_id=contract_id, generation=generation)
        self.refresh_error = refresh_error
        self.refresh_calls = 0

    async def refresh(self) -> None:
        self.refresh_calls += 1
        if self.refresh_error is not None:
            raise self.refresh_error


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
                actionId=metadata.uuid,
                status="available",
                descriptor=ActionDescriptor(
                    actionId=metadata.uuid,
                    name=metadata.name,
                    settingsSchema=metadata.settings_schema,
                    providerSettingsSchema=metadata.provider_settings_schema,
                ),
            ),
        ),
        now=now,
    )


class _ServiceViewWatchServices:
    def __init__(self) -> None:
        self._send, self._receive = anyio.create_memory_object_stream(10)
        self.entered = anyio.Event()
        self.use_count = 0
        self.closed_count = 0
        self.lease_open = False
        self.leases = []
        self.watch_calls = []

    async def send(self, payload) -> None:
        await self._send.send(payload)

    @asynccontextmanager
    async def use(self, descriptor):
        self.use_count += 1
        lease = _RuntimeLease(
            descriptor,
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

    async def watch_view(self, lease, view):
        self.watch_calls.append((lease, view))
        while True:
            payload = await self._receive.receive()
            if isinstance(payload, BaseException):
                raise payload
            yield payload


class _SendFailingServices:
    def __init__(self, error: ServiceUnavailable) -> None:
        self.error = error
        self.send_calls = []

    async def send(self, lease, name, *, params=None, event=None):
        self.send_calls.append((lease, name, params, event))
        raise self.error


@pytest.mark.parametrize(
    ("record", "live_contract", "cause", "template"),
    [
        (None, None, ActionUnavailableCause.MISSING, "unavailable_missing"),
        (
            _unavailable_record(reason="openhab_service_unavailable"),
            None,
            ActionUnavailableCause.SERVICE,
            "unavailable_service",
        ),
        (
            _unavailable_record(reason="demo_service_unavailable"),
            None,
            ActionUnavailableCause.SERVICE,
            "unavailable_service",
        ),
        (
            _unavailable_record(reason=PROVIDER_SESSION_INVALID_REASON),
            None,
            ActionUnavailableCause.SESSION,
            "unavailable_session",
        ),
        (
            _unavailable_record(reason="resource_unavailable"),
            None,
            ActionUnavailableCause.REJECTED,
            "unavailable_rejected",
        ),
        (
            _unavailable_record(reason="account_disconnected"),
            None,
            ActionUnavailableCause.UNKNOWN,
            "unavailable_unknown",
        ),
        (
            _unavailable_record(),
            False,
            ActionUnavailableCause.SESSION,
            "unavailable_session",
        ),
    ],
)
def test_action_unavailable_cause_maps_records_to_overlay_templates(
    record,
    live_contract,
    cause,
    template,
):
    mapped = action_unavailable_cause(
        record,
        has_live_provider_session_contract=live_contract,
    )

    assert mapped == cause
    assert unavailable_overlay_template(mapped) == template


def test_explicit_provider_intent_matches_only_that_provider():
    cache = ActionAvailabilityCache()
    alpha = _metadata("action.same", provider_instance_id="provider-alpha")
    beta = _metadata("action.same", provider_instance_id="provider-beta")
    beta_intent = _intent(
        "action.same",
        provider_instance_id="provider-beta",
    )
    missing_intent = _intent(
        "action.same",
        provider_instance_id="provider-missing",
    )

    cache.record_available(alpha, now=0.0)
    cache.record_available(beta, now=0.0)
    snapshot = cache.planning_snapshot((beta_intent, missing_intent), now=0.0)

    assert snapshot.metadata == {beta_intent: beta}
    assert snapshot.unavailable == frozenset({missing_intent})


def test_label_constrained_intent_ignores_unavailable_mismatch_probe():
    cache = ActionAvailabilityCache()
    unavailable = _metadata(
        "action.labelled",
        provider_instance_id="provider-alpha",
        provider_labels={"room": "office"},
    )
    mismatch = _metadata(
        "action.labelled",
        provider_instance_id="provider-beta",
        provider_labels={"room": "kitchen"},
    )
    match = _metadata(
        "action.labelled",
        provider_instance_id="provider-gamma",
        provider_labels={"room": "office"},
    )
    intent = _intent("action.labelled", provider_labels={"room": "office"})

    cache.record_unavailable(
        ProviderActionKey("provider-alpha", "action.labelled"),
        metadata=unavailable,
        now=0.0,
        intent=intent,
    )
    cache.record_probing(
        ProviderActionKey("provider-beta", "action.labelled"),
        metadata=mismatch,
        now=0.0,
    )
    cache.record_probing(
        ProviderActionKey("provider-gamma", "action.labelled"),
        metadata=match,
        now=0.0,
    )
    snapshot = cache.planning_snapshot((intent,), now=0.0)

    assert snapshot.metadata == {}
    assert snapshot.pending == frozenset({intent})
    assert snapshot.unavailable == frozenset()
    assert cache.record_for_intent(intent, now=0.0).metadata is match


def test_current_service_view_probe_is_pending():
    cache = ActionAvailabilityCache(
        policy=ActionAvailabilityPolicy(
            fresh_ttl_seconds=10.0,
            stale_grace_seconds=5.0,
        )
    )
    metadata = _metadata("action.expired", provider_instance_id="provider-alpha")
    key = ProviderActionKey("provider-alpha", "action.expired")
    intent = _intent(
        "action.expired",
        provider_instance_id="provider-alpha",
    )

    cache.record_available(metadata, now=100.0, intent=intent)
    cache.record_probing(key, metadata=metadata, now=115.0, intent=intent)
    snapshot = cache.planning_snapshot(
        (intent,),
        now=116.0,
        stale_provider_keys=(key,),
    )

    assert cache.state_for(key, now=116.0) == ActionAvailabilityState.PROBING
    assert snapshot.metadata == {}
    assert snapshot.pending == frozenset({intent})
    assert snapshot.unavailable == frozenset()


def test_service_watchers_prefer_newest_duplicate_service_descriptor():
    scheduled: list[tuple[object, tuple[object, ...]]] = []
    service = ControllerActionService(
        controller_id=CONTROLLER_ID,
        controller_session_id=CONTROLLER_SESSION_ID,
        manager=MagicMock(),
        start_soon=lambda fn, *args: scheduled.append((fn, args)),
    )
    service_id = action_runtime_service_id("provider-alpha")
    stopping = object()
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

    service._reconcile_service_watchers((newest, stale), stopping=stopping)

    assert service._service_descriptor_keys[service_id] == (
        str(newest.endpoint),
        "new-provider-session",
        "available",
    )
    watch_tasks = [
        (fn, args) for fn, args in scheduled if fn == service._run_service_view_watch
    ]
    assert watch_tasks == [
        (
            service._run_service_view_watch,
            (service_id, newest, 1, stopping),
        )
    ]


@pytest.mark.asyncio
async def test_descriptor_removal_preserves_active_runtime_lease():
    scheduled: list[tuple[object, tuple[object, ...]]] = []
    service = ControllerActionService(
        controller_id=CONTROLLER_ID,
        controller_session_id=CONTROLLER_SESSION_ID,
        manager=MagicMock(),
        services=MagicMock(),
        start_soon=lambda fn, *args: scheduled.append((fn, args)),
    )
    service_id = action_runtime_service_id("provider-alpha")
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
    lease = _RuntimeLease(descriptor)
    scope = anyio.CancelScope()

    service._service_descriptor_keys[service_id] = (
        str(descriptor.endpoint),
        descriptor.session_id,
        descriptor.backend_status.value,
    )
    service._service_watch_scopes[service_id] = scope
    service._remember_runtime_lease(
        session_key,
        lease=lease,
        service_id=service_id,
        generation=1,
    )

    service._reconcile_service_watchers((), stopping=anyio.Event())

    assert scope.cancel_called is False
    assert service.current_contract(session_key) == ContractPointer(
        contractId="provider-session-contract",
        generation=1,
    )
    assert scheduled == []


@pytest.mark.asyncio
async def test_runtime_send_lease_loss_restarts_service_watch():
    scheduled: list[tuple[object, tuple[object, ...]]] = []
    services = _SendFailingServices(
        ServiceUnavailable(
            "contract_cancelled",
            "service-use contract cancelled",
        )
    )
    service = ControllerActionService(
        controller_id=CONTROLLER_ID,
        controller_session_id=CONTROLLER_SESSION_ID,
        manager=MagicMock(),
        services=services,
        start_soon=lambda fn, *args: scheduled.append((fn, args)),
    )
    service_id = action_runtime_service_id("provider-alpha")
    descriptor = _service_descriptor(
        "provider-alpha",
        session_id="provider-session",
        updated_at=datetime(2026, 1, 2, tzinfo=UTC),
        advertisement_id="availability",
    )
    stopping = anyio.Event()
    service._reconcile_service_watchers((descriptor,), stopping=stopping)
    scheduled.clear()

    session_key = ProviderSessionKey(
        "provider-alpha",
        "provider.test",
        "provider-session",
    )
    lease = _RuntimeLease(descriptor)
    scope = anyio.CancelScope()
    service._service_watch_scopes[service_id] = scope
    service._remember_runtime_lease(
        session_key,
        lease=lease,
        service_id=service_id,
        generation=1,
    )

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
    )

    assert sent is False
    assert services.send_calls
    assert service.current_contract(session_key) is None
    assert scope.cancel_called is True
    assert scheduled == [
        (
            service._run_service_view_watch,
            (service_id, descriptor, 2, stopping),
        )
    ]


def test_service_ingests_action_availability_view_payload():
    service = ControllerActionService(
        controller_id=CONTROLLER_ID,
        controller_session_id=CONTROLLER_SESSION_ID,
        manager=MagicMock(),
        start_soon=None,
    )
    key = ProviderActionKey("provider-alpha", "action.alpha")
    service_id = action_runtime_service_id("provider-alpha")
    payload = _availability_view(
        "provider-alpha",
        entries=(
            ActionAvailabilityEntry(
                actionId="action.alpha",
                status="available",
                descriptor=ActionDescriptor(
                    actionId="action.alpha",
                    name="Alpha",
                    settingsSchema={"type": "object"},
                ),
            ),
        ),
    )

    assert service.ingest_service_view_payload(
        payload,
        service_id=service_id,
    ) == frozenset({key})
    record = service.record_for_key(key)
    assert record is not None
    assert record.source == ActionAvailabilitySource.SERVICE_VIEW
    assert record.state == ActionAvailabilityState.AVAILABLE
    assert record.metadata is not None
    assert record.metadata.name == "Alpha"
    assert record.metadata.provider_session_id == PROVIDER_SESSION_ID
    assert record.metadata.settings_schema == {"type": "object"}

    changed = service.ingest_service_view_payload(
        _availability_view(
            "provider-alpha",
            entries=(
                ActionAvailabilityEntry(
                    actionId="action.alpha",
                    status="unavailable",
                    reason="disabled",
                ),
            ),
        ),
        service_id=service_id,
    )

    assert changed == frozenset({key})
    record = service.record_for_key(key)
    assert record is not None
    assert record.state == ActionAvailabilityState.UNAVAILABLE
    assert record.reason == "disabled"
    planning = service.planning_snapshot((_intent("action.alpha"),))
    assert planning.metadata == {}
    assert planning.unavailable == frozenset({_intent("action.alpha")})

    changed = service.ingest_service_view_payload(
        _availability_view("provider-alpha"),
        service_id=service_id,
    )

    assert changed == frozenset({key})
    record = service.record_for_key(key)
    assert record is not None
    assert record.state == ActionAvailabilityState.UNAVAILABLE
    assert record.reason == "action_availability_view_missing"


def test_service_view_same_action_from_multiple_providers_stays_distinct():
    service = ControllerActionService(
        controller_id=CONTROLLER_ID,
        controller_session_id=CONTROLLER_SESSION_ID,
        manager=MagicMock(),
        start_soon=None,
    )
    intent = _intent("action.shared")

    service.ingest_service_view_payload(
        _availability_view(
            "provider-alpha",
            provider_id="provider.alpha",
            service_session_id="alpha-provider-session",
            entries=(
                ActionAvailabilityEntry(
                    actionId="action.shared",
                    status="available",
                    descriptor=ActionDescriptor(actionId="action.shared", name="Alpha"),
                ),
            ),
        ),
        service_id=action_runtime_service_id("provider-alpha"),
    )
    service.ingest_service_view_payload(
        _availability_view(
            "provider-beta",
            provider_id="provider.beta",
            service_session_id="beta-provider-session",
            entries=(
                ActionAvailabilityEntry(
                    actionId="action.shared",
                    status="available",
                    descriptor=ActionDescriptor(actionId="action.shared", name="Beta"),
                ),
            ),
        ),
        service_id=action_runtime_service_id("provider-beta"),
    )

    default_snapshot = service.planning_snapshot((intent,))
    beta_snapshot = service.planning_snapshot(
        (_intent("action.shared", provider_instance_id="provider-beta"),)
    )

    assert default_snapshot.metadata[intent].provider_instance_id == "provider-alpha"
    assert default_snapshot.metadata[intent].name == "Alpha"
    beta_intent = _intent("action.shared", provider_instance_id="provider-beta")
    assert beta_snapshot.metadata[beta_intent].provider_instance_id == "provider-beta"
    assert beta_snapshot.metadata[beta_intent].name == "Beta"

    service.mark_provider_service_unavailable("provider-alpha", now=1.0)
    fallback_snapshot = service.planning_snapshot((intent,), now=1.0)

    assert fallback_snapshot.metadata[intent].provider_instance_id == "provider-beta"
    assert fallback_snapshot.metadata[intent].name == "Beta"
    alpha_record = service.record_for_key(
        ProviderActionKey("provider-alpha", "action.shared")
    )
    assert alpha_record is not None
    assert alpha_record.state == ActionAvailabilityState.UNAVAILABLE
    assert service.record_for_key(
        ProviderActionKey("provider-beta", "action.shared")
    ) is not None


def test_missing_service_view_marks_known_provider_actions_unavailable():
    service = ControllerActionService(
        controller_id=CONTROLLER_ID,
        controller_session_id=CONTROLLER_SESSION_ID,
        manager=MagicMock(),
        start_soon=None,
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

    changed = service.mark_provider_service_unavailable(
        "provider-alpha",
        reason="action_availability_view_missing",
        now=1.0,
    )

    assert changed == frozenset({key})
    record = service.record_for_key(key)
    assert record is not None
    assert record.state == ActionAvailabilityState.UNAVAILABLE
    assert record.reason == "action_availability_view_missing"


@pytest.mark.asyncio
async def test_service_view_watch_missing_view_keeps_service_use_lease_open(
    monkeypatch,
):
    monkeypatch.setattr(
        "deckr.controller._actions._service._SERVICE_WATCH_RETRY_SECONDS",
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
        tg.start_soon(service._run_service_view_watch, service_id, descriptor, 1, stopping)
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
        "deckr.controller._actions._service._SERVICE_WATCH_RETRY_SECONDS",
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
        tg.start_soon(service._run_service_view_watch, service_id, descriptor, 1, stopping)
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
async def test_service_view_watch_transient_unavailable_keeps_same_service_use_lease(
    monkeypatch,
):
    monkeypatch.setattr(
        "deckr.controller._actions._service._SERVICE_WATCH_RETRY_SECONDS",
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
        tg.start_soon(service._run_service_view_watch, service_id, descriptor, 1, stopping)
        with anyio.fail_after(1):
            await services.entered.wait()

        await services.send(
            ServiceUnavailable(
                "contract_unavailable",
                "contract materialized view unavailable",
            )
        )
        with anyio.fail_after(1):
            while len(services.watch_calls) < 2:
                await anyio.sleep(0.01)

        with anyio.move_on_after(0.05) as scope:
            await notify_receive.receive()
        assert scope.cancel_called
        assert services.use_count == 1
        assert services.closed_count == 0
        assert services.lease_open is True
        assert services.watch_calls[0][0] is services.leases[0]
        assert services.watch_calls[1][0] is services.leases[0]
        record = service.record_for_key(key)
        assert record is not None
        assert record.state == ActionAvailabilityState.AVAILABLE

        await services.send(payload.to_dict())
        with anyio.fail_after(1):
            assert await notify_receive.receive() == frozenset({key})
        assert services.use_count == 1
        assert services.closed_count == 0
        assert services.lease_open is True

        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_service_view_watch_terminal_unavailable_retries_descriptor(
    monkeypatch,
):
    monkeypatch.setattr(
        "deckr.controller._actions._service._SERVICE_WATCH_RETRY_SECONDS",
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
        tg.start_soon(service._run_service_view_watch, service_id, descriptor, 1, stopping)
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


@pytest.mark.asyncio
async def test_service_view_watch_rejects_mismatched_payload_provider(
    monkeypatch,
):
    monkeypatch.setattr(
        "deckr.controller._actions._service._SERVICE_WATCH_RETRY_SECONDS",
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
        tg.start_soon(service._run_service_view_watch, service_id, descriptor, 1, stopping)
        with anyio.fail_after(1):
            await services.entered.wait()

        await services.send(payload.to_dict())
        await anyio.sleep(0.05)

        assert service.current_contract(mismatched_key) is None
        assert service.record_for_key(
            ProviderActionKey("provider-beta", "action.beta")
        ) is None
        with anyio.move_on_after(0.05) as scope:
            await notify_receive.receive()
        assert scope.cancel_called

        tg.cancel_scope.cancel()


def test_service_logs_unchanged_provider_snapshot(caplog):
    caplog.set_level(
        logging.DEBUG,
        logger="deckr.controller._actions._service",
    )
    service = ControllerActionService(
        controller_id=CONTROLLER_ID,
        controller_session_id=CONTROLLER_SESSION_ID,
        manager=MagicMock(),
        start_soon=None,
    )
    key = ProviderActionKey("provider-alpha", "action.alpha")
    entry = ActionAvailabilityEntry(
        actionId="action.alpha",
        status="available",
        descriptor=ActionDescriptor(actionId="action.alpha", name="Alpha"),
    )

    assert service.ingest_provider_entries(
        provider_instance_id="provider-alpha",
        provider_id="provider.test",
        entries=(entry,),
    ) == frozenset({key})
    caplog.clear()

    assert service.ingest_provider_entries(
        provider_instance_id="provider-alpha",
        provider_id="provider.test",
        entries=(entry,),
    ) == frozenset()

    assert "Action availability entry ingested" in caplog.text
    assert "same_as_existing=True" in caplog.text
    assert "changed_keys=0" in caplog.text


def test_service_planning_snapshot_preserves_only_existing_stale_bindings():
    now = 112.0
    metadata = _metadata("action.stale", provider_instance_id="provider-alpha")
    key = ProviderActionKey("provider-alpha", "action.stale")
    intent = _intent(
        "action.stale",
        provider_instance_id="provider-alpha",
    )
    cache = ActionAvailabilityCache(
        policy=ActionAvailabilityPolicy(
            fresh_ttl_seconds=10.0,
            stale_grace_seconds=5.0,
        ),
        clock=lambda: now,
    )
    cache.record_available(metadata, now=100.0, intent=intent)
    service = ControllerActionService(
        controller_id=CONTROLLER_ID,
        controller_session_id=CONTROLLER_SESSION_ID,
        manager=MagicMock(),
        start_soon=None,
        cache=cache,
        clock=lambda: now,
    )

    fresh_binding = service.planning_snapshot((intent,), now=now)
    retained_binding = service.planning_snapshot(
        (intent,),
        existing_provider_keys=(key,),
        now=now,
    )

    assert fresh_binding.metadata == {}
    assert fresh_binding.pending == frozenset({intent})
    assert retained_binding.metadata == {intent: metadata}
    assert retained_binding.pending == frozenset()


def test_default_policy_keeps_service_view_records_authoritative_after_60s():
    cache = ActionAvailabilityCache()
    metadata = _metadata("action.current", provider_instance_id="provider-alpha")
    key = ProviderActionKey("provider-alpha", "action.current")
    intent = _intent("action.current", provider_instance_id="provider-alpha")

    cache.record_available(metadata, now=0.0, intent=intent)
    snapshot = cache.planning_snapshot((intent,), now=61.0)

    assert cache.state_for(key, now=61.0) == ActionAvailabilityState.AVAILABLE
    assert snapshot.metadata == {intent: metadata}
    assert snapshot.pending == frozenset()
    assert snapshot.unavailable == frozenset()


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
    service = ControllerActionService(
        controller_id=CONTROLLER_ID,
        controller_session_id=CONTROLLER_SESSION_ID,
        manager=MagicMock(),
        services=MagicMock(),
        start_soon=None,
    )
    lease = _RuntimeLease(
        _service_descriptor(
            "provider-alpha",
            session_id="negotiating-session",
            updated_at=datetime(2026, 1, 2, tzinfo=UTC),
            advertisement_id="availability",
        ),
        contract_id="provider-session-contract",
    )
    service._remember_runtime_lease(
        session_key,
        lease=lease,
        service_id=action_runtime_service_id("provider-alpha"),
        generation=1,
    )
    _record_service_available(service, metadata, now=0.0)

    planning = service.planning_snapshot((intent,))

    assert planning.metadata[intent].uuid == metadata.uuid
    assert planning.metadata[intent].provider_instance_id == metadata.provider_instance_id
    assert planning.metadata[intent].provider_id == metadata.provider_id
    assert planning.metadata[intent].provider_session_id == metadata.provider_session_id
    assert planning.pending == frozenset()
    assert service.current_contract(session_key) == ContractPointer(
        contractId="provider-session-contract",
        generation=1,
    )


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
    service = ControllerActionService(
        controller_id=CONTROLLER_ID,
        controller_session_id=CONTROLLER_SESSION_ID,
        manager=MagicMock(),
        services=MagicMock(),
        start_soon=None,
    )
    mismatched_key = ProviderSessionKey(
        "provider-alpha",
        "provider.test",
        "different-session",
    )
    lease = _RuntimeLease(
        _service_descriptor(
            "provider-alpha",
            session_id="different-session",
            updated_at=datetime(2026, 1, 2, tzinfo=UTC),
            advertisement_id="availability",
        )
    )
    service._remember_runtime_lease(
        mismatched_key,
        lease=lease,
        service_id=action_runtime_service_id("provider-alpha"),
        generation=1,
    )
    _record_service_available(service, metadata, now=0.0)

    planning = service.planning_snapshot((intent,), now=1.0)

    assert planning.metadata == {}
    assert planning.pending == frozenset({intent})
    assert service.current_contract(session_key) is None


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
