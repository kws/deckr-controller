"""DeviceSession tests. Uses mock devices (no VirtualDevice)."""

from unittest.mock import AsyncMock, MagicMock

import anyio
import pytest
from deckr.actions.messages import (
    ACTION_INSTANCE_CREATED,
    ACTION_INSTANCE_DESTROYED,
    BINDING_ATTACHED,
    DynamicPageCommand,
    PageChildBindingDescriptor,
    PageChildBindingTarget,
)

from test_support.action_catalog import (
    _metadata,
)
from test_support.binding_builders import (
    SetRasterImageOnAppearAction,
    _binding_lease_for_control,
)
from test_support.binding_constants import (
    CONTROLLER_ID,
    PROVIDER_ADDR,
    PROVIDER_INSTANCE_ID,
)
from test_support.binding_messages import (
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
    _dynamic_page_with_action_child,
    _dynamic_page_with_action_children,
    _hardware_ref,
    _make_control,
    _make_mock_device,
)


@pytest.mark.asyncio
async def test_close_dynamic_page_destroys_external_action_child(
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
                    "dynamic-page",
                    "1,0",
                    action_id="test.virtual.child",
                    provider_instance_id=PROVIDER_INSTANCE_ID,
                ),
                context_id=owner_ctx.id,
            )
            await manager.wait_idle()
            session = manager.active_page_session()
            assert session is not None
            child_lease = _binding_lease_for_control(manager, "1,0")
            assert child_lease is not None
            child_action_instance_id = child_lease.action_instance_id
            assert child_action_instance_id in manager.snapshot().action_instances
            await _drain_action_messages(stream)

            await manager.close_page(context_id=session.context_id)
            await manager.wait_idle()

            messages = await _collect_action_messages(stream)
            destroyed = [
                message
                for message in messages
                if message.message_type == ACTION_INSTANCE_DESTROYED
                and message.body["metadata"]["actionInstanceId"]
                == child_action_instance_id
            ]
            assert child_action_instance_id not in manager.snapshot().action_instances
            assert len(destroyed) == 1
            assert destroyed[0].body["reason"] == "close"
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_replace_dynamic_page_remounts_all_external_action_children(
    device_config_set_raster_image,
):
    device = _make_mock_device(
        controls=[
            _make_control("0,0"),
            _make_control("1,0"),
            _make_control("2,0"),
        ]
    )
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
                _metadata("test.virtual.keep"),
                _metadata("test.virtual.drop"),
            )
            await manager.set_page(profile="default", page=0)
            await manager.wait_idle()
            owner_ctx = manager.context_for_control("0,0")
            assert owner_ctx is not None
            await _drain_action_messages(stream)

            await manager.open_page(
                descriptor=_dynamic_page_with_action_children(
                    "dynamic-page",
                    ("1,0", "test.virtual.keep"),
                    ("2,0", "test.virtual.drop"),
                ),
                context_id=owner_ctx.id,
            )
            await manager.wait_idle()
            session = manager.active_page_session()
            assert session is not None
            keep_lease = _binding_lease_for_control(manager, "1,0")
            drop_lease = _binding_lease_for_control(manager, "2,0")
            assert keep_lease is not None
            assert drop_lease is not None
            keep_action_instance_id = keep_lease.action_instance_id
            drop_action_instance_id = drop_lease.action_instance_id
            await _drain_action_messages(stream)

            await manager.replace_page(
                descriptor=_dynamic_page_with_action_children(
                    session.page_id,
                    ("1,0", "test.virtual.keep"),
                ),
                context_id=session.context_id,
            )
            await manager.wait_idle()

            replacement_keep = _binding_lease_for_control(manager, "1,0")
            assert replacement_keep is not None
            assert replacement_keep.action_instance_id != keep_action_instance_id
            assert (
                replacement_keep.action_instance_id
                in manager.snapshot().action_instances
            )
            assert keep_action_instance_id not in manager.snapshot().action_instances
            assert drop_action_instance_id not in manager.snapshot().action_instances

            messages = await _collect_action_messages(stream)
            destroyed_ids = [
                message.body["metadata"]["actionInstanceId"]
                for message in messages
                if message.message_type == ACTION_INSTANCE_DESTROYED
            ]
            assert set(destroyed_ids) == {
                keep_action_instance_id,
                drop_action_instance_id,
            }
            destroyed_reasons = {
                message.body["metadata"]["actionInstanceId"]: message.body["reason"]
                for message in messages
                if message.message_type == ACTION_INSTANCE_DESTROYED
            }
            assert destroyed_reasons == {
                keep_action_instance_id: "page_child_removed",
                drop_action_instance_id: "page_child_removed",
            }
        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_dynamic_page_external_child_lifecycle_attach_carries_settings(
    device_config_set_raster_image,
):
    child_action_id = "test.virtual.child"
    device = _make_mock_device(controls=[_make_control("0,0"), _make_control("1,0")])
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
                _metadata(child_action_id),
            )
            await manager.set_page(profile="default", page=0)
            await manager.wait_idle()
            owner_ctx = manager.context_for_control("0,0")
            assert owner_ctx is not None
            await _drain_action_messages(stream)

            await manager.open_page(
                descriptor=DynamicPageCommand(
                    pageId="dynamic-page",
                    bindings=(
                        PageChildBindingDescriptor(
                            controlId="1,0",
                            target=PageChildBindingTarget(
                                kind="action",
                                actionId=child_action_id,
                                providerInstanceId=PROVIDER_INSTANCE_ID,
                                instanceKey="child",
                            ),
                            settings={"zoneName": "Bedroom"},
                        ),
                    ),
                ),
                context_id=owner_ctx.id,
            )
            await manager.wait_idle()

            messages = await _collect_action_messages(stream)

        tg.cancel_scope.cancel()

    created = [
        message
        for message in messages
        if message.message_type == ACTION_INSTANCE_CREATED
        and message.body["metadata"]["actionId"] == child_action_id
    ]
    attached = [
        message
        for message in messages
        if message.message_type == BINDING_ATTACHED
        and message.body["binding"]["actionId"] == child_action_id
    ]
    assert len(created) == 1
    assert len(attached) == 1
    assert "settings" not in created[0].body
    assert attached[0].body["settings"] == {"zoneName": "Bedroom"}
    assert attached[0].body["internal"] == {}
