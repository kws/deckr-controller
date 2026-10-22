"""Pure decisions comparing immutable desired and installed binding facts."""

from dataclasses import dataclass
from enum import StrEnum

from deckr.controller._binding_plan import BindingPlanStatus


class BindingChange(StrEnum):
    RETAIN = "retain"
    PENDING = "pending"
    UNAVAILABLE = "unavailable"
    INVALID = "invalid"
    INSTALL = "install"
    REFRESH = "refresh"
    REPLACE = "replace"


@dataclass(frozen=True, slots=True)
class InstalledBindingFacts:
    """Facts read under the device gate; no live collaborators cross this boundary."""

    same_action: bool
    same_session: bool
    same_requirements: bool
    current_agreement: bool
    successor_available: bool
    recovery_requested: bool


@dataclass(frozen=True, slots=True)
class BindingDecision:
    change: BindingChange
    retire: bool = False
    destroy_instance: bool = False
    notify_provider: bool = True
    reason: str = ""


def binding_decision(
    status: BindingPlanStatus, installed: InstalledBindingFacts | None
) -> BindingDecision:
    if status in {
        BindingPlanStatus.INVALID_CONFIG,
        BindingPlanStatus.INVALID_DEVICE_CONTROL,
    }:
        return BindingDecision(
            BindingChange.INVALID, installed is not None, reason="requirements_changed"
        )
    if status == BindingPlanStatus.PENDING:
        if installed and installed.current_agreement:
            return BindingDecision(BindingChange.RETAIN)
        return BindingDecision(
            BindingChange.PENDING,
            installed is not None,
            reason="provider_authority_lost",
        )
    if status == BindingPlanStatus.UNAVAILABLE:
        return BindingDecision(
            BindingChange.UNAVAILABLE,
            installed is not None,
            reason="action_unavailable",
        )
    if installed is None:
        return BindingDecision(BindingChange.INSTALL)
    if not installed.same_action:
        return BindingDecision(
            BindingChange.REPLACE,
            True,
            notify_provider=False,
            reason="action_availability_changed",
        )
    if not installed.same_session:
        return BindingDecision(
            BindingChange.REPLACE, True, True, reason="provider_session_changed"
        )
    if not installed.same_requirements:
        return BindingDecision(
            BindingChange.REPLACE, True, reason="requirements_changed"
        )
    if not installed.current_agreement:
        if not installed.successor_available:
            return BindingDecision(
                BindingChange.PENDING, True, reason="provider_authority_lost"
            )
        return BindingDecision(
            BindingChange.REPLACE,
            True,
            True,
            reason="provider_session_contract_changed",
        )
    if installed.recovery_requested:
        return BindingDecision(
            BindingChange.REPLACE, True, True, reason="provider_session_recovered"
        )
    return BindingDecision(BindingChange.REFRESH)
