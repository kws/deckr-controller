"""Tests for the controller settings service boundary."""

import pytest

from deckr.controller.settings import FileBackedSettingsService, SettingsTarget


@pytest.mark.asyncio
async def test_context_settings_round_trip_and_subscription(tmp_path):
    service = FileBackedSettingsService(settings_dir=tmp_path)
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
async def test_prune_context_targets_removes_stale_rows(tmp_path):
    service = FileBackedSettingsService(settings_dir=tmp_path)
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

    removed = await service.prune_context_targets(
        controller_id="controller-main",
        config_id="config-1",
        valid_keys={active.as_key()},
    )

    assert removed == 1
    assert await service.get(active) == {"name": "active"}
    assert await service.get(stale) == {}
