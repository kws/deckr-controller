"""DeviceSession tests. Uses mock devices (no VirtualDevice)."""

import logging
from unittest.mock import AsyncMock, MagicMock

import anyio
import pytest

from deckr.controller._actions import (
    ProviderSessionKey,
)
from test_support.action_catalog import (
    _availability_entry,
    _metadata,
)
from test_support.binding_builders import (
    ControlledFrameBackend,
    SetRasterImageOnAppearAction,
    _binding_lease_for_control,
    _solid_key_image,
)
from test_support.binding_constants import (
    CONTROLLER_ID,
    PROVIDER_ADDR,
    PROVIDER_ID,
    PROVIDER_INSTANCE_ID,
    PROVIDER_SESSION_ID,
)
from test_support.binding_messages import (
    _drain_action_messages,
)
from test_support.binding_runtime import (
    _action_service_for,
    _actions_bus,
    _actions_session,
    _ingest_provider_entries,
    _make_binding_service,
    _record_provider_runtime_contract,
    _seed_action_availability,
)
from test_support.device_layouts import (
    FakeHardwareCommandService,
    _dynamic_page,
    _hardware_ref,
    _make_mock_device,
)


@pytest.mark.asyncio
async def test_dynamic_page_retires_when_owner_agreement_is_replaced(
    device_config_set_raster_image,
):
    device = _make_mock_device()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=_metadata(SetRasterImageOnAppearAction.action_id)
    )
    action_bus = _actions_bus()
    successor_session_id = "new-provider-session"

    async with anyio.create_task_group() as tg:
        manager = _make_binding_service(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=FakeHardwareCommandService(),
            config=device_config_set_raster_image,
            manager=registry,
            actions_bus=_actions_session(action_bus),
            start_soon=tg.start_soon,
        )
        async with action_bus.subscribe(PROVIDER_ADDR) as stream:
            _seed_action_availability(
                manager,
                _metadata(SetRasterImageOnAppearAction.action_id),
            )
            await manager.set_page(profile="default", page=0)
            await manager.wait_idle()
            owner_ctx = manager.context_for_control("0,0")
            assert owner_ctx is not None
            await _drain_action_messages(stream)

            await manager.open_page(
                descriptor=_dynamic_page("dynamic-page", "1,0"),
                context_id=owner_ctx.id,
            )
            await manager.wait_idle()
            session = manager.active_page_session()
            assert session is not None
            child_ctx = manager.context_for_control("1,0")
            assert child_ctx is not None
            child_lease = _binding_lease_for_control(manager, "1,0")
            assert child_lease is not None
            assert session.owner_provider_session_id == PROVIDER_SESSION_ID
            await _drain_action_messages(stream)

            changed = _ingest_provider_entries(
                manager,
                provider_instance_id=PROVIDER_INSTANCE_ID,
                provider_id=PROVIDER_ID,
                provider_session_id=successor_session_id,
                entries=[_availability_entry(SetRasterImageOnAppearAction.action_id)],
            )
            _record_provider_runtime_contract(
                manager,
                _metadata(
                    SetRasterImageOnAppearAction.action_id,
                    provider_session_id=successor_session_id,
                ),
            )
            old_key = ProviderSessionKey(
                PROVIDER_INSTANCE_ID, PROVIDER_ID, PROVIDER_SESSION_ID
            )
            _action_service_for(manager)._contracts.pop(old_key)
            await manager.on_action_availability_changed(changed)
            await manager.wait_idle()
            assert manager.active_page_session() is None
            restored_ctx = manager.context_for_control("0,0")
            assert restored_ctx is not None
            assert restored_ctx.provider_session_id == successor_session_id
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_close_dynamic_page_restores_cached_static_plan_without_metadata_lookup(
    device_config_set_raster_image,
):
    device = _make_mock_device()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=_metadata(SetRasterImageOnAppearAction.action_id)
    )
    action_bus = _actions_bus()

    async with anyio.create_task_group() as tg:
        manager = _make_binding_service(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=FakeHardwareCommandService(),
            config=device_config_set_raster_image,
            manager=registry,
            actions_bus=_actions_session(action_bus),
            start_soon=tg.start_soon,
        )
        _seed_action_availability(
            manager,
            _metadata(SetRasterImageOnAppearAction.action_id),
        )
        await manager.set_page(profile="default", page=0)
        await manager.wait_idle()
        owner_ctx = manager.context_for_control("0,0")
        assert owner_ctx is not None

        await manager.open_page(
            descriptor=_dynamic_page("dynamic-page", "1,0"),
            context_id=owner_ctx.id,
        )
        await manager.wait_idle()
        session = manager.active_page_session()
        assert session is not None
        child_ctx = manager.context_for_control("1,0")
        assert child_ctx is not None

        assert manager.active_page_session() is session
        assert manager.context_for_control("1,0") is child_ctx

        registry.get_action.return_value = None
        registry.get_action.reset_mock()
        await manager.close_page(context_id=session.context_id)
        await manager.wait_idle()

        registry.get_action.assert_not_awaited()
        restored_ctx = manager.context_for_control("0,0")
        assert restored_ctx is not None
        assert restored_ctx.provider_session_id == PROVIDER_SESSION_ID
        assert manager.active_page_session() is None

        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_dynamic_page_replace_preserves_rebound_control_outputs(
    device_config_set_raster_image, persistence_tmp_dir
):
    """Dynamic page replacement should not blank controls that are rebound immediately."""
    device = _make_mock_device()
    action_bus = _actions_bus()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=_metadata(SetRasterImageOnAppearAction.action_id)
    )
    command_service = FakeHardwareCommandService()

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
        )
        _seed_action_availability(
            manager,
            _metadata(SetRasterImageOnAppearAction.action_id),
        )
        await manager.set_page(profile="default", page=0)
        await manager.wait_idle()
        owner_ctx = manager.context_for_control("0,0")
        assert owner_ctx is not None
        registry.get_action.reset_mock()
        await manager.open_page(
            descriptor=_dynamic_page("dynamic-page", "0,0", "1,0"),
            context_id=owner_ctx.id,
        )
        await manager.wait_idle()
        assert registry.get_action.await_count == 0
        session = manager.active_page_session()
        assert session is not None

        command_service.clear_raster.reset_mock()
        await manager.replace_page(
            descriptor=_dynamic_page(session.page_id, "0,0", "1,0"),
            context_id=session.context_id,
        )
        await manager.wait_idle()

        command_service.clear_raster.assert_not_awaited()
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_action_availability_refresh_repaints_existing_binding_output(
    device_config_set_raster_image, persistence_tmp_dir, caplog
):
    """Existing leases must repaint cached output during availability reconciliation."""
    caplog.set_level(logging.DEBUG, logger="deckr.controller")
    caplog.set_level(logging.DEBUG, logger="deckr.controller._presentation")
    device = _make_mock_device()
    action_bus = _actions_bus()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=_metadata(SetRasterImageOnAppearAction.action_id)
    )
    command_service = FakeHardwareCommandService()
    render_backend = ControlledFrameBackend()

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

        await ctx._router.set_raster_image(_solid_key_image(), generation=1)

        with anyio.fail_after(5.0):
            while not render_backend.calls:
                await anyio.sleep(0.01)
        render_backend.release(render_backend.calls[-1])
        with anyio.fail_after(5.0):
            while command_service.set_raster_frame.call_count == 0:
                await anyio.sleep(0.01)

        baseline_render_calls = len(render_backend.calls)
        baseline_output_calls = command_service.set_raster_frame.call_count
        caplog.clear()

        await manager.on_action_availability_changed()
        await manager.wait_idle()

        with anyio.fail_after(5.0):
            while len(render_backend.calls) <= baseline_render_calls:
                await anyio.sleep(0.01)
        render_backend.release(render_backend.calls[-1])
        with anyio.fail_after(5.0):
            while command_service.set_raster_frame.call_count <= baseline_output_calls:
                await anyio.sleep(0.01)
        assert "Action availability page refresh decision" in caplog.text
        assert "changed_keys=0 affected=True" in caplog.text
        assert "Refreshing cached binding output" in caplog.text
        assert "content_kind=" in caplog.text
        assert "Command router render enqueue" in caplog.text
        tg.cancel_scope.cancel()
