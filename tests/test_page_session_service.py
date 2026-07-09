"""Tests for controller page/session state decisions."""

from dataclasses import replace

import pytest
from deckr.actions.messages import (
    DynamicPageCommand,
    PageChildBindingDescriptor,
    PageChildBindingTarget,
)

from deckr.controller._actions import ActionMetadata
from deckr.controller._binding_planner import PagePlan
from deckr.controller._pages import (
    PageOwnerBinding,
    PageSessionService,
    StaticPageRef,
)
from deckr.controller.config._data import (
    Control,
    DeviceConfig,
    Page,
    Profile,
)


@pytest.fixture
def device_config():
    return DeviceConfig(
        id="dev1",
        name="Test Device",
        match={"fingerprint": "fingerprint-dev1"},
        profiles=[
            Profile(
                name="default",
                widget_timeout_ms=250,
                pages=[
                    Page(
                        widget_timeout_ms=100,
                        controls=[
                            Control(
                                selector={"control_id": "0,0"},
                                action="action.a",
                                settings={"x": 1},
                            ),
                            Control(
                                selector={"control_id": "0,1"},
                                action="action.b",
                                settings={},
                            ),
                        ],
                    ),
                    Page(
                        controls=[
                            Control(
                                selector={"control_id": "1,0"},
                                action="action.c",
                                settings={},
                            ),
                        ],
                    ),
                ],
            ),
        ],
    )


def _dynamic_page(page_id: str, *control_ids: str) -> DynamicPageCommand:
    return DynamicPageCommand(
        pageId=page_id,
        bindings=tuple(
            PageChildBindingDescriptor(
                controlId=control_id,
                target=PageChildBindingTarget(kind="self"),
                itemKey=f"item-{index}",
            )
            for index, control_id in enumerate(control_ids)
        ),
    )


def _plan(entry, *, page_session=None) -> PagePlan:
    if isinstance(entry, StaticPageRef):
        return PagePlan(
            entry=entry,
            profile_id=entry.profile_name,
            page_id=str(entry.page_index),
            page_session=None,
            bindings=(),
        )
    return PagePlan(
        entry=entry,
        profile_id="_dynamic",
        page_id=entry.page_id,
        page_session=page_session,
        bindings=(),
    )


def _owner() -> PageOwnerBinding:
    return PageOwnerBinding(
        context_id="owner-context",
        binding_id="owner-binding",
        control_id="0,0",
        action_uuid="action.owner",
        provider_instance_id="python",
        provider_id="test.provider",
        provider_session_id="provider-session",
        action_instance_id="owner-action-instance",
        profile_id="default",
        page_id="0",
        page_session_id=None,
        settings_target=None,
    )


def test_resolve_static_bindings_returns_control_bindings(device_config):
    service = PageSessionService(device_config, clock=lambda: 0.0)
    ref = StaticPageRef(profile_name="default", page_index=0)

    bindings = service.resolve_static_bindings(ref)

    assert len(bindings) == 2
    assert bindings[0].control_id == "0,0"
    assert bindings[0].action_uuid == "action.a"
    assert bindings[0].settings == {"x": 1}
    assert bindings[1].control_id == "0,1"
    assert bindings[1].action_uuid == "action.b"


def test_static_page_draft_commit_creates_root_frame(device_config):
    service = PageSessionService(device_config, clock=lambda: 0.0)

    draft = service.begin_set_page(profile="default", page=0)
    assert draft is not None
    plan = _plan(draft.entry, page_session=draft.page_session)

    effects = service.commit(draft, plan)

    snapshot = service.snapshot()
    assert snapshot.current_frame is not None
    assert snapshot.current_frame.entry == StaticPageRef("default", 0)
    assert snapshot.current_plan is plan
    assert effects.sessions_to_close == ()


