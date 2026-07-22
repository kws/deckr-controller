from __future__ import annotations

from deckr.concord import (
    ContractValidity,
    ContractValidityStatus,
)

from deckr.controller._hardware._models import OwnedHardwareClaim

HARDWARE_CLAIM_TERMINAL_STATUSES = frozenset(
    {
        ContractValidityStatus.CANCELLED,
        ContractValidityStatus.MISSING_CONTRACT,
        ContractValidityStatus.INVALID_CONTRACT,
        ContractValidityStatus.INVALID_TOKEN,
        ContractValidityStatus.MISSING_TOKEN,
        ContractValidityStatus.GENERATION_MISMATCH,
        ContractValidityStatus.SESSION_MISMATCH,
        ContractValidityStatus.TERMS_HASH_MISMATCH,
    }
)


async def validate_owned_claim(
    owned: OwnedHardwareClaim,
) -> ContractValidity:
    return owned.agreement.state.validity or ContractValidity(
        ContractValidityStatus.NOT_YET_FULFILLED
    )


def pending_hardware_claim_validity(validity: ContractValidity) -> bool:
    return validity.status == ContractValidityStatus.NOT_YET_FULFILLED
