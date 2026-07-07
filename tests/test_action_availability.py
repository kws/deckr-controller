from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, call

import pytest
from deckr.actions.endpoints import action_provider_address
from deckr.actions.messages import (
    ACTION_AVAILABILITY_CHANGED,
    ACTION_AVAILABILITY_REQUEST,
    ACTION_AVAILABILITY_SNAPSHOT,
    ACTION_INTEREST_UPDATE,
    ActionAvailabilityChangedBody,
    ActionAvailabilityEntry,
    ActionAvailabilityRequestBody,
    ActionAvailabilitySnapshotBody,
    ActionDescriptor,
    ActionInterestUpdateBody,
    action_provider_instance_subject,
)
from deckr.contracts.messages import (
    ACTIONS_LANE,
    DeckrMessage,
    controller_address,
    endpoint_target,
)

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
from deckr.controller._action_interest import (
    ActionInterestRecord,
    ActionInterestSnapshot,
    ActionInterestSource,
    ActionInterestStrength,
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
        source=ActionAvailabilitySource.PROVIDER_DIRECT,
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
    valid: bool | list[bool] = True,
) -> MagicMock:
    provider_sessions = MagicMock()
    provider_sessions.set_change_callback = MagicMock()
    provider_sessions.aclose = AsyncMock()

    async def prepare_many(actions):
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
    provider_sessions.valid = AsyncMock(
        side_effect=list(valid) if isinstance(valid, list) else None,
        return_value=valid if isinstance(valid, bool) else None,
    )
    return provider_sessions


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


def _availability_message(
    *,
    message_type: str,
    body: ActionAvailabilityChangedBody | ActionAvailabilitySnapshotBody,
    provider_instance_id: str = "provider-alpha",
    provider_session_id: str = PROVIDER_SESSION_ID,
    provider_id: str = "provider.test",
) -> DeckrMessage:
    return DeckrMessage(
        lane=ACTIONS_LANE,
        messageType=message_type,
        sender=action_provider_address(provider_instance_id),
        senderSessionId=provider_session_id,
        recipient=endpoint_target(controller_address(CONTROLLER_ID)),
        subject=action_provider_instance_subject(
            provider_instance_id,
            provider_id=provider_id,
        ),
        body=body.to_dict(),
    )


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


def test_expired_provider_direct_record_stays_pending_with_live_candidate():
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


@pytest.mark.asyncio
async def test_service_flushes_interest_and_availability_request_to_candidate_provider():
    actions_bus = _actions_bus()
    metadata = _metadata(
        "action.alpha",
        provider_instance_id="provider-alpha",
        provider_id="provider.test",
        provider_session_id=PROVIDER_SESSION_ID,
    )
    manager = _manager_with_provider_session()
    provider_sessions = _provider_sessions_mock()
    service = ActionAvailabilityService(
        controller_id=CONTROLLER_ID,
        controller_session_id=CONTROLLER_SESSION_ID,
        actions_bus=actions_bus,
        manager=manager,
        start_soon=None,
        provider_sessions=provider_sessions,
    )
    intent = _intent(
        "action.alpha",
        provider_instance_id="provider-alpha",
    )
    service.cache.record_candidate(metadata, now=0.0, intent=intent)
    service.update_config_interest(
        "config-a",
        ActionInterestSnapshot(
            records=(
                ActionInterestRecord(
                    intent=intent,
                    source=ActionInterestSource.VISIBLE_BINDING,
                    strength=ActionInterestStrength.STRONG,
                    first_needed_at=0.0,
                    last_needed_at=0.0,
                    retain_until=None,
                ),
            )
        ),
    )

    await service.flush_interest(force_requests=True)

    manager.provider_session_candidate.assert_called_once_with(
        "provider-alpha",
        "provider.test",
    )
    provider_sessions.prepare_many.assert_awaited_once()
    prepared = provider_sessions.prepare_many.await_args.args[0]
    assert [
        (
            action.provider_instance_id,
            action.provider_id,
            action.uuid,
            action.provider_session_id,
        )
        for action in prepared
    ] == [
        (
            "provider-alpha",
            "provider.test",
            "action.alpha",
            PROVIDER_SESSION_ID,
        )
    ]

    assert actions_bus.send.await_count == 2
    interest = actions_bus.send.await_args_list[0].kwargs
    request = actions_bus.send.await_args_list[1].kwargs
    assert interest["message_type"] == ACTION_INTEREST_UPDATE
    assert interest["recipient"] == action_provider_address("provider-alpha")
    assert interest["recipient_session_id"] == PROVIDER_SESSION_ID
    assert interest["subject"] == action_provider_instance_subject(
        "provider-alpha",
        provider_id="provider.test",
    )
    interest_body = ActionInterestUpdateBody.model_validate(interest["body"])
    assert interest_body.provider_instance_id == "provider-alpha"
    assert interest_body.provider_id == "provider.test"
    assert [(entry.action_id, entry.level) for entry in interest_body.entries] == [
        ("action.alpha", "strong")
    ]

    assert request["message_type"] == ACTION_AVAILABILITY_REQUEST
    assert request["recipient"] == action_provider_address("provider-alpha")
    assert request["recipient_session_id"] == PROVIDER_SESSION_ID
    assert request["subject"] == action_provider_instance_subject(
        "provider-alpha",
        provider_id="provider.test",
    )
    request_body = ActionAvailabilityRequestBody.model_validate(request["body"])
    assert [selector.action_id for selector in request_body.selectors] == [
        "action.alpha"
    ]


