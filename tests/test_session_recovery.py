"""Acceptance tests across the device state / adapter boundary."""

import pytest
from deckr.actions.messages import (
    ACTION_INSTANCE_CREATED,
    BINDING_ATTACHED,
    CLOSE_PAGE,
)

from test_support.action_catalog import _metadata
from test_support.binding_messages import _hardware_input, _page_session_command
from test_support.binding_runtime import _runtime_sender_for, _seed_action_availability
from test_support.device_layouts import _dynamic_page
from test_support.device_session import gate_notification, session


@pytest.mark.asyncio
async def test_lifecycle_recovery_does_not_reattach_a_retired_binding():
    async with session() as device:
        await device.set_page(page=0)
        await device.wait_idle()
        old = next(iter(device.iter_binding_leases()))
        _runtime_sender_for(device).messages.clear()
        entered, release = gate_notification(device, ACTION_INSTANCE_CREATED)
        await device.components.lifecycle.recover_binding_lifecycle(
            old, reason="stale_lifecycle"
        )
        await entered.wait()
        await device.set_page(page=1)
        release.set()
        await device.wait_idle()
        assert not any(
            message.message_type == BINDING_ATTACHED
            and message.body.binding.binding_id == old.binding_id
            for message in _runtime_sender_for(device).messages
        )
        assert device.context_for_control("0,0").action_id == "action.second"


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
        await device.components.lifecycle.recover_binding_lifecycle(
            old, reason="stale_lifecycle"
        )
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
        await device.components.lifecycle.recover_binding_lifecycle(
            old, reason="stale_lifecycle"
        )
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
            assert await requests.request(
                ClosePage(device.components.requests._page_grant(page))
            )
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
            device.components.actions._contracts.pop(key)
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
