"""Targeted tests for CommandRouter state routing, overlay race safety, and DeviceOutput."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from deckr.hardware.events import HWSImageFormat

from deckr.controller._command_router import CommandRouter, DeviceOutput
from deckr.controller._persistence import PersistenceKey
from deckr.controller._render import RenderService
from deckr.controller._render_dispatcher import RenderDispatcher
from deckr.controller._state_store import (
    ControlStateStore,
    StateOverride,
    TransientOverlay,
)

# --- DeviceOutput: last_frame tracking ---


@pytest.mark.asyncio
async def test_device_output_records_last_frame():
    """DeviceOutput records last written frame and clears it on clear()."""
    device = AsyncMock()
    device.set_image = AsyncMock()
    device.clear_slot = AsyncMock()

    output = DeviceOutput(device, "0,0")
    assert output.last_frame is None

    await output.write(b"frame1")
    assert output.last_frame == b"frame1"
    device.set_image.assert_called_once_with("0,0", b"frame1")

    await output.write(b"frame2")
    assert output.last_frame == b"frame2"

    await output.clear()
    assert output.last_frame is None
    device.clear_slot.assert_called_once_with("0,0")


# --- CommandRouter: Elgato-compatible state targeting ---


@pytest_asyncio.fixture
def router_with_mocks():
    """CommandRouter with mock dispatcher and render service."""
    store = ControlStateStore(context_id="dev.slot0")
    store.settings = {}

    render_service = MagicMock(spec=RenderService)
    render_service.build_request = MagicMock(return_value=object())
    render_dispatcher = MagicMock(spec=RenderDispatcher)
    render_dispatcher.submit_request = AsyncMock()

    output = DeviceOutput(AsyncMock(), "0,0")
    image_format = HWSImageFormat(width=72, height=72)

    def no_start_soon(*args, **kwargs):
        pass  # don't run overlay expiry in tests

    router = CommandRouter(
        store=store,
        render_service=render_service,
        render_dispatcher=render_dispatcher,
        output=output,
        image_format=image_format,
        start_soon=no_start_soon,
    )
    return router


@pytest.mark.asyncio
async def test_render_no_op_when_image_format_none():
    """When image_format is None (non-image control), _render does not write to output."""
    store = ControlStateStore(context_id="dev.B1")
    store.settings = {}
    store.overrides[0] = StateOverride(title="Back")
    render_service = MagicMock(spec=RenderService)
    render_service.build_request = MagicMock(return_value=object())
    render_dispatcher = MagicMock(spec=RenderDispatcher)
    render_dispatcher.submit_request = AsyncMock()
    device = AsyncMock()
    output = DeviceOutput(device, "B1")
    router = CommandRouter(
        store=store,
        render_service=render_service,
        render_dispatcher=render_dispatcher,
        output=output,
        image_format=None,
        start_soon=lambda *args, **kwargs: None,
    )
    await router.set_title("Back")
    assert output.last_frame is None
    device.set_image.assert_not_called()
    render_dispatcher.submit_request.assert_not_called()


@pytest.mark.asyncio
async def test_set_title_uses_current_state_index_when_state_omitted(router_with_mocks):
    """When state is omitted, set_title targets current state_index (Elgato default)."""
    router = router_with_mocks
    router._store.state_index = 1
    await router.set_title("State1Title")
    assert (
        router._store.overrides.get(0) is None
        or router._store.overrides[0].title is None
    )
    assert router._store.overrides.get(1) is not None
    assert router._store.overrides[1].title == "State1Title"


@pytest.mark.asyncio
async def test_set_title_respects_explicit_state(router_with_mocks):
    """Explicit state parameter targets that state (Elgato setTitle(state=n))."""
    router = router_with_mocks
    router._store.state_index = 0
    await router.set_title("ForState2", state=2)
    assert router._store.overrides.get(2) is not None
    assert router._store.overrides[2].title == "ForState2"
    # state 0 unchanged
    assert (
        router._store.overrides.get(0) is None
        or router._store.overrides[0].title is None
    )


@pytest.mark.asyncio
async def test_set_image_respects_explicit_state(router_with_mocks):
    """Explicit state parameter for set_image targets that state."""
    router = router_with_mocks
    await router.set_image("https://example.com/img.png", state=1)
    assert router._store.overrides.get(1) is not None
    assert router._store.overrides[1].image == "https://example.com/img.png"


@pytest.mark.asyncio
async def test_render_enqueues_request_without_waiting_for_device_write(
    router_with_mocks,
):
    """Render-affecting commands should enqueue background work instead of writing inline."""
    router = router_with_mocks

    await router.set_title("Queued")

    router._render_service.build_request.assert_called_once()
    router._render_dispatcher.submit_request.assert_awaited_once()
    assert router._output.last_frame is None


# --- Overlay expiry: token prevents stale expiry from clearing newer overlay ---


@pytest.mark.asyncio
async def test_overlay_expiry_respects_token(router_with_mocks):
    """Expiring with an old token does not clear overlay; only matching token clears."""
    router = router_with_mocks
    router._overlay_token = 2
    router._store.overlay = TransientOverlay(type="ok", expires_at=999.0)

    with patch("deckr.controller._command_router.anyio.sleep", new_callable=AsyncMock):
        await router._expire_overlay(1)
    assert router._store.overlay is not None

    with patch("deckr.controller._command_router.anyio.sleep", new_callable=AsyncMock):
        await router._expire_overlay(2)
    assert router._store.overlay is None


@pytest.mark.asyncio
async def test_get_settings_hydrates_from_persistence():
    store = ControlStateStore(context_id="dev.slot0")
    store.settings = {"default_only": "x"}

    render_service = MagicMock(spec=RenderService)
    render_service.build_request = MagicMock(return_value=object())
    render_dispatcher = MagicMock(spec=RenderDispatcher)
    render_dispatcher.submit_request = AsyncMock()
    output = DeviceOutput(AsyncMock(), "0,0")
    image_format = HWSImageFormat(width=72, height=72)

    class FakePersistence:
        def __init__(self):
            self.calls = 0

        def get_settings(self, key):
            self.calls += 1
            return {"persisted": 42}

        def get_value(self, _legacy_key):
            return None

        def set_settings(self, key, value):
            pass

        def delete_value(self, _legacy_key):
            return 0

    persistence = FakePersistence()
    key = PersistenceKey(
        device_id="dev",
        profile_id="default",
        page_id="0",
        slot_id="0,0",
        action_uuid="action",
    )

    router = CommandRouter(
        store=store,
        render_service=render_service,
        render_dispatcher=render_dispatcher,
        output=output,
        image_format=image_format,
        start_soon=lambda *args, **kwargs: None,
        persistence=persistence,
        persistence_key=key,
    )

    settings = await router.get_settings()
    assert settings.default_only == "x"
    assert settings.persisted == 42
    assert persistence.calls == 1

    # second read should not hit persistence again
    settings_again = await router.get_settings()
    assert settings_again.persisted == 42
    assert persistence.calls == 1


@pytest.mark.asyncio
async def test_set_settings_fail_fast_does_not_mutate_store():
    store = ControlStateStore(context_id="dev.slot0")
    store.settings = {"existing": 1}

    render_service = MagicMock(spec=RenderService)
    render_service.build_request = MagicMock(return_value=object())
    render_dispatcher = MagicMock(spec=RenderDispatcher)
    render_dispatcher.submit_request = AsyncMock()
    output = DeviceOutput(AsyncMock(), "0,0")
    image_format = HWSImageFormat(width=72, height=72)

    class FailingPersistence:
        def get_settings(self, key):
            return None

        def get_value(self, _legacy_key):
            return None

        def set_settings(self, key, value):
            raise OSError("disk full")

        def delete_value(self, _legacy_key):
            return 0

    key = PersistenceKey(
        device_id="dev",
        profile_id="default",
        page_id="0",
        slot_id="0,0",
        action_uuid="action",
    )
    router = CommandRouter(
        store=store,
        render_service=render_service,
        render_dispatcher=render_dispatcher,
        output=output,
        image_format=image_format,
        start_soon=lambda *args, **kwargs: None,
        persistence=FailingPersistence(),
        persistence_key=key,
    )

    with pytest.raises(OSError):
        await router.set_settings({"new": 2})

    assert store.settings == {"existing": 1}


@pytest.mark.asyncio
async def test_hydrate_settings_migrates_legacy_key_to_composite():
    """When no composite row exists, hydrate reads legacy context_id key and migrates to composite then deletes legacy."""
    store = ControlStateStore(context_id="dev.slot0")
    store.settings = {"from_config": "a"}

    migrated_to = []
    legacy_deleted = []

    class LegacyMigrationPersistence:
        def get_settings(self, key):
            return None

        def get_value(self, legacy_key):
            if legacy_key == "dev.slot0":
                return {"legacy_key": 100}
            return None

        def set_settings(self, key, value):
            migrated_to.append((key.as_key(), value))

        def delete_value(self, legacy_key):
            legacy_deleted.append(legacy_key)
            return 1

    key = PersistenceKey(
        device_id="dev",
        profile_id="default",
        page_id="0",
        slot_id="slot0",
        action_uuid="action",
    )
    persistence = LegacyMigrationPersistence()
    render_service = MagicMock(spec=RenderService)
    render_service.build_request = MagicMock(return_value=object())
    render_dispatcher = MagicMock(spec=RenderDispatcher)
    render_dispatcher.submit_request = AsyncMock()
    output = DeviceOutput(AsyncMock(), "0,0")
    image_format = HWSImageFormat(width=72, height=72)

    router = CommandRouter(
        store=store,
        render_service=render_service,
        render_dispatcher=render_dispatcher,
        output=output,
        image_format=image_format,
        start_soon=lambda *args, **kwargs: None,
        persistence=persistence,
        persistence_key=key,
    )

    settings = await router.get_settings()
    assert settings.from_config == "a"
    assert settings.legacy_key == 100
    assert len(migrated_to) == 1
    assert migrated_to[0][1] == {
        "legacy_key": 100
    }  # migration writes legacy payload to composite key
    assert legacy_deleted == ["dev.slot0"]
