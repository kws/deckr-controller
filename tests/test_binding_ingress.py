"""DeviceSession tests. Uses mock devices (no VirtualDevice)."""

from unittest.mock import AsyncMock, MagicMock

import anyio
import pytest
from deckr.actions.messages import (
    context_subject,
)
from deckr.contracts.messages import DeckrMessage, endpoint_target

from deckr.controller.settings import ConfigBackedSettingsService
from test_support.action_catalog import (
    _metadata,
)
from test_support.binding_builders import (
    ControlledFrameBackend,
    MemoryConfigService,
    SetRasterImageOnAppearAction,
    _contract_pointer_for_provider_session,
    _settings_reload_config,
    _solid_key_image,
)
from test_support.binding_constants import (
    CONTROLLER_ADDR,
    CONTROLLER_ID,
    PROVIDER_ADDR,
    PROVIDER_ID,
    PROVIDER_INSTANCE_ID,
    PROVIDER_SESSION_ID,
    UNSUPPORTED_ACTION_MESSAGE,
)
from test_support.binding_messages import (
    _action_command_for_active_binding,
    _assert_no_action_message,
    _drain_action_messages,
)
from test_support.binding_runtime import (
    _actions_bus,
    _actions_session,
    _make_binding_service,
    _seed_action_availability,
)
from test_support.device_layouts import (
    FakeHardwareCommandService,
    _hardware_ref,
    _make_mock_device,
)


@pytest.mark.asyncio
async def test_unsupported_action_message_for_active_binding_is_ignored():
    config_service = MemoryConfigService(_settings_reload_config())
    device = _make_mock_device()
    action_bus = _actions_bus()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=_metadata(SetRasterImageOnAppearAction.action_id)
    )

    async with anyio.create_task_group() as tg:
        manager = _make_binding_service(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=FakeHardwareCommandService(),
            config=config_service.config,
            manager=registry,
            actions_bus=_actions_session(action_bus),
            start_soon=tg.start_soon,
            settings_service=ConfigBackedSettingsService(
                controller_id=CONTROLLER_ID,
                config_service=config_service,
            ),
        )
        _seed_action_availability(
            manager,
            _metadata(SetRasterImageOnAppearAction.action_id),
        )
        async with action_bus.subscribe(PROVIDER_ADDR) as stream:
            await manager.set_page(profile="default", page=0)
            await manager.wait_idle()
            old_ctx = manager.context_for_control("0,0")
            assert old_ctx is not None
            assert old_ctx.settings == {"label": "old"}
            assert old_ctx.settings_target is not None
            await _drain_action_messages(stream)

            await manager.handle_provider_command(
                DeckrMessage(
                    lane="actions",
                    messageType=UNSUPPORTED_ACTION_MESSAGE,
                    sender=PROVIDER_ADDR,
                    senderSessionId=PROVIDER_SESSION_ID,
                    recipient=endpoint_target(CONTROLLER_ADDR),
                    subject=context_subject(
                        old_ctx.id,
                        provider_instance_id=PROVIDER_INSTANCE_ID,
                        provider_id=PROVIDER_ID,
                        config_id="test-device",
                        action_instance_id=old_ctx.action_instance_id,
                        binding_id=old_ctx.binding_id,
                    ),
                    body={
                        "target": old_ctx.settings_target.to_dict(),
                        "settings": {"label": "new", "extra": True},
                    },
                    contract=_contract_pointer_for_provider_session(),
                )
            )
            await manager.wait_idle()

            new_ctx = manager.context_for_control("0,0")
            assert new_ctx is old_ctx
            assert new_ctx.settings == {"label": "old"}
            assert config_service.config.profiles[0].pages[0].controls[0].settings == {
                "label": "old"
            }
            await _assert_no_action_message(stream)

        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_binding_overlay_and_clear_source_reaches_render_request(
    device_config_set_raster_image, persistence_tmp_dir
):
    from deckr.actions.messages import BINDING_OVERLAY, BINDING_OVERLAY_CLEAR

    device = _make_mock_device()
    action_bus = _actions_bus()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=_metadata(SetRasterImageOnAppearAction.action_id)
    )
    command_service = FakeHardwareCommandService()
    render_backend = ControlledFrameBackend()

    async def wait_for_render_count(count: int) -> None:
        with anyio.fail_after(5.0):
            while len(render_backend.calls) < count:
                await anyio.sleep(0.01)

    async def release_and_wait_for_frame(count: int) -> None:
        render_backend.release(render_backend.calls[-1])
        with anyio.fail_after(5.0):
            while command_service.set_raster_frame.call_count < count:
                await anyio.sleep(0.01)

    async with anyio.create_task_group() as tg:
        manager = _make_binding_service(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=command_service,
            config=device_config_set_raster_image,
            manager=registry,
            actions_bus=_actions_session(action_bus),
            start_soon=tg.start_soon,
            render_backend=render_backend,
        )
        _seed_action_availability(
            manager,
            _metadata(SetRasterImageOnAppearAction.action_id),
        )
        await manager.set_page(profile="default", page=0)
        await manager.wait_idle()
        ctx = manager.context_for_control("0,0")
        assert ctx is not None
        binding = ctx.metadata.model_copy(update={"output_generation": 1})

        await ctx._router.set_raster_image(_solid_key_image(), generation=1)
        await wait_for_render_count(1)
        base_request = render_backend.requests[-1]
        await release_and_wait_for_frame(1)

        overlay_msg = await _action_command_for_active_binding(
            manager,
            BINDING_OVERLAY,
            {
                "binding": binding.model_dump(
                    by_alias=True,
                    exclude_none=True,
                    mode="json",
                ),
                "template": "ok",
                "title": "OK",
                "durationSeconds": 30.0,
                "overlayId": "save-feedback",
                "generation": 1,
            },
        )
        await manager.handle_provider_command(overlay_msg)
        await manager.wait_idle()
        await wait_for_render_count(2)
        overlay_request = render_backend.requests[-1]
        await release_and_wait_for_frame(2)

        clear_msg = await _action_command_for_active_binding(
            manager,
            BINDING_OVERLAY_CLEAR,
            {
                "binding": binding.model_dump(
                    by_alias=True,
                    exclude_none=True,
                    mode="json",
                ),
                "overlayId": "save-feedback",
                "generation": 2,
            },
        )
        await manager.handle_provider_command(clear_msg)
        await manager.wait_idle()
        await wait_for_render_count(3)
        clear_request = render_backend.requests[-1]
        await release_and_wait_for_frame(3)
        tg.cancel_scope.cancel()

    assert base_request.source is None

    assert overlay_request.source is not None
    assert overlay_request.source.command_type == BINDING_OVERLAY
    assert overlay_request.source.content_kind == "overlay:ok"
    assert overlay_request.source.overlay_generation == 1
    assert overlay_request.source.binding_output_generation == 1
    assert overlay_request.source.action_message_id == overlay_msg.message_id

    assert clear_request.source is not None
    assert clear_request.source.command_type == BINDING_OVERLAY_CLEAR
    assert clear_request.source.content_kind == "invariant_graph"
    assert clear_request.source.overlay_generation == 2
    assert clear_request.source.binding_output_generation == 1
    assert clear_request.source.action_message_id == clear_msg.message_id
