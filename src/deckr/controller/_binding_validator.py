"""Pure binding validation: slot existence and action lookup."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from deckr.hardware.events import HWDevice, HWSlot
    from deckr.plugin.interface import PluginAction

    from deckr.controller._navigation_service import SlotBinding

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ValidationError:
    """One binding validation failure."""

    code: str
    message: str
    slot_id: str
    action_uuid: str
    profile_id: str | None = None
    page_id: str | None = None
    details: list[str] = field(default_factory=list)


# Error codes that block page load (page cannot activate).
BLOCKING_ERROR_CODES = frozenset({"slot_not_found"})

# Error codes that allow partial load (slot gets "unavailable" display).
NON_BLOCKING_ERROR_CODES = frozenset({"action_not_found"})


@dataclass
class ValidationResult:
    """Result of validating a set of bindings."""

    valid: bool
    errors: list[ValidationError] = field(default_factory=list)

    @property
    def has_blocking_errors(self) -> bool:
        """True if any error blocks page activation (e.g. slot_not_found)."""
        return any(e.code in BLOCKING_ERROR_CODES for e in self.errors)

    @property
    def has_non_blocking_errors(self) -> bool:
        """True if any error allows partial load (e.g. action_not_found)."""
        return any(e.code in NON_BLOCKING_ERROR_CODES for e in self.errors)

    def add_error(
        self,
        code: str,
        message: str,
        slot_id: str,
        action_uuid: str,
        profile_id: str | None = None,
        page_id: str | None = None,
        details: list[str] | None = None,
    ) -> None:
        self.valid = False
        self.errors.append(
            ValidationError(
                code=code,
                message=message,
                slot_id=slot_id,
                action_uuid=action_uuid,
                profile_id=profile_id,
                page_id=page_id,
                details=details or [],
            )
        )


def _slot_by_id(device: HWDevice, slot_id: str) -> HWSlot | None:
    for slot in device.slots:
        if slot.id == slot_id:
            return slot
    return None


async def validate_page_bindings(
    bindings: list[SlotBinding],
    device: HWDevice,
    get_action: Callable[[str], Awaitable[PluginAction | None]],
    profile_id: str | None = None,
    page_id: str | None = None,
) -> ValidationResult:
    """Validate all bindings for a page: slot existence and action lookup."""
    result = ValidationResult(valid=True)
    for binding in bindings:
        slot = _slot_by_id(device, binding.slot_id)
        if slot is None:
            result.add_error(
                code="slot_not_found",
                message=f"slot '{binding.slot_id}' not found on device",
                slot_id=binding.slot_id,
                action_uuid=binding.action_uuid,
                profile_id=profile_id,
                page_id=page_id,
            )
            continue
        action = await get_action(binding.action_uuid)
        if action is None:
            # Non-blocking: page loads; this slot shows "unavailable"
            result.add_error(
                code="action_not_found",
                message=f"action '{binding.action_uuid}' not found",
                slot_id=binding.slot_id,
                action_uuid=binding.action_uuid,
                profile_id=profile_id,
                page_id=page_id,
            )
            continue
    # Page can load if no blocking errors (slot_not_found). action_not_found is non-blocking.
    result.valid = not result.has_blocking_errors
    return result


def format_validation_summary(result: ValidationResult | list[ValidationError]) -> str:
    """Return a concise one-line summary of validation failures for logging or UI."""
    errors = result.errors if isinstance(result, ValidationResult) else result
    if not errors:
        return "validation passed"
    parts = [f"{len(errors)} error(s):"]
    for e in errors[:3]:
        parts.append(f" [{e.code}] {e.slot_id!r} / {e.action_uuid!r}")
    if len(errors) > 3:
        parts.append(f" ... and {len(errors) - 3} more")
    return "; ".join(parts)
