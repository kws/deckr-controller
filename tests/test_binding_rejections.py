"""DeviceSession tests. Uses mock devices (no VirtualDevice)."""

from unittest.mock import AsyncMock, MagicMock

import anyio
import pytest
from deckr.actions.messages import (
    ACTION_LIFECYCLE_REJECTED,
    PAGE_SESSION_OPENED,
)

from deckr.controller._actions import (
    ProviderActionKey,
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
    _action_instance_body,
    _action_instance_command,
    _page_session_command,
)
from test_support.binding_runtime import (
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
    _make_mock_device,
)


@pytest.mark.asyncio
async def test_action_lifecycle_rejected_binding_detaches_only_that_binding(
    session_tasks,
    device_config_set_raster_image,
):
    device = _make_mock_device()
    action_bus = _actions_bus()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=_metadata(SetRasterImageOnAppearAction.action_id)
    )
    command_service = FakeHardwareCommandService()

    manager = _make_binding_service(
        controller_id=CONTROLLER_ID,
        device=device,
        hardware_ref=_hardware_ref(device),
        command_service=command_service,
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
            "reason": "invalid_settings",
        },
    )
    await manager.handle_provider_command(msg)
    await manager.wait_idle()

    assert manager.context_for_control("0,0") is None
    assert manager.snapshot().binding_leases == {}
    assert ctx.action_instance_id in manager.snapshot().action_instances


@pytest.mark.asyncio
async def test_terminal_action_lifecycle_rejected_action_instance_destroys_affected_bindings(
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
    metadata = manager.snapshot().action_instances[ctx.action_instance_id]

    await manager.handle_provider_command(
        _action_instance_command(
            ACTION_LIFECYCLE_REJECTED,
            {
                "targetKind": "action_instance",
                "actionInstance": _action_instance_body(metadata),
                "reason": "invalid_settings",
            },
            context_id=metadata.context_id,
            action_instance_id=metadata.action_instance_id,
        )
    )
    await manager.wait_idle()

    assert manager.context_for_control("0,0") is None
    assert manager.snapshot().binding_leases == {}
    assert ctx.action_instance_id not in manager.snapshot().action_instances
    assert ctx.action_instance_id not in manager.snapshot().provider_session_keys


@pytest.mark.asyncio
async def test_action_instance_rejection_from_owner_during_nonterminal_unavailable_replans(
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
    metadata = manager.snapshot().action_instances[ctx.action_instance_id]

    await manager.on_action_availability_changed(
        {
            ProviderActionKey(
                PROVIDER_INSTANCE_ID, SetRasterImageOnAppearAction.action_id
            )
        }
    )
    await manager.wait_idle()

    payload = {
        "targetKind": "action_instance",
        "actionInstance": _action_instance_body(metadata),
        "reason": "action_not_available",
    }
    await manager.handle_provider_command(
        _action_instance_command(
            ACTION_LIFECYCLE_REJECTED,
            payload,
            context_id=metadata.context_id,
            action_instance_id=metadata.action_instance_id,
            sender_session_id="stale",
        )
    )
    await manager.wait_idle()

    assert manager.context_for_control("0,0") is ctx
    assert ctx.action_instance_id in manager.snapshot().action_instances

    await manager.handle_provider_command(
        _action_instance_command(
            ACTION_LIFECYCLE_REJECTED,
            payload,
            context_id=metadata.context_id,
            action_instance_id=metadata.action_instance_id,
        )
    )
    await manager.wait_idle()

    assert manager.context_for_control("0,0") is None
    assert manager.snapshot().binding_leases == {}
    assert ctx.action_instance_id in manager.snapshot().action_instances


@pytest.mark.asyncio
async def test_action_lifecycle_rejected_page_session_resource_unavailable_replans(
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
        runtime_sender = _runtime_sender_for(manager)
        assert isinstance(runtime_sender, _RecordingRuntimeSender)
        opened = [
            msg
            for msg in runtime_sender.messages
            if msg.message_type == PAGE_SESSION_OPENED
        ]
        assert opened

        await manager.handle_provider_command(
            _page_session_command(
                ACTION_LIFECYCLE_REJECTED,
                {
                    "targetKind": "page_session",
                    "pageSession": opened[-1].body.page_session.model_dump(
                        by_alias=True,
                        exclude_none=True,
                        mode="json",
                    ),
                    "reason": "resource_unavailable",
                },
                session_id=session.page_session_id,
                context_id=session.context_id,
                action_instance_id=session.action_instance_id,
            )
        )
        await manager.wait_idle()

        assert manager.active_page_session() is session
        assert manager.context_for_control("1,0") is None
        assert all(
            lease.page_session_id != session.page_session_id
            for lease in manager.snapshot().binding_leases.values()
        )
        tg.cancel_scope.cancel()
