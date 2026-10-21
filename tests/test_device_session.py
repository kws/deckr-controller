"""Acceptance tests across the device state / adapter boundary."""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import anyio
import pytest
from deckr.actions.messages import (
    ACTION_INSTANCE_CREATED,
    BINDING_ATTACHED,
    BINDING_DETACHED,
    CAPABILITY_INPUT,
    CLOSE_PAGE,
)
from test_binding_action_service import (
    CONTROLLER_ID,
    FakeHardwareCommandService,
    _actions_bus,
    _actions_session,
    _dynamic_page,
    _hardware_input,
    _hardware_ref,
    _make_binding_service,
    _make_mock_device,
    _metadata,
    _page_session_command,
    _runtime_sender_for,
    _seed_action_availability,
)

from deckr.controller.config._data import Control, DeviceConfig, Page, Profile
from test_support.managed_hardware import eventually


@asynccontextmanager
async def session(*, settings=None, hardware=None):
    device = _make_mock_device()
    config = DeviceConfig(
        id="test-device",
        name="Session",
        match={"fingerprint": "fingerprint:test-device"},
        profiles=[
            Profile(
                name="default",
                pages=[
                    Page(
                        controls=[
                            Control(selector={"control_id": "0,0"}, action=action)
                        ]
                    )
                    for action in ("action.first", "action.second")
                ],
            )
        ],
    )
    registry = MagicMock()
    registry.get_action = AsyncMock(side_effect=lambda action, **_: _metadata(action))
    async with anyio.create_task_group() as tg:
        manager = _make_binding_service(
            controller_id=CONTROLLER_ID,
            device=device,
            hardware_ref=_hardware_ref(device),
            command_service=hardware or FakeHardwareCommandService(),
            config=config,
            manager=registry,
            actions_bus=_actions_session(_actions_bus()),
            start_soon=tg.start_soon,
            settings_service=settings,
        )
        _seed_action_availability(
            manager, _metadata("action.first"), _metadata("action.second")
        )
        try:
            yield manager
        finally:
            tg.cancel_scope.cancel()


def gate_notification(manager, message_type):
    sender = _runtime_sender_for(manager)
    send = sender.send_action_runtime_message
    entered, release = anyio.Event(), anyio.Event()

    async def gated(**kwargs):
        if kwargs["message_type"] == message_type and not entered.is_set():
            entered.set()
            await release.wait()
        return await send(**kwargs)

    sender.send_action_runtime_message = gated
    return entered, release


@pytest.mark.asyncio
@pytest.mark.parametrize("message_type", [ACTION_INSTANCE_CREATED, BINDING_ATTACHED])
async def test_navigation_commits_while_old_preparation_waits(message_type):
    async with session() as device:
        entered, release = gate_notification(device, message_type)
        with anyio.fail_after(0.5):
            assert await device.set_page(page=0)
            await entered.wait()
            old = next(iter(device.snapshot().binding_leases.values()))
            assert await device.set_page(page=1)
        assert device.state.navigation.state.configured_route.entry.page_index == 1
        assert device.binding_by_id(old.binding_id) is None
        await eventually(lambda: device.context_for_control("0,0") is not None)
        current = device.context_for_control("0,0")
        assert current.action_uuid == "action.second"
        release.set()
        await device.wait_idle()
        assert device.context_for_control("0,0") is current
        assert not device.state.operations


@pytest.mark.asyncio
async def test_slow_detach_does_not_delay_revocation_or_new_page():
    async with session() as device:
        await device.set_page(page=0)
        await device.wait_idle()
        old = device.context_for_control("0,0")
        entered, release = gate_notification(device, BINDING_DETACHED)
        with anyio.fail_after(0.5):
            assert await device.set_page(page=1)
            await entered.wait()
            await eventually(lambda: device.context_for_control("0,0") is not None)
        assert device.binding_by_id(old.binding_id) is None
        assert device.context_for_control("0,0").action_uuid == "action.second"
        release.set()


@pytest.mark.asyncio
async def test_settings_completion_cannot_install_an_old_page():
    entered, release = anyio.Event(), anyio.Event()

    class Settings:
        async def get(self, target):
            if target.action_id == "action.first":
                entered.set()
                await release.wait()
            raise KeyError(target.action_id)

    async with session(settings=Settings()) as device:
        await device.set_page(page=0)
        await entered.wait()
        with anyio.fail_after(0.5):
            await device.set_page(page=1)
        release.set()
        await device.wait_idle()
        assert device.context_for_control("0,0").action_uuid == "action.second"
        assert all(
            lease.action_uuid == "action.second"
            for lease in device.iter_binding_leases()
        )


