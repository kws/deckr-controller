"""DeviceSession tests. Uses mock devices (no VirtualDevice)."""

from unittest.mock import AsyncMock, MagicMock

import anyio
import pytest
from deckr.actions.messages import (
    ACTION_INSTANCE_CREATED,
    ACTION_LIFECYCLE_REJECTED,
    BINDING_ATTACHED,
)

from deckr.controller.config._data import Control, DeviceConfig, Page, Profile
from test_support.action_catalog import (
    _availability_entry,
    _metadata,
)
from test_support.binding_builders import (
    ConfigurableBuiltinActionRegistry,
    ControlledFrameBackend,
    SetRasterImageOnAppearAction,
    _binding_lease_for_control,
)
from test_support.binding_constants import (
    ACTION_X_UUID,
    CONTROLLER_ID,
    PROVIDER_ADDR,
    PROVIDER_ID,
    PROVIDER_INSTANCE_ID,
    PROVIDER_SESSION_ID,
)
from test_support.binding_messages import (
    _action_command_for_active_binding,
    _assert_no_action_message,
    _collect_action_messages,
    _drain_action_messages,
)
from test_support.binding_runtime import (
    _actions_bus,
    _actions_session,
    _ingest_provider_entries,
    _make_binding_service,
    _record_provider_runtime_contract,
    _seed_action_availability,
)
from test_support.device_layouts import (
    FakeHardwareCommandService,
    _hardware_ref,
    _make_mock_device,
)


@pytest.mark.asyncio
async def test_delayed_unavailable_status_does_not_write_after_binding_restored(
    device_config_set_raster_image,
):
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

        msg = await _action_command_for_active_binding(
            manager,
            ACTION_LIFECYCLE_REJECTED,
            {
                "targetKind": "binding",
                "binding": ctx.metadata.model_dump(
                    by_alias=True,
                    exclude_none=True,
                    mode="json",
                ),
                "reason": "invalid_settings",
                "retryable": True,
            },
        )
        await manager.handle_provider_command(msg)
        await manager.wait_idle()

        assert manager.context_for_control("0,0") is None
        with anyio.fail_after(5.0):
            while not render_backend.calls:
                await anyio.sleep(0.01)
        unavailable_render = render_backend.calls[-1]
        assert render_backend.requests[-1].binding_id is None

        changed = _ingest_provider_entries(
            manager,
            provider_instance_id=PROVIDER_INSTANCE_ID,
            provider_id=PROVIDER_ID,
            provider_session_id=PROVIDER_SESSION_ID,
            entries=[_availability_entry(SetRasterImageOnAppearAction.action_id)],
        )
        await manager.on_action_availability_changed(changed)
        await manager.wait_idle()

        restored_ctx = manager.context_for_control("0,0")
        assert restored_ctx is not None
        assert restored_ctx.binding_id != ctx.binding_id

        baseline_calls = command_service.set_raster_frame.call_count
        render_backend.release(unavailable_render)
        with anyio.move_on_after(0.2):
            while command_service.set_raster_frame.call_count == baseline_calls:
                await anyio.sleep(0.01)

        assert command_service.set_raster_frame.call_count == baseline_calls
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_stale_lifecycle_rejection_from_current_session_recovers_binding(
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
        async with action_bus.subscribe(PROVIDER_ADDR) as stream:
            _seed_action_availability(
                manager,
                _metadata(SetRasterImageOnAppearAction.action_id),
            )
            await manager.set_page(profile="default", page=0)
            await manager.wait_idle()
            ctx = manager.context_for_control("0,0")
            lease = _binding_lease_for_control(manager, "0,0")
            assert ctx is not None
            assert lease is not None
            await _drain_action_messages(stream)

            msg = await _action_command_for_active_binding(
                manager,
                ACTION_LIFECYCLE_REJECTED,
                {
                    "targetKind": "binding",
                    "binding": ctx.metadata.model_dump(
                        by_alias=True,
                        exclude_none=True,
                        mode="json",
                    ),
                    "reason": "stale_lifecycle",
                },
            )
            await manager.handle_provider_command(msg)
            await manager.wait_idle()

            messages = await _collect_action_messages(stream)
            created = [
                event
                for event in messages
                if event.message_type == ACTION_INSTANCE_CREATED
            ]
            attached = [
                event for event in messages if event.message_type == BINDING_ATTACHED
            ]
            assert len(created) == 1
            assert len(attached) == 1
            assert created[0].recipient_session_id == PROVIDER_SESSION_ID
            assert attached[0].recipient_session_id == PROVIDER_SESSION_ID
            recovered = _binding_lease_for_control(manager, "0,0")
            assert recovered is not None
            assert recovered.stale_lifecycle_recoveries == 1

            await manager.handle_provider_command(msg)
            await manager.wait_idle()
            await _assert_no_action_message(stream)

        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_action_lifecycle_rejected_from_stale_provider_session_is_ignored(
    session_tasks,
    device_config_set_raster_image,
):
    device = _make_mock_device()
    action_bus = _actions_bus()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=_metadata(SetRasterImageOnAppearAction.action_id)
    )

    manager = _make_binding_service(
        controller_id=CONTROLLER_ID,
        device=device,
        hardware_ref=_hardware_ref(device),
        command_service=FakeHardwareCommandService(),
        config=device_config_set_raster_image,
        manager=registry,
        actions_bus=_actions_session(action_bus),
        start_soon=session_tasks,
    )
    _seed_action_availability(
        manager,
        _metadata(SetRasterImageOnAppearAction.action_id),
    )
    await manager.set_page(profile="default", page=0)
    await manager.wait_idle()
    ctx = manager.context_for_control("0,0")
    assert ctx is not None

    msg = await _action_command_for_active_binding(
        manager,
        ACTION_LIFECYCLE_REJECTED,
        {
            "targetKind": "binding",
            "binding": ctx.metadata.model_dump(
                by_alias=True,
                exclude_none=True,
                mode="json",
            ),
            "reason": "stale_lifecycle",
        },
    )
    await manager.handle_provider_command(
        msg.model_copy(update={"sender_session_id": "stale"})
    )
    await manager.wait_idle()

    assert manager.context_for_control("0,0") is ctx
    assert manager.snapshot().binding_leases


