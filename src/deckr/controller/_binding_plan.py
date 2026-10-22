"""Pure page binding planning for the controller runtime."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from deckr.actions.messages import (
    PageChildBindingDescriptor,
)
from deckr.contracts.models import freeze_json

from deckr.controller._actions._models import ActionIntentKey, ActionMetadata
from deckr.controller._binding_resolution import (
    ResolvedControlBinding,
)
from deckr.controller._pages import DynamicPageSession, PageRef


@dataclass(frozen=True)
class ValidationError:
    """One binding planning failure."""

    code: str
    message: str
    control_ref: str
    action_id: str
    profile_id: str | None = None
    page_id: str | None = None
    details: list[str] = field(default_factory=list)


def format_validation_summary(errors: Sequence[ValidationError]) -> str:
    """Return a concise one-line summary of binding planning failures."""
    if not errors:
        return "validation passed"
    parts = [f"{len(errors)} error(s):"]
    for error in errors[:3]:
        parts.append(f" [{error.code}] {error.control_ref!r} / {error.action_id!r}")
    if len(errors) > 3:
        parts.append(f" ... and {len(errors) - 3} more")
    return "; ".join(parts)


class BindingPlanStatus(StrEnum):
    BOUND = "bound"
    PENDING = "pending"
    UNAVAILABLE = "unavailable"
    INVALID_CONFIG = "invalid_config"
    INVALID_DEVICE_CONTROL = "invalid_device_control"


@dataclass(frozen=True, slots=True)
class PlannedBinding:
    binding: ResolvedControlBinding
    action_instance_id: str
    status: BindingPlanStatus
    action_meta: ActionMetadata | None
    page_session_id: str | None
    settings_target_enabled: bool
    item_key: str | None = None
    handler: str | None = None
    child: PageChildBindingDescriptor | None = None
    internal: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "internal", freeze_json(self.internal))

    @property
    def control_id(self) -> str:
        return self.binding.control_id


@dataclass(frozen=True, slots=True)
class PagePlan:
    """Desired binding values with a boundary-local borrowed page occurrence."""

    entry: PageRef
    profile_id: str
    page_id: str
    page_session: DynamicPageSession | None
    bindings: tuple[PlannedBinding, ...]


@dataclass(frozen=True, slots=True)
class BindingPlanOutcome:
    status: BindingPlanStatus
    intent: ActionIntentKey
    control_ref: str
    control_id: str | None = None
    action_instance_id: str | None = None
    planned: PlannedBinding | None = None
    error: ValidationError | None = None


@dataclass(frozen=True, slots=True)
class PagePlanBuildResult:
    plan: PagePlan | None
    outcomes: tuple[BindingPlanOutcome, ...]
    validation_errors: tuple[ValidationError, ...] = ()
