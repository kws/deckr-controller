from __future__ import annotations

from collections.abc import Mapping

import anyio
import pytest
from conftest import LaneHarness
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
from deckr.contracts.messages import ACTIONS_LANE, DeckrMessage, controller_address

from deckr.controller._action_availability import (
    ActionAvailabilityCache,
    ActionAvailabilityPolicy,
    ActionAvailabilityService,
    ActionAvailabilitySource,
    ActionAvailabilityState,
    ProviderActionKey,
)
from deckr.controller._action_interest import (
    ActionInterestRecord,
    ActionInterestSnapshot,
    ActionInterestSource,
    ActionInterestStrength,
)
from deckr.controller._binding_planner import ActionIntentKey
from deckr.controller.action_provider.provider import ActionMetadata

CONTROLLER_ID = "controller-main"


def _metadata(
    action_uuid: str,
    *,
    provider_instance_id: str = "provider-a",
    provider_id: str = "provider",
    provider_labels: dict[str, str] | None = None,
) -> ActionMetadata:
    return ActionMetadata(
        uuid=action_uuid,
        provider_instance_id=provider_instance_id,
        provider_id=provider_id,
        provider_labels=provider_labels,
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


class _FakeActionProviderManager:
    def __init__(
        self,
        *actions: ActionMetadata,
        provider_sessions: Mapping[str, str | None] | None = None,
    ) -> None:
        self._actions = {
            (action.provider_instance_id, action.uuid): action for action in actions
        }
        self._provider_sessions = dict(provider_sessions or {})

    async def get_action(
        self,
        uuid: str,
        *,
        provider_instance_id: str | None = None,
        provider_labels: Mapping[str, str] | None = None,
    ) -> ActionMetadata | None:
        labels = provider_labels or {}
        for action in self._actions.values():
            if action.uuid != uuid:
                continue
            if (
                provider_instance_id is not None
                and action.provider_instance_id != provider_instance_id
            ):
                continue
            action_labels = action.provider_labels or {}
            if all(action_labels.get(key) == value for key, value in labels.items()):
                return action
        return None

    def provider_instance_provides_provider(
        self,
        provider_instance_id: str,
        provider_id: str,
    ) -> bool:
        return any(
            action.provider_instance_id == provider_instance_id
            and action.provider_id == provider_id
            for action in self._actions.values()
        )

    def provider_session_id(self, provider_instance_id: str) -> str | None:
        return self._provider_sessions.get(provider_instance_id)


async def _next_action_message(stream, *, timeout: float = 1.0) -> DeckrMessage:
    with anyio.fail_after(timeout):
        return await anext(stream)


def test_records_are_keyed_by_provider_instance_and_action_id():
    cache = ActionAvailabilityCache()
    alpha = _metadata("action.same", provider_instance_id="provider-alpha")
    beta = _metadata("action.same", provider_instance_id="provider-beta")

    cache.record_available(alpha, now=0.0)
    cache.record_available(beta, now=1.0)

    alpha_record = cache.record_for(
        ProviderActionKey("provider-alpha", "action.same")
    )
    beta_record = cache.record_for(ProviderActionKey("provider-beta", "action.same"))
    assert alpha_record is not None
    assert beta_record is not None
    assert alpha_record.metadata is alpha
    assert beta_record.metadata is beta


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


def test_provider_label_intent_uses_required_subset_matching():
    cache = ActionAvailabilityCache()
    office = _metadata(
        "action.labelled",
        provider_instance_id="provider-office",
        provider_labels={"room": "office", "site": "hq"},
    )
    kitchen = _metadata(
        "action.labelled",
        provider_instance_id="provider-kitchen",
        provider_labels={"room": "kitchen", "site": "hq"},
    )
    matching_intent = _intent(
        "action.labelled",
        provider_labels={"room": "office"},
    )
    mismatching_intent = _intent(
        "action.labelled",
        provider_labels={"room": "lab"},
    )

    cache.record_available(kitchen, now=0.0)
    cache.record_available(office, now=0.0)
    snapshot = cache.snapshot_for_intents(
        (matching_intent, mismatching_intent),
        now=0.0,
    )

    assert snapshot == {matching_intent: office}


def test_unqualified_intent_selects_lexicographically_first_provider_instance():
    cache = ActionAvailabilityCache()
    alpha = _metadata("action.same", provider_instance_id="provider-alpha")
    beta = _metadata("action.same", provider_instance_id="provider-beta")
    intent = _intent("action.same")

    cache.record_available(beta, now=0.0)
    cache.record_available(alpha, now=0.0)
    snapshot = cache.snapshot_for_intents((intent,), now=0.0)

    assert snapshot == {intent: alpha}


def test_beacon_candidate_metadata_records_unknown_but_omits_snapshot_entry():
    cache = ActionAvailabilityCache()
    metadata = _metadata("action.available", provider_instance_id="provider-alpha")
    intent = _intent(
        "action.available",
        provider_instance_id="provider-alpha",
    )

    record = cache.record_candidate(metadata, now=0.0)
    snapshot = cache.snapshot_for_intents((intent,), now=0.0)

    assert record.state == ActionAvailabilityState.UNKNOWN
    assert record.source == ActionAvailabilitySource.BEACON_CANDIDATE
    assert snapshot == {}


def test_probing_candidate_metadata_records_probing_state():
    cache = ActionAvailabilityCache()
    metadata = _metadata("action.probing", provider_instance_id="provider-alpha")
    key = ProviderActionKey("provider-alpha", "action.probing")

    record = cache.record_candidate(
        metadata,
        now=0.0,
        state=ActionAvailabilityState.PROBING,
    )

    assert record.state == ActionAvailabilityState.PROBING
    assert cache.state_for(key, now=0.0) == ActionAvailabilityState.PROBING


def test_provider_direct_available_metadata_converts_to_planner_snapshot_entry():
    cache = ActionAvailabilityCache()
    metadata = _metadata("action.available", provider_instance_id="provider-alpha")
    intent = _intent(
        "action.available",
        provider_instance_id="provider-alpha",
    )

    record = cache.record_available(metadata, now=0.0)
    snapshot = cache.snapshot_for_intents((intent,), now=0.0)

    assert record.state == ActionAvailabilityState.AVAILABLE
    assert record.source == ActionAvailabilitySource.PROVIDER_DIRECT
    assert snapshot == {intent: metadata}


def test_candidate_refresh_does_not_downgrade_provider_direct_record():
    cache = ActionAvailabilityCache()
    authoritative = _metadata(
        "action.available",
        provider_instance_id="provider-alpha",
        provider_id="provider-direct",
    )
    candidate = _metadata(
        "action.available",
        provider_instance_id="provider-alpha",
        provider_id="beacon-provider",
    )
    key = ProviderActionKey("provider-alpha", "action.available")
    intent = _intent(
        "action.available",
        provider_instance_id="provider-alpha",
    )

    cache.record_available(authoritative, now=0.0, intent=intent)
    cache.record_candidate(candidate, now=1.0, intent=intent)

    assert cache.record_for(key).metadata is authoritative
    assert cache.state_for(key, now=1.0) == ActionAvailabilityState.AVAILABLE
    assert cache.snapshot_for_intents((intent,), now=1.0) == {
        intent: authoritative
    }


def test_missing_records_produce_no_snapshot_metadata():
    cache = ActionAvailabilityCache()
    intent = _intent("action.missing")

    assert cache.snapshot_for_intents((intent,), now=0.0) == {}


def test_finite_ttl_policy_omits_expired_records():
    cache = ActionAvailabilityCache(
        policy=ActionAvailabilityPolicy(
            fresh_ttl_seconds=10.0,
            stale_grace_seconds=None,
        )
    )
    metadata = _metadata("action.expiring", provider_instance_id="provider-alpha")
    key = ProviderActionKey("provider-alpha", "action.expiring")
    intent = _intent(
        "action.expiring",
        provider_instance_id="provider-alpha",
    )

    cache.record_available(metadata, now=100.0)

    assert cache.state_for(key, now=111.0) == ActionAvailabilityState.EXPIRED
    assert cache.snapshot_for_intents((intent,), now=111.0) == {}


def test_stale_grace_policy_keeps_stale_but_grace_valid_metadata():
    cache = ActionAvailabilityCache(
        policy=ActionAvailabilityPolicy(
            fresh_ttl_seconds=10.0,
            stale_grace_seconds=5.0,
        )
    )
    metadata = _metadata("action.stale", provider_instance_id="provider-alpha")
    key = ProviderActionKey("provider-alpha", "action.stale")
    intent = _intent(
        "action.stale",
        provider_instance_id="provider-alpha",
    )

    cache.record_available(metadata, now=100.0)

    assert cache.state_for(key, now=112.0) == ActionAvailabilityState.STALE
    assert cache.snapshot_for_intents((intent,), now=112.0) == {}
    assert cache.snapshot_for_intents(
        (intent,),
        now=112.0,
        stale_provider_keys=(key,),
    ) == {intent: metadata}
    assert cache.state_for(key, now=116.0) == ActionAvailabilityState.EXPIRED
    assert cache.snapshot_for_intents(
        (intent,),
        now=116.0,
        stale_provider_keys=(key,),
    ) == {}


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


def test_snapshot_output_keys_are_original_requested_intents():
    cache = ActionAvailabilityCache()
    metadata = _metadata(
        "action.labelled",
        provider_instance_id="provider-alpha",
        provider_labels={"room": "office", "site": "hq"},
    )
    original_intent = ActionIntentKey(
        action_uuid="action.labelled",
        provider_instance_id=None,
        provider_labels=(("room", "office"),),
    )

    cache.record_available(metadata, now=0.0)
    snapshot = cache.snapshot_for_intents((original_intent,), now=0.0)

    assert list(snapshot) == [original_intent]
    assert snapshot[original_intent] is metadata


@pytest.mark.asyncio
async def test_service_flushes_interest_and_availability_request_to_candidate_provider():
    action_bus = LaneHarness(
        ACTIONS_LANE,
        default_endpoint=controller_address(CONTROLLER_ID),
    )
    provider_address = action_provider_address("provider-alpha")
    provider_session_id = action_bus.endpoint(provider_address).session_id
    metadata = _metadata(
        "action.alpha",
        provider_instance_id="provider-alpha",
        provider_id="provider.test",
    )
    manager = _FakeActionProviderManager(
        metadata,
        provider_sessions={"provider-alpha": provider_session_id},
    )
    service = ActionAvailabilityService(
        controller_id=CONTROLLER_ID,
        controller_session_id=action_bus.session_id,
        actions_bus=action_bus.endpoint().session,
        manager=manager,
        start_soon=None,
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

    async with action_bus.subscribe(provider_address) as stream:
        await service.flush_interest(force_requests=True)
        interest = await _next_action_message(stream)
        request = await _next_action_message(stream)

    assert interest.message_type == ACTION_INTEREST_UPDATE
    assert interest.recipient_session_id == provider_session_id
    assert interest.subject == action_provider_instance_subject(
        "provider-alpha",
        provider_id="provider.test",
    )
    interest_body = ActionInterestUpdateBody.model_validate(interest.body)
    assert interest_body.provider_instance_id == "provider-alpha"
    assert interest_body.provider_id == "provider.test"
    assert [(entry.action_id, entry.level) for entry in interest_body.entries] == [
        ("action.alpha", "strong")
    ]

    assert request.message_type == ACTION_AVAILABILITY_REQUEST
    assert request.recipient_session_id == provider_session_id
    assert request.subject == action_provider_instance_subject(
        "provider-alpha",
        provider_id="provider.test",
    )
    request_body = ActionAvailabilityRequestBody.model_validate(request.body)
    assert [selector.action_id for selector in request_body.selectors] == [
        "action.alpha"
    ]


@pytest.mark.asyncio
async def test_service_ingests_provider_snapshot_and_change_messages():
    action_bus = LaneHarness(
        ACTIONS_LANE,
        default_endpoint=controller_address(CONTROLLER_ID),
    )
    provider_address = action_provider_address("provider-alpha")
    provider_endpoint = action_bus.endpoint(provider_address)
    manager = _FakeActionProviderManager(
        provider_sessions={"provider-alpha": provider_endpoint.session_id}
    )
    service = ActionAvailabilityService(
        controller_id=CONTROLLER_ID,
        controller_session_id=action_bus.session_id,
        actions_bus=action_bus.endpoint().session,
        manager=manager,
        start_soon=None,
    )
    key = ProviderActionKey("provider-alpha", "action.alpha")

    snapshot = await provider_endpoint.send(
        lane=ACTIONS_LANE,
        recipient=controller_address(CONTROLLER_ID),
        subject=action_provider_instance_subject(
            "provider-alpha",
            provider_id="provider.test",
        ),
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
        ).to_dict(),
    )

    assert await service.handle_availability_message(snapshot) == frozenset({key})
    record = service.cache.record_for(key)
    assert record is not None
    assert record.source == ActionAvailabilitySource.PROVIDER_DIRECT
    assert record.state == ActionAvailabilityState.AVAILABLE
    assert record.metadata is not None
    assert record.metadata.name == "Alpha"
    assert record.metadata.settings_schema == {"type": "object"}

    changed = await provider_endpoint.send(
        lane=ACTIONS_LANE,
        recipient=controller_address(CONTROLLER_ID),
        subject=action_provider_instance_subject(
            "provider-alpha",
            provider_id="provider.test",
        ),
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
        ).to_dict(),
    )

    assert await service.handle_availability_message(changed) == frozenset({key})
    record = service.cache.record_for(key)
    assert record is not None
    assert record.state == ActionAvailabilityState.UNAVAILABLE
    assert record.reason == "disabled"
    planning = service.planning_snapshot((_intent("action.alpha"),))
    assert planning.metadata == {}
    assert planning.unavailable == frozenset({_intent("action.alpha")})


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
    action_bus = LaneHarness(
        ACTIONS_LANE,
        default_endpoint=controller_address(CONTROLLER_ID),
    )
    service = ActionAvailabilityService(
        controller_id=CONTROLLER_ID,
        controller_session_id=action_bus.session_id,
        actions_bus=action_bus.endpoint().session,
        manager=_FakeActionProviderManager(metadata),
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
