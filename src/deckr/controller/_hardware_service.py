from __future__ import annotations

from dataclasses import dataclass

from deckr.contracts.messages import hardware_manager_address
from deckr.hardware import events as hw_events
from deckr.transports.bus import EventBus


@dataclass(frozen=True, slots=True)
class LiveHardwareDevice:
    config_id: str
    ref: hw_events.HardwareDeviceRef
    device: hw_events.HardwareDevice


class HardwareDeviceRegistry:
    """Controller-local cache of live hardware metadata by config id and live ref."""

    def __init__(self) -> None:
        self._devices_by_config: dict[str, LiveHardwareDevice] = {}
        self._config_by_ref: dict[hw_events.HardwareDeviceRef, str] = {}

    def connect(
        self,
        *,
        config_id: str,
        ref: hw_events.HardwareDeviceRef,
        device: hw_events.HardwareDevice,
    ) -> LiveHardwareDevice:
        self.disconnect_config(config_id)
        live = LiveHardwareDevice(config_id=config_id, ref=ref, device=device)
        self._devices_by_config[config_id] = live
        self._config_by_ref[ref] = config_id
        return live

    def disconnect_config(self, config_id: str) -> LiveHardwareDevice | None:
        live = self._devices_by_config.pop(config_id, None)
        if live is not None:
            self._config_by_ref.pop(live.ref, None)
        return live

    def disconnect_ref(self, ref: hw_events.HardwareDeviceRef) -> LiveHardwareDevice | None:
        config_id = self._config_by_ref.pop(ref, None)
        if config_id is None:
            return None
        return self._devices_by_config.pop(config_id, None)

    def get(self, config_id: str) -> LiveHardwareDevice | None:
        return self._devices_by_config.get(config_id)

    def get_by_ref(self, ref: hw_events.HardwareDeviceRef) -> LiveHardwareDevice | None:
        config_id = self._config_by_ref.get(ref)
        if config_id is None:
            return None
        return self._devices_by_config.get(config_id)

    def for_manager(self, manager_id: str) -> tuple[LiveHardwareDevice, ...]:
        return tuple(
            live
            for live in self._devices_by_config.values()
            if live.ref.manager_id == manager_id
        )


class HardwareCommandService:
    """Publishes hardware output commands onto the hardware lane."""

    def __init__(self, event_bus: EventBus, *, controller_id: str) -> None:
        self._event_bus = event_bus
        self._controller_id = controller_id
        self._ref_by_config_id: dict[str, hw_events.HardwareDeviceRef] = {}

    def register_device(self, *, config_id: str, ref: hw_events.HardwareDeviceRef) -> None:
        self._ref_by_config_id[config_id] = ref

    def unregister_config(self, config_id: str) -> None:
        self._ref_by_config_id.pop(config_id, None)

    async def _ref_for(self, config_id: str) -> hw_events.HardwareDeviceRef:
        ref = self._ref_by_config_id.get(config_id)
        if ref is None:
            raise LookupError(f"No live hardware route for config {config_id!r}")
        endpoint = hardware_manager_address(ref.manager_id)
        route = await self._event_bus.route_table.route_for(
            endpoint,
            lane=self._event_bus.lane,
        )
        if route is None:
            raise LookupError(
                f"Hardware manager endpoint {endpoint} is not reachable"
            )
        return ref

    async def set_image(self, config_id: str, slot_id: str, image: bytes) -> None:
        ref = await self._ref_for(config_id)
        await self._event_bus.send(
            hw_events.hardware_command_for_control(
                controller_id=self._controller_id,
                ref=hw_events.HardwareControlRef(
                    manager_id=ref.manager_id,
                    device_id=ref.device_id,
                    control_id=slot_id,
                    control_kind="slot",
                ),
                message_type=hw_events.SET_IMAGE,
                body=hw_events.SetImageMessage(slot_id=slot_id, image=image),
            )
        )

    async def clear_slot(self, config_id: str, slot_id: str) -> None:
        ref = await self._ref_for(config_id)
        await self._event_bus.send(
            hw_events.hardware_command_for_control(
                controller_id=self._controller_id,
                ref=hw_events.HardwareControlRef(
                    manager_id=ref.manager_id,
                    device_id=ref.device_id,
                    control_id=slot_id,
                    control_kind="slot",
                ),
                message_type=hw_events.CLEAR_SLOT,
                body=hw_events.ClearSlotMessage(slot_id=slot_id),
            )
        )

    async def sleep_screen(self, config_id: str) -> None:
        ref = await self._ref_for(config_id)
        await self._event_bus.send(
            hw_events.hardware_command_for_device(
                controller_id=self._controller_id,
                ref=ref,
                message_type=hw_events.SLEEP_SCREEN,
                body=hw_events.SleepScreenMessage(),
            )
        )

    async def wake_screen(self, config_id: str) -> None:
        ref = await self._ref_for(config_id)
        await self._event_bus.send(
            hw_events.hardware_command_for_device(
                controller_id=self._controller_id,
                ref=ref,
                message_type=hw_events.WAKE_SCREEN,
                body=hw_events.WakeScreenMessage(),
            )
        )
