"""Translate hardware input events to plugin events and dispatch metadata."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from deckr.hardware import messages as hw_messages
from deckr.python_plugin.events import (
    DialRotate,
    KeyDown,
    KeyUp,
    TouchSwipe,
    TouchTap,
)


@dataclass(frozen=True, slots=True, kw_only=True)
class TranslatedEvent:
    """Result of translating a hardware event for plugin dispatch."""

    slot_id: str
    method_name: str
    plugin_event: Any
    gesture: str


class EventTranslator:
    """
    Maps capability-targeted control input to current plugin dispatch methods.
    Returns None for non-interaction events.
    """

    def __init__(
        self,
        controller_id: str,
        *,
        is_gesture_supported: Callable[[str, str], bool] | None = None,
    ):
        """
        Optional is_gesture_supported(control_id, gesture) -> bool.
        If None, all gestures are considered supported.
        """
        self._controller_id = controller_id
        self._is_gesture_supported = is_gesture_supported or (lambda _s, _g: True)

    def translate(
        self, event: hw_messages.HardwareTransportMessage, config_id: str
    ) -> TranslatedEvent | None:
        """
        Translate a hardware event to plugin dispatch metadata.
        Returns None if event is not an interaction type.
        Caller is responsible for resolving action context by control id.
        """
        del config_id
        if not isinstance(event, hw_messages.ControlInputMessage):
            return None
        if event.event_type == "down":
            return self._translate_key_down(event)
        if event.event_type == "up":
            return self._translate_key_up(event)
        if event.event_type == "rotate":
            return self._translate_dial_rotate(event)
        if event.event_type == "tap":
            return self._translate_touch_tap(event)
        if event.event_type == "swipe":
            return self._translate_touch_swipe(event)
        return None

    def _translate_key_down(
        self, event: hw_messages.ControlInputMessage
    ) -> TranslatedEvent | None:
        control_id = event.control_id
        if not self._is_gesture_supported(control_id, "key_down"):
            return None
        return TranslatedEvent(
            slot_id=control_id,
            method_name="on_key_down",
            plugin_event=KeyDown(context="", slot_id=control_id),
            gesture="key_down",
        )

    def _translate_key_up(
        self, event: hw_messages.ControlInputMessage
    ) -> TranslatedEvent | None:
        control_id = event.control_id
        if not self._is_gesture_supported(control_id, "key_up"):
            return None
        return TranslatedEvent(
            slot_id=control_id,
            method_name="on_key_up",
            plugin_event=KeyUp(context="", slot_id=control_id),
            gesture="key_up",
        )

    def _translate_dial_rotate(
        self, event: hw_messages.ControlInputMessage
    ) -> TranslatedEvent | None:
        control_id = event.control_id
        if not self._is_gesture_supported(control_id, "encoder_rotate"):
            return None
        value = event.value if isinstance(event.value, Mapping) else {}
        direction = value.get("direction")
        if direction not in {"clockwise", "counterclockwise"}:
            delta = value.get("delta")
            direction = (
                "clockwise"
                if isinstance(delta, int | float) and delta >= 0
                else "counterclockwise"
            )
        return TranslatedEvent(
            slot_id=control_id,
            method_name="on_dial_rotate",
            plugin_event=DialRotate(
                context="",
                slot_id=control_id,
                direction=direction,
            ),
            gesture="encoder_rotate",
        )

    def _translate_touch_tap(
        self, event: hw_messages.ControlInputMessage
    ) -> TranslatedEvent | None:
        control_id = event.control_id
        if not self._is_gesture_supported(control_id, "touch_tap"):
            return None
        return TranslatedEvent(
            slot_id=control_id,
            method_name="on_touch_tap",
            plugin_event=TouchTap(context="", slot_id=control_id),
            gesture="touch_tap",
        )

    def _translate_touch_swipe(
        self, event: hw_messages.ControlInputMessage
    ) -> TranslatedEvent | None:
        control_id = event.control_id
        if not self._is_gesture_supported(control_id, "touch_swipe"):
            return None
        value = event.value if isinstance(event.value, Mapping) else {}
        direction = value.get("direction")
        if direction not in {"left", "right"}:
            return None
        return TranslatedEvent(
            slot_id=control_id,
            method_name="on_touch_swipe",
            plugin_event=TouchSwipe(
                context="",
                slot_id=control_id,
                direction=direction,
            ),
            gesture="touch_swipe",
        )
