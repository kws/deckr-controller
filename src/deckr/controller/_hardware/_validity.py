from __future__ import annotations

from collections.abc import Mapping

from deckr.concord import (
    Concord,
    ConcordConflict,
    ConcordUnavailable,
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
    concord: Concord,
    owned: OwnedHardwareClaim,
    *,
    current_sessions: Mapping[str, str] | None = None,
) -> ContractValidity:
    sessions = current_sessions or owned.current_sessions
    try:
        validity = await owned.agreement.refresh()
    except ConcordConflict:
        try:
            validity = await concord.validate(
                owned.contract,
                current_sessions=sessions,
            )
        except ConcordUnavailable:
            return ContractValidity(ContractValidityStatus.UNAVAILABLE)
    else:
        if current_sessions is not None:
            try:
                validity = await concord.validate(
                    owned.contract,
                    current_sessions=sessions,
                )
            except ConcordUnavailable:
                return ContractValidity(ContractValidityStatus.UNAVAILABLE)
    owned.agreement._validity = validity  # noqa: SLF001
    return validity


def pending_hardware_claim_validity(validity: ContractValidity) -> bool:
    return validity.status == ContractValidityStatus.NOT_YET_FULFILLED
