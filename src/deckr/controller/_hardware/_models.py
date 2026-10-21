from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from deckr.contracts.messages import DeckrMessage
from deckr.hardware import HardwareCandidate, HardwareClaimState
from deckr.hardware import messages as hw_messages
from deckr.hardware.descriptors import DeviceDescriptor, DeviceRef

if TYPE_CHECKING:
    from deckr.controller._hardware._routes import LiveDeviceRoute
    from deckr.controller.config import DeviceConfig


def ref_key(ref: DeviceRef) -> tuple[str, str]:
    return ref.manager_id, ref.device_id


@dataclass(frozen=True, slots=True)
class HardwareClaimSnapshot:
    config_id: str
    ref: DeviceRef
    state: HardwareClaimState


@dataclass(frozen=True, slots=True)
class ControllerHardwareSnapshot:
    candidates: tuple[HardwareCandidate, ...]
    owned_claims: tuple[HardwareClaimSnapshot, ...]
    live_routes: tuple[LiveDeviceRoute, ...]


class HardwareServiceCallbacks(Protocol):
    async def on_hardware_connected(
        self,
        live: LiveDeviceRoute,
        *,
        initial_config: DeviceConfig,
    ) -> None: ...

    async def on_hardware_disconnected(
        self,
        config_id: str,
        *,
        reason: str,
    ) -> None: ...

    async def on_hardware_descriptor_changed(
        self,
        config_id: str,
        device: DeviceDescriptor,
    ) -> None: ...

    async def on_hardware_control_input(
        self,
        live: LiveDeviceRoute,
        message: DeckrMessage,
    ) -> None: ...

    async def on_hardware_capability_state_changed(
        self,
        live: LiveDeviceRoute,
        event: hw_messages.CapabilityStateChangedMessage,
    ) -> None: ...

    async def on_hardware_command_rejected(
        self,
        live: LiveDeviceRoute,
        event: hw_messages.CommandRejectedMessage,
    ) -> None: ...
