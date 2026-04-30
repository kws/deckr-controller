from __future__ import annotations

import base64
import logging
from dataclasses import dataclass

from deckr.hardware import messages as hw_messages
from deckr.hardware.descriptors import (
    DECKR_DEVICE_POWER,
    CapabilityRef,
    DeviceDescriptor,
    DeviceRef,
)
from deckr.lanes import EndpointLane

logger = logging.getLogger(__name__)


def _ref_key(ref: DeviceRef) -> tuple[str, str]:
    return ref.manager_id, ref.device_id


@dataclass(frozen=True, slots=True)
class LiveHardwareDevice:
    config_id: str
    ref: DeviceRef
    device: DeviceDescriptor


class HardwareDeviceRegistry:
    """Controller-local cache of live hardware metadata by config id and live ref."""

    def __init__(self) -> None:
        self._devices_by_config: dict[str, LiveHardwareDevice] = {}
        self._config_by_ref: dict[tuple[str, str], str] = {}

    def connect(
        self,
        *,
        config_id: str,
        ref: DeviceRef,
        device: DeviceDescriptor,
    ) -> LiveHardwareDevice:
        self.disconnect_config(config_id)
        live = LiveHardwareDevice(config_id=config_id, ref=ref, device=device)
        self._devices_by_config[config_id] = live
        self._config_by_ref[_ref_key(ref)] = config_id
        return live

    def update_descriptor(
        self,
        *,
        ref: DeviceRef,
        device: DeviceDescriptor,
    ) -> LiveHardwareDevice | None:
        config_id = self._config_by_ref.get(_ref_key(ref))
        if config_id is None:
            return None
        live = LiveHardwareDevice(config_id=config_id, ref=ref, device=device)
        self._devices_by_config[config_id] = live
        return live

    def disconnect_config(self, config_id: str) -> LiveHardwareDevice | None:
        live = self._devices_by_config.pop(config_id, None)
        if live is not None:
            self._config_by_ref.pop(_ref_key(live.ref), None)
        return live

    def disconnect_ref(self, ref: DeviceRef) -> LiveHardwareDevice | None:
        config_id = self._config_by_ref.pop(_ref_key(ref), None)
        if config_id is None:
            return None
        return self._devices_by_config.pop(config_id, None)

    def get(self, config_id: str) -> LiveHardwareDevice | None:
        return self._devices_by_config.get(config_id)

    def get_by_ref(self, ref: DeviceRef) -> LiveHardwareDevice | None:
        config_id = self._config_by_ref.get(_ref_key(ref))
        if config_id is None:
            return None
        return self._devices_by_config.get(config_id)

    def for_manager(self, manager_id: str) -> tuple[LiveHardwareDevice, ...]:
        return tuple(
            live
            for live in self._devices_by_config.values()
            if live.ref.manager_id == manager_id
        )

    def all(self) -> tuple[LiveHardwareDevice, ...]:
        return tuple(self._devices_by_config.values())


class HardwareCommandService:
    """Publishes hardware output commands onto the hardware lane."""

    def __init__(self, endpoint: EndpointLane, *, controller_id: str) -> None:
        self._endpoint = endpoint
        self._controller_id = controller_id
        self._devices_by_config_id: dict[str, LiveHardwareDevice] = {}

    def register_device(
        self,
        *,
        config_id: str,
        ref: DeviceRef,
        device: DeviceDescriptor,
    ) -> None:
        self._devices_by_config_id[config_id] = LiveHardwareDevice(
            config_id=config_id,
            ref=ref,
            device=device,
        )

    def unregister_config(self, config_id: str) -> None:
        self._devices_by_config_id.pop(config_id, None)

    async def _live_for(self, config_id: str) -> LiveHardwareDevice | None:
        live = self._devices_by_config_id.get(config_id)
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
    ) -> None:
        live = await self._live_for(config_id)
        if live is None:
            return
        await self._endpoint.publish(
            hw_messages.control_command_for_capability(
                controller_id=self._controller_id,
                ref=CapabilityRef(
                    deviceRef=live.ref,
                    controlId=control_id,
                    capabilityId=capability_id,
                ),
                command_type="set_frame",
                params={
                    "commandType": "set_frame",
                    "image": base64.b64encode(image).decode("ascii"),
                    "encoding": "jpeg",
                },
            )
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
        await self._endpoint.publish(
            hw_messages.control_command_for_capability(
                controller_id=self._controller_id,
                ref=CapabilityRef(
                    deviceRef=live.ref,
                    controlId=control_id,
                    capabilityId=capability_id,
                ),
                command_type="clear",
                params={"commandType": "clear"},
            )
        )

    async def sleep_screen(self, config_id: str) -> None:
        await self._send_power_command(config_id, command_type="sleep")

    async def wake_screen(self, config_id: str) -> None:
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
        await self._endpoint.publish(
            hw_messages.control_command_for_capability(
                controller_id=self._controller_id,
                ref=CapabilityRef(
                    deviceRef=live.ref,
                    capabilityId=capability_id,
                ),
                command_type=command_type,
                params={"commandType": command_type},
            )
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
