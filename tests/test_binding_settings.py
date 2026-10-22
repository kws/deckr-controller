"""DeviceSession tests. Uses mock devices (no VirtualDevice)."""

from unittest.mock import AsyncMock, MagicMock

import anyio
import pytest
from deckr.actions.messages import (
    BINDING_OUTPUT,
)

from deckr.controller.config._data import Control, DeviceConfig, Page, Profile
from deckr.controller.settings import ConfigBackedSettingsService
from test_support.action_catalog import (
    _metadata,
)
from test_support.binding_builders import (
    MemoryConfigService,
    NoopAction,
    SetRasterImageOnAppearAction,
)
from test_support.binding_constants import (
    CONTROLLER_ID,
)
from test_support.binding_messages import (
    _action_command_for_active_binding,
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
async def test_set_raster_image_last_write_wins_same_control(
    device_config_set_raster_image, persistence_tmp_dir
):
    """bindingOutput rejects stale or mismatched output generations."""

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
        ctx = manager.context_for_control("0,0")
        assert ctx is not None
        binding = ctx.metadata.model_copy(update={"output_generation": 1})
        msg = await _action_command_for_active_binding(
            manager,
            BINDING_OUTPUT,
            {
                "binding": binding.model_dump(
                    by_alias=True,
                    exclude_none=True,
                    mode="json",
                ),
                "capability": {
                    "deviceRef": {
                        "managerId": "manager-main",
                        "deviceId": "test-device",
                    },
                    "controlId": "0,0",
                    "capabilityId": "raster.bitmap",
                },
                "commandType": "set_frame",
                "params": {"image": "ZnJhbWU=", "encoding": "jpeg"},
                "generation": 2,
            },
        )

        await manager.handle_provider_command(msg)
        await manager.wait_idle()

        command_service.set_raster_frame.assert_not_awaited()
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_settings_isolated_by_control_same_action(persistence_tmp_dir):
    """Same action on different controls keeps separate settings."""
    device = _make_mock_device()
    registry = MagicMock()
    registry.get_action = AsyncMock(return_value=_metadata(NoopAction.action_id))
    action_bus = _actions_bus()

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
                                action=NoopAction.action_id,
                                settings={"control_marker": "A"},
                            ),
                            Control(
                                selector={"control_id": "1,0"},
                                action=NoopAction.action_id,
                                settings={"control_marker": "B"},
                            ),
                        ]
                    )
                ],
            )
        ],
    )
    config_service = MemoryConfigService(config)
    settings_service = ConfigBackedSettingsService(
        controller_id=CONTROLLER_ID,
        config_service=config_service,
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
            settings_service=settings_service,
        )
        _seed_action_availability(manager, _metadata(NoopAction.action_id))
        await manager.set_page(profile="default", page=0)
        await manager.wait_idle()
        await anyio.sleep(0.05)
        control_a = manager.context_for_control("0,0")
        control_b = manager.context_for_control("1,0")
        assert control_a is not None
        assert control_b is not None
        settings_a = control_a.controller_context.settings
        settings_b = control_b.controller_context.settings
        assert settings_a.control_marker == "A"
        assert settings_b.control_marker == "B"


@pytest.mark.asyncio
async def test_config_reload_replaces_read_only_settings_snapshot(persistence_tmp_dir):
    device = _make_mock_device()
    registry = MagicMock()
    registry.get_action = AsyncMock(return_value=_metadata(NoopAction.action_id))
    action_bus = _actions_bus()
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
                                action=NoopAction.action_id,
                                settings={
                                    "label": "from-config",
                                    "nested": {"role": {"page": "root"}},
                                },
                            )
                        ]
                    )
                ],
            )
        ],
    )
    reloaded_config = DeviceConfig(
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
                                action=NoopAction.action_id,
                                settings={"label": "from-reload"},
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
        _seed_action_availability(manager, _metadata(NoopAction.action_id))
        await manager.set_page(profile="default", page=0)
        await manager.wait_idle()
        ctx = manager.context_for_control("0,0")
        assert ctx is not None
        settings = ctx.controller_context.settings
        assert settings.label == "from-config"
        assert settings.nested == {"role": {"page": "root"}}

        await manager.on_config_changed(reloaded_config)
        await manager.wait_idle()
        reloaded_ctx = manager.context_for_control("0,0")
        assert reloaded_ctx is not None
        reloaded_settings = reloaded_ctx.controller_context.settings
        assert reloaded_settings.label == "from-reload"
        with pytest.raises(TypeError):
            reloaded_settings._values["label"] = "mutated"