@pytest.mark.asyncio
async def test_service_direct_availability_request_prepares_provider_session():
    actions_bus = _actions_bus()
    manager = _manager_with_provider_session()
    provider_sessions = _provider_sessions_mock()
    service = ActionAvailabilityService(
        controller_id=CONTROLLER_ID,
        controller_session_id=CONTROLLER_SESSION_ID,
        actions_bus=actions_bus,
        manager=manager,
        provider_sessions=provider_sessions,
        start_soon=None,
    )

    await service.request_provider_availability(
        "provider-alpha",
        "provider.test",
        ("action.alpha",),
        force=True,
    )

    manager.provider_session_candidate.assert_called_once_with(
        "provider-alpha",
        "provider.test",
    )
    provider_sessions.prepare_many.assert_awaited_once()
    prepared = provider_sessions.prepare_many.await_args.args[0]
    assert [(action.uuid, action.provider_session_id) for action in prepared] == [
        ("action.alpha", PROVIDER_SESSION_ID)
    ]
    actions_bus.send.assert_awaited_once()
    request = actions_bus.send.await_args.kwargs
    assert request["message_type"] == ACTION_AVAILABILITY_REQUEST
    assert request["recipient"] == action_provider_address("provider-alpha")
    assert request["recipient_session_id"] == PROVIDER_SESSION_ID


@pytest.mark.asyncio
async def test_service_pending_provider_session_sends_no_interest_or_request():
    actions_bus = _actions_bus()
    metadata = _metadata(
        "action.alpha",
        provider_instance_id="provider-alpha",
        provider_id="provider.test",
        provider_session_id=PROVIDER_SESSION_ID,
    )
    manager = _manager_with_provider_session()
    provider_sessions = _provider_sessions_mock(prepare_ready=False)
    service = ActionAvailabilityService(
        controller_id=CONTROLLER_ID,
        controller_session_id=CONTROLLER_SESSION_ID,
        actions_bus=actions_bus,
        manager=manager,
        start_soon=None,
        provider_sessions=provider_sessions,
    )
    intent = _intent("action.alpha", provider_instance_id="provider-alpha")
    service.cache.record_candidate(metadata, now=0.0, intent=intent)
    service.update_config_interest(
        "config-a",
        ActionInterestSnapshot(
            records=(
                ActionInterestRecord(
                    intent=intent,
                    source=ActionInterestSource.VISIBLE_BINDING,
                    strength=ActionInterestStrength.STRONG,
                    first_needed_at=0.0,
                    last_needed_at=0.0,
                    retain_until=None,
                ),
            )
        ),
    )

    await service.flush_interest(force_requests=True)

    manager.provider_session_candidate.assert_called_once_with(
        "provider-alpha",
        "provider.test",
    )
    provider_sessions.prepare_many.assert_awaited_once()
    actions_bus.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_service_pending_provider_session_direct_request_sends_nothing():
    actions_bus = _actions_bus()
    manager = _manager_with_provider_session()
    provider_sessions = _provider_sessions_mock(prepare_ready=False)
    service = ActionAvailabilityService(
        controller_id=CONTROLLER_ID,
        controller_session_id=CONTROLLER_SESSION_ID,
        actions_bus=actions_bus,
        manager=manager,
        provider_sessions=provider_sessions,
        start_soon=None,
    )

    await service.request_provider_availability(
        "provider-alpha",
        "provider.test",
        ("action.alpha",),
        force=True,
    )

    manager.provider_session_candidate.assert_called_once_with(
        "provider-alpha",
        "provider.test",
    )
    provider_sessions.prepare_many.assert_awaited_once()
    actions_bus.send.assert_not_awaited()


def test_service_provider_session_change_schedules_immediate_flush():
    actions_bus = _actions_bus()
    scheduled: list[object] = []
    service = ActionAvailabilityService(
        controller_id=CONTROLLER_ID,
        controller_session_id=CONTROLLER_SESSION_ID,
        actions_bus=actions_bus,
        manager=MagicMock(),
        start_soon=lambda fn, *args: scheduled.append((fn, args)),
    )
    service._last_request_at_by_provider["provider-alpha"] = 12.0
    service._last_request_at_by_provider["provider-beta"] = 24.0

    service._provider_session_changed()

    assert not service._last_request_at_by_provider
    assert scheduled == [(service.flush_interest, ())]


