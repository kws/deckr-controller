"""DeviceSession tests. Uses mock devices (no VirtualDevice)."""

from unittest.mock import AsyncMock, MagicMock

import anyio
import pytest
from deckr.actions.messages import (
    CLOSE_PAGE,
    REPLACE_PAGE,
)

from test_support.action_catalog import (
    _metadata,
)
from test_support.binding_builders import (
    SetRasterImageOnAppearAction,
)
from test_support.binding_constants import (
    CONTROLLER_ID,
    PROVIDER_INSTANCE_ID,
)
from test_support.binding_messages import (
    _action_command_for_active_binding,
    _page_session_command,
)
from test_support.binding_runtime import (
    _actions_bus,
    _actions_session,
    _make_binding_service,
    _seed_action_availability,
)
from test_support.device_layouts import (
    FakeHardwareCommandService,
    _dynamic_page,
    _dynamic_page_with_action_child,
    _hardware_ref,
    _make_mock_device,
)


@pytest.mark.asyncio
async def test_replace_page_from_non_owner_is_noop(
    device_config_set_raster_image, persistence_tmp_dir
):
    device = _make_mock_device()
    action_bus = _actions_bus()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        side_effect=lambda action_id, provider_instance_id=None, **_: _metadata(
            action_id,
            provider_instance_id=provider_instance_id or PROVIDER_INSTANCE_ID,
        )
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
            _metadata(
                "test.virtual.child",
                provider_instance_id="python-child",
            ),
        )
        await manager.set_page(profile="default", page=0)
        await manager.wait_idle()
        owner_ctx = manager.context_for_control("0,0")
        assert owner_ctx is not None
        await manager.open_page(
            descriptor=_dynamic_page_with_action_child(
                "dynamic-page",
                "1,0",
                action_id="test.virtual.child",
                provider_instance_id="python-child",
            ),
            context_id=owner_ctx.id,
        )
        await manager.wait_idle()
        session = manager.active_page_session()
        assert session is not None
        child_ctx = manager.context_for_control("1,0")
        assert child_ctx is not None

        await manager.replace_page(
            descriptor=_dynamic_page(session.page_id, "0,0"),
            context_id=child_ctx.id,
        )
        await manager.wait_idle()

        assert manager.active_page_session() is session
        assert manager.context_for_control("1,0") is child_ctx
        assert manager.context_for_control("0,0") is None
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_close_page_from_non_owner_is_noop(
    device_config_set_raster_image, persistence_tmp_dir
):
    device = _make_mock_device()
    action_bus = _actions_bus()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        side_effect=lambda action_id, provider_instance_id=None, **_: _metadata(
            action_id,
            provider_instance_id=provider_instance_id or PROVIDER_INSTANCE_ID,
        )
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
            _metadata(
                "test.virtual.child",
                provider_instance_id="python-child",
            ),
        )
        await manager.set_page(profile="default", page=0)
        await manager.wait_idle()
        owner_ctx = manager.context_for_control("0,0")
        assert owner_ctx is not None
        await manager.open_page(
            descriptor=_dynamic_page_with_action_child(
                "dynamic-page",
                "1,0",
                action_id="test.virtual.child",
                provider_instance_id="python-child",
            ),
            context_id=owner_ctx.id,
        )
        await manager.wait_idle()
        session = manager.active_page_session()
        assert session is not None
        child_ctx = manager.context_for_control("1,0")
        assert child_ctx is not None

        await manager.close_page(context_id=child_ctx.id)
        await manager.wait_idle()

        assert manager.active_page_session() is session
        assert manager.context_for_control("1,0") is child_ctx
        assert manager.context_for_control("0,0") is None
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_child_binding_commands_cannot_close_or_replace_page_session(
    device_config_set_raster_image, persistence_tmp_dir
):
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

        child_replace = await _action_command_for_active_binding(
            manager,
            REPLACE_PAGE,
            {
                "descriptor": _dynamic_page(session.page_id, "0,0").model_dump(
                    by_alias=True,
                    exclude_none=True,
                    mode="json",
                )
            },
            control_id="1,0",
        )
        await manager.handle_provider_command(child_replace)
        await manager.wait_idle()

        assert manager.active_page_session() is session
        assert manager.context_for_control("1,0") is child_ctx
        assert manager.context_for_control("0,0") is None

        child_close = await _action_command_for_active_binding(
            manager,
            CLOSE_PAGE,
            control_id="1,0",
        )
        await manager.handle_provider_command(child_close)
        await manager.wait_idle()

        assert manager.active_page_session() is session
        assert manager.context_for_control("1,0") is child_ctx
        assert manager.context_for_control("0,0") is None

        await manager.handle_provider_command(
            _page_session_command(
                CLOSE_PAGE,
                {},
                session_id=session.page_session_id,
                context_id=session.context_id,
                action_instance_id=session.action_instance_id,
            )
        )
        await manager.wait_idle()

        assert manager.active_page_session() is None
        assert manager.context_for_control("0,0") is not None
        tg.cancel_scope.cancel()
