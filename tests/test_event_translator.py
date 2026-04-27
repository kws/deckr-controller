"""Unit tests for EventTranslator."""

import pytest
from deckr.hardware import messages as hw_messages
from deckr.python_plugin.events import DialRotate, KeyDown, KeyUp, TouchSwipe, TouchTap

from deckr.controller._event_translator import (
    EventTranslator,
    TranslatedEvent,
)

CONTROLLER_ID = "controller-main"


class TestEventTranslator:
    """Translate each HW event type and verify payload + method name."""

    @pytest.fixture
    def translator(self):
        return EventTranslator(CONTROLLER_ID)

    def test_key_down_event(self, translator):
        event = hw_messages.KeyDownMessage(key_id="1,2")
        out = translator.translate(event, "d1")
        assert out is not None
        assert isinstance(out, TranslatedEvent)
        assert out.slot_id == "1,2"
        assert out.method_name == "on_key_down"
        assert out.gesture == "key_down"
        assert isinstance(out.plugin_event, KeyDown)
        assert out.plugin_event.context == ""
        assert out.plugin_event.slot_id == "1,2"

    def test_key_up_event(self, translator):
        event = hw_messages.KeyUpMessage(key_id="0,0")
        out = translator.translate(event, "d1")
        assert out is not None
        assert out.slot_id == "0,0"
        assert out.method_name == "on_key_up"
        assert out.gesture == "key_up"
        assert isinstance(out.plugin_event, KeyUp)
        assert out.plugin_event.context == ""
        assert out.plugin_event.slot_id == "0,0"

    def test_dial_rotate_event(self, translator):
        event = hw_messages.DialRotateMessage(dial_id="dial1", direction="clockwise")
        out = translator.translate(event, "d1")
        assert out is not None
        assert out.slot_id == "dial1"
        assert out.method_name == "on_dial_rotate"
        assert out.gesture == "encoder_rotate"
        assert isinstance(out.plugin_event, DialRotate)
        assert out.plugin_event.context == ""
        assert out.plugin_event.slot_id == "dial1"
        assert out.plugin_event.direction == "clockwise"

        event_cc = hw_messages.DialRotateMessage(
            dial_id="d2", direction="counterclockwise"
        )
        out_cc = translator.translate(event_cc, "d1")
        assert out_cc is not None
        assert out_cc.plugin_event.direction == "counterclockwise"

    def test_touch_tap_event(self, translator):
        event = hw_messages.TouchTapMessage(touch_id="TouchStrip")
        out = translator.translate(event, "d1")
        assert out is not None
        assert out.slot_id == "TouchStrip"
        assert out.method_name == "on_touch_tap"
        assert out.gesture == "touch_tap"
        assert isinstance(out.plugin_event, TouchTap)
        assert out.plugin_event.context == ""
        assert out.plugin_event.slot_id == "TouchStrip"

    def test_touch_swipe_event(self, translator):
        event = hw_messages.TouchSwipeMessage(touch_id="strip", direction="left")
        out = translator.translate(event, "d1")
        assert out is not None
        assert out.slot_id == "strip"
        assert out.method_name == "on_touch_swipe"
        assert out.gesture == "touch_swipe"
        assert isinstance(out.plugin_event, TouchSwipe)
        assert out.plugin_event.context == ""
        assert out.plugin_event.slot_id == "strip"
        assert out.plugin_event.direction == "left"

        event_r = hw_messages.TouchSwipeMessage(touch_id="strip", direction="right")
        out_r = translator.translate(event_r, "d1")
        assert out_r is not None
        assert out_r.plugin_event.direction == "right"

    def test_non_interaction_events_return_none(self, translator):
        event = hw_messages.DeviceDisconnectedMessage()
        assert translator.translate(event, "d1") is None

    def test_gesture_unsupported_returns_none(self):
        def no_gestures(slot_id: str, gesture: str) -> bool:
            return False

        translator = EventTranslator(
            CONTROLLER_ID,
            is_gesture_supported=no_gestures,
        )
        event = hw_messages.KeyUpMessage(key_id="0,0")
        assert translator.translate(event, "d1") is None

    def test_gesture_supported_filter(self):
        def only_key_up(slot_id: str, gesture: str) -> bool:
            return gesture == "key_up"

        translator = EventTranslator(
            CONTROLLER_ID,
            is_gesture_supported=only_key_up,
        )
        assert (
            translator.translate(
                hw_messages.KeyUpMessage(key_id="0,0"), "d1"
            )
            is not None
        )
        assert (
            translator.translate(
                hw_messages.KeyDownMessage(key_id="0,0"), "d1"
            )
            is None
        )
