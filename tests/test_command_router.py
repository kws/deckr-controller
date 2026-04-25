"""Targeted tests for CommandRouter routing, overlay race safety, and DeviceOutput."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from deckr.hardware.events import HWSImageFormat
from deckr.plugin.rendering import TitleOptions

from deckr.controller import _persistence
from deckr.controller._command_router import CommandRouter, DeviceOutput
from deckr.controller._persistence import ControllerPersistence
from deckr.controller._render import RenderService
from deckr.controller._render_dispatcher import RenderDispatcher
from deckr.controller._state_store import (
    ControlStateStore,
    TransientOverlay,
)
from deckr.controller.settings import FileBackedSettingsService, SettingsTarget

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


# --- CommandRouter content updates ---


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
    store.content.title = "Back"
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
async def test_set_title_updates_current_content(router_with_mocks):
    """set_title updates the current render content."""
    router = router_with_mocks
    await router.set_title("State1Title")
    assert router._store.content.title == "State1Title"
    assert router._store.content.image is None


@pytest.mark.asyncio
async def test_set_title_applies_explicit_title_options(router_with_mocks):
    """set_title can update title styling alongside the text."""
    router = router_with_mocks
    title_options = TitleOptions(font_family="Audiowide", font_size="85vw")
    await router.set_title("Styled", title_options=title_options)
    assert router._store.content.title == "Styled"
    assert router._store.content.title_options == title_options


@pytest.mark.asyncio
async def test_set_title_without_title_options_clears_explicit_options(router_with_mocks):
    """A plain title update should fall back to binding defaults instead of reusing old explicit styles."""
    router = router_with_mocks
    await router.set_title("Styled", title_options=TitleOptions(font_family="Inter"))
    await router.set_title("Plain")
    assert router._store.content.title == "Plain"
    assert router._store.content.title_options is None


@pytest.mark.asyncio
async def test_set_image_replaces_title_content(router_with_mocks):
    """set_image replaces title content with an explicit image."""
    router = router_with_mocks
    await router.set_title("Styled", title_options=TitleOptions(font_family="Inter"))
    await router.set_image("https://example.com/img.png")
    assert router._store.content.image == "https://example.com/img.png"
    assert router._store.content.title is None


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

    class FakeSettingsService:
        def __init__(self):
            self.calls = 0

        async def exists(self, target):
            return True

        async def get(self, target):
            self.calls += 1
            return {"persisted": 42}

        async def merge(self, target, patch):
            return {"persisted": 42, **dict(patch)}

    settings_service = FakeSettingsService()
    target = SettingsTarget.for_context(
        controller_id="controller-main",
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
        settings_service=settings_service,
        settings_target=target,
    )

    settings = await router.get_settings()
    assert settings.default_only == "x"
    assert settings.persisted == 42
    assert settings_service.calls == 1

    # second read should not hit persistence again
    settings_again = await router.get_settings()
    assert settings_again.persisted == 42
    assert settings_service.calls == 1


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

    class FailingSettingsService:
        async def exists(self, target):
            return True

        async def get(self, target):
            return {}

        async def merge(self, target, patch):
            raise OSError("disk full")

    target = SettingsTarget.for_context(
        controller_id="controller-main",
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
        settings_service=FailingSettingsService(),
        settings_target=target,
    )

    with pytest.raises(OSError):
        await router.set_settings({"new": 2})

    assert store.settings == {"existing": 1}


@pytest.mark.asyncio
async def test_hydrate_settings_migrates_legacy_key_to_composite(monkeypatch, tmp_path):
    """When no composite row exists, hydrate reads legacy context_id key and migrates to composite then deletes legacy."""
    store = ControlStateStore(context_id="dev.slot0")
    store.settings = {"from_config": "a"}

    class TmpDirs:
        user_data_dir = str(tmp_path)

    monkeypatch.setattr(_persistence, "dirs", TmpDirs())
    legacy = ControllerPersistence("dev")
    legacy.set_value("dev.slot0", {"legacy_key": 100})

    target = SettingsTarget.for_context(
        controller_id="controller-main",
        device_id="dev",
        profile_id="default",
        page_id="0",
        slot_id="slot0",
        action_uuid="action",
        legacy_context_id="dev.slot0",
    )
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
        settings_service=FileBackedSettingsService(settings_dir=tmp_path),
        settings_target=target,
    )

    settings = await router.get_settings()
    assert settings.from_config == "a"
    assert settings.legacy_key == 100
    assert legacy.get_value("dev.slot0") is None
