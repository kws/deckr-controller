from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from deckr.concord import (
    ConcordAgreementLease,
    ContractHandle,
    ParticipantHandle,
)
from deckr.contracts.authority import ContractPointer
from deckr.contracts.messages import DeckrMessage, EndpointAddress
from deckr.hardware import messages as hw_messages
from deckr.hardware.descriptors import DeviceDescriptor, DeviceRef
from deckr.hardware.profiles import HardwareBeaconPayload

if TYPE_CHECKING:
    from deckr.controller._hardware._routes import LiveDeviceRoute
    from deckr.controller.config import DeviceConfig


def ref_key(ref: DeviceRef) -> tuple[str, str]:
    return ref.manager_id, ref.device_id


@dataclass(frozen=True, slots=True)
class HardwareCandidate:
    advertisement_key: str
    advertisement_id: str
    advertisement_endpoint: EndpointAddress
    advertisement_session_id: str
    advertisement_revision: int
    advertisement_refresh_seq: int
    payload: HardwareBeaconPayload
    ref: DeviceRef
    device: DeviceDescriptor
    labels: Mapping[str, str]


@dataclass(slots=True)
class OwnedHardwareClaim:
    claim_id: str
    config_id: str
    ref: DeviceRef
    device: DeviceDescriptor
    agreement: ConcordAgreementLease
    current_sessions: dict[str, str]
    controller_token: ParticipantHandle | None = None
    live: bool = False

    @property
    def contract(self) -> ContractHandle:
        return self.agreement.contract

    @property
    def contract_pointer(self) -> ContractPointer:
        return ContractPointer(
            contractId=self.contract.contract_id,
            generation=self.contract.generation,
        )


@dataclass(frozen=True, slots=True)
class OwnedHardwareClaimSnapshot:
    claim_id: str
    config_id: str
    ref: DeviceRef
    device: DeviceDescriptor
    contract: ContractHandle
    contract_pointer: ContractPointer
    current_sessions: Mapping[str, str]
    controller_token: ParticipantHandle | None
    live: bool

    @classmethod
    def from_claim(cls, owned: OwnedHardwareClaim) -> OwnedHardwareClaimSnapshot:
        return cls(
            claim_id=owned.claim_id,
            config_id=owned.config_id,
            ref=owned.ref,
            device=owned.device,
            contract=owned.contract,
            contract_pointer=owned.contract_pointer,
            current_sessions=dict(owned.current_sessions),
            controller_token=owned.controller_token,
            live=owned.live,
        )


@dataclass(frozen=True, slots=True)
class ControllerHardwareSnapshot:
    candidates: tuple[HardwareCandidate, ...]
    owned_claims: tuple[OwnedHardwareClaimSnapshot, ...]
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
