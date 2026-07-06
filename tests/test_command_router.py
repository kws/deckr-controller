"""Targeted tests for CommandRouter routing and DeviceOutput."""

from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from deckr.actions.messages import SettingsSnapshot, SettingsTargetRef

from deckr.controller._command_router import (
    CommandRouter,
    DeviceOutput,
)
from deckr.controller._device_layout import RasterImageFormat
from deckr.controller._render import RenderService
from deckr.controller._render_dispatcher import RenderDispatcher
from deckr.controller._state_store import ControlStateStore


class FakeHardwareCommandService:
    def __init__(self):
        self.set_raster_frame = AsyncMock()
        self.clear_raster = AsyncMock()


def _make_output(
    control_id: str = "0,0",
    *,
    capability_id: str = "raster.bitmap",
    config_id: str = "config-dev",
    command_service: FakeHardwareCommandService | None = None,
) -> DeviceOutput:
    return DeviceOutput(
        command_service or FakeHardwareCommandService(),
        config_id,
        control_id,
        capability_id,
    )


# --- DeviceOutput: last_frame tracking ---


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

    output = _make_output()
    image_format = RasterImageFormat(width=72, height=72)

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
    command_service = FakeHardwareCommandService()
    output = _make_output("B1", command_service=command_service)
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
    command_service.set_raster_frame.assert_not_called()
    render_dispatcher.submit_request.assert_not_called()


@pytest.mark.asyncio
async def test_set_raster_image_replaces_title_content(router_with_mocks):
    """set_raster_image replaces title content with an explicit raster image."""
    router = router_with_mocks
    await router.set_title("Styled")
    await router.set_raster_image("https://example.com/img.png")
    assert router._store.content.image == "https://example.com/img.png"
    assert router._store.content.title is None


@pytest.mark.asyncio
async def test_binding_overlay_clear_restores_base_state(router_with_mocks):
    router = router_with_mocks
    await router.set_title("Album", generation=3)
    await router.show_overlay(
        template="ok",
        title="OK",
        params={},
        duration_seconds=None,
        overlay_id="playback-ok",
        generation=1,
        binding_output_generation=3,
    )

    ok = await router.clear_overlay(
        overlay_id="playback-ok",
        generation=1,
        binding_output_generation=3,
    )

    assert ok is True
    assert router._store.content.title == "Album"
    assert router._store.overlay is None
    assert router._render_dispatcher.submit_request.await_count == 3


@pytest.mark.asyncio
async def test_binding_overlay_unknown_template_uses_unknown_fallback():
    store = ControlStateStore(context_id="dev.slot0")
    render_service = MagicMock(spec=RenderService)
    render_service.build_request = MagicMock(return_value=object())
    render_dispatcher = MagicMock(spec=RenderDispatcher)
    render_dispatcher.submit_request = AsyncMock()
    started = []

    router = CommandRouter(
        store=store,
        render_service=render_service,
        render_dispatcher=render_dispatcher,
        output=_make_output(),
        image_format=RasterImageFormat(width=72, height=72),
        start_soon=lambda func, *args: started.append((func, args)),
    )

    await router.show_overlay(
        template="surprise",
        title=None,
        params={},
        duration_seconds=None,
        overlay_id=None,
        generation=1,
        binding_output_generation=0,
    )

    assert store.overlay is not None
    assert store.overlay.template == "unknown"
    assert started[0][1][-1] == 2.0


@pytest.mark.asyncio
async def test_pending_overlay_is_persistent_until_replaced_or_cleared():
    store = ControlStateStore(context_id="dev.slot0")
    render_service = MagicMock(spec=RenderService)
    render_service.build_request = MagicMock(return_value=object())
    render_dispatcher = MagicMock(spec=RenderDispatcher)
    render_dispatcher.submit_request = AsyncMock()
    started = []

    router = CommandRouter(
        store=store,
        render_service=render_service,
        render_dispatcher=render_dispatcher,
        output=_make_output(),
        image_format=RasterImageFormat(width=72, height=72),
        start_soon=lambda func, *args: started.append((func, args)),
    )

    ok = await router.show_overlay(
        template="pending",
        title=None,
        params={},
        duration_seconds=None,
        overlay_id="playback",
        generation=1,
        binding_output_generation=0,
    )

    assert ok is True
    assert store.overlay is not None
    assert store.overlay.template == "pending"
    assert store.overlay.overlay_id == "playback"
    assert started == []


