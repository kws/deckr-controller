"""Terminal input events retain the exact routed press provenance."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from deckr.actions.messages import CapabilityInputEvent
from deckr.hardware.descriptors import CapabilityRef, DeviceRef

from deckr.controller._bindings._attachments import ControlAttachmentState
from deckr.controller._bindings._input import InputInteractions
from deckr.controller._event_translator import TranslatedEvent


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provenance,view", [("projection", "projected"), ("derivation", "derived")]
)
async def test_cancel_retains_projected_or_derived_press(provenance, view):
    capability = CapabilityRef(
        deviceRef=DeviceRef(managerId="manager", deviceId="device"),
        controlId="key",
        capabilityId="press",
    )
    source = capability.model_copy(update={"capability_id": "touch"})
    event = CapabilityInputEvent(
        capability=capability,
        eventType="down",
        value=True,
        sequence=1,
        occurredAt="2026-10-22T10:00:00Z",
        producer="manager",
        view="native",
    )
    context = SimpleNamespace(
        on_input=AsyncMock(),
        metadata=SimpleNamespace(
            matched_capabilities=[
                SimpleNamespace(
                    capability=capability, provenance=provenance, source=source
                )
            ]
        ),
    )
    lease = SimpleNamespace(
        binding_id="binding",
        context_id="context",
        attached=True,
        ready=True,
        action_instance_id="instance",
        action_id="action",
        input_capability_ids={"press"},
        provider_session_key=None,
        context=context,
    )
    attachments = ControlAttachmentState()
    attachments.add_binding(lease)
    effects = []

    class Queue:
        def submit(self, key, effect):
            effects.append(effect)
            return True

    inputs = InputInteractions(
        state=SimpleNamespace(hardware_current=lambda _: True, attachments=attachments),
        effects=Queue(),
        translator=None,
        catalog=None,
        hardware=None,
    )
    await inputs._deliver_input_to_lease(
        lease,
        TranslatedEvent(control_id="key", capability_id="press", action_event=event),
    )
    await inputs._cancel_held_inputs_for_binding(lease)
    for effect in effects:
        await effect.run()
    down, cancel = [call.args[0] for call in context.on_input.await_args_list]
    assert (down.event_type, cancel.event_type) == ("down", "cancel")
    assert down.view == cancel.view == view
    assert down.source == cancel.source == source
    assert not attachments.held_input_bindings
