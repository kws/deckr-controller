"""MQTT bridge plugin host: connects controller plugin bus to remote deckr-mqtt-host via MQTT."""

from __future__ import annotations

import logging

from deckr.core.mqtt import MqttGateway
from deckr.plugin.messages import HostMessage

logger = logging.getLogger(__name__)

_MQTT_REQUIRED_MSG = (
    "MQTT bridge requires MQTT_HOSTNAME and MQTT_TOPIC. "
    "Set via environment variables or .env file."
)


def host_factory(event_bus: object) -> MqttGateway:
    """Return MqttGateway for bridging plugin bus to remote host. Fails if not configured."""
    if not MqttGateway.is_enabled():
        logger.error(_MQTT_REQUIRED_MSG)
        raise ValueError(_MQTT_REQUIRED_MSG)
    return MqttGateway(
        event_bus=event_bus,
        serialize=lambda m: m.to_dict(),
        deserialize=HostMessage.from_dict,
        deserialize_from_mqtt=lambda d: HostMessage.from_dict(
            d, internal_metadata={"x-from-mqtt": True}
        ),
        is_event=lambda e: isinstance(e, HostMessage),
    )
