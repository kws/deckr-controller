"""DeviceSession tests. Uses mock devices (no VirtualDevice)."""

from unittest.mock import AsyncMock, MagicMock

import anyio
import pytest
from deckr.actions.messages import (
    ACTION_LIFECYCLE_REJECTED,
)

from deckr.controller._actions import (
    PROVIDER_SESSION_INVALID_REASON,
)
from deckr.controller.config._data import Control, DeviceConfig, Page, Profile
from test_support.action_catalog import (
    _metadata,
)
from test_support.binding_builders import (
    ControlledFrameBackend,
    SetRasterImageOnAppearAction,
    _binding_lease_for_control,
    _wait_for_render_request,
)
from test_support.binding_constants import (
    ACTION_X_UUID,
    CONTROLLER_ID,
    PROVIDER_ID,
    PROVIDER_INSTANCE_ID,
    PROVIDER_SESSION_ID,
)
from test_support.binding_messages import (
    _action_command_for_active_binding,
)
from test_support.binding_runtime import (
    _actions_bus,
    _actions_session,
    _make_binding_service,
    _record_lifecycle_unavailable,
    _seed_action_availability,
)
from test_support.device_layouts import (
    FakeHardwareCommandService,
    _hardware_ref,
    _make_mock_device,
)


@pytest.mark.asyncio
async def test_missing_action_renders_missing_unavailable_fallback():
    device = _make_mock_device()
    action_bus = _actions_bus()
    registry = MagicMock()
    registry.get_action = AsyncMock(return_value=None)
    command_service = FakeHardwareCommandService()
    render_backend = ControlledFrameBackend()
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
        manager = _make_binding_service(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=command_service,
            config=config,
            manager=registry,
            actions_bus=_actions_session(action_bus),
            start_soon=tg.start_soon,
            render_backend=render_backend,
        )

        await manager.set_page(profile="default", page=0)
        await manager.wait_idle()
        request = await _wait_for_render_request(render_backend)
        assert request.source is not None
        assert request.source.command_type == "controller_fallback"
        assert request.source.content_kind == "overlay:unavailable_missing"
        assert request.source.availability_cause == "missing"
        assert request.source.availability_reason is None
        render_backend.release(request.generation)
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_service_view_service_unavailable_renders_service_fallback():
    device = _make_mock_device()
    action_bus = _actions_bus()
    registry = MagicMock()
    registry.get_action = AsyncMock(return_value=None)
    command_service = FakeHardwareCommandService()
    render_backend = ControlledFrameBackend()
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
        manager = _make_binding_service(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=command_service,
            config=config,
            manager=registry,
            actions_bus=_actions_session(action_bus),
            start_soon=tg.start_soon,
            render_backend=render_backend,
        )
        _record_lifecycle_unavailable(
            manager,
            provider_instance_id=PROVIDER_INSTANCE_ID,
            provider_id=PROVIDER_ID,
            action_id=ACTION_X_UUID,
            provider_session_id=PROVIDER_SESSION_ID,
            reason="sonos_service_unavailable",
        )

        await manager.set_page(profile="default", page=0)
        await manager.wait_idle()
        request = await _wait_for_render_request(render_backend)
        assert request.source is not None
        assert request.source.content_kind == "overlay:unavailable_service"
        assert request.source.availability_cause == "service"
        assert request.source.availability_state == "unavailable"
        assert request.source.availability_source == "service_view"
        assert request.source.availability_reason == "sonos_service_unavailable"
        render_backend.release(request.generation)
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_provider_session_invalidation_renders_session_fallback(
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
        lease = _binding_lease_for_control(manager, "0,0")
        assert lease is not None
        key = _record_lifecycle_unavailable(
            manager,
            provider_instance_id=PROVIDER_INSTANCE_ID,
            provider_id=PROVIDER_ID,
            provider_session_id=PROVIDER_SESSION_ID,
            action_id=SetRasterImageOnAppearAction.action_id,
            reason=PROVIDER_SESSION_INVALID_REASON,
            intent=lease.planned_intent,
        )

        await manager.on_action_availability_changed(frozenset({key}))
        await manager.wait_idle()
        request = await _wait_for_render_request(render_backend)
        assert request.source is not None
        assert request.source.content_kind == "overlay:unavailable_session"
        assert request.source.availability_cause == "session"
        assert request.source.availability_reason == PROVIDER_SESSION_INVALID_REASON
        render_backend.release(request.generation)
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_nonterminal_lifecycle_rejection_renders_rejected_fallback(
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
                "reason": "provider_not_ready",
            },
        )
        await manager.handle_provider_command(msg)
        await manager.wait_idle()

        request = await _wait_for_render_request(render_backend)
        assert request.source is not None
        assert request.source.content_kind == "overlay:unavailable_rejected"
        assert request.source.availability_cause == "rejected"
        assert request.source.availability_reason == "provider_not_ready"
        render_backend.release(request.generation)
        tg.cancel_scope.cancel()
