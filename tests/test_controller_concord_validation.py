from __future__ import annotations

from types import SimpleNamespace

import pytest
from deckr.concord import ContractValidity, ContractValidityStatus

from deckr.controller._hardware._models import OwnedHardwareClaim
from deckr.controller._hardware._validity import validate_owned_claim


def _owned(validity: ContractValidity | None) -> OwnedHardwareClaim:
    agreement = SimpleNamespace(state=SimpleNamespace(validity=validity))
    return OwnedHardwareClaim(
        claim_id="claim-1",
        config_id="device-1",
        ref=None,
        device=None,
        agreement=agreement,
        current_sessions={"controller:controller-main": "controller-session"},
    )


@pytest.mark.asyncio
async def test_hardware_claim_validity_uses_agreement_state() -> None:
    validity = ContractValidity(ContractValidityStatus.VALID)

    assert await validate_owned_claim(_owned(validity)) is validity


@pytest.mark.asyncio
async def test_starting_hardware_claim_is_not_yet_fulfilled() -> None:
    validity = await validate_owned_claim(_owned(None))

    assert validity.status == ContractValidityStatus.NOT_YET_FULFILLED