@pytest.mark.asyncio
async def test_service_view_availability_resolves_unavailable_control(
    persistence_tmp_dir,
):
    device = _make_mock_device()
    action_bus = _actions_bus()
    registry = ConfigurableBuiltinActionRegistry()
    config = DeviceConfig(
        id="test-device",
        name="Test Device",
        match={"fingerprint": "fingerprint:test-device"},
        profiles=[
            Profile(
                name="default",
                pages=[
                    Page(
                        controls=[
                            Control(
                                selector={"control_id": "0,0"},
                                action=ACTION_X_UUID,
                                settings={},
                            )
                        ]
                    )
                ],
            )
        ],
    )

    async with anyio.create_task_group() as tg:
        start_soon = tg.start_soon

        manager = _make_binding_service(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=FakeHardwareCommandService(),
            config=config,
            manager=registry,
            actions_bus=_actions_session(action_bus),
            start_soon=start_soon,
        )
        await manager.set_page(profile="default", page=0)
        await manager.wait_idle()
        await anyio.sleep(0.05)

        ctx_before = manager.context_for_control("0,0")
        assert ctx_before is None

        changed = _ingest_provider_entries(
            manager,
            provider_instance_id="test-provider",
            provider_id="test",
            provider_session_id=PROVIDER_SESSION_ID,
            entries=[_availability_entry(ACTION_X_UUID)],
        )
        _record_provider_runtime_contract(
            manager,
            _metadata(
                ACTION_X_UUID,
                provider_instance_id="test-provider",
                provider_id="test",
            ),
        )
        await manager.on_action_availability_changed(changed)
        await manager.wait_idle()

        ctx_after = manager.context_for_control("0,0")
        assert ctx_after is not None
        assert ctx_after.action_id == ACTION_X_UUID
