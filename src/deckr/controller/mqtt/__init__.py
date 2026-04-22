"""MQTT bridge plugin host: connects controller plugin bus to remote deckr-mqtt-host via MQTT."""

from __future__ import annotations

import logging
import os

from deckr.core.mqtt import MqttGateway, MqttGatewayConfig
from deckr.plugin.messages import HostMessage

logger = logging.getLogger(__name__)

_MQTT_REQUIRED_MSG = (
    "MQTT bridge requires MQTT_HOSTNAME and MQTT_TOPIC. "
    "Set via environment variables or .env file."
)


def _load_gateway_config() -> MqttGatewayConfig:
    username = os.getenv("MQTT_USERNAME", "").strip() or None
    password = os.getenv("MQTT_PASSWORD", "").strip() if username else None
    return MqttGatewayConfig(
        hostname=os.getenv("MQTT_HOSTNAME", "").strip(),
        port=int(os.getenv("MQTT_PORT", "1883")),
        topic=os.getenv("MQTT_TOPIC", "").strip(),
        username=username,
        password=password or None,
    )


def host_factory(
    event_bus: object,
    *,
    config: MqttGatewayConfig | None = None,
) -> MqttGateway:
    """Return MqttGateway for bridging plugin bus to remote host. Fails if not configured."""
    gateway_config = config or _load_gateway_config()
    if not gateway_config.enabled:
        logger.error(_MQTT_REQUIRED_MSG)
        raise ValueError(_MQTT_REQUIRED_MSG)
    return MqttGateway(
        event_bus=event_bus,
        config=gateway_config,
        serialize=lambda m: m.to_dict(),
        deserialize=HostMessage.from_dict,
        deserialize_from_mqtt=lambda d: HostMessage.from_dict(
            d, internal_metadata={"x-from-mqtt": True}
        ),
        is_event=lambda e: isinstance(e, HostMessage),
    )
