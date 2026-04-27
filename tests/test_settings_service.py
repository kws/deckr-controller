"""Tests for the controller settings service boundary."""

import pytest
from deckr.contracts.models import freeze_json

from deckr.controller.settings import InMemorySettingsService, SettingsTarget


@pytest.mark.asyncio
async def test_context_settings_round_trip_and_subscription():
    service = InMemorySettingsService()
    target = SettingsTarget.for_context(
        controller_id="controller-main",
        config_id="config-1",
        profile_id="default",
        page_id="0",
        slot_id="0,0",
        action_uuid="action.a",
    )

    stream = service.subscribe(target)
    first = await anext(stream)
    assert first == {}

    merged = await service.merge(target, {"volume": 50})
    assert merged == {"volume": 50}
    assert await service.get(target) == {"volume": 50}

    second = await anext(stream)
    assert second == {"volume": 50}
    await stream.aclose()


@pytest.mark.asyncio
async def test_clear_config_targets_removes_runtime_overlays():
    service = InMemorySettingsService()
    active = SettingsTarget.for_context(
        controller_id="controller-main",
        config_id="config-1",
        profile_id="default",
        page_id="0",
        slot_id="0,0",
        action_uuid="action.a",
    )
    stale = SettingsTarget.for_context(
        controller_id="controller-main",
        config_id="config-1",
        profile_id="default",
        page_id="1",
        slot_id="0,0",
        action_uuid="action.a",
    )

    await service.merge(active, {"name": "active"})
    await service.merge(stale, {"name": "stale"})

    removed = await service.clear_config_targets(
        controller_id="controller-main",
        config_id="config-1"
    )

    assert removed == 2
    assert await service.get(active) == {}
    assert await service.get(stale) == {}


@pytest.mark.asyncio
async def test_runtime_overlays_do_not_survive_new_service():
    target = SettingsTarget.for_context(
        controller_id="controller-main",
        config_id="config-1",
        profile_id="default",
        page_id="0",
        slot_id="0,0",
        action_uuid="action.a",
    )
    service = InMemorySettingsService()
    await service.merge(target, {"mode": "date"})

    restarted_service = InMemorySettingsService()

    assert await restarted_service.get(target) == {}


@pytest.mark.asyncio
async def test_merge_thaws_frozen_nested_settings():
    service = InMemorySettingsService()
    target = SettingsTarget.for_context(
        controller_id="controller-main",
        config_id="config-1",
        profile_id="default",
        page_id="0",
        slot_id="0,0",
        action_uuid="action.a",
    )

    frozen = freeze_json({"pager": {"current_page": {"page_type": "browse"}}})
    merged = await service.merge(target, dict(frozen))

    assert merged == {"pager": {"current_page": {"page_type": "browse"}}}
    assert await service.get(target) == merged
