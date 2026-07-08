from __future__ import annotations

import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from deckr.actions.availability import (
    ACTION_AVAILABILITY_SERVICE_PROTOCOL,
    ActionAvailabilityViewPayload,
    action_availability_service_id,
)
from deckr.actions.endpoints import action_provider_address
from deckr.actions.messages import (
    ActionAvailabilityEntry,
    ActionDescriptor,
)
from deckr.contracts.authority import ContractPointer
from deckr.contracts.messages import service_address
from deckr.services import ServiceBackendStatus, ServiceDescriptor

from deckr.controller._action_availability import (
    PROVIDER_SESSION_INVALID_REASON,
    ActionAvailabilityCache,
    ActionAvailabilityPolicy,
    ActionAvailabilityRecord,
    ActionAvailabilityService,
    ActionAvailabilitySource,
    ActionAvailabilityState,
    ActionUnavailableCause,
    ProviderActionKey,
    action_unavailable_cause,
    unavailable_overlay_template,
)
from deckr.controller._action_provider_sessions import ProviderSessionKey
from deckr.controller._binding_planner import ActionIntentKey
from deckr.controller.action_provider.provider import (
    ActionMetadata,
    ActionProviderSessionCandidate,
)

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


def _actions_bus() -> SimpleNamespace:
    return SimpleNamespace(send=AsyncMock())


def _manager_with_provider_session(
    provider_session_id: str | None = PROVIDER_SESSION_ID,
) -> MagicMock:
    manager = MagicMock()

    def provider_session_candidate(
        provider_instance_id: str,
        provider_id: str,
    ) -> ActionProviderSessionCandidate | None:
        if provider_session_id is None:
            return None
        return ActionProviderSessionCandidate(
            provider_instance_id=provider_instance_id,
            provider_id=provider_id,
            provider_session_id=provider_session_id,
        )

    manager.provider_session_candidate.side_effect = provider_session_candidate
    return manager


def _provider_session_key(action: ActionMetadata) -> ProviderSessionKey:
    assert action.provider_session_id is not None
    return ProviderSessionKey(
        action.provider_instance_id,
        action.provider_id,
        action.provider_session_id,
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


def _provider_sessions_mock(
    *,
    prepare_ready: bool = True,
    prepare_terminal: bool = False,
    refresh_ready: bool = True,
    refresh_terminal: bool = False,
    cached_ready: bool | None = None,
    valid: bool | list[bool] = True,
) -> MagicMock:
    provider_sessions = MagicMock()
    provider_sessions.set_change_callback = MagicMock()
    provider_sessions.aclose = AsyncMock()
    cached_ready_state = {
        "ready": refresh_ready if cached_ready is None else cached_ready,
    }

    async def prepare_many(actions):
        cached_ready_state["ready"] = prepare_ready
        return {
            key: SimpleNamespace(
                key=key,
                ready=prepare_ready,
                terminal=prepare_terminal,
            )
            for action in tuple(actions)
            if action.provider_session_id is not None
            for key in (_provider_session_key(action),)
        }

    async def refresh_many(keys):
        cached_ready_state["ready"] = refresh_ready
        return {
            key: SimpleNamespace(
                key=key,
                ready=refresh_ready,
                terminal=refresh_terminal,
            )
            for key in tuple(keys)
        }

    provider_sessions.prepare_many = AsyncMock(side_effect=prepare_many)
    provider_sessions.refresh_many = AsyncMock(side_effect=refresh_many)
    provider_sessions.cached_ready = MagicMock(
        side_effect=lambda _key: cached_ready_state["ready"],
    )
    provider_sessions.contract_pointer = MagicMock(
        return_value=ContractPointer(contractId="provider-session-contract", generation=1)
    )
    provider_sessions.valid = AsyncMock(
        side_effect=list(valid) if isinstance(valid, list) else None,
        return_value=valid if isinstance(valid, bool) else None,
    )
    return provider_sessions


def _service_descriptor(
    provider_instance_id: str,
    *,
    session_id: str,
    updated_at: datetime,
    advertisement_id: str,
    refresh_seq: int = 1,
    backend_status: ServiceBackendStatus = ServiceBackendStatus.AVAILABLE,
) -> ServiceDescriptor:
    service_id = action_availability_service_id(provider_instance_id)
    protocol = ACTION_AVAILABILITY_SERVICE_PROTOCOL
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
        views={},
        backend_status=backend_status,
        diagnostics={},
    )


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
    snapshot = cache.snapshot_for_intents((beta_intent, missing_intent), now=0.0)

    assert snapshot == {beta_intent: beta}


