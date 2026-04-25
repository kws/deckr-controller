from __future__ import annotations

from deckr.hardware import events as hw_events
from deckr.transports.bus import EventBus


class HardwareDeviceRegistry:
    """Controller-local cache of connected hardware metadata."""

    def __init__(self) -> None:
        self._devices: dict[str, hw_events.HardwareDevice] = {}

    def connect(
        self,
        message: hw_events.DeviceConnectedMessage,
    ) -> hw_events.HardwareDevice:
        info = message.device.model_copy(update={"id": message.device_id})
        self._devices[message.device_id] = info
        return info

    def disconnect(self, device_id: str) -> hw_events.HardwareDevice | None:
        return self._devices.pop(device_id, None)

    def get(self, device_id: str) -> hw_events.HardwareDevice | None:
        return self._devices.get(device_id)


class HardwareCommandService:
    """Publishes hardware output commands onto the hardware lane."""

    def __init__(self, event_bus: EventBus) -> None:
        self._event_bus = event_bus

    async def set_image(self, device_id: str, slot_id: str, image: bytes) -> None:
        await self._event_bus.send(
            hw_events.SetImageMessage(
                device_id=device_id,
                slot_id=slot_id,
                image=image,
            )
        )

    async def clear_slot(self, device_id: str, slot_id: str) -> None:
        await self._event_bus.send(
            hw_events.ClearSlotMessage(
                device_id=device_id,
                slot_id=slot_id,
            )
        )

    async def sleep_screen(self, device_id: str) -> None:
        await self._event_bus.send(hw_events.SleepScreenMessage(device_id=device_id))

    async def wake_screen(self, device_id: str) -> None:
        await self._event_bus.send(hw_events.WakeScreenMessage(device_id=device_id))
