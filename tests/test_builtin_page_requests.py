"""DeviceSession tests. Uses mock devices (no VirtualDevice)."""

from unittest.mock import AsyncMock, MagicMock

import anyio
import pytest
from deckr.actions.endpoints import (
    BUILTIN_ACTION_PROVIDER_ID,
)
from deckr.actions.messages import (
    REPLACE_PAGE,
)

from deckr.controller._actions import (
    ProviderActionKey,
)
from deckr.controller.config._data import Control, DeviceConfig, Page, Profile
from test_support.action_catalog import (
    _builtin_metadata,
    _metadata,
)
from test_support.binding_builders import (
    CapturingBuiltinAction,
    SetRasterImageOnAppearAction,
)
from test_support.binding_constants import (
    CONTROLLER_ID,
    PROVIDER_INSTANCE_ID,
)
from test_support.binding_messages import (
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
async def test_builtin_context_replaces_and_closes_opened_dynamic_page():
    owner_action = CapturingBuiltinAction("test.builtin.owner")
    child_action = CapturingBuiltinAction("test.builtin.child")
    builtin_actions = {
        owner_action.action_id: owner_action,
        child_action.action_id: child_action,
    }
    device = _make_mock_device()
    action_bus = _actions_bus()
    registry = MagicMock()

    async def get_action(action_id, provider_instance_id=None, **kwargs):
        del kwargs
        if provider_instance_id not in {None, BUILTIN_ACTION_PROVIDER_ID}:
            return None
        if action_id in builtin_actions:
            return _builtin_metadata(action_id)
        return None

    registry.get_action = get_action
    registry.get_builtin_action.side_effect = builtin_actions.get
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
                                action=owner_action.action_id,
                                provider_instance_id=BUILTIN_ACTION_PROVIDER_ID,
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
        _seed_action_availability(
            manager,
            _builtin_metadata(owner_action.action_id),
            _builtin_metadata(child_action.action_id),
        )
        await manager.set_page(profile="default", page=0)
        await manager.wait_idle()
        owner_context = owner_action.contexts[-1]

        await owner_context.open_page(
            _dynamic_page_with_action_child(
                "dynamic-page",
                "1,0",
                action_id=child_action.action_id,
                provider_instance_id=BUILTIN_ACTION_PROVIDER_ID,
            )
        )
        await manager.wait_idle()
        session = manager.active_page_session()
        assert session is not None
        assert manager.context_for_control("1,0") is not None

        await owner_context.replace_page(
            _dynamic_page_with_action_child(
                session.page_id,
                "0,0",
                action_id=child_action.action_id,
                provider_instance_id=BUILTIN_ACTION_PROVIDER_ID,
            )
        )
        await manager.wait_idle()

        assert manager.active_page_session() is session
        assert manager.context_for_control("1,0") is None
        assert manager.context_for_control("0,0") is not None

        await owner_context.close_page()
        await manager.wait_idle()

        assert manager.active_page_session() is None
        restored = manager.context_for_control("0,0")
        assert restored is not None
        assert restored.action_id == owner_action.action_id
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_builtin_child_context_cannot_control_parent_page_session():
    owner_action = CapturingBuiltinAction("test.builtin.owner")
    child_action = CapturingBuiltinAction("test.builtin.child")
    builtin_actions = {
        owner_action.action_id: owner_action,
        child_action.action_id: child_action,
    }
    device = _make_mock_device()
    action_bus = _actions_bus()
    registry = MagicMock()

    async def get_action(action_id, provider_instance_id=None, **kwargs):
        del kwargs
        if provider_instance_id not in {None, BUILTIN_ACTION_PROVIDER_ID}:
            return None
        if action_id in builtin_actions:
            return _builtin_metadata(action_id)
        return None

    registry.get_action = get_action
    registry.get_builtin_action.side_effect = builtin_actions.get
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
                                action=owner_action.action_id,
                                provider_instance_id=BUILTIN_ACTION_PROVIDER_ID,
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
        _seed_action_availability(
            manager,
            _builtin_metadata(owner_action.action_id),
            _builtin_metadata(child_action.action_id),
        )
        await manager.set_page(profile="default", page=0)
        await manager.wait_idle()
        owner_context = owner_action.contexts[-1]
        await owner_context.open_page(
            _dynamic_page_with_action_child(
                "dynamic-page",
                "1,0",
                action_id=child_action.action_id,
                provider_instance_id=BUILTIN_ACTION_PROVIDER_ID,
            )
        )
        await manager.wait_idle()
        session = manager.active_page_session()
        assert session is not None
        child_context = child_action.contexts[-1]
        child_ctx = manager.context_for_control("1,0")
        assert child_ctx is not None

        await child_context.replace_page(
            _dynamic_page_with_action_child(
                session.page_id,
                "0,0",
                action_id=child_action.action_id,
                provider_instance_id=BUILTIN_ACTION_PROVIDER_ID,
            )
        )
        await manager.wait_idle()

        assert manager.active_page_session() is session
        assert manager.context_for_control("1,0") is child_ctx
        assert manager.context_for_control("0,0") is None

        await child_context.close_page()
        await manager.wait_idle()

        assert manager.active_page_session() is session
        assert manager.context_for_control("1,0") is child_ctx
        assert manager.context_for_control("0,0") is None
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_dynamic_page_replace_from_owner_during_nonterminal_unavailable(
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

    await manager.on_action_availability_changed(
        {
            ProviderActionKey(
                PROVIDER_INSTANCE_ID, SetRasterImageOnAppearAction.action_id
            )
        }
    )
    await manager.wait_idle()

    replacement = _dynamic_page(session.page_id, "0,0")
    await manager.handle_provider_command(
        _page_session_command(
            REPLACE_PAGE,
            {
                "descriptor": replacement.model_dump(
                    by_alias=True,
                    exclude_none=True,
                    mode="json",
                )
            },
            session_id=session.page_session_id,
            context_id=session.context_id,
            action_instance_id=session.action_instance_id,
        )
    )
    await manager.wait_idle()

    assert manager.active_page_session() is session
    assert manager.context_for_control("1,0") is None
    assert manager.context_for_control("0,0") is not None
