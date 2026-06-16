from __future__ import annotations

from deckr.controller._action_availability import (
    ActionAvailabilityCache,
    ActionAvailabilityPolicy,
    ActionAvailabilitySource,
    ActionAvailabilityState,
    ProviderActionKey,
)
from deckr.controller._binding_planner import ActionIntentKey
from deckr.controller.action_provider.provider import ActionMetadata


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
    assert cache.snapshot_for_intents((intent,), now=112.0) == {intent: metadata}
    assert cache.state_for(key, now=116.0) == ActionAvailabilityState.EXPIRED
    assert cache.snapshot_for_intents((intent,), now=116.0) == {}


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
