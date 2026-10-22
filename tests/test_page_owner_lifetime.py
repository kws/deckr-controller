"""DeviceSession tests. Uses mock devices (no VirtualDevice)."""

from unittest.mock import AsyncMock, MagicMock

import anyio
import pytest
from deckr.actions.messages import (
    ACTION_INSTANCE_DESTROYED,
    OPEN_PAGE,
    PAGE_SESSION_CLOSED,
)

from test_support.action_catalog import (
    _metadata,
)
from test_support.binding_builders import (
    SetRasterImageOnAppearAction,
)
from test_support.binding_constants import (
    CONTROLLER_ID,
    PROVIDER_ADDR,
    PROVIDER_INSTANCE_ID,
)
from test_support.binding_messages import (
    _action_command,
    _collect_action_messages,
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
    _dynamic_page,
    _dynamic_page_with_action_child,
    _hardware_ref,
    _make_mock_device,
)


@pytest.mark.asyncio
async def test_external_action_child_owner_survives_successor_dynamic_page(
    device_config_set_raster_image,
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
                _metadata("test.virtual.child"),
            )
            await manager.set_page(profile="default", page=0)
            await manager.wait_idle()
            owner_ctx = manager.context_for_control("0,0")
            assert owner_ctx is not None
            await _drain_action_messages(stream)

            await manager.open_page(
                descriptor=_dynamic_page_with_action_child(
                    "first-page",
                    "1,0",
                    action_id="test.virtual.child",
                    provider_instance_id=PROVIDER_INSTANCE_ID,
                ),
                context_id=owner_ctx.id,
            )
            await manager.wait_idle()
            first_session = manager.active_page_session()
            assert first_session is not None
            child_ctx = manager.context_for_control("1,0")
            assert child_ctx is not None
            child_action_instance_id = child_ctx.action_instance_id
            await _drain_action_messages(stream)

            await manager.open_page(
                descriptor=_dynamic_page("successor-page", "0,0"),
                context_id=child_ctx.id,
            )
            await manager.wait_idle()

            successor_session = manager.active_page_session()
            assert successor_session is not None
            assert successor_session.page_id == "successor-page"
            assert successor_session.page_session_id != first_session.page_session_id
            assert successor_session.action_instance_id == child_action_instance_id
            assert child_action_instance_id in manager.snapshot().action_instances

            messages = await _collect_action_messages(stream)
            closed_sessions = [
                message
                for message in messages
                if message.message_type == PAGE_SESSION_CLOSED
                and message.body["pageSession"]["pageSessionId"]
                == first_session.page_session_id
            ]
            destroyed_ids = [
                message.body["metadata"]["actionInstanceId"]
                for message in messages
                if message.message_type == ACTION_INSTANCE_DESTROYED
            ]
            assert len(closed_sessions) == 1
            assert closed_sessions[0].body["reason"] == "open_page"
            assert child_action_instance_id not in destroyed_ids
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_open_page_from_dynamic_child_dismisses_previous_owner(
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
                "first-page",
                "1,0",
                action_id="test.virtual.child",
                provider_instance_id="python-child",
            ),
            context_id=owner_ctx.id,
        )
        await manager.wait_idle()
        first_session = manager.active_page_session()
        assert first_session is not None
        child_ctx = manager.context_for_control("1,0")
        assert child_ctx is not None

        await manager.open_page(
            descriptor=_dynamic_page("second-page", "0,0"),
            context_id=child_ctx.id,
        )
        await manager.wait_idle()

        second_session = manager.active_page_session()
        assert second_session is not None
        assert second_session.page_id == "second-page"
        assert second_session.page_session_id != first_session.page_session_id
        assert second_session.owner_binding_id == child_ctx.binding_id
        assert second_session.owner_provider_instance_id == "python-child"

        await manager.close_page(context_id=second_session.context_id)
        await manager.wait_idle()

        assert manager.active_page_session() is None
        restored_owner_ctx = manager.context_for_control("0,0")
        assert restored_owner_ctx is not None
        assert restored_owner_ctx.page_session_id is None
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_stale_opener_binding_cannot_open_page_after_transition(
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
        stale_open = _action_command(
            OPEN_PAGE,
            {
                "descriptor": _dynamic_page("stale-page", "0,0").model_dump(
                    by_alias=True,
                    exclude_none=True,
                    mode="json",
                )
            },
            context_id=owner_ctx.id,
            action_instance_id=owner_ctx.action_instance_id,
            binding_id=owner_ctx.binding_id,
        )

        await manager.open_page(
            descriptor=_dynamic_page("active-page", "1,0"),
            context_id=owner_ctx.id,
        )
        await manager.wait_idle()
        session = manager.active_page_session()
        assert session is not None
        assert session.page_id == "active-page"
        child_ctx = manager.context_for_control("1,0")
        assert child_ctx is not None

        await manager.handle_provider_command(stale_open)
        await manager.wait_idle()

        assert manager.active_page_session() is session
        assert manager.context_for_control("1,0") is child_ctx
        assert manager.context_for_control("0,0") is None
        tg.cancel_scope.cancel()