@pytest.mark.asyncio
async def test_page_retains_owner_instance_after_launch_binding_detaches():
    async with session() as device:
        await device.set_page(page=0)
        await device.wait_idle()
        owner = device.context_for_control("0,0")
        page = await device.open_page(
            descriptor=_dynamic_page("temporary", "1,0"),
            context_id=owner.id,
            binding_id=owner.binding_id,
        )
        await device.wait_idle()
        assert device.binding_by_id(owner.binding_id) is None
        assert device.active_page_session() is page
        assert owner.action_instance_id in device.snapshot().action_instances
        assert any(
            message.message_type == BINDING_DETACHED
            and message.body.binding.binding_id == owner.binding_id
            for message in _runtime_sender_for(device).messages
        )
        await device.close_page(context_id=page.context_id)
        await device.wait_idle()
        assert device.active_page_session() is None
        assert device.context_for_control("0,0").binding_id != owner.binding_id


@pytest.mark.asyncio
async def test_device_configuration_and_runtime_identities_are_distinct():
    async with session() as first, session() as second:
        assert first.config_id == second.config_id
        assert first.hardware_ref == second.hardware_ref
        assert first.identity.session_id != second.identity.session_id


@pytest.mark.asyncio
@pytest.mark.parametrize("refused", [False, True])
async def test_failed_attachment_retires_grant_and_reconciliation_issues_a_new_one(
    refused,
):
    async with session() as device:
        sender = _runtime_sender_for(device)
        send = sender.send_action_runtime_message
        failed = []

        async def fail_once(**kwargs):
            if kwargs["message_type"] == BINDING_ATTACHED and not failed:
                failed.append(kwargs["body"].binding.binding_id)
                if refused:
                    return False
                raise RuntimeError("attachment refused")
            return await send(**kwargs)

        sender.send_action_runtime_message = fail_once
        await device.set_page(page=0)
        await device.wait_idle()
        assert device.context_for_control("0,0") is None
        assert device.binding_by_id(failed[0]) is None
        assert not device.state.operations
        assert not device.snapshot().binding_leases
        await device.on_action_availability_changed()
        await device.wait_idle()
        assert device.context_for_control("0,0").binding_id != failed[0]


@pytest.mark.asyncio
async def test_disconnect_rejects_pending_completion():
    async with session() as device:
        entered, release = gate_notification(device, BINDING_ATTACHED)
        await device.set_page(page=0)
        await entered.wait()
        async with anyio.create_task_group() as tg:
            tg.start_soon(device.aclose)
            await eventually(lambda: device.state.stopped)
            assert not device.snapshot().active_contexts
            release.set()
        assert not device.state.operations
        assert not device.snapshot().binding_leases
        assert not await device.set_page(page=1)


@pytest.mark.asyncio
async def test_retained_instance_start_is_not_replayed_after_navigation():
    async with session() as device:
        entered, release = gate_notification(device, ACTION_INSTANCE_CREATED)
        await device.set_page(page=0)
        await entered.wait()
        await device.set_page(page=1)
        await device.set_page(page=0)
        release.set()
        await device.wait_idle()
        creates = [
            message
            for message in _runtime_sender_for(device).messages
            if message.message_type == ACTION_INSTANCE_CREATED
            and message.body.metadata.action_id == "action.first"
        ]
        assert len(creates) == 1
        assert device.context_for_control("0,0").action_uuid == "action.first"


@pytest.mark.asyncio
async def test_provider_replacement_rejects_old_attachment_completion():
    async with session() as device:
        entered, release = gate_notification(device, BINDING_ATTACHED)
        await device.set_page(page=0)
        await entered.wait()
        old = next(iter(device.iter_binding_leases()))
        _seed_action_availability(
            device, _metadata("action.first", provider_session_id="successor")
        )
        await device.on_action_availability_changed()
        assert device.binding_by_id(old.binding_id) is None
        release.set()
        await device.wait_idle()
        current = device.context_for_control("0,0")
        assert current.provider_session_id == "successor"
        assert current.binding_id != old.binding_id


@pytest.mark.asyncio
async def test_hardware_successor_cannot_receive_old_preparation():
    class Hardware(FakeHardwareCommandService):
        claim = "original"

        def claim_identity(self, config_id):
            return self.claim

    hardware = Hardware()
    async with session(hardware=hardware) as device:
        entered, release = gate_notification(device, ACTION_INSTANCE_CREATED)
        await device.set_page(page=0)
        await entered.wait()
        hardware.claim = "successor"
        release.set()
        await device.wait_idle()
        assert device.context_for_control("0,0") is None
        assert not device.state.operations
        assert not any(
            message.message_type == BINDING_ATTACHED
            for message in _runtime_sender_for(device).messages
        )
        assert not device.snapshot().binding_leases


