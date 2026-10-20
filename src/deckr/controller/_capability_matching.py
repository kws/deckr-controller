"""Controller selection policy over the core action requirement models."""

from deckr.actions.bindings import CapabilityRequirement, MatchedCapability
from deckr.hardware.descriptors import (
    CORE_CAPABILITY_FAMILIES,
    CapabilityDescriptor,
    CapabilityRef,
    DeviceRef,
)

from deckr.controller._binding_resolution import ResolvedControlBinding


def capability_view(capability: CapabilityDescriptor) -> str:
    if capability.projection is not None:
        return {
            "projection": "projected",
            "derivation": "derived",
        }[capability.projection.projection_type]
    return "native" if capability.family in CORE_CAPABILITY_FAMILIES else "extension"


def select_requirements(
    binding: ResolvedControlBinding,
    requirements: tuple[CapabilityRequirement, ...] | None,
) -> tuple[tuple[str | None, CapabilityDescriptor], ...]:
    """Pick the first supported preference within the configured control scope.

    No controller-generated projections are invented. An unsatisfied required
    capability prevents attachment; optional requirements can remain unmatched.
    """
    ids = (
        binding.input_capability_ids
        | binding.output_capability_ids
        | binding.state_capability_ids
        | binding.config_capability_ids
        | binding.diagnostic_capability_ids
    )
    available = tuple(
        cap for cap in binding.control.capabilities if cap.capability_id in ids
    )
    if requirements is None:
        return tuple((None, cap) for cap in available)
    matches = []
    for requirement in requirements:
        match = next(
            (
                cap
                for preference in requirement.preferences
                for cap in available
                if all(
                    expected is None or getattr(cap, field) == expected
                    for field, expected in (
                        ("capability_id", preference.capability_id),
                        ("family", preference.family),
                        ("capability_type", preference.capability_type),
                        ("direction", preference.direction),
                    )
                )
                and set(preference.event_types + requirement.event_types)
                <= set(cap.event_types)
                and set(preference.command_types + requirement.command_types)
                <= set(cap.command_types)
                and (not requirement.views or capability_view(cap) in requirement.views)
            ),
            None,
        )
        if match is None:
            if requirement.required:
                raise ValueError(
                    f"Required capability {requirement.name!r} is unavailable"
                )
        else:
            matches.append((requirement.name, match))
    return tuple(matches)


def matched_capabilities(
    binding: ResolvedControlBinding,
    requirements: tuple[CapabilityRequirement, ...] | None,
    device_ref: DeviceRef,
) -> tuple[MatchedCapability, ...]:
    matches = []
    for name, cap in select_requirements(binding, requirements):
        projection = cap.projection
        matches.append(
            MatchedCapability(
                requirementName=name,
                capability=CapabilityRef(
                    deviceRef=device_ref,
                    controlId=binding.control_id,
                    capabilityId=cap.capability_id,
                ),
                family=cap.family,
                type=cap.capability_type,
                direction=cap.direction,
                eventTypes=cap.event_types,
                commandTypes=cap.command_types,
                provenance=(
                    projection.projection_type
                    if projection is not None
                    else capability_view(cap)
                ),
                source=(
                    CapabilityRef(
                        deviceRef=device_ref,
                        controlId=projection.source.control_id,
                        capabilityId=projection.source.capability_id,
                    )
                    if projection is not None
                    else None
                ),
            )
        )
    return tuple(matches)
