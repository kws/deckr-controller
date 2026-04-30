"""Pure binding validation: selector resolution and action lookup."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from deckr.hardware.descriptors import DeviceDescriptor

    from deckr.controller.plugin.provider import ActionMetadata

from deckr.controller._binding_resolution import (
    ConfiguredControlBinding,
    ResolvedControlBinding,
    exact_control_binding,
    resolve_binding,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ValidationError:
    """One binding validation failure."""

    code: str
    message: str
    control_ref: str
    action_uuid: str
    profile_id: str | None = None
    page_id: str | None = None
    details: list[str] = field(default_factory=list)


# Error codes that block page load (page cannot activate).
BLOCKING_ERROR_CODES = frozenset(
    {
        "capability_not_found",
        "control_not_found",
        "control_selector_ambiguous",
    }
)

# Error codes that allow partial load (control gets "unavailable" display).
NON_BLOCKING_ERROR_CODES = frozenset({"action_not_found"})


@dataclass
class ValidationResult:
    """Result of validating a set of bindings."""

    valid: bool
    errors: list[ValidationError] = field(default_factory=list)
    bindings: list[ResolvedControlBinding] = field(default_factory=list)

    @property
    def has_blocking_errors(self) -> bool:
        """True if any error blocks page activation."""
        return any(e.code in BLOCKING_ERROR_CODES for e in self.errors)

    @property
    def has_non_blocking_errors(self) -> bool:
        """True if any error allows partial load (e.g. action_not_found)."""
        return any(e.code in NON_BLOCKING_ERROR_CODES for e in self.errors)

    def add_error(
        self,
        code: str,
        message: str,
        control_ref: str,
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
                control_ref=control_ref,
                action_uuid=action_uuid,
                profile_id=profile_id,
                page_id=page_id,
                details=details or [],
            )
        )


async def validate_page_bindings(
    bindings: list[ConfiguredControlBinding],
    device: DeviceDescriptor,
    get_action: Callable[[str], Awaitable[ActionMetadata | None]],
    profile_id: str | None = None,
    page_id: str | None = None,
) -> ValidationResult:
    """Validate and resolve all bindings for a page."""
    result = ValidationResult(valid=True)
    for binding in bindings:
        resolution = resolve_binding(binding, device.controls)
        if not resolution.ok:
            result.add_error(
                code=resolution.code or "control_not_found",
                message=resolution.message or "control selector did not resolve",
                control_ref=", ".join(resolution.details) or "<selector>",
                action_uuid=binding.action_uuid,
                profile_id=profile_id,
                page_id=page_id,
                details=list(resolution.details),
            )
            continue
        result.bindings.append(resolution.binding)
        action = await get_action(binding.action_uuid)
        if action is None:
            # Non-blocking: page loads; this control shows "unavailable"
            result.add_error(
                code="action_not_found",
                message=f"action '{binding.action_uuid}' not found",
                control_ref=resolution.binding.control_id,
                action_uuid=binding.action_uuid,
                profile_id=profile_id,
                page_id=page_id,
            )
            continue
    # Page can load if no selector/capability errors. action_not_found is non-blocking.
    result.valid = not result.has_blocking_errors
    return result


async def validate_exact_control_bindings(
    bindings: list,
    device: DeviceDescriptor,
    get_action: Callable[[str], Awaitable[ActionMetadata | None]],
    *,
    action_uuid: str,
    profile_id: str | None = None,
    page_id: str | None = None,
) -> ValidationResult:
    """Validate plugin-provided exact control bindings."""

    return await validate_page_bindings(
        [
            exact_control_binding(
                control_id=binding.control_id,
                action_uuid=action_uuid,
                settings=binding.settings,
                title_options=binding.title_options,
            )
            for binding in bindings
        ],
        device,
        get_action,
        profile_id=profile_id,
        page_id=page_id,
    )


def format_validation_summary(result: ValidationResult | list[ValidationError]) -> str:
    """Return a concise one-line summary of validation failures for logging or UI."""
    errors = result.errors if isinstance(result, ValidationResult) else result
    if not errors:
        return "validation passed"
    parts = [f"{len(errors)} error(s):"]
    for e in errors[:3]:
        parts.append(f" [{e.code}] {e.control_ref!r} / {e.action_uuid!r}")
    if len(errors) > 3:
        parts.append(f" ... and {len(errors) - 3} more")
    return "; ".join(parts)
