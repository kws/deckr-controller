"""Pure control selector resolution against hardware descriptors."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from deckr.hardware.descriptors import CapabilityDescriptor, ControlDescriptor

from deckr.controller.config import CapabilitySelector, ControlSelector

CapabilityBucket = Literal[
    "input",
    "output",
    "state",
    "config",
    "diagnostic",
]


@dataclass(frozen=True, slots=True)
class ConfiguredControlBinding:
    """A static-page action binding before descriptor selector resolution."""

    selector: ControlSelector
    action_uuid: str
    provider_instance_id: str | None
    provider_labels: Mapping[str, str]
    settings: Mapping[str, Any]
    stable_id: str | None = None
    template_overrides: Mapping[str, Any] | None = None

    @property
    def control_id(self) -> str | None:
        return self.selector.control_id


@dataclass(frozen=True, slots=True)
class ResolvedControlBinding:
    """A binding resolved to one descriptor control and selected capabilities."""

    control: ControlDescriptor
    action_uuid: str
    provider_instance_id: str | None
    provider_labels: Mapping[str, str]
    settings: Mapping[str, Any]
    stable_id: str | None
    template_overrides: Mapping[str, Any] | None
    selector: ControlSelector
    input_capability_ids: frozenset[str]
    output_capability_ids: frozenset[str]
    state_capability_ids: frozenset[str]
    config_capability_ids: frozenset[str]
    diagnostic_capability_ids: frozenset[str]

    @property
    def control_id(self) -> str:
        return self.control.control_id


@dataclass(frozen=True, slots=True)
class SelectorResolution:
    """Outcome of resolving a control selector."""

    binding: ResolvedControlBinding | None = None
    code: str | None = None
    message: str | None = None
    details: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.binding is not None


def resolve_binding(
    binding: ConfiguredControlBinding,
    controls: tuple[ControlDescriptor, ...],
) -> SelectorResolution:
    """Resolve one configured binding against a descriptor's controls."""

    matches = [
        control for control in _candidate_controls(binding.selector, controls)
        if _control_matches(binding.selector, control)
    ]
    if not matches:
        target = _selector_summary(binding.selector)
        return SelectorResolution(
            code="control_not_found",
            message=f"no descriptor control matched selector {target}",
            details=(target,),
        )
    if len(matches) > 1:
        ids = tuple(control.control_id for control in matches)
        return SelectorResolution(
            code="control_selector_ambiguous",
            message=(
                "control selector matched multiple controls: "
                + ", ".join(sorted(ids))
            ),
            details=tuple(sorted(ids)),
        )

    control = matches[0]
    capability_check = _missing_capability_requirements(binding.selector, control)
    if capability_check:
        return SelectorResolution(
            code="capability_not_found",
            message=(
                f"control {control.control_id!r} does not advertise required "
                "capability: "
                + capability_check[0]
            ),
            details=capability_check,
        )

    return SelectorResolution(
        binding=ResolvedControlBinding(
            control=control,
            action_uuid=binding.action_uuid,
            provider_instance_id=binding.provider_instance_id,
            provider_labels=binding.provider_labels,
            settings=binding.settings,
            stable_id=binding.stable_id,
            template_overrides=binding.template_overrides,
            selector=binding.selector,
            input_capability_ids=_selected_capability_ids(
                control,
                "input",
                binding.selector.input_capabilities,
                binding.selector.capabilities,
            ),
            output_capability_ids=_selected_capability_ids(
                control,
                "output",
                binding.selector.output_capabilities,
                binding.selector.capabilities,
            ),
            state_capability_ids=_selected_capability_ids(
                control,
                "state",
                binding.selector.state_capabilities,
                binding.selector.capabilities,
            ),
            config_capability_ids=_selected_capability_ids(
                control,
                "config",
                binding.selector.config_capabilities,
                binding.selector.capabilities,
            ),
            diagnostic_capability_ids=_selected_capability_ids(
                control,
                "diagnostic",
                binding.selector.diagnostic_capabilities,
                binding.selector.capabilities,
            ),
        )
    )


def exact_control_binding(
    *,
    control_id: str,
    action_uuid: str,
    provider_instance_id: str | None = None,
    provider_labels: Mapping[str, str] | None = None,
    settings: Mapping[str, Any],
) -> ConfiguredControlBinding:
    """Construct a selector binding for an exact descriptor control id."""

    return ConfiguredControlBinding(
        selector=ControlSelector(control_id=control_id),
        action_uuid=action_uuid,
        provider_instance_id=provider_instance_id,
        provider_labels=dict(provider_labels or {}),
        settings=settings,
        stable_id=None,
        template_overrides=None,
    )


def _candidate_controls(
    selector: ControlSelector,
    controls: tuple[ControlDescriptor, ...],
) -> tuple[ControlDescriptor, ...]:
    if selector.control_id is None:
        return controls
    return tuple(control for control in controls if control.control_id == selector.control_id)


