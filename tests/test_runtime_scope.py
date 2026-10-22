"""Service-use changes fence queued effects, including same-session recovery."""

import anyio
import pytest
from deckr.action_runtime import action_availability_view_ref
from deckr.actions.messages import (
    ACTION_INSTANCE_DESTROYED,
    ActionInstanceLifecycleBody,
)

from deckr.controller._device_components import DeferredLifecycleNotifications
from deckr.controller._session_effects import SessionEffects
from test_support.action_runtime import running_controller
from test_support.managed_hardware import eventually


@pytest.mark.asyncio
async def test_cleanup_queued_before_same_session_successor_is_discarded(
    managed_substrate,
):
    async with running_controller(managed_substrate) as h:
        context = h.device.context_for_control("key-1")
        lease = h.device.binding_by_id(context.binding_id)
        original_contract = context.contract
        scheduled = []
        effects = SessionEffects(
            lambda function, *args: scheduled.append((function, args))
        )
        sender = DeferredLifecycleNotifications(effects, h.device)
        metadata = h.device.components.lifecycle.instance_metadata(
            lease.action_instance_id
        )
        assert await sender.send_action_runtime_message(
            provider_session_key=lease.provider_session_key,
            contract=original_contract,
            message_type=ACTION_INSTANCE_DESTROYED,
            body=ActionInstanceLifecycleBody(
                metadata=metadata, reason="queued before recovery"
            ),
        )
        await h.provider.cancel_authorized(
            h.received[-1].authorization, reason="same-session recovery"
        )
        await eventually(
            lambda: h.actions.current_contract(lease.provider_session_key)
            not in (None, original_contract)
        )
        before = len(h.received)
        for function, args in scheduled:
            await function(*args)
        # The scoped gateway refuses the send synchronously; draining lets an
        # accidental transport delivery reach the provider before assertion.
        for _ in range(10):
            await anyio.sleep(0)
        assert len(h.received) == before
        assert (
            h.actions.current_contract(lease.provider_session_key) != original_contract
        )


@pytest.mark.asyncio
async def test_malformed_availability_does_not_end_watch_or_other_work(
    managed_substrate, caplog
):
    async with running_controller(managed_substrate) as h:
        original = h.device.context_for_control("key-1")
        access = h.channel.view_access(h.availability_store)
        ref = action_availability_view_ref("action-runtime.test")
        await access.put(ref, {})
        await eventually(
            lambda: "Ignoring invalid action availability view" in caplog.text
        )
        assert h.device.context_for_control("key-1") is original
        # A subsequent valid projection must still reach the same watch worker.
        removed = h.availability_payload.model_copy(update={"entries": ()})
        await access.put(ref, removed.model_dump(by_alias=True, mode="json"))
        await eventually(lambda: h.device.context_for_control("key-1") is None)
