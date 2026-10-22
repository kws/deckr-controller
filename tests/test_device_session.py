"""Acceptance tests across the device state / adapter boundary."""

import anyio
import pytest
from deckr.actions.messages import (
    ACTION_INSTANCE_CREATED,
    BINDING_ATTACHED,
    BINDING_DETACHED,
    CAPABILITY_INPUT,
)

from test_support.action_catalog import _metadata
from test_support.binding_messages import _hardware_input
from test_support.binding_runtime import _runtime_sender_for, _seed_action_availability
from test_support.device_layouts import FakeHardwareCommandService, _dynamic_page
from test_support.device_session import gate_notification, session
from test_support.managed_hardware import eventually


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
        assert current.action_id == "action.second"
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
        assert device.context_for_control("0,0").action_id == "action.second"
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
        assert device.context_for_control("0,0").action_id == "action.second"
        assert all(
            lease.action_id == "action.second" for lease in device.iter_binding_leases()
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
        assert device.context_for_control("0,0").action_id == "action.first"


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
        device.components.effects._capacity = 1
        release = anyio.Event()
        assert device.components.effects.submit("busy", Effect("busy", release.wait))
        await device.set_page(page=0)
        assert device.context_for_control("0,0") is None
        assert not device.state.operations
        release.set()
        with anyio.fail_after(1):
            await device.wait_idle()
        assert device.context_for_control("0,0").action_id == "action.first"
        assert not device.state.operations


@pytest.mark.asyncio
async def test_refused_release_cancels_the_original_press_with_reserved_capacity():
    async with session() as device:
        await device.set_page(page=0)
        await device.wait_idle()
        device.components.effects._capacity = 1
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
        device.components.effects.submit(
            owner.action_instance_id, Effect("busy", release.wait)
        )
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
