"""DeviceSession tests. Uses mock devices (no VirtualDevice)."""

from unittest.mock import AsyncMock, MagicMock

import anyio
import pytest
from deckr.actions.messages import (
    ACTION_INSTANCE_CREATED,
    BINDING_ATTACHED,
)

from deckr.controller._bindings import _installation as binding_service_module
from deckr.controller.config._data import Control, DeviceConfig, Page, Profile
from test_support.action_catalog import (
    _metadata,
)
from test_support.binding_builders import (
    NoopAction,
    SetRasterImageOnAppearAction,
    _binding_lease_for_control,
)
from test_support.binding_constants import (
    ACTION_X_UUID,
    CONTROLLER_ID,
    PROVIDER_ADDR,
)
from test_support.binding_messages import (
    _assert_no_action_message,
    _collect_action_messages,
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
    _hardware_ref,
    _make_mock_device,
)


@pytest.mark.asyncio
async def test_binding_without_live_provider_session_remains_pending(
    device_config_set_raster_image,
):
    device = _make_mock_device()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=_metadata(
            SetRasterImageOnAppearAction.action_id,
            provider_session_id=None,
        )
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
        async with action_bus.subscribe(PROVIDER_ADDR) as stream:
            _seed_action_availability(
                manager,
                _metadata(
                    SetRasterImageOnAppearAction.action_id,
                    provider_session_id=None,
                ),
            )
            await manager.set_page(profile="default", page=0)
            await manager.wait_idle()
            ctx = manager.context_for_control("0,0")
            assert ctx is None
            assert manager.snapshot().binding_leases == {}
            await _assert_no_action_message(stream)

        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_binding_activation_timeout_does_not_block_static_page_bind_loop(
    session_tasks,
    monkeypatch,
):
    blocked_action = "test.virtual.blocked"
    device = _make_mock_device()
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
                                action=blocked_action,
                                settings={},
                            ),
                            Control(
                                selector={"control_id": "1,0"},
                                action=NoopAction.action_id,
                                settings={},
                            ),
                        ]
                    )
                ],
            )
        ],
    )
    registry = MagicMock()
    registry.get_action = AsyncMock(
        side_effect=lambda action_id, **_: _metadata(action_id),
    )
    action_bus = _actions_bus()

    monkeypatch.setattr(
        binding_service_module,
        "ACTION_INSTANCE_CREATE_TIMEOUT_SECONDS",
        0.01,
    )
    manager = _make_binding_service(
        controller_id=CONTROLLER_ID,
        device=device,
        hardware_ref=_hardware_ref(device),
        command_service=FakeHardwareCommandService(),
        config=config,
        manager=registry,
        actions_bus=_actions_session(action_bus),
        start_soon=session_tasks,
    )
    _seed_action_availability(
        manager,
        _metadata(blocked_action),
        _metadata(NoopAction.action_id),
    )
    runtime_sender = _runtime_sender_for(manager)
    assert isinstance(runtime_sender, _RecordingRuntimeSender)
    runtime_sender.block_action_instance_creates_for.add(blocked_action)

    with anyio.fail_after(1):
        await manager.set_page(profile="default", page=0)
        blocked_lease = _binding_lease_for_control(manager, "0,0")
        await manager.wait_idle()

    resolved_lease = _binding_lease_for_control(manager, "1,0")
    assert blocked_lease is not None
    assert not blocked_lease.attached
    assert manager.context_for_control("0,0") is None
    assert manager.state.binding_status[blocked_lease.binding_id] == "retired"
    assert manager.binding_by_id(blocked_lease.binding_id) is None
    assert not manager.state.operations
    assert resolved_lease is not None
    assert resolved_lease.attached
    assert manager.context_for_control("1,0") is not None


@pytest.mark.asyncio
async def test_settings_snapshot_timeout_does_not_block_static_page_bind_loop(
    session_tasks,
    monkeypatch,
):
    class HangingSettingsService:
        async def get(self, target):
            await anyio.sleep_forever()

    device = _make_mock_device()
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
                                settings={"configured": "first"},
                            ),
                            Control(
                                selector={"control_id": "1,0"},
                                action=NoopAction.action_id,
                                settings={"configured": "second"},
                            ),
                        ]
                    )
                ],
            )
        ],
    )
    registry = MagicMock()
    registry.get_action = AsyncMock(
        side_effect=lambda action_id, **_: _metadata(action_id),
    )
    action_bus = _actions_bus()
    monkeypatch.setattr(
        binding_service_module,
        "SETTINGS_SERVICE_TIMEOUT_SECONDS",
        0.01,
    )
    manager = _make_binding_service(
        controller_id=CONTROLLER_ID,
        device=device,
        hardware_ref=_hardware_ref(device),
        command_service=FakeHardwareCommandService(),
        config=config,
        manager=registry,
        actions_bus=_actions_session(action_bus),
        start_soon=session_tasks,
        settings_service=HangingSettingsService(),
    )
    _seed_action_availability(
        manager,
        _metadata(ACTION_X_UUID),
        _metadata(NoopAction.action_id),
    )

    with anyio.fail_after(1):
        await manager.set_page(profile="default", page=0)
        await manager.wait_idle()

    first = manager.context_for_control("0,0")
    second = manager.context_for_control("1,0")
    assert first is not None
    assert first.settings == {"configured": "first"}
    assert second is not None
    assert second.settings == {"configured": "second"}


@pytest.mark.asyncio
async def test_static_binding_lifecycle_messages_carry_settings_only_on_attach():
    device = _make_mock_device()
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
                                settings={"configured": "first"},
                            ),
                        ],
                    ),
                ],
            ),
        ],
    )
    registry = MagicMock()
    registry.get_action = AsyncMock(
        side_effect=lambda action_id, **_: _metadata(action_id),
    )
    action_bus = _actions_bus()

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
            _seed_action_availability(manager, _metadata(ACTION_X_UUID))
            await manager.set_page(profile="default", page=0)
            await manager.wait_idle()

            messages = await _collect_action_messages(stream)

        tg.cancel_scope.cancel()

    created = [
        message
        for message in messages
        if message.message_type == ACTION_INSTANCE_CREATED
    ]
    attached = [
        message for message in messages if message.message_type == BINDING_ATTACHED
    ]
    assert len(created) == 1
    assert len(attached) == 1
    assert "settings" not in created[0].body
    assert "internal" not in created[0].body
    assert attached[0].body["settings"] == {"configured": "first"}
    assert attached[0].body["internal"] == {}