def test_dynamic_open_creates_session_and_pushes_frame(device_config):
    service = PageSessionService(device_config, clock=lambda: 10.0)
    root = service.begin_set_page(profile="default", page=0)
    assert root is not None
    service.commit(root, _plan(root.entry))

    draft = service.begin_open_page(
        descriptor=_dynamic_page("dynamic-page", "1,0"),
        owner=_owner(),
    )
    assert draft is not None
    assert draft.page_session is not None
    dynamic_plan = _plan(draft.entry, page_session=draft.page_session)

    service.commit(draft, dynamic_plan)

    snapshot = service.snapshot()
    assert len(snapshot.frames) == 2
    assert snapshot.active_dynamic_session is draft.page_session
    assert snapshot.current_frame is not None
    assert snapshot.current_frame.committed_plan is dynamic_plan
    assert draft.page_session.timeout_ms == 100
    assert draft.page_session.last_activity == 10.0


def test_replace_preserves_session_identity_and_increments_on_commit(device_config):
    service = PageSessionService(device_config, clock=lambda: 0.0)
    root = service.begin_set_page(profile="default", page=0)
    assert root is not None
    service.commit(root, _plan(root.entry))
    opened = service.begin_open_page(
        descriptor=_dynamic_page("dynamic-page", "1,0"),
        owner=_owner(),
    )
    assert opened is not None
    session = opened.page_session
    assert session is not None
    service.commit(opened, _plan(opened.entry, page_session=session))

    replacement = service.begin_replace_page(
        descriptor=_dynamic_page("dynamic-page", "0,1"),
        context_id=session.context_id,
    )

    assert replacement is not None
    assert replacement.page_session is session
    assert replacement.page_session_generation == 1
    assert session.generation == 0

    service.commit(replacement, _plan(replacement.entry, page_session=session))

    assert service.active_dynamic_session() is session
    assert session.generation == 1


def test_close_restores_prior_frame_and_returns_closed_session_effects(device_config):
    service = PageSessionService(device_config, clock=lambda: 0.0)
    root = service.begin_set_page(profile="default", page=0)
    assert root is not None
    root_plan = _plan(root.entry)
    service.commit(root, root_plan)
    opened = service.begin_open_page(
        descriptor=_dynamic_page("dynamic-page", "1,0"),
        owner=_owner(),
    )
    assert opened is not None and opened.page_session is not None
    session = opened.page_session
    dynamic_plan = _plan(opened.entry, page_session=session)
    service.commit(opened, dynamic_plan)

    close = service.begin_close_page(context_id=session.context_id, reason="close")
    assert close is not None
    effects = service.commit(close, root_plan)

    snapshot = service.snapshot()
    assert snapshot.current_plan is root_plan
    assert snapshot.active_dynamic_session is None
    assert effects.sessions_to_close == (session,)
    assert effects.previous_dynamic_plans == (dynamic_plan,)
    assert effects.cleanup_reason == "close"


def test_invalid_dynamic_requests_are_noops(device_config):
    service = PageSessionService(device_config, clock=lambda: 0.0)

    assert service.begin_replace_page(
        descriptor=_dynamic_page("dynamic-page", "1,0"),
        context_id="missing",
    ) is None
    assert service.begin_close_page(context_id="missing") is None
    assert service.begin_set_page(
        descriptor=_dynamic_page("dynamic-page", "1,0"),
    ) is None


