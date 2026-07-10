from __future__ import annotations

import pytest
from deckr.concord import (
    ConcordConflict,
    ConcordConflictCode,
    ContractValidity,
    ContractValidityStatus,
)

from deckr.controller._hardware._models import OwnedHardwareClaim
from deckr.controller._hardware._validity import validate_owned_claim


class _Agreement:
    def __init__(
        self,
        validity: ContractValidity,
        *,
        conflict: bool = False,
    ) -> None:
        self.contract = object()
        self.local_token = None
        self._validity = ContractValidity(ContractValidityStatus.NOT_YET_FULFILLED)
        self._next_validity = validity
        self._conflict = conflict

    async def refresh(self) -> ContractValidity:
        if self._conflict:
            raise ConcordConflict(
                ConcordConflictCode.REVISION_CHANGED,
                "refresh conflict",
            )
        self._validity = self._next_validity
        return self._next_validity


class _Concord:
    def __init__(self, validity: ContractValidity) -> None:
        self.validity = validity
        self.validate_calls = 0

    async def validate(self, *args, **kwargs) -> ContractValidity:  # noqa: ANN002, ANN003
        self.validate_calls += 1
        return self.validity

    async def validate_exact(self, *args, **kwargs) -> ContractValidity:  # noqa: ANN002, ANN003
        raise AssertionError("validate_exact should not run during hardware claim checks")


def _owned(agreement: _Agreement) -> OwnedHardwareClaim:
    return OwnedHardwareClaim(
        claim_id="claim-1",
        config_id="device-1",
        ref=None,
        device=None,
        agreement=agreement,
        current_sessions={"controller:controller-main": "controller-session"},
    )


@pytest.mark.asyncio
async def test_hardware_claim_validity_conflict_falls_back_to_cached_validation() -> None:
    validity = ContractValidity(ContractValidityStatus.VALID)
    concord = _Concord(validity)
    agreement = _Agreement(
        ContractValidity(ContractValidityStatus.NOT_YET_FULFILLED),
        conflict=True,
    )
    owned = _owned(agreement)

    result = await validate_owned_claim(concord, owned)

    assert result is validity
    assert agreement._validity is validity
    assert concord.validate_calls == 1
