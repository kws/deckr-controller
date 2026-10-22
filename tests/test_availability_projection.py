from __future__ import annotations

import logging
from unittest.mock import MagicMock

from deckr.action_runtime import (
    action_runtime_service_id,
)
from deckr.actions.messages import (
    ActionAvailabilityEntry,
    ActionDescriptor,
)

from deckr.controller._actions import (
    ActionAvailabilityPolicy,
    ActionAvailabilitySource,
    ActionAvailabilityState,
    ControllerActionService,
    ProviderActionKey,
)
from deckr.controller._actions._availability import ActionAvailabilityCache
from test_support.availability import (
    _availability_view,
    _intent,
    _metadata,
    _record_service_available,
)

CONTROLLER_ID = "controller-main"


CONTROLLER_SESSION_ID = "controller-session"


PROVIDER_SESSION_ID = "provider-session"


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
    assert (
        service.record_for_key(ProviderActionKey("provider-beta", "action.shared"))
        is not None
    )


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


def test_service_logs_unchanged_provider_snapshot(caplog):
    caplog.set_level(
        logging.DEBUG,
        logger="deckr.controller._actions",
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

    assert (
        service.ingest_provider_entries(
            provider_instance_id="provider-alpha",
            provider_id="provider.test",
            entries=(entry,),
        )
        == frozenset()
    )

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
