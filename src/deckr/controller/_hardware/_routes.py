from __future__ import annotations

from dataclasses import dataclass

from deckr.contracts.authority import ContractPointer
from deckr.hardware.descriptors import DeviceDescriptor, DeviceRef

from deckr.controller._hardware._models import ref_key


@dataclass(frozen=True, slots=True)
class LiveDeviceRoute:
    config_id: str
    ref: DeviceRef
    device: DeviceDescriptor
    contract: ContractPointer
    manager_session_id: str | None = None


class DeviceRouteRegistry:
    """Controller-local cache of live device routes by config id and device ref."""

    def __init__(self) -> None:
        self._devices_by_config: dict[str, LiveDeviceRoute] = {}
        self._config_by_ref: dict[tuple[str, str], str] = {}

    def connect(
        self,
        *,
        config_id: str,
        ref: DeviceRef,
        device: DeviceDescriptor,
        contract: ContractPointer,
        manager_session_id: str | None = None,
    ) -> LiveDeviceRoute:
        self.disconnect_config(config_id)
        live = LiveDeviceRoute(
            config_id=config_id,
            ref=ref,
            device=device,
            contract=contract,
            manager_session_id=manager_session_id,
        )
        self._devices_by_config[config_id] = live
        self._config_by_ref[ref_key(ref)] = config_id
        return live

    def update_descriptor(
        self,
        *,
        ref: DeviceRef,
        device: DeviceDescriptor,
        contract: ContractPointer | None = None,
        manager_session_id: str | None = None,
    ) -> LiveDeviceRoute | None:
        config_id = self._config_by_ref.get(ref_key(ref))
        if config_id is None:
            return None
        current = self._devices_by_config.get(config_id)
        live = LiveDeviceRoute(
            config_id=config_id,
            ref=ref,
            device=device,
            contract=contract if contract is not None else current.contract,
            manager_session_id=manager_session_id
            if manager_session_id is not None
            else (current.manager_session_id if current is not None else None),
        )
        self._devices_by_config[config_id] = live
        return live

    def disconnect_config(self, config_id: str) -> LiveDeviceRoute | None:
        live = self._devices_by_config.pop(config_id, None)
        if live is not None:
            self._config_by_ref.pop(ref_key(live.ref), None)
        return live

    def get(self, config_id: str) -> LiveDeviceRoute | None:
        return self._devices_by_config.get(config_id)

    def get_by_ref(self, ref: DeviceRef) -> LiveDeviceRoute | None:
        config_id = self._config_by_ref.get(ref_key(ref))
        if config_id is None:
            return None
        return self._devices_by_config.get(config_id)

    def all(self) -> tuple[LiveDeviceRoute, ...]:
        return tuple(self._devices_by_config.values())