@pytest.mark.asyncio
async def test_service_ingests_provider_snapshot_and_change_messages():
    actions_bus = _actions_bus()
    provider_sessions = _provider_sessions_mock(valid=True)
    service = ActionAvailabilityService(
        controller_id=CONTROLLER_ID,
        controller_session_id=CONTROLLER_SESSION_ID,
        actions_bus=actions_bus,
        manager=MagicMock(),
        provider_sessions=provider_sessions,
        start_soon=None,
    )
    key = ProviderActionKey("provider-alpha", "action.alpha")

    snapshot = _availability_message(
        message_type=ACTION_AVAILABILITY_SNAPSHOT,
        body=ActionAvailabilitySnapshotBody(
            providerInstanceId="provider-alpha",
            providerId="provider.test",
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
        ),
    )

    assert await service.handle_availability_message(snapshot) == frozenset({key})
    record = service.cache.record_for(key)
    assert record is not None
    assert record.source == ActionAvailabilitySource.PROVIDER_DIRECT
    assert record.state == ActionAvailabilityState.AVAILABLE
    assert record.metadata is not None
    assert record.metadata.name == "Alpha"
    assert record.metadata.provider_session_id == PROVIDER_SESSION_ID
    assert record.metadata.settings_schema == {"type": "object"}

    changed = _availability_message(
        message_type=ACTION_AVAILABILITY_CHANGED,
        body=ActionAvailabilityChangedBody(
            providerInstanceId="provider-alpha",
            providerId="provider.test",
            entries=(
                ActionAvailabilityEntry(
                    actionId="action.alpha",
                    status="unavailable",
                    reason="disabled",
                ),
            ),
        ),
    )

    assert await service.handle_availability_message(changed) == frozenset({key})
    record = service.cache.record_for(key)
    assert record is not None
    assert record.state == ActionAvailabilityState.UNAVAILABLE
    assert record.reason == "disabled"
    planning = service.planning_snapshot((_intent("action.alpha"),))
    assert planning.metadata == {}
    assert planning.unavailable == frozenset({_intent("action.alpha")})
    assert provider_sessions.valid.await_args_list == [
        call(
            provider_instance_id="provider-alpha",
            provider_id="provider.test",
            provider_session_id=PROVIDER_SESSION_ID,
        ),
        call(
            provider_instance_id="provider-alpha",
            provider_id="provider.test",
            provider_session_id=PROVIDER_SESSION_ID,
        ),
    ]


@pytest.mark.asyncio
async def test_service_ignores_provider_snapshot_without_valid_session():
    actions_bus = _actions_bus()
    provider_sessions = _provider_sessions_mock(valid=False)
    service = ActionAvailabilityService(
        controller_id=CONTROLLER_ID,
        controller_session_id=CONTROLLER_SESSION_ID,
        actions_bus=actions_bus,
        manager=MagicMock(),
        provider_sessions=provider_sessions,
        start_soon=None,
        provider_session_validation_wait_seconds=0.0,
    )
    key = ProviderActionKey("provider-alpha", "action.alpha")

    snapshot = _availability_message(
        message_type=ACTION_AVAILABILITY_SNAPSHOT,
        body=ActionAvailabilitySnapshotBody(
            providerInstanceId="provider-alpha",
            providerId="provider.test",
            entries=(
                ActionAvailabilityEntry(
                    actionId="action.alpha",
                    status="available",
                    descriptor=ActionDescriptor(actionId="action.alpha", name="Alpha"),
                ),
            ),
        ),
    )

    assert await service.handle_availability_message(snapshot) == frozenset()
    assert service.cache.record_for(key) is None
    provider_sessions.valid.assert_awaited_once_with(
        provider_instance_id="provider-alpha",
        provider_id="provider.test",
        provider_session_id=PROVIDER_SESSION_ID,
    )


@pytest.mark.asyncio
async def test_service_waits_for_provider_snapshot_session_validation():
    actions_bus = _actions_bus()
    provider_sessions = _provider_sessions_mock(valid=[False, True])
    service = ActionAvailabilityService(
        controller_id=CONTROLLER_ID,
        controller_session_id=CONTROLLER_SESSION_ID,
        actions_bus=actions_bus,
        manager=MagicMock(),
        provider_sessions=provider_sessions,
        start_soon=None,
        provider_session_validation_wait_seconds=0.2,
    )
    key = ProviderActionKey("provider-alpha", "action.alpha")

    snapshot = _availability_message(
        message_type=ACTION_AVAILABILITY_SNAPSHOT,
        body=ActionAvailabilitySnapshotBody(
            providerInstanceId="provider-alpha",
            providerId="provider.test",
            entries=(
                ActionAvailabilityEntry(
                    actionId="action.alpha",
                    status="available",
                    descriptor=ActionDescriptor(actionId="action.alpha", name="Alpha"),
                ),
            ),
        ),
    )

    assert await service.handle_availability_message(snapshot) == frozenset({key})
    record = service.cache.record_for(key)
    assert record is not None
    assert record.state == ActionAvailabilityState.AVAILABLE
    assert record.metadata is not None
    assert record.metadata.provider_session_id == PROVIDER_SESSION_ID
    assert provider_sessions.valid.await_count == 2


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


@pytest.mark.asyncio
async def test_service_reconcile_preserves_nonterminal_provider_session_unavailable():
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
    assert service.planning_snapshot((intent,)).metadata[intent] == metadata


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
