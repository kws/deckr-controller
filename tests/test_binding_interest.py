"""DeviceSession tests. Uses mock devices (no VirtualDevice)."""

from unittest.mock import MagicMock

import anyio
import pytest

from deckr.controller._action_interest import (
    ActionInterestSource,
    ActionInterestStrength,
)
from deckr.controller._actions import (
    ActionMetadata,
    ProviderActionKey,
)
from deckr.controller.config._data import Control, DeviceConfig, Page, Profile
from test_support.action_catalog import (
    _metadata,
)
from test_support.binding_builders import (
    ConfigurableBuiltinActionRegistry,
)
from test_support.binding_constants import (
    ACTION_X_UUID,
    CONTROLLER_ID,
    PROVIDER_INSTANCE_ID,
)
from test_support.binding_runtime import (
    _actions_bus,
    _actions_session,
    _make_binding_service,
    _seed_action_availability,
)
from test_support.device_layouts import (
    FakeHardwareCommandService,
    _dynamic_page_with_action_child,
    _hardware_ref,
    _make_mock_device,
)


@pytest.mark.asyncio
async def test_device_manager_tracks_visible_and_dynamic_page_action_interest():
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
                                action="action.owner",
                            )
                        ]
                    )
                ],
            )
        ],
    )

    async def get_action(address: str, **kwargs) -> ActionMetadata:
        del kwargs
        return _metadata(address)

    registry = MagicMock()
    registry.get_action = get_action

    async with anyio.create_task_group() as tg:
        manager = _make_binding_service(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=FakeHardwareCommandService(),
            config=config,
            manager=registry,
            actions_bus=_actions_session(_actions_bus()),
            start_soon=tg.start_soon,
            clock=lambda: 20.0,
        )
        _seed_action_availability(
            manager,
            _metadata("action.owner"),
            _metadata("action.child", provider_instance_id=PROVIDER_INSTANCE_ID),
        )
        await manager.set_page(profile="default", page=0)
        await manager.wait_idle()
        static_snapshot = manager.action_interest_snapshot(now=20.0)
        assert any(
            record.source == ActionInterestSource.VISIBLE_BINDING
            and record.strength == ActionInterestStrength.STRONG
            and record.intent.action_id == "action.owner"
            for record in static_snapshot.records
        )

        owner = manager.context_for_control("0,0")
        assert owner is not None
        await manager.open_page(
            descriptor=_dynamic_page_with_action_child(
                "dynamic-page",
                "1,0",
                action_id="action.child",
                provider_instance_id=PROVIDER_INSTANCE_ID,
            ),
            context_id=owner.id,
        )
        await manager.wait_idle()

        dynamic_snapshot = manager.action_interest_snapshot(now=20.0)
        assert any(
            record.source == ActionInterestSource.DYNAMIC_PAGE
            and record.strength == ActionInterestStrength.STRONG
            and record.intent.action_id == "action.child"
            for record in dynamic_snapshot.records
        )
        assert any(
            record.source == ActionInterestSource.VISIBLE_BINDING
            and record.strength == ActionInterestStrength.WARM
            and record.intent.action_id == "action.owner"
            for record in dynamic_snapshot.records
        )
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_unrelated_availability_change_does_not_rebuild_current_page(
    persistence_tmp_dir,
):
    device = _make_mock_device()
    action_bus = _actions_bus()
    registry = ConfigurableBuiltinActionRegistry()
    registry.add_action(
        ACTION_X_UUID,
        _metadata(
            ACTION_X_UUID,
            provider_instance_id="test-provider",
            provider_id="test",
        ),
    )
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
        command_service = FakeHardwareCommandService()
        manager = _make_binding_service(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=command_service,
            config=config,
            manager=registry,
            actions_bus=_actions_session(action_bus),
            start_soon=tg.start_soon,
        )
        _seed_action_availability(
            manager,
            _metadata(
                ACTION_X_UUID,
                provider_instance_id="test-provider",
                provider_id="test",
            ),
        )
        await manager.set_page(profile="default", page=0)
        await manager.wait_idle()
        ctx_before = manager.context_for_control("0,0")
        assert ctx_before is not None

        command_service.clear_raster.reset_mock()

        await manager.on_action_availability_changed(
            {ProviderActionKey("other-provider", "test.action.unrelated")}
        )
        await manager.wait_idle()

        command_service.clear_raster.assert_not_awaited()
        assert manager.context_for_control("0,0") is ctx_before
        tg.cancel_scope.cancel()