def test_label_constrained_intent_ignores_unavailable_mismatch_candidate():
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
    cache.record_candidate(mismatch, now=0.0)
    cache.record_candidate(match, now=0.0)
    snapshot = cache.planning_snapshot((intent,), now=0.0)

    assert snapshot.metadata == {}
    assert snapshot.pending == frozenset({intent})
    assert snapshot.unavailable == frozenset()
    assert cache.record_for_intent(intent, now=0.0).metadata is match


def test_expired_service_view_record_stays_pending_with_live_candidate():
    cache = ActionAvailabilityCache(
        policy=ActionAvailabilityPolicy(
            fresh_ttl_seconds=10.0,
            stale_grace_seconds=5.0,
            candidate_ttl_seconds=10.0,
        )
    )
    metadata = _metadata("action.expired", provider_instance_id="provider-alpha")
    key = ProviderActionKey("provider-alpha", "action.expired")
    intent = _intent(
        "action.expired",
        provider_instance_id="provider-alpha",
    )

    cache.record_available(metadata, now=100.0, intent=intent)
    cache.record_candidate(metadata, now=115.0)
    snapshot = cache.planning_snapshot(
        (intent,),
        now=116.0,
        stale_provider_keys=(key,),
    )

    assert cache.state_for(key, now=116.0) == ActionAvailabilityState.EXPIRED
    assert snapshot.metadata == {}
    assert snapshot.pending == frozenset({intent})
    assert snapshot.unavailable == frozenset()


def test_candidate_removal_clears_records_and_intent_mappings():
    cache = ActionAvailabilityCache()
    metadata = _metadata("action.removed", provider_instance_id="provider-alpha")
    key = ProviderActionKey("provider-alpha", "action.removed")
    intent = _intent(
        "action.removed",
        provider_instance_id="provider-alpha",
    )

    cache.record_candidate(metadata, now=0.0, intent=intent)

    removed = cache.remove_candidate(key)

    assert removed is not None
    assert cache.record_for(key) is None
    assert cache._record_keys_by_intent == {}
    assert cache.snapshot_for_intents((intent,), now=0.0) == {}


def test_candidate_expiry_returns_expired_and_omits_snapshot_metadata():
    cache = ActionAvailabilityCache(
        policy=ActionAvailabilityPolicy(candidate_ttl_seconds=10.0)
    )
    metadata = _metadata("action.expiring", provider_instance_id="provider-alpha")
    key = ProviderActionKey("provider-alpha", "action.expiring")
    intent = _intent(
        "action.expiring",
        provider_instance_id="provider-alpha",
    )

    cache.record_candidate(metadata, now=100.0)

    assert cache.state_for(key, now=111.0) == ActionAvailabilityState.EXPIRED
    assert cache.snapshot_for_intents((intent,), now=111.0) == {}


def test_service_provider_session_change_schedules_reconcile():
    actions_bus = _actions_bus()
    scheduled: list[object] = []
    provider_sessions = _provider_sessions_mock()
    service = ActionAvailabilityService(
        controller_id=CONTROLLER_ID,
        controller_session_id=CONTROLLER_SESSION_ID,
        actions_bus=actions_bus,
        manager=MagicMock(),
        provider_sessions=provider_sessions,
        start_soon=lambda fn, *args: scheduled.append((fn, args)),
    )

    service._provider_session_changed()

    assert scheduled == [(service._provider_session_reconcile_task, ())]


