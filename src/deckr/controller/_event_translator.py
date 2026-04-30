"""Translate hardware input messages to capability-native plugin input."""

from collections.abc import Callable
from dataclasses import dataclass

from deckr.contracts.models import thaw_json
from deckr.hardware import messages as hw_messages
from deckr.hardware.descriptors import CapabilityRef
from deckr.pluginhost.messages import CapabilityInputEvent


@dataclass(frozen=True, slots=True, kw_only=True)
class TranslatedEvent:
    """Capability-native plugin input plus the control used for lease lookup."""

    control_id: str
    capability_id: str
    plugin_event: CapabilityInputEvent


class EventTranslator:
    """Maps descriptor capability input to the plugin input contract."""

    def __init__(
        self,
        controller_id: str,
        *,
        is_gesture_supported: Callable[[str, str], bool] | None = None,
    ):
        del controller_id
        self._is_gesture_supported = is_gesture_supported or (lambda _s, _g: True)

    def translate(
        self, event: hw_messages.HardwareTransportMessage, config_id: str
    ) -> TranslatedEvent | None:
        del config_id
        if not isinstance(event, hw_messages.ControlInputMessage):
            return None
        if not self._is_gesture_supported(event.control_id, event.event_type):
            return None
        capability = CapabilityRef(
            deviceRef=event.device_ref,
            controlId=event.control_id,
            capabilityId=event.capability_id,
        )
        return TranslatedEvent(
            control_id=event.control_id,
            capability_id=event.capability_id,
            plugin_event=CapabilityInputEvent(
                capability=capability,
                eventType=event.event_type,
                value=thaw_json(event.value),
                sequence=event.sequence,
                occurredAt=event.occurred_at,
                producer=event.device_ref.manager_id,
                view="native",
            ),
        )
