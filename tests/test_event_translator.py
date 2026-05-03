"""Unit tests for capability-native EventTranslator."""

import pytest
from deckr.hardware import messages as hw_messages

from deckr.controller._event_translator import EventTranslator, TranslatedEvent

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
        sender_session_id="manager-session",
        device_id="d1",
        control_id=control_id,
        capability_id=capability_id,
        event_type=event_type,
        value=value,
    )
    return hw_messages.hardware_body_from_message(message)


class TestEventTranslator:
    @pytest.fixture
    def translator(self):
        return EventTranslator(CONTROLLER_ID)

    @pytest.mark.parametrize(
        ("event_type", "value"),
        [
            ("down", None),
            ("up", None),
            ("press", None),
            ("rotate", {"delta": 1}),
            ("tap", None),
            ("swipe", {"direction": "left"}),
        ],
    )
    def test_input_event_is_delivered_as_capability_event(
        self,
        translator,
        event_type,
        value,
    ):
        event = _event(
            control_id="1,2",
            capability_id="button.momentary",
            event_type=event_type,
            value=value,
        )
        out = translator.translate(event, "d1")
        assert out is not None
        assert isinstance(out, TranslatedEvent)
        assert out.control_id == "1,2"
        assert out.capability_id == "button.momentary"
        assert out.action_event.event_type == event_type
        assert out.action_event.value == value
        assert out.action_event.capability.control_id == "1,2"
        assert out.action_event.capability.capability_id == "button.momentary"
        assert out.action_event.producer == "manager-main"
        assert out.action_event.view == "native"

    def test_non_interaction_events_return_none(self, translator):
        event = hw_messages.DeviceUnavailableMessage(
            deviceRef={"managerId": "manager-main", "deviceId": "d1"}
        )
        assert translator.translate(event, "d1") is None

    def test_gesture_unsupported_returns_none(self):
        translator = EventTranslator(
            CONTROLLER_ID,
            is_gesture_supported=lambda _control_id, _event_type: False,
        )
        event = _event(
            control_id="0,0",
            capability_id="button.momentary",
            event_type="up",
        )
        assert translator.translate(event, "d1") is None

    def test_gesture_supported_filter(self):
        translator = EventTranslator(
            CONTROLLER_ID,
            is_gesture_supported=lambda _control_id, event_type: event_type == "up",
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
