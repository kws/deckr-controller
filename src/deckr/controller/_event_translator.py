"""Translate hardware events to plugin events and dispatch metadata."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from deckr.hardware import messages as hw_messages
from deckr.pluginhost.messages import build_context_id as _build_context_id
from deckr.python_plugin.events import (
    DialRotate,
    KeyDown,
    KeyUp,
    TouchSwipe,
    TouchTap,
)


def build_context_id(controller_id: str, config_id: str, slot_id: str) -> str:
    """Canonical controller-scoped context ID."""
    return _build_context_id(controller_id, config_id, slot_id)


@dataclass(frozen=True, slots=True, kw_only=True)
class TranslatedEvent:
    """Result of translating a hardware event for plugin dispatch."""

    slot_id: str
    method_name: str
    plugin_event: Any
    gesture: str


class EventTranslator:
    """
    Maps hw_messages.* to plugin.events.* and dispatch method names.
    Returns None for non-interaction events.
    """

    def __init__(
        self,
        controller_id: str,
        *,
        is_gesture_supported: Callable[[str, str], bool] | None = None,
    ):
        """
        Optional is_gesture_supported(slot_id, gesture) -> bool.
        If None, all gestures are considered supported (permissive fallback).
        """
        self._controller_id = controller_id
        self._is_gesture_supported = is_gesture_supported or (lambda _s, _g: True)

    def translate(
        self, event: hw_messages.HardwareTransportMessage, config_id: str
    ) -> TranslatedEvent | None:
        """
        Translate a hardware event to plugin dispatch metadata.
        Returns None if event is not an interaction type.
        Caller is responsible for resolving action context by slot_id.
        """
        if isinstance(event, hw_messages.KeyDownMessage):
            return self._translate_key_down(event, config_id)
        if isinstance(event, hw_messages.KeyUpMessage):
            return self._translate_key_up(event, config_id)
        if isinstance(event, hw_messages.DialRotateMessage):
            return self._translate_dial_rotate(event, config_id)
        if isinstance(event, hw_messages.TouchTapMessage):
            return self._translate_touch_tap(event, config_id)
        if isinstance(event, hw_messages.TouchSwipeMessage):
            return self._translate_touch_swipe(event, config_id)

        return None

    def _translate_key_down(
        self, event: hw_messages.KeyDownMessage, config_id: str
    ) -> TranslatedEvent | None:
        slot_id = event.key_id
        if not self._is_gesture_supported(slot_id, "key_down"):
            return None
        context = build_context_id(self._controller_id, config_id, slot_id)
        return TranslatedEvent(
            slot_id=slot_id,
            method_name="on_key_down",
            plugin_event=KeyDown(context=context, slot_id=slot_id),
            gesture="key_down",
        )

    def _translate_key_up(
        self, event: hw_messages.KeyUpMessage, config_id: str
    ) -> TranslatedEvent | None:
        slot_id = event.key_id
        if not self._is_gesture_supported(slot_id, "key_up"):
            return None
        context = build_context_id(self._controller_id, config_id, slot_id)
        return TranslatedEvent(
            slot_id=slot_id,
            method_name="on_key_up",
            plugin_event=KeyUp(context=context, slot_id=slot_id),
            gesture="key_up",
        )

    def _translate_dial_rotate(
        self, event: hw_messages.DialRotateMessage, config_id: str
    ) -> TranslatedEvent | None:
        slot_id = event.dial_id
        if not self._is_gesture_supported(slot_id, "encoder_rotate"):
            return None
        context = build_context_id(self._controller_id, config_id, slot_id)
        return TranslatedEvent(
            slot_id=slot_id,
            method_name="on_dial_rotate",
            plugin_event=DialRotate(
                context=context,
                slot_id=slot_id,
                direction=event.direction,
            ),
            gesture="encoder_rotate",
        )

    def _translate_touch_tap(
        self, event: hw_messages.TouchTapMessage, config_id: str
    ) -> TranslatedEvent | None:
        slot_id = event.touch_id
        if not self._is_gesture_supported(slot_id, "touch_tap"):
            return None
        context = build_context_id(self._controller_id, config_id, slot_id)
        return TranslatedEvent(
            slot_id=slot_id,
            method_name="on_touch_tap",
            plugin_event=TouchTap(context=context, slot_id=slot_id),
            gesture="touch_tap",
        )

    def _translate_touch_swipe(
        self, event: hw_messages.TouchSwipeMessage, config_id: str
    ) -> TranslatedEvent | None:
        slot_id = event.touch_id
        if not self._is_gesture_supported(slot_id, "touch_swipe"):
            return None
        context = build_context_id(self._controller_id, config_id, slot_id)
        return TranslatedEvent(
            slot_id=slot_id,
            method_name="on_touch_swipe",
            plugin_event=TouchSwipe(
                context=context,
                slot_id=slot_id,
                direction=event.direction,
            ),
            gesture="touch_swipe",
        )
