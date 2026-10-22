from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from deckr.controller._actions import (
    PROVIDER_SESSION_INVALID_REASON,
    ActionAvailabilityPolicy,
    ActionAvailabilityState,
    ActionUnavailableCause,
    ControllerActionService,
    ProviderActionKey,
    ProviderSessionKey,
    action_unavailable_cause,
    unavailable_overlay_template,
)
from deckr.controller._actions._availability import ActionAvailabilityCache
from test_support.availability import (
    _intent,
    _metadata,
    _record_service_available,
    _unavailable_record,
)

CONTROLLER_ID = "controller-main"


CONTROLLER_SESSION_ID = "controller-session"


PROVIDER_SESSION_ID = "provider-session"


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


def test_refreshed_provider_labels_replace_cached_labels():
    service = ControllerActionService(
        controller_id=CONTROLLER_ID,
        controller_session_id=CONTROLLER_SESSION_ID,
        manager=MagicMock(),
    )
    first = _metadata("action.labels", provider_labels={"room": "office"})
    _record_service_available(service, first)
    _record_service_available(
        service, _metadata("action.labels", provider_labels={"room": "kitchen"})
    )
    record = service.record_for_key(ProviderActionKey("provider-a", "action.labels"))
    assert record.metadata.provider_labels == {"room": "kitchen"}
    _record_service_available(service, _metadata("action.labels", provider_labels={}))
    assert service.record_for_key(record.key).metadata.provider_labels == {}


def test_cache_retention_protects_live_and_configured_actions_and_bounds_orphans():
    now = 10.0
    cache = ActionAvailabilityCache(
        clock=lambda: now,
        policy=ActionAvailabilityPolicy(
            unreferenced_retention_seconds=10, max_unreferenced_records=2
        ),
    )
    live = _metadata("live", provider_instance_id="live-provider")
    configured = _metadata("configured", provider_instance_id="configured-provider")
    cache.record_available(live, now=0)
    cache.record_available(configured, now=0)
    for i in range(4):
        metadata = _metadata(f"orphan-{i}", provider_instance_id=f"orphan-{i}")
        cache.record_available(metadata, now=9, intent=_intent(metadata.action_id))
    retained = frozenset(
        {
            ProviderSessionKey(
                live.provider_instance_id, live.provider_id, live.provider_session_id
            )
        }
    )
    interests = (_intent("configured", provider_instance_id="configured-provider"),)
    cache.prune(live_sessions=retained, interests=interests)
    assert len(cache.service_view_records()) == 4
    now = 30
    cache.prune(live_sessions=retained, interests=interests)
    assert {record.key.action_id for record in cache.service_view_records()} == {
        "live",
        "configured",
    }
    assert cache.intent_for_key(ProviderActionKey("orphan-0", "orphan-0")) is None
