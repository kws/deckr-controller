from __future__ import annotations

from deckr.hardware import events as hw_events
from deckr.transports.bus import EventBus


class HardwareDeviceRegistry:
    """Controller-local cache of connected hardware metadata."""

    def __init__(self) -> None:
        self._devices: dict[str, hw_events.HardwareDevice] = {}

    def connect(
        self,
        envelope,
        message: hw_events.DeviceConnectedMessage,
    ) -> hw_events.HardwareDevice:
        device_id = hw_events.subject_device_id(envelope.subject)
        if device_id is None:
            raise ValueError("Hardware device connection is missing subject deviceId")
        info = message.device.model_copy(update={"id": device_id})
        self._devices[device_id] = info
        return info

    def disconnect(self, device_id: str) -> hw_events.HardwareDevice | None:
        return self._devices.pop(device_id, None)

    def get(self, device_id: str) -> hw_events.HardwareDevice | None:
        return self._devices.get(device_id)


class HardwareCommandService:
    """Publishes hardware output commands onto the hardware lane."""

    def __init__(self, event_bus: EventBus, *, controller_id: str) -> None:
        self._event_bus = event_bus
        self._controller_id = controller_id
        self._manager_by_device_id: dict[str, str] = {}

    def register_device(self, envelope) -> None:
        manager_id = hw_events.subject_manager_id(envelope.subject)
        device_id = hw_events.subject_device_id(envelope.subject)
        if manager_id is None or device_id is None:
            return
        self._manager_by_device_id[device_id] = manager_id

    def unregister_device(self, device_id: str) -> None:
        self._manager_by_device_id.pop(device_id, None)

    def _manager_id_for(self, device_id: str) -> str:
        manager_id = self._manager_by_device_id.get(device_id)
        if manager_id is None:
            raise LookupError(f"No hardware manager route for device {device_id!r}")
        return manager_id

    async def set_image(self, device_id: str, slot_id: str, image: bytes) -> None:
        await self._event_bus.send(
            hw_events.hardware_command_message(
                controller_id=self._controller_id,
                manager_id=self._manager_id_for(device_id),
                message_type=hw_events.SET_IMAGE,
                device_id=device_id,
                body=hw_events.SetImageMessage(slot_id=slot_id, image=image),
                control_id=slot_id,
                control_kind="slot",
            )
        )

    async def clear_slot(self, device_id: str, slot_id: str) -> None:
        await self._event_bus.send(
            hw_events.hardware_command_message(
                controller_id=self._controller_id,
                manager_id=self._manager_id_for(device_id),
                message_type=hw_events.CLEAR_SLOT,
                device_id=device_id,
                body=hw_events.ClearSlotMessage(slot_id=slot_id),
                control_id=slot_id,
                control_kind="slot",
            )
        )

    async def sleep_screen(self, device_id: str) -> None:
        await self._event_bus.send(
            hw_events.hardware_command_message(
                controller_id=self._controller_id,
                manager_id=self._manager_id_for(device_id),
                message_type=hw_events.SLEEP_SCREEN,
                device_id=device_id,
                body=hw_events.SleepScreenMessage(),
            )
        )

    async def wake_screen(self, device_id: str) -> None:
        await self._event_bus.send(
            hw_events.hardware_command_message(
                controller_id=self._controller_id,
                manager_id=self._manager_id_for(device_id),
                message_type=hw_events.WAKE_SCREEN,
                device_id=device_id,
                body=hw_events.WakeScreenMessage(),
            )
        )