def test_service_watchers_prefer_newest_duplicate_service_descriptor():
    actions_bus = _actions_bus()
    scheduled: list[tuple[object, tuple[object, ...]]] = []
    service = ActionAvailabilityService(
        controller_id=CONTROLLER_ID,
        controller_session_id=CONTROLLER_SESSION_ID,
        actions_bus=actions_bus,
        manager=MagicMock(),
        start_soon=lambda fn, *args: scheduled.append((fn, args)),
    )
    service_id = action_availability_service_id("provider-alpha")
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
            (service_id, newest, stopping),
        )
    ]


def test_service_ingests_action_availability_view_payload():
    actions_bus = _actions_bus()
    service = ActionAvailabilityService(
        controller_id=CONTROLLER_ID,
        controller_session_id=CONTROLLER_SESSION_ID,
        actions_bus=actions_bus,
        manager=MagicMock(),
        start_soon=None,
    )
    key = ProviderActionKey("provider-alpha", "action.alpha")
    service_id = action_availability_service_id("provider-alpha")
    payload = ActionAvailabilityViewPayload(
        providerInstanceId="provider-alpha",
        providerEndpoint=action_provider_address("provider-alpha"),
        providerId="provider.test",
        providerSessionId=PROVIDER_SESSION_ID,
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
    record = service.cache.record_for(key)
    assert record is not None
    assert record.source == ActionAvailabilitySource.SERVICE_VIEW
    assert record.state == ActionAvailabilityState.AVAILABLE
    assert record.metadata is not None
    assert record.metadata.name == "Alpha"
    assert record.metadata.provider_session_id == PROVIDER_SESSION_ID
    assert record.metadata.settings_schema == {"type": "object"}

    changed = service.ingest_service_view_payload(
        ActionAvailabilityViewPayload(
            providerInstanceId="provider-alpha",
            providerEndpoint=action_provider_address("provider-alpha"),
            providerId="provider.test",
            providerSessionId=PROVIDER_SESSION_ID,
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
    record = service.cache.record_for(key)
    assert record is not None
    assert record.state == ActionAvailabilityState.UNAVAILABLE
    assert record.reason == "disabled"
    planning = service.planning_snapshot((_intent("action.alpha"),))
    assert planning.metadata == {}
    assert planning.unavailable == frozenset({_intent("action.alpha")})

    changed = service.ingest_service_view_payload(
        ActionAvailabilityViewPayload(
            providerInstanceId="provider-alpha",
            providerEndpoint=action_provider_address("provider-alpha"),
            providerId="provider.test",
            providerSessionId=PROVIDER_SESSION_ID,
            entries=(),
        ),
        service_id=service_id,
    )

    assert changed == frozenset({key})
    record = service.cache.record_for(key)
    assert record is not None
    assert record.state == ActionAvailabilityState.UNAVAILABLE
    assert record.reason == "action_availability_view_missing"


def test_service_view_same_action_from_multiple_providers_stays_distinct():
    actions_bus = _actions_bus()
    provider_sessions = _provider_sessions_mock(cached_ready=True)
    service = ActionAvailabilityService(
        controller_id=CONTROLLER_ID,
        controller_session_id=CONTROLLER_SESSION_ID,
        actions_bus=actions_bus,
        manager=MagicMock(),
        provider_sessions=provider_sessions,
        start_soon=None,
    )
    intent = _intent("action.shared")

    service.ingest_service_view_payload(
        ActionAvailabilityViewPayload(
            providerInstanceId="provider-alpha",
            providerEndpoint=action_provider_address("provider-alpha"),
            providerId="provider.alpha",
            providerSessionId="alpha-provider-session",
            entries=(
                ActionAvailabilityEntry(
                    actionId="action.shared",
                    status="available",
                    descriptor=ActionDescriptor(actionId="action.shared", name="Alpha"),
                ),
            ),
        ),
        service_id=action_availability_service_id("provider-alpha"),
    )
    service.ingest_service_view_payload(
        ActionAvailabilityViewPayload(
            providerInstanceId="provider-beta",
            providerEndpoint=action_provider_address("provider-beta"),
            providerId="provider.beta",
            providerSessionId="beta-provider-session",
            entries=(
                ActionAvailabilityEntry(
                    actionId="action.shared",
                    status="available",
                    descriptor=ActionDescriptor(actionId="action.shared", name="Beta"),
                ),
            ),
        ),
        service_id=action_availability_service_id("provider-beta"),
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
    alpha_record = service.cache.record_for(
        ProviderActionKey("provider-alpha", "action.shared")
    )
    assert alpha_record is not None
    assert alpha_record.state == ActionAvailabilityState.UNAVAILABLE
    assert service.cache.record_for(
        ProviderActionKey("provider-beta", "action.shared")
    ) is not None


def test_missing_service_view_marks_known_provider_actions_unavailable():
    actions_bus = _actions_bus()
    service = ActionAvailabilityService(
        controller_id=CONTROLLER_ID,
        controller_session_id=CONTROLLER_SESSION_ID,
        actions_bus=actions_bus,
        manager=MagicMock(),
        start_soon=None,
    )
    key = ProviderActionKey("provider-alpha", "action.alpha")
    service.cache.record_available(
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
    record = service.cache.record_for(key)
    assert record is not None
    assert record.state == ActionAvailabilityState.UNAVAILABLE
    assert record.reason == "action_availability_view_missing"


def test_service_logs_unchanged_provider_snapshot(caplog):
    caplog.set_level(
        logging.DEBUG,
        logger="deckr.controller._action_availability",
    )
    actions_bus = _actions_bus()
    service = ActionAvailabilityService(
        controller_id=CONTROLLER_ID,
        controller_session_id=CONTROLLER_SESSION_ID,
        actions_bus=actions_bus,
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
    actions_bus = _actions_bus()
    service = ActionAvailabilityService(
        controller_id=CONTROLLER_ID,
        controller_session_id=CONTROLLER_SESSION_ID,
        actions_bus=actions_bus,
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


def test_service_view_available_with_unready_provider_session_is_pending():
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
    actions_bus = _actions_bus()
    provider_sessions = _provider_sessions_mock(cached_ready=False)
    service = ActionAvailabilityService(
        controller_id=CONTROLLER_ID,
        controller_session_id=CONTROLLER_SESSION_ID,
        actions_bus=actions_bus,
        manager=MagicMock(),
        provider_sessions=provider_sessions,
        start_soon=None,
    )
    service.cache.record_available(metadata, intent=intent, now=0.0)

    snapshot = service.planning_snapshot((intent,), now=1.0)

    assert snapshot.metadata == {}
    assert snapshot.pending == frozenset({intent})
    assert snapshot.unavailable == frozenset()
    assert service.contract_pointer(session_key) is None
    provider_sessions.contract_pointer.assert_not_called()


@pytest.mark.asyncio
async def test_service_reconcile_preserves_nonterminal_provider_session_as_pending():
    metadata = _metadata(
        "action.alpha",
        provider_instance_id="provider-alpha",
        provider_id="provider.test",
        provider_session_id="negotiating-session",
    )
    key = ProviderActionKey("provider-alpha", "action.alpha")
    intent = _intent("action.alpha", provider_instance_id="provider-alpha")
    actions_bus = _actions_bus()
    provider_sessions = _provider_sessions_mock(
        refresh_ready=False,
        refresh_terminal=False,
    )
    service = ActionAvailabilityService(
        controller_id=CONTROLLER_ID,
        controller_session_id=CONTROLLER_SESSION_ID,
        actions_bus=actions_bus,
        manager=MagicMock(),
        provider_sessions=provider_sessions,
        start_soon=None,
    )
    service.cache.record_available(metadata, intent=intent, now=service._clock())

    changed = await service.reconcile_provider_sessions()

    assert changed == frozenset()
    provider_sessions.refresh_many.assert_awaited_once_with(
        (
            ProviderSessionKey(
                "provider-alpha",
                "provider.test",
                "negotiating-session",
            ),
        )
    )
    record = service.cache.record_for(key)
    assert record is not None
    assert record.state == ActionAvailabilityState.AVAILABLE
    planning = service.planning_snapshot((intent,))
    assert planning.metadata == {}
    assert planning.pending == frozenset({intent})
    assert planning.unavailable == frozenset()


@pytest.mark.asyncio
async def test_provider_session_ready_transition_notifies_and_unlocks_planning():
    metadata = _metadata(
        "action.alpha",
        provider_instance_id="provider-alpha",
        provider_id="provider.test",
        provider_session_id="negotiating-session",
    )
    key = ProviderActionKey("provider-alpha", "action.alpha")
    intent = _intent("action.alpha", provider_instance_id="provider-alpha")
    scheduled: list[tuple[object, tuple[object, ...]]] = []
    notified: list[frozenset[ProviderActionKey]] = []
    actions_bus = _actions_bus()
    provider_sessions = _provider_sessions_mock(
        cached_ready=False,
        refresh_ready=True,
        refresh_terminal=False,
    )
    service = ActionAvailabilityService(
        controller_id=CONTROLLER_ID,
        controller_session_id=CONTROLLER_SESSION_ID,
        actions_bus=actions_bus,
        manager=MagicMock(),
        provider_sessions=provider_sessions,
        start_soon=lambda fn, *args: scheduled.append((fn, args)),
        on_availability_changed=lambda keys: notified.append(keys),
    )
    service.cache.record_available(metadata, intent=intent, now=0.0)

    pending = service.planning_snapshot((intent,), now=1.0)
    service._provider_session_changed()
    reconcile = next(
        (fn, args)
        for fn, args in scheduled
        if fn == service._provider_session_reconcile_task
    )
    await reconcile[0](*reconcile[1])
    ready = service.planning_snapshot((intent,), now=2.0)

    assert pending.metadata == {}
    assert pending.pending == frozenset({intent})
    assert notified == [frozenset({key})]
    assert ready.metadata == {intent: metadata}
    assert ready.pending == frozenset()


@pytest.mark.asyncio
async def test_provider_session_change_reconciles_and_notifies_invalid_records():
    metadata = _metadata(
        "action.alpha",
        provider_instance_id="provider-alpha",
        provider_id="provider.test",
        provider_session_id="stale-session",
    )
    key = ProviderActionKey("provider-alpha", "action.alpha")
    scheduled: list[tuple[object, tuple[object, ...]]] = []
    notified: list[frozenset[ProviderActionKey]] = []
    actions_bus = _actions_bus()
    provider_sessions = _provider_sessions_mock(
        refresh_ready=False,
        refresh_terminal=True,
    )
    service = ActionAvailabilityService(
        controller_id=CONTROLLER_ID,
        controller_session_id=CONTROLLER_SESSION_ID,
        actions_bus=actions_bus,
        manager=MagicMock(),
        provider_sessions=provider_sessions,
        start_soon=lambda fn, *args: scheduled.append((fn, args)),
        on_availability_changed=lambda keys: notified.append(keys),
    )
    service.cache.record_available(metadata, now=0.0)

    service._provider_session_changed()

    reconcile = next(
        (fn, args)
        for fn, args in scheduled
        if fn == service._provider_session_reconcile_task
    )
    await reconcile[0](*reconcile[1])

    assert notified == [frozenset({key})]
    provider_sessions.refresh_many.assert_awaited_once_with(
        (
            ProviderSessionKey(
                "provider-alpha",
                "provider.test",
                "stale-session",
            ),
        )
    )
    record = service.cache.record_for(key)
    assert record is not None
    assert record.reason == PROVIDER_SESSION_INVALID_REASON