def _control_matches(selector: ControlSelector, control: ControlDescriptor) -> bool:
    if selector.control_id is not None and control.control_id != selector.control_id:
        return False
    if selector.kind is not None and control.kind != selector.kind:
        return False
    if selector.group_id is not None and control.group_id != selector.group_id:
        return False
    if (
        selector.parent_control_id is not None
        and control.parent_control_id != selector.parent_control_id
    ):
        return False
    if selector.surface_id is not None and control.surface_id != selector.surface_id:
        return False
    if selector.label is not None and control.label != selector.label:
        return False
    return selector.geometry is None or _geometry_matches(selector, control)


def _geometry_matches(selector: ControlSelector, control: ControlDescriptor) -> bool:
    expected = selector.geometry
    geometry = control.geometry
    if expected is None:
        return True
    if geometry is None:
        return False
    for field in ("x", "y", "width", "height", "unit", "rotation", "layer"):
        expected_value = getattr(expected, field)
        if expected_value is not None and getattr(geometry, field) != expected_value:
            return False
    return True


def _missing_capability_requirements(
    selector: ControlSelector,
    control: ControlDescriptor,
) -> tuple[str, ...]:
    checks: list[tuple[CapabilityBucket, tuple[CapabilitySelector, ...]]] = [
        ("input", selector.input_capabilities),
        ("output", selector.output_capabilities),
        ("state", selector.state_capabilities),
        ("config", selector.config_capabilities),
        ("diagnostic", selector.diagnostic_capabilities),
    ]
    missing: list[str] = []
    for requirement in selector.capabilities:
        if not any(
            _capability_matches(requirement, capability)
            for capability in _all_capabilities(control)
        ):
            missing.append(_capability_summary(requirement))
    for bucket, requirements in checks:
        capabilities = _capabilities_for_bucket(control, bucket)
        for requirement in requirements:
            if not any(
                _capability_matches(requirement, capability)
                for capability in capabilities
            ):
                missing.append(f"{bucket}:{_capability_summary(requirement)}")
    return tuple(missing)


def _selected_capability_ids(
    control: ControlDescriptor,
    bucket: CapabilityBucket,
    bucket_requirements: tuple[CapabilitySelector, ...],
    generic_requirements: tuple[CapabilitySelector, ...],
) -> frozenset[str]:
    capabilities = _capabilities_for_bucket(control, bucket)
    requirements = list(bucket_requirements)
    requirements.extend(
        requirement
        for requirement in generic_requirements
        if any(_capability_matches(requirement, capability) for capability in capabilities)
    )
    if not requirements:
        return frozenset(capability.capability_id for capability in capabilities)
    return frozenset(
        capability.capability_id
        for capability in capabilities
        if any(_capability_matches(requirement, capability) for requirement in requirements)
    )


def _capabilities_for_bucket(
    control: ControlDescriptor,
    bucket: CapabilityBucket,
) -> tuple[CapabilityDescriptor, ...]:
    if bucket == "input":
        return control.input_capabilities
    if bucket == "output":
        return control.output_capabilities
    if bucket == "state":
        return control.state_capabilities
    if bucket == "config":
        return control.config_capabilities
    return control.diagnostic_capabilities


def _all_capabilities(control: ControlDescriptor) -> tuple[CapabilityDescriptor, ...]:
    return (
        *control.input_capabilities,
        *control.output_capabilities,
        *control.state_capabilities,
        *control.config_capabilities,
        *control.diagnostic_capabilities,
    )


def _capability_matches(
    selector: CapabilitySelector,
    capability: CapabilityDescriptor,
) -> bool:
    if selector.capability_id is not None and (
        capability.capability_id != selector.capability_id
    ):
        return False
    if selector.family is not None and capability.family != selector.family:
        return False
    if selector.capability_type is not None and (
        capability.capability_type != selector.capability_type
    ):
        return False
    if selector.direction is not None and capability.direction != selector.direction:
        return False
    if selector.event_types and not set(selector.event_types).issubset(
        capability.event_types
    ):
        return False
    return not selector.command_types or set(selector.command_types).issubset(
        capability.command_types
    )


def _selector_summary(selector: ControlSelector) -> str:
    if selector.control_id is not None:
        return f"control_id={selector.control_id!r}"
    parts: list[str] = []
    for field in ("kind", "group_id", "parent_control_id", "surface_id", "label"):
        value = getattr(selector, field)
        if value is not None:
            parts.append(f"{field}={value!r}")
    if selector.geometry is not None:
        parts.append("geometry")
    for requirement in (
        *selector.capabilities,
        *selector.input_capabilities,
        *selector.output_capabilities,
        *selector.state_capabilities,
        *selector.config_capabilities,
        *selector.diagnostic_capabilities,
    ):
        parts.append(_capability_summary(requirement))
    return ", ".join(parts) if parts else "<empty>"


def _capability_summary(selector: CapabilitySelector) -> str:
    parts: list[str] = []
    if selector.capability_id is not None:
        parts.append(f"id={selector.capability_id}")
    if selector.family is not None:
        parts.append(f"family={selector.family}")
    if selector.capability_type is not None:
        parts.append(f"type={selector.capability_type}")
    if selector.direction is not None:
        parts.append(f"direction={selector.direction}")
    if selector.event_types:
        parts.append("events=" + ",".join(selector.event_types))
    if selector.command_types:
        parts.append("commands=" + ",".join(selector.command_types))
    return " ".join(parts)
