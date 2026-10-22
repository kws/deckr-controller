"""DeviceSession tests. Uses mock devices (no VirtualDevice)."""

from unittest.mock import AsyncMock, MagicMock

import anyio
import pytest
from deckr.actions.messages import (
    PAGE_SESSION_CLOSED,
)

from deckr.controller.config._data import Control, DeviceConfig, Page, Profile
from test_support.action_catalog import (
    _metadata,
)
from test_support.binding_builders import (
    SetRasterImageOnAppearAction,
)
from test_support.binding_constants import (
    CONTROLLER_ID,
    PROVIDER_ADDR,
)
from test_support.binding_messages import (
    _assert_no_action_message,
    _drain_action_messages,
    _hardware_input,
    _next_capability_input,
)
from test_support.binding_runtime import (
    _action_service_for,
    _actions_bus,
    _actions_session,
    _make_binding_service,
    _RecordingRuntimeSender,
    _runtime_sender_for,
    _seed_action_availability,
)
from test_support.device_layouts import (
    FakeHardwareCommandService,
    _dynamic_page,
    _hardware_ref,
    _make_mars_msd_two_device,
    _make_mock_device,
)


@pytest.mark.asyncio
async def test_mars_same_control_dynamic_page_rebinds_opener_slot(persistence_tmp_dir):
    device = _make_mars_msd_two_device()
    action_bus = _actions_bus()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=_metadata(SetRasterImageOnAppearAction.action_id)
    )
    config = DeviceConfig(
        id="mars-gaming-msd-two",
        name="Mars Gaming MSD-TWO",
        match={"fingerprint": device.fingerprint},
        profiles=[
            Profile(
                name="default",
                pages=[
                    Page(
                        controls=[
                            Control(
                                selector={"control_id": "1,0"},
                                action=SetRasterImageOnAppearAction.action_id,
                                settings={},
                            )
                        ]
                    )
                ],
            )
        ],
    )

    async with anyio.create_task_group() as tg:
        manager = _make_binding_service(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=FakeHardwareCommandService(),
            config=config,
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
            await _drain_action_messages(stream)
            owner_ctx = manager.context_for_control("1,0")
            assert owner_ctx is not None

            await manager.handle_hardware_input(
                _hardware_input("1,0", "down", sequence=1, device_id=device.device_id)
            )
            await manager.wait_idle()
            body = await _next_capability_input(stream)
            assert body.event.event_type == "down"
            assert body.binding.binding_id == owner_ctx.binding_id

            await manager.open_page(
                descriptor=_dynamic_page("album-page", "1,0", "2,0"),
                context_id=owner_ctx.id,
            )
            await manager.wait_idle()
            child_ctx = manager.context_for_control("1,0")
            assert child_ctx is not None
            assert child_ctx.binding_id != owner_ctx.binding_id
            body = await _next_capability_input(stream)
            assert body.event.event_type == "cancel"
            assert body.binding.binding_id == owner_ctx.binding_id
            await _drain_action_messages(stream)

            await manager.handle_hardware_input(
                _hardware_input("1,0", "up", sequence=2, device_id=device.device_id)
            )
            await manager.wait_idle()
            await _assert_no_action_message(stream)

            await manager.handle_hardware_input(
                _hardware_input("1,0", "down", sequence=3, device_id=device.device_id)
            )
            await manager.wait_idle()
            body = await _next_capability_input(stream)
            assert body.event.event_type == "down"
            assert body.binding.binding_id == child_ctx.binding_id

            await manager.handle_hardware_input(
                _hardware_input("1,0", "up", sequence=4, device_id=device.device_id)
            )
            await manager.wait_idle()
            body = await _next_capability_input(stream)
            assert body.event.event_type == "up"
            assert body.binding.binding_id == child_ctx.binding_id
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_held_input_cancelled_when_config_is_removed(
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
        async with action_bus.subscribe(PROVIDER_ADDR) as stream:
            _seed_action_availability(
                manager,
                _metadata(SetRasterImageOnAppearAction.action_id),
            )
            await manager.set_page(profile="default", page=0)
            await manager.wait_idle()
            await _drain_action_messages(stream)
            ctx = manager.context_for_control("0,0")
            assert ctx is not None

            await manager.handle_hardware_input(
                _hardware_input("0,0", "down", sequence=1)
            )
            await manager.wait_idle()
            body = await _next_capability_input(stream)
            assert body.event.event_type == "down"
            assert body.binding.binding_id == ctx.binding_id

            await manager.on_config_changed(None)
            await manager.wait_idle()

            body = await _next_capability_input(stream)
            assert body.event.event_type == "cancel"
            assert body.binding.binding_id == ctx.binding_id
            await _drain_action_messages(stream)
            assert not manager.config_active
            assert manager.context_for_control("0,0") is None
            assert manager.snapshot().binding_leases == {}
            assert not _action_service_for(manager).has_config_interest(
                manager.config_id
            )

            await manager.handle_hardware_input(
                _hardware_input("0,0", "up", sequence=2)
            )
            await manager.wait_idle()
            await _assert_no_action_message(stream)
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_close_dynamic_page_restores_when_close_notification_fails(
    device_config_set_raster_image,
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
        runtime_sender = _runtime_sender_for(manager)
        assert isinstance(runtime_sender, _RecordingRuntimeSender)
        runtime_sender.raise_for_message_types[PAGE_SESSION_CLOSED] = RuntimeError(
            "provider offline"
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

        await manager.close_page(context_id=session.context_id)
        await manager.wait_idle()

        assert manager.active_page_session() is None
        assert manager.context_for_control("0,0") is not None
        tg.cancel_scope.cancel()