@pytest.mark.asyncio
async def test_effects_are_bounded_ordered_and_do_not_retry_failed_input():
    from deckr.controller._session_effects import Effect, SessionEffects

    entered, release, independent = anyio.Event(), anyio.Event(), anyio.Event()
    calls = []

    async def first():
        entered.set()
        await release.wait()
        calls.append("press")

    async def failed_input():
        calls.append("release")
        raise RuntimeError("delivery uncertain")

    async def other():
        independent.set()

    async with anyio.create_task_group() as tg:
        effects = SessionEffects(tg.start_soon, capacity=3)
        assert effects.submit("a", Effect("press", first))
        await entered.wait()
        assert effects.submit("a", Effect("release", failed_input))
        assert effects.submit("b", Effect("independent", other))
        assert not effects.submit("a", Effect("overflow", failed_input))
        for _ in range(3):
            assert effects.submit("a", Effect("cleanup", other, cleanup=True))
        assert not effects.submit("a", Effect("cleanup overflow", other, cleanup=True))
        with anyio.fail_after(0.5):
            await independent.wait()
        assert not calls
        release.set()
        await effects.idle()
        assert calls == ["press", "release"]


@pytest.mark.asyncio
async def test_config_queued_before_disconnect_cannot_reactivate_stopped_session():
    async with session() as device:
        await device.set_page(page=0)
        await device.wait_idle()
        replacement = device.state.config.model_copy(update={"name": "Changed"})
        async with anyio.create_task_group() as tg, device.boundary:
            tg.start_soon(device.on_config_changed, replacement)
            await eventually(lambda: device.boundary.statistics().tasks_waiting == 1)
            # Stop commits first while the config event is waiting to enter.
            await device.aclose()
        assert not device.config_active
        assert device.state.config.name != "Changed"
        assert not device.snapshot().active_contexts


@pytest.mark.asyncio
async def test_preparation_resumes_when_queue_capacity_returns():
    from deckr.controller._session_effects import Effect

    async with session() as device:
        device._effects._capacity = 1
        release = anyio.Event()
        assert device._effects.submit("busy", Effect("busy", release.wait))
        await device.set_page(page=0)
        assert device.context_for_control("0,0") is None
        assert not device.state.operations
        release.set()
        with anyio.fail_after(1):
            await device.wait_idle()
        assert device.context_for_control("0,0").action_uuid == "action.first"
        assert not device.state.operations


@pytest.mark.asyncio
async def test_refused_release_cancels_the_original_press_with_reserved_capacity():
    async with session() as device:
        await device.set_page(page=0)
        await device.wait_idle()
        device._effects._capacity = 1
        entered, release = gate_notification(device, CAPABILITY_INPUT)
        await device.handle_hardware_input(_hardware_input("0,0", "down"))
        await entered.wait()
        await device.handle_hardware_input(_hardware_input("0,0", "up"))
        assert not device.snapshot().held_inputs
        release.set()
        await device.wait_idle()
        assert [
            message.body.event.event_type
            for message in _runtime_sender_for(device).messages
            if message.message_type == CAPABILITY_INPUT
        ] == ["down", "cancel"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "revoke_via", ["navigation", "disconnect", "hardware_successor"]
)
async def test_queued_press_cannot_execute_after_authority_ends(revoke_via):
    from deckr.controller._session_effects import Effect

    class Hardware(FakeHardwareCommandService):
        claim = "original"

        def claim_identity(self, config_id):
            return self.claim

    hardware = Hardware()
    async with session(hardware=hardware) as device:
        await device.set_page(page=0)
        await device.wait_idle()
        owner = device.context_for_control("0,0")
        release = anyio.Event()
        device._effects.submit(owner.action_instance_id, Effect("busy", release.wait))
        await device.handle_hardware_input(_hardware_input("0,0", "down"))
        if revoke_via == "navigation":
            await device.set_page(page=1)
        elif revoke_via == "hardware_successor":
            hardware.claim = "successor"
        else:
            async with anyio.create_task_group() as tg:
                tg.start_soon(device.aclose)
                await eventually(lambda: device.state.stopped)
                release.set()
        release.set()
        await device.wait_idle()
        assert not any(
            message.message_type == CAPABILITY_INPUT
            and message.body.event.event_type == "down"
            for message in _runtime_sender_for(device).messages
        )


@pytest.mark.asyncio
async def test_lifecycle_recovery_does_not_reattach_a_retired_binding():
    async with session() as device:
        await device.set_page(page=0)
        await device.wait_idle()
        old = next(iter(device.iter_binding_leases()))
        _runtime_sender_for(device).messages.clear()
        entered, release = gate_notification(device, ACTION_INSTANCE_CREATED)
        await device._lifecycle.recover_binding_lifecycle(old, reason="stale_lifecycle")
        await entered.wait()
        await device.set_page(page=1)
        release.set()
        await device.wait_idle()
        assert not any(
            message.message_type == BINDING_ATTACHED
            and message.body.binding.binding_id == old.binding_id
            for message in _runtime_sender_for(device).messages
        )
        assert device.context_for_control("0,0").action_uuid == "action.second"


