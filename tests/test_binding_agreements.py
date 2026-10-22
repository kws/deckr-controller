"""DeviceSession tests. Uses mock devices (no VirtualDevice)."""

from unittest.mock import AsyncMock, MagicMock

import anyio
import pytest
from deckr.actions.messages import (
    ACTION_INSTANCE_CREATED,
    ACTION_INSTANCE_DESTROYED,
    BINDING_ATTACHED,
    BINDING_DETACHED,
    BINDING_OUTPUT,
    ActionAvailabilityEntry,
)
from deckr.contracts.authority import ContractPointer

from deckr.controller._actions import (
    PROVIDER_SESSION_INVALID_REASON,
    ProviderActionKey,
)
from test_support.action_catalog import (
    _availability_entry,
    _metadata,
)
from test_support.binding_builders import (
    SetRasterImageOnAppearAction,
    _binding_lease_for_control,
)
from test_support.binding_constants import (
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
    _action_service_for,
    _actions_bus,
    _actions_session,
    _ingest_provider_entries,
    _make_binding_service,
    _record_for_key,
    _record_lifecycle_unavailable,
    _record_provider_runtime_contract,
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
async def test_service_view_pending_preserves_attached_binding(
    device_config_set_raster_image,
):
    device = _make_mock_device()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=_metadata(SetRasterImageOnAppearAction.action_id)
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
                _metadata(SetRasterImageOnAppearAction.action_id),
            )
            await manager.set_page(profile="default", page=0)
            await manager.wait_idle()
            ctx = manager.context_for_control("0,0")
            lease = _binding_lease_for_control(manager, "0,0")
            assert ctx is not None
            assert lease is not None
            assert lease.attached
            await _drain_action_messages(stream)
            manager.components.presentation._render_pending_to_control = AsyncMock()

            changed = _ingest_provider_entries(
                manager,
                provider_instance_id=PROVIDER_INSTANCE_ID,
                provider_id=PROVIDER_ID,
                provider_session_id=PROVIDER_SESSION_ID,
                entries=(
                    ActionAvailabilityEntry(
                        actionId=SetRasterImageOnAppearAction.action_id,
                        status="probing",
                        reason="successor_lease_negotiating",
                    ),
                ),
            )
            assert changed == frozenset(
                {
                    ProviderActionKey(
                        PROVIDER_INSTANCE_ID,
                        SetRasterImageOnAppearAction.action_id,
                    )
                }
            )
            await manager.on_action_availability_changed(changed)
            await manager.wait_idle()

            assert manager.context_for_control("0,0") is ctx
            preserved = _binding_lease_for_control(manager, "0,0")
            assert preserved is not None
            assert preserved.binding_id == lease.binding_id
            assert lease.attached
            manager.components.presentation._render_pending_to_control.assert_not_awaited()
            await _assert_no_action_message(stream)

        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_service_view_availability_recovers_same_session_after_invalidated(
    device_config_set_raster_image,
):
    device = _make_mock_device()
    registry = MagicMock()
    registry.get_action = AsyncMock(
        return_value=_metadata(SetRasterImageOnAppearAction.action_id)
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
        runtime_sender = _runtime_sender_for(manager)
        assert isinstance(runtime_sender, _RecordingRuntimeSender)
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
            assert lease.provider_session_id == PROVIDER_SESSION_ID
            await _drain_action_messages(stream)
            runtime_sender.messages.clear()

            key = _record_lifecycle_unavailable(
                manager,
                provider_instance_id=PROVIDER_INSTANCE_ID,
                provider_id=PROVIDER_ID,
                provider_session_id=PROVIDER_SESSION_ID,
                action_id=SetRasterImageOnAppearAction.action_id,
                reason=PROVIDER_SESSION_INVALID_REASON,
                intent=lease.planned_intent,
            )
            changed = _ingest_provider_entries(
                manager,
                provider_instance_id=PROVIDER_INSTANCE_ID,
                provider_id=PROVIDER_ID,
                provider_session_id=PROVIDER_SESSION_ID,
                entries=[_availability_entry(SetRasterImageOnAppearAction.action_id)],
            )
            record = _record_for_key(manager, key)
            assert record is not None
            assert record.requires_provider_lifecycle_recovery

            await manager.on_action_availability_changed(changed)
            await manager.wait_idle()

            replacement_ctx = manager.context_for_control("0,0")
            replacement_lease = _binding_lease_for_control(manager, "0,0")
            assert replacement_ctx is not None
            assert replacement_ctx is not ctx
            assert replacement_lease is not None
            assert replacement_lease.binding_id != lease.binding_id
            assert replacement_lease.provider_session_id == PROVIDER_SESSION_ID
            assert replacement_lease.attached
            assert not _action_service_for(
                manager
            ).provider_lifecycle_recovery_required(key)

            await _collect_action_messages(stream)
            messages = runtime_sender.messages
            created = [
                msg for msg in messages if msg.message_type == ACTION_INSTANCE_CREATED
            ]
            destroyed = [
                msg for msg in messages if msg.message_type == ACTION_INSTANCE_DESTROYED
            ]
            attached = [msg for msg in messages if msg.message_type == BINDING_ATTACHED]
            detached = [msg for msg in messages if msg.message_type == BINDING_DETACHED]
            assert len(created) == 1
            assert len(destroyed) == 1
            assert len(attached) == 1
            assert len(detached) == 1
            assert destroyed[0].recipient_session_id == PROVIDER_SESSION_ID
            assert detached[0].recipient_session_id == PROVIDER_SESSION_ID
            assert created[0].recipient_session_id == PROVIDER_SESSION_ID
            assert attached[0].recipient_session_id == PROVIDER_SESSION_ID

        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_provider_session_contract_change_replaces_matching_binding(
    device_config_set_raster_image,
):
    device = _make_mock_device()
    registry = MagicMock()
    metadata = _metadata(SetRasterImageOnAppearAction.action_id)
    registry.get_action = AsyncMock(return_value=metadata)
    action_bus = _actions_bus()
    first_contract = ContractPointer(
        contractId="provider-session-contract-1", generation=1
    )
    next_contract = ContractPointer(
        contractId="provider-session-contract-2", generation=1
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
        runtime_sender = _runtime_sender_for(manager)
        assert isinstance(runtime_sender, _RecordingRuntimeSender)
        async with action_bus.subscribe(PROVIDER_ADDR) as stream:
            _seed_action_availability(
                manager,
                metadata,
            )
            _record_provider_runtime_contract(
                manager, metadata, contract=first_contract
            )
            await manager.set_page(profile="default", page=0)
            await manager.wait_idle()
            ctx = manager.context_for_control("0,0")
            lease = _binding_lease_for_control(manager, "0,0")
            assert ctx is not None
            assert lease is not None
            assert lease.contract == first_contract
            await _drain_action_messages(stream)
            runtime_sender.messages.clear()

            _record_provider_runtime_contract(manager, metadata, contract=next_contract)
            await manager.on_action_availability_changed(
                {
                    ProviderActionKey(
                        PROVIDER_INSTANCE_ID,
                        SetRasterImageOnAppearAction.action_id,
                    )
                }
            )
            await manager.wait_idle()

            replacement_ctx = manager.context_for_control("0,0")
            replacement_lease = _binding_lease_for_control(manager, "0,0")
            assert replacement_ctx is not None
            assert replacement_ctx is not ctx
            assert replacement_lease is not None
            assert replacement_lease.binding_id != lease.binding_id
            assert replacement_lease.provider_session_id == PROVIDER_SESSION_ID
            assert replacement_lease.contract == next_contract
            assert replacement_lease.attached

            await _collect_action_messages(stream)
            messages = runtime_sender.messages
            created = [
                msg for msg in messages if msg.message_type == ACTION_INSTANCE_CREATED
            ]
            attached = [msg for msg in messages if msg.message_type == BINDING_ATTACHED]
            assert len(created) == 1
            assert len(attached) == 1
            assert created[0].contract == next_contract
            assert attached[0].contract == next_contract

        tg.cancel_scope.cancel()


@pytest.mark.asyncio
async def test_attached_binding_survives_nonterminal_provider_session_unavailable(
    session_tasks,
    device_config_set_raster_image,
):
    device = _make_mock_device()
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
        actions_bus=_actions_session(_actions_bus()),
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
    lease = _binding_lease_for_control(manager, "0,0")
    assert lease is not None
    assert lease.attached
    command_service.clear_raster.reset_mock()

    await manager.on_action_availability_changed(
        {
            ProviderActionKey(
                PROVIDER_INSTANCE_ID, SetRasterImageOnAppearAction.action_id
            )
        }
    )
    await manager.wait_idle()

    assert manager.context_for_control("0,0") is ctx
    preserved = _binding_lease_for_control(manager, "0,0")
    assert preserved is not None
    assert preserved.binding_id == lease.binding_id
    assert lease.attached
    command_service.clear_raster.assert_not_awaited()

    manager.render_direct_output = AsyncMock()
    command = await _action_command_for_active_binding(
        manager,
        BINDING_OUTPUT,
        {
            "binding": ctx.metadata.model_dump(
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
            "commandType": "clear",
            "generation": ctx.metadata.output_generation,
        },
    )
    await manager.handle_provider_command(
        command.model_copy(update={"sender_session_id": "stale"})
    )
    await manager.wait_idle()
    command_service.clear_raster.assert_not_awaited()

    await manager.handle_provider_command(command)
    await manager.wait_idle()

    manager.render_direct_output.assert_awaited_once()
    assert (
        manager.render_direct_output.await_args.args[0].binding_id == lease.binding_id
    )
    assert manager.render_direct_output.await_args.args[1].command_type == "clear"