def test_open_dismisses_existing_dynamic_session(device_config):
    service = PageSessionService(device_config, clock=lambda: 0.0)
    root = service.begin_set_page(profile="default", page=0)
    assert root is not None
    root_plan = _plan(root.entry)
    service.commit(root, root_plan)
    first = service.begin_open_page(
        descriptor=_dynamic_page("first", "1,0"),
        owner=_owner(),
    )
    assert first is not None and first.page_session is not None
    first_plan = _plan(first.entry, page_session=first.page_session)
    service.commit(first, first_plan)

    second = service.begin_open_page(
        descriptor=_dynamic_page("second", "0,1"),
        owner=replace(
            _owner(),
            context_id=first.page_session.context_id,
            page_session_id=first.page_session.page_session_id,
            action_instance_id="child-action-instance",
            binding_id="child-binding",
        ),
    )
    assert second is not None and second.page_session is not None
    assert second.sessions_to_close == (first.page_session,)
    assert second.previous_dynamic_plans == (first_plan,)
    second_plan = _plan(second.entry, page_session=second.page_session)

    effects = service.commit(second, second_plan)

    snapshot = service.snapshot()
    assert len(snapshot.frames) == 2
    assert snapshot.frames[0].page_session is None
    assert snapshot.frames[0].committed_plan is root_plan
    assert snapshot.frames[1].committed_plan is second_plan
    assert snapshot.active_dynamic_session is second.page_session
    assert effects.sessions_to_close == (first.page_session,)
    assert effects.previous_dynamic_plans == (first_plan,)
    assert effects.cleanup_reason == "open_page"


def test_clear_returns_dynamic_sessions_and_finalized_plans(device_config):
    service = PageSessionService(device_config, clock=lambda: 0.0)
    root = service.begin_set_page(profile="default", page=0)
    assert root is not None
    service.commit(root, _plan(root.entry))
    opened = service.begin_open_page(
        descriptor=_dynamic_page("dynamic-page", "1,0"),
        owner=_owner(),
    )
    assert opened is not None and opened.page_session is not None
    dynamic_plan = _plan(opened.entry, page_session=opened.page_session)
    service.commit(opened, dynamic_plan)

    effects = service.clear(reason="clear")

    assert service.snapshot().frames == ()
    assert effects.sessions_to_close == (opened.page_session,)
    assert effects.previous_dynamic_plans == (dynamic_plan,)
    assert effects.cleanup_reason == "clear"


def test_config_inactive_blocks_transitions_and_update_reactivates(device_config):
    service = PageSessionService(device_config, clock=lambda: 0.0)

    service.mark_config_inactive()

    assert service.begin_set_page(profile="default", page=0) is None
    assert not service.snapshot().config_active

    draft = service.update_config(device_config)

    assert service.snapshot().config_active
    assert draft.entry == StaticPageRef("default", 0)


def test_timeout_detection_and_activity_reset_use_clock(device_config):
    now = 0.0

    def clock() -> float:
        return now

    service = PageSessionService(device_config, clock=clock)
    root = service.begin_set_page(profile="default", page=0)
    assert root is not None
    service.commit(root, _plan(root.entry))
    opened = service.begin_open_page(
        descriptor=_dynamic_page("dynamic-page", "1,0"),
        owner=_owner(),
    )
    assert opened is not None and opened.page_session is not None
    session = opened.page_session
    service.commit(opened, _plan(opened.entry, page_session=session))

    now = 0.099
    assert service.expired_session() is None
    now = 0.100
    assert service.expired_session() is session

    now = 0.150
    service.record_activity()
    assert session.last_activity == 0.150
    now = 0.249
    assert service.expired_session() is None


def test_move_owner_provider_session_updates_active_session(device_config):
    service = PageSessionService(device_config, clock=lambda: 0.0)
    root = service.begin_set_page(profile="default", page=0)
    assert root is not None
    service.commit(root, _plan(root.entry))
    opened = service.begin_open_page(
        descriptor=_dynamic_page("dynamic-page", "1,0"),
        owner=_owner(),
    )
    assert opened is not None and opened.page_session is not None
    session = opened.page_session
    service.commit(opened, _plan(opened.entry, page_session=session))

    moved = service.move_owner_provider_session(
        ActionMetadata(
            uuid="action.owner",
            provider_instance_id="python",
            provider_id="test.provider",
            provider_session_id="successor",
        )
    )

    assert moved
    assert session.owner_provider_session_id == "successor"
