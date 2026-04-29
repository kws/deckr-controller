"""Unit tests for EventTranslator."""

import pytest
from deckr.hardware import messages as hw_messages
from deckr.python_plugin.events import DialRotate, KeyDown, KeyUp, TouchSwipe, TouchTap

from deckr.controller._event_translator import (
    EventTranslator,
    TranslatedEvent,
)

CONTROLLER_ID = "controller-main"


def _event(
    *,
    control_id: str,
    capability_id: str,
    event_type: str,
    value: object | None = None,
) -> hw_messages.ControlInputMessage:
    message = hw_messages.control_input_message(
        manager_id="manager-main",
        device_id="d1",
        control_id=control_id,
        capability_id=capability_id,
        event_type=event_type,
        value=value,
    )
    return hw_messages.hardware_body_from_message(message)


class TestEventTranslator:
    """Translate each hardware input type and verify payload + method name."""

    @pytest.fixture
    def translator(self):
        return EventTranslator(CONTROLLER_ID)

    def test_key_down_event(self, translator):
        event = _event(
            control_id="1,2",
            capability_id="button.momentary",
            event_type="down",
        )
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
        event = _event(
            control_id="0,0",
            capability_id="button.momentary",
            event_type="up",
        )
        out = translator.translate(event, "d1")
        assert out is not None
        assert out.slot_id == "0,0"
        assert out.method_name == "on_key_up"
        assert out.gesture == "key_up"
        assert isinstance(out.plugin_event, KeyUp)
        assert out.plugin_event.context == ""
        assert out.plugin_event.slot_id == "0,0"

    def test_dial_rotate_event(self, translator):
        event = _event(
            control_id="dial1",
            capability_id="encoder.relative",
            event_type="rotate",
            value={"direction": "clockwise"},
        )
        out = translator.translate(event, "d1")
        assert out is not None
        assert out.slot_id == "dial1"
        assert out.method_name == "on_dial_rotate"
        assert out.gesture == "encoder_rotate"
        assert isinstance(out.plugin_event, DialRotate)
        assert out.plugin_event.context == ""
        assert out.plugin_event.slot_id == "dial1"
        assert out.plugin_event.direction == "clockwise"

        event_cc = _event(
            control_id="d2",
            capability_id="encoder.relative",
            event_type="rotate",
            value={"direction": "counterclockwise"},
        )
        out_cc = translator.translate(event_cc, "d1")
        assert out_cc is not None
        assert out_cc.plugin_event.direction == "counterclockwise"

    def test_touch_tap_event(self, translator):
        event = _event(
            control_id="TouchStrip",
            capability_id="touch.gesture",
            event_type="tap",
        )
        out = translator.translate(event, "d1")
        assert out is not None
        assert out.slot_id == "TouchStrip"
        assert out.method_name == "on_touch_tap"
        assert out.gesture == "touch_tap"
        assert isinstance(out.plugin_event, TouchTap)
        assert out.plugin_event.context == ""
        assert out.plugin_event.slot_id == "TouchStrip"

    def test_touch_swipe_event(self, translator):
        event = _event(
            control_id="strip",
            capability_id="touch.gesture",
            event_type="swipe",
            value={"direction": "left"},
        )
        out = translator.translate(event, "d1")
        assert out is not None
        assert out.slot_id == "strip"
        assert out.method_name == "on_touch_swipe"
        assert out.gesture == "touch_swipe"
        assert isinstance(out.plugin_event, TouchSwipe)
        assert out.plugin_event.context == ""
        assert out.plugin_event.slot_id == "strip"
        assert out.plugin_event.direction == "left"

        event_r = _event(
            control_id="strip",
            capability_id="touch.gesture",
            event_type="swipe",
            value={"direction": "right"},
        )
        out_r = translator.translate(event_r, "d1")
        assert out_r is not None
        assert out_r.plugin_event.direction == "right"

    def test_non_interaction_events_return_none(self, translator):
        event = hw_messages.DeviceUnavailableMessage(
            deviceRef={"managerId": "manager-main", "deviceId": "d1"}
        )
        assert translator.translate(event, "d1") is None

    def test_gesture_unsupported_returns_none(self):
        def no_gestures(slot_id: str, gesture: str) -> bool:
            return False

        translator = EventTranslator(
            CONTROLLER_ID,
            is_gesture_supported=no_gestures,
        )
        event = _event(
            control_id="0,0",
            capability_id="button.momentary",
            event_type="up",
        )
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
                _event(
                    control_id="0,0",
                    capability_id="button.momentary",
                    event_type="up",
                ),
                "d1",
            )
            is not None
        )
        assert (
            translator.translate(
                _event(
                    control_id="0,0",
                    capability_id="button.momentary",
                    event_type="down",
                ),
                "d1",
            )
            is None
        )
