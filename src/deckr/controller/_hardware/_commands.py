from __future__ import annotations

import base64
import logging
from collections.abc import Callable, Mapping
from typing import Any

from deckr.contracts.messages import HARDWARE_MESSAGES_LANE, hardware_manager_address
from deckr.hardware import messages as hw_messages
from deckr.hardware.capabilities import (
    RasterBitmapEncoding,
    device_power_command_params,
    raster_bitmap_command_params,
)
from deckr.hardware.descriptors import (
    DECKR_DEVICE_POWER,
    CapabilityRef,
    DeviceDescriptor,
)
from deckr.lanes import EndpointSession

from deckr.controller._hardware._routes import LiveDeviceRoute

logger = logging.getLogger(__name__)


class HardwareCommandService:
    """Publishes hardware output commands onto the hardware lane."""

    def __init__(
        self,
        endpoint: EndpointSession,
        *,
        route_lookup: Callable[[str], LiveDeviceRoute | None],
    ) -> None:
        self._endpoint = endpoint
        self._route_lookup = route_lookup

    async def _live_for(self, config_id: str) -> LiveDeviceRoute | None:
        live = self._route_lookup(config_id)
        if live is None:
            logger.info(
                "Dropping hardware output for unavailable config %s",
                config_id,
            )
        return live

    async def set_raster_frame(
        self,
        config_id: str,
        control_id: str,
        capability_id: str,
        image: bytes,
        encoding: RasterBitmapEncoding = "jpeg",
    ) -> None:
        live = await self._live_for(config_id)
        if live is None:
            return
        params = raster_bitmap_command_params(
            "set_frame",
            {
                "image": base64.b64encode(image).decode("ascii"),
                "encoding": encoding,
            },
        ).model_dump(by_alias=True, exclude_none=True)
        await self._send_control_command(
            live=live,
            ref=CapabilityRef(
                deviceRef=live.ref,
                controlId=control_id,
                capabilityId=capability_id,
            ),
            command_type="set_frame",
            params=params,
        )

    async def clear_raster(
        self,
        config_id: str,
        control_id: str,
        capability_id: str,
    ) -> None:
        live = await self._live_for(config_id)
        if live is None:
            return
        params = raster_bitmap_command_params("clear", {}).model_dump(
            by_alias=True,
            exclude_none=True,
        )
        await self._send_control_command(
            live=live,
            ref=CapabilityRef(
                deviceRef=live.ref,
                controlId=control_id,
                capabilityId=capability_id,
            ),
            command_type="clear",
            params=params,
        )

    async def sleep_device(self, config_id: str) -> None:
        await self._send_power_command(config_id, command_type="sleep")

    async def wake_device(self, config_id: str) -> None:
        await self._send_power_command(config_id, command_type="wake")

    async def _send_power_command(self, config_id: str, *, command_type: str) -> None:
        live = await self._live_for(config_id)
        if live is None:
            return
        capability_id = _device_power_capability_id(live.device, command_type)
        if capability_id is None:
            logger.info(
                "Dropping %s command for config %s without advertised power capability",
                command_type,
                config_id,
            )
            return
        await self._send_control_command(
            live=live,
            ref=CapabilityRef(
                deviceRef=live.ref,
                capabilityId=capability_id,
            ),
            command_type=command_type,
            params=device_power_command_params({}).model_dump(
                by_alias=True,
                exclude_none=True,
            ),
        )

    async def _send_control_command(
        self,
        *,
        live: LiveDeviceRoute,
        ref: CapabilityRef,
        command_type: str,
        params: Mapping[str, Any],
    ) -> None:
        device = ref.device_ref
        if device is None:
            raise ValueError("hardware control commands require deviceRef")
        body = hw_messages.ControlCommandMessage(
            deviceRef=device,
            controlId=ref.control_id,
            capabilityId=ref.capability_id,
            commandType=command_type,
            params=dict(params),
        )
        await self._endpoint.send(
            lane=HARDWARE_MESSAGES_LANE,
            recipient=hardware_manager_address(device.manager_id),
            recipient_session_id=live.manager_session_id,
            message_type=hw_messages.CONTROL_COMMAND,
            body=hw_messages.hardware_body_to_dict(body),
            subject=hw_messages.hardware_subject_for_capability(ref),
            contract=live.contract,
        )


def _device_power_capability_id(
    device: DeviceDescriptor,
    command_type: str,
) -> str | None:
    for capability in device.capabilities:
        if capability.family != DECKR_DEVICE_POWER:
            continue
        if capability.command_types and command_type not in capability.command_types:
            continue
        return capability.capability_id
    return None