@pytest.mark.asyncio
async def test_binding_overlay_stale_generations_do_not_render(router_with_mocks):
    router = router_with_mocks
    await router.set_raster_image("https://example.invalid/base.jpg", generation=5)

    old_base = await router.show_overlay(
        template="ok",
        title=None,
        params={},
        duration_seconds=None,
        overlay_id=None,
        generation=1,
        binding_output_generation=4,
    )
    current_base = await router.show_overlay(
        template="ok",
        title=None,
        params={},
        duration_seconds=None,
        overlay_id=None,
        generation=2,
        binding_output_generation=5,
    )
    old_overlay = await router.show_overlay(
        template="error",
        title=None,
        params={},
        duration_seconds=None,
        overlay_id=None,
        generation=1,
        binding_output_generation=5,
    )

    assert old_base is False
    assert current_base is True
    assert old_overlay is False
    assert router._store.overlay is not None
    assert router._store.overlay.template == "ok"


@pytest.mark.asyncio
async def test_duplicate_base_output_generation_does_not_clear_overlay(
    router_with_mocks,
):
    router = router_with_mocks
    await router.set_raster_image("https://example.invalid/base.jpg", generation=5)
    await router.show_overlay(
        template="ok",
        title=None,
        params={},
        duration_seconds=None,
        overlay_id=None,
        generation=1,
        binding_output_generation=5,
    )

    await router.set_raster_image("https://example.invalid/base.jpg", generation=5)

    assert router._store.overlay is not None
    assert router._store.overlay.template == "ok"


@pytest.mark.asyncio
async def test_clear_invalidates_render_and_clears_content(router_with_mocks):
    """clear removes current render content and invalidates pending output."""
    router = router_with_mocks
    router._store.binding_id = "binding-1"

    await router.set_raster_image("https://example.com/img.png")
    await router.clear()

    assert router._store.content.image is None
    assert router._store.content.title is None
    router._render_dispatcher.clear_control.assert_awaited_once_with(
        router._output.control_id,
        context_id=router._store.context_id,
        binding_id="binding-1",
        output=router._output,
    )


@pytest.mark.asyncio
async def test_get_settings_hydrates_from_runtime_overlay():
    store = ControlStateStore(context_id="dev.slot0")
    store.settings = {"default_only": "x"}

    render_service = MagicMock(spec=RenderService)
    render_service.build_request = MagicMock(return_value=object())
    render_dispatcher = MagicMock(spec=RenderDispatcher)
    render_dispatcher.submit_request = AsyncMock()
    output = _make_output()
    image_format = RasterImageFormat(width=72, height=72)

    class FakeSettingsService:
        def __init__(self):
            self.calls = 0

        async def get(self, target):
            self.calls += 1
            return SettingsSnapshot(target=target, settings={"runtime": 42})

        async def patch(self, target, patch):
            return SettingsSnapshot(
                target=target,
                settings={"runtime": 42, **dict(patch)},
            )

    settings_service = FakeSettingsService()
    target = SettingsTargetRef(
        scope="action_instance",
        controllerId="controller-main",
        configId="config-dev",
        providerInstanceId="python-dev.deckr.clock",
        providerId="dev.deckr.clock",
        actionId="action",
        actionInstanceId="instance-a",
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
    assert settings.runtime == 42
    assert settings_service.calls == 1

    # second read should not hit the runtime settings service again
    settings_again = await router.get_settings()
    assert settings_again.runtime == 42
    assert settings_service.calls == 1


@pytest.mark.asyncio
async def test_set_settings_fail_fast_does_not_mutate_store():
    store = ControlStateStore(context_id="dev.slot0")
    store.settings = {"existing": 1}

    render_service = MagicMock(spec=RenderService)
    render_service.build_request = MagicMock(return_value=object())
    render_dispatcher = MagicMock(spec=RenderDispatcher)
    render_dispatcher.submit_request = AsyncMock()
    output = _make_output()
    image_format = RasterImageFormat(width=72, height=72)

    class FailingSettingsService:
        async def get(self, target):
            return SettingsSnapshot(target=target, settings={})

        async def patch(self, target, patch):
            raise OSError("disk full")

    target = SettingsTargetRef(
        scope="action_instance",
        controllerId="controller-main",
        configId="config-dev",
        providerInstanceId="python-dev.deckr.clock",
        providerId="dev.deckr.clock",
        actionId="action",
        actionInstanceId="instance-a",
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