@pytest.mark.asyncio
@pytest.mark.parametrize("refuse_attachment", [False, True])
async def test_recovery_is_a_pending_operation_that_blocks_input(refuse_attachment):
    async with session() as device:
        await device.set_page(page=0)
        await device.wait_idle()
        old = next(iter(device.iter_binding_leases()))
        entered, release = gate_notification(device, ACTION_INSTANCE_CREATED)
        sender = _runtime_sender_for(device)
        send = sender.send_action_runtime_message

        async def recovering(**kwargs):
            if refuse_attachment and kwargs["message_type"] == BINDING_ATTACHED:
                return False
            return await send(**kwargs)

        sender.send_action_runtime_message = recovering
        await device._lifecycle.recover_binding_lifecycle(old, reason="stale_lifecycle")
        await entered.wait()
        assert device.context_for_control("0,0") is None
        assert [op.kind for op in device.state.operations.values()] == [
            "recover_binding"
        ]
        await device.handle_hardware_input(_hardware_input("0,0", "down"))
        assert not device.snapshot().held_inputs
        release.set()
        await device.wait_idle()
        assert not device.state.operations
        if refuse_attachment:
            assert device.binding_by_id(old.binding_id) is None
            assert device.context_for_control("0,0") is None
        else:
            assert device.context_for_control("0,0") is old.context


@pytest.mark.asyncio
async def test_failed_recovery_forgets_previous_instance_readiness():
    async with session() as device:
        await device.set_page(page=0)
        await device.wait_idle()
        old = next(iter(device.iter_binding_leases()))
        sender = _runtime_sender_for(device)
        send = sender.send_action_runtime_message
        creates = []

        async def fail_once(**kwargs):
            if kwargs["message_type"] == ACTION_INSTANCE_CREATED:
                creates.append(kwargs["body"])
                if len(creates) == 1:
                    return False
            return await send(**kwargs)

        sender.send_action_runtime_message = fail_once
        await device._lifecycle.recover_binding_lifecycle(old, reason="stale_lifecycle")
        await device.wait_idle()
        assert device.binding_by_id(old.binding_id) is None
        await device.on_action_availability_changed()
        await device.wait_idle()
        assert len(creates) == 2
        assert device.context_for_control("0,0").binding_id != old.binding_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "close_via", ["direct", "typed", "remote", "expiry", "provider_loss"]
)
async def test_temporary_page_closes_even_if_underlying_layout_disappears(close_via):
    from deckr.controller._actions._requests import ClosePage

    async with session() as device:
        await device.set_page(page=0)
        await device.wait_idle()
        owner = device.context_for_control("0,0")
        requests = device.scoped_requests(owner.metadata)
        page = await device.open_page(
            descriptor=_dynamic_page("temporary", "1,0"),
            context_id=owner.id,
            binding_id=owner.binding_id,
        )
        await device.wait_idle()
        original = device.state.device
        await device.on_device_descriptor_changed(
            original.model_copy(
                update={
                    "controls": tuple(
                        control
                        for control in original.controls
                        if control.control_id != "0,0"
                    ),
                }
            )
        )
        await device.wait_idle()
        assert device.active_page_session() is page
        if close_via == "direct":
            await device.close_page(context_id=page.context_id)
        elif close_via == "typed":
            assert await requests.request(ClosePage(device._page_grant(page)))
        elif close_via == "remote":
            await device.handle_page_command(
                _page_session_command(
                    CLOSE_PAGE,
                    {},
                    session_id=page.page_session_id,
                    context_id=page.context_id,
                    action_instance_id=page.action_instance_id,
                    generation=page.generation,
                )
            )
        elif close_via == "expiry":
            deadline = device.state.navigation.deadline()
            await device.page_expired(deadline, now=deadline.due_at)
        else:
            key = device.provider_session_key_for_session(
                provider_instance_id=owner.provider_instance_id,
                provider_id=owner.provider_id,
                provider_session_id=owner.provider_session_id,
            )
            device._action_service._contracts.pop(key)
            _seed_action_availability(
                device, _metadata("action.first", provider_session_id="successor")
            )
            await device.on_action_availability_changed()
        await device.wait_idle()
        assert device.active_page_session() is None
        assert not device.snapshot().binding_leases
        assert device.state.navigation.state.configured_route.entry.page_index == 0
        await device.on_device_descriptor_changed(original)
        await device.wait_idle()
        assert device.context_for_control("0,0") is not None
