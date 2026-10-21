from __future__ import annotations

import base64
import logging
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from typing import Any

from deckr.hardware import ManagedHardwareClaim
from deckr.hardware.capabilities import (
    RasterBitmapEncoding,
    device_power_command_params,
    raster_bitmap_command_params,
)
from deckr.hardware.descriptors import (
    DECKR_DEVICE_POWER,
    CapabilityRef,
    DeviceDescriptor,
    DeviceRef,
)
from deckr.hardware.state import HardwareUnavailable

from deckr.controller._hardware._routes import LiveDeviceRoute

logger = logging.getLogger(__name__)


class HardwareCommandService:
    """Publishes hardware output commands onto the hardware lane."""

    def __init__(
        self,
        *,
        route_lookup: Callable[[str], LiveDeviceRoute | None],
        claim_lookup: Callable[[str], ManagedHardwareClaim | None],
    ) -> None:
        self._claim_lookup = claim_lookup
        self._route_lookup = route_lookup
        self._render_rejections = OrderedDict()

    def claim_identity(self, config_id):
        live = self._route_lookup(config_id)
        claim = self._claim_lookup(config_id)
        if live is None or claim is None:
            return None
        return (claim, live.contract, live.ref)

    def observe_render_command(
        self, message_id, config_id, control_id, capability_id, command_type, callback
    ):
        now = time.monotonic()
        for key, record in tuple(self._render_rejections.items()):
            if record[0] < now:
                self._render_rejections.pop(key, None)
        while len(self._render_rejections) >= 1024:
            self._render_rejections.popitem(last=False)
        self._render_rejections[message_id] = (
            now + 60,
            self.claim_identity(config_id),
            control_id,
            capability_id,
            command_type,
            callback,
        )

    async def render_command_rejected(self, live, message):
        from deckr.hardware.messages import hardware_body_from_message

        record = self._render_rejections.get(message.in_reply_to)
        if record is None:
            return
        expiry, claim, control_id, capability_id, command_type, callback = record
        event = hardware_body_from_message(message)
        if (
            expiry < time.monotonic()
            or claim != self.claim_identity(live.config_id)
            or message.contract != live.contract
            or event.device_ref != live.ref
            or event.control_id != control_id
            or event.capability_id != capability_id
            or event.command_type != command_type
        ):
            return
        self._render_rejections.pop(message.in_reply_to, None)
        await callback(message)

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
        encoding: RasterBitmapEncoding | None = None,
        *,
        message_id: str | None = None,
        expected_claim=None,
    ) -> None:
        live = await self._live_for(config_id)
        if live is None:
            if message_id is not None:
                raise HardwareUnavailable(
                    "claim_unavailable", "Render output has no live route"
                )
            return
        if encoding is None:
            encoding = "png" if image.startswith(b"\x89PNG\r\n\x1a\n") else "jpeg"
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
            message_id=message_id,
            expected_claim=expected_claim,
        )

    async def clear_raster(
        self,
        config_id: str,
        control_id: str,
        capability_id: str,
        *,
        message_id: str | None = None,
        expected_claim=None,
    ) -> None:
        live = await self._live_for(config_id)
        if live is None:
            if message_id is not None:
                raise HardwareUnavailable(
                    "claim_unavailable", "Render output has no live route"
                )
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
            message_id=message_id,
            expected_claim=expected_claim,
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
        message_id: str | None = None,
        expected_claim=None,
    ) -> None:
        claim = self._claim_lookup(live.config_id)
        if (
            claim is None
            or claim.state.contract != live.contract
            or (
                expected_claim is not None
                and expected_claim != self.claim_identity(live.config_id)
            )
        ):
            if message_id is not None:
                raise HardwareUnavailable(
                    "claim_unavailable", "Render output claim changed"
                )
            return
        await claim.command(
            ref.device_ref.device_id,
            ref.capability_id,
            command_type,
            control_id=ref.control_id,
            params=params,
            **({"message_id": message_id} if message_id is not None else {}),
        )

    def has_authority(self, config_id: str, device_ref: DeviceRef) -> bool:
        live = self._route_lookup(config_id)
        return (
            live is not None
            and live.ref == device_ref
            and self._claim_lookup(config_id) is not None
            and self._claim_lookup(config_id).state.local_authority
            and self._claim_lookup(config_id).state.contract == live.contract
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
