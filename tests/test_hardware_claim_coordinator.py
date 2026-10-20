from __future__ import annotations

from types import SimpleNamespace

import pytest
from deckr.concord import ContractValidity, ContractValidityStatus
from deckr.contracts.messages import controller_address, hardware_manager_address
from deckr.hardware.descriptors import DeviceDescriptor, DeviceRef
from deckr.hardware.profiles import HardwareBeaconPayload

from deckr.controller._hardware._claims import HardwareClaimCoordinator
from deckr.controller._hardware._models import (
    HardwareCandidate,
    OwnedHardwareClaim,
    ref_key,
)
from deckr.controller._hardware._routes import DeviceRouteRegistry


def _ref() -> DeviceRef:
    return DeviceRef(managerId="room-a", deviceId="deck")


def _device() -> DeviceDescriptor:
    return DeviceDescriptor(
        deviceId="deck",
        displayName="Test Device",
        fingerprint="serial-a",
    )


def _candidate() -> HardwareCandidate:
    ref = _ref()
    device = _device()
    payload = HardwareBeaconPayload(
        managerId=ref.manager_id,
        managerEndpoint=hardware_manager_address(ref.manager_id),
        sessionId="manager-session",
        labels={},
        devices={
            ref.device_id: {
                "deviceRef": ref.model_dump(by_alias=True, exclude_none=True),
                "descriptor": device.model_dump(
                    by_alias=True,
                    exclude_none=True,
                    mode="json",
                ),
            }
        },
    )
    return HardwareCandidate(
        advertisement_key="hardware-ad",
        advertisement_id="hardware-ad",
        advertisement_endpoint=hardware_manager_address(ref.manager_id),
        advertisement_session_id="manager-session",
        advertisement_revision=1,
        advertisement_refresh_seq=1,
        payload=payload,
        ref=ref,
        device=device,
        labels={},
    )


class _Agreement:
    def __init__(
        self,
        validity: ContractValidity,
        *,
        contract_id: str = "contract-a",
    ) -> None:
        self.contract = SimpleNamespace(contract_id=contract_id, generation=1)
        self.state = SimpleNamespace(validity=validity)
        self.cancelled = False
        self.closed = False

    async def __aenter__(self):
        return self

    async def cancel(self, reason: str | None = None) -> bool:
        self.cancelled = True
        return True

    async def aclose(self) -> None:
        self.closed = True


class _Concord:
    def __init__(
        self,
        *,
        proposed_validity: ContractValidity | None = None,
    ) -> None:
        self.proposed_validity = proposed_validity or ContractValidity(
            ContractValidityStatus.NOT_YET_FULFILLED
        )
        self.proposals = []

    def propose(self, endpoint, spec):  # noqa: ANN001
        self.proposals.append(spec)
        return _Agreement(
            self.proposed_validity,
            contract_id=f"successor-{len(self.proposals)}",
        )


class _ConfigService:
    config = SimpleNamespace(id="config-room-a", enabled=True)

    async def match_device(self, *, fingerprint: str, labels):
        if fingerprint == "serial-a" and labels == {}:
            return self.config
        return None

    async def get_config(self, config_id: str):
        if config_id == self.config.id:
            return self.config
        return None


class _Callbacks:
    pass


def _coordinator(concord: _Concord) -> HardwareClaimCoordinator:
    return HardwareClaimCoordinator(
        concord=concord,
        config_service=_ConfigService(),
        route_registry=DeviceRouteRegistry(),
        callbacks=_Callbacks(),
        endpoint=SimpleNamespace(
            address=controller_address("controller-main"),
            session_id="controller-session",
        ),
    )


def _owned(agreement: _Agreement) -> OwnedHardwareClaim:
    candidate = _candidate()
    return OwnedHardwareClaim(
        claim_id="claim-a",
        config_id="config-room-a",
        ref=candidate.ref,
        device=candidate.device,
        agreement=agreement,
        current_sessions={
            str(controller_address("controller-main")): "controller-session",
            str(hardware_manager_address("room-a")): "manager-session",
        },
    )


def _seed_claim(
    coordinator: HardwareClaimCoordinator,
    agreement: _Agreement,
) -> OwnedHardwareClaim:
    owned = _owned(agreement)
    coordinator._owned_claims[ref_key(owned.ref)] = owned  # noqa: SLF001
    return owned


@pytest.mark.asyncio
async def test_pending_claim_survives_unavailable_refresh_without_candidate() -> None:
    coordinator = _coordinator(_Concord())
    owned = _seed_claim(
        coordinator,
        _Agreement(ContractValidity(ContractValidityStatus.UNAVAILABLE)),
    )

    await coordinator.reconcile({}, reason="test unavailable refresh")

    claims = coordinator.snapshot()
    assert len(claims) == 1
    assert claims[0].claim_id == owned.claim_id


@pytest.mark.asyncio
async def test_pending_claim_with_candidate_survives_unavailable_refresh() -> None:
    concord = _Concord()
    coordinator = _coordinator(concord)
    owned = _seed_claim(
        coordinator,
        _Agreement(ContractValidity(ContractValidityStatus.UNAVAILABLE)),
    )
    candidate = _candidate()

    await coordinator.reconcile(
        {ref_key(candidate.ref): candidate},
        reason="test unavailable refresh",
    )

    claims = coordinator.snapshot()
    assert len(claims) == 1
    assert claims[0].claim_id == owned.claim_id
    assert concord.proposals == []


@pytest.mark.asyncio
async def test_terminal_state_cancels_and_allows_successor_claim() -> None:
    concord = _Concord()
    coordinator = _coordinator(concord)
    owned = _seed_claim(
        coordinator,
        _Agreement(ContractValidity(ContractValidityStatus.MISSING_TOKEN)),
    )
    candidate = _candidate()

    await coordinator.reconcile(
        {ref_key(candidate.ref): candidate},
        reason="test terminal refresh",
    )

    claims = coordinator.snapshot()
    assert claims[0].claim_id != owned.claim_id
    assert owned.agreement.cancelled is True
    assert len(concord.proposals) == 1


@pytest.mark.asyncio
async def test_pending_old_session_is_cancelled_before_successor_proposal() -> None:
    concord = _Concord()
    coordinator = _coordinator(concord)
    owned = _seed_claim(
        coordinator,
        _Agreement(ContractValidity(ContractValidityStatus.NOT_YET_FULFILLED)),
    )
    owned.current_sessions[str(hardware_manager_address("room-a"))] = "old-session"
    candidate = _candidate()

    await coordinator.reconcile(
        {ref_key(candidate.ref): candidate}, reason="replacement discovery"
    )

    assert owned.agreement.cancelled is True
    assert owned.agreement.closed is True
    assert coordinator.snapshot()[0].claim_id != owned.claim_id
    assert len(concord.proposals) == 1
    assert concord.proposals[0].participant_sessions[str(hardware_manager_address("room-a"))] == "manager-session"
