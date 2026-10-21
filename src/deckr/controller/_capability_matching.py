"""Configured controller scope passed to the core requirement matcher."""

from deckr.actions.bindings import CapabilityRequirement, MatchedCapability
from deckr.actions.catalog import ActionDescriptor
from deckr.actions.matching import match_capability_requirements
from deckr.hardware.descriptors import DeviceDescriptor, DeviceRef

from deckr.controller._binding_resolution import ResolvedControlBinding


def matched_capabilities(
    binding: ResolvedControlBinding,
    requirements: tuple[CapabilityRequirement, ...] | None,
    device_ref: DeviceRef,
    device: DeviceDescriptor,
) -> tuple[MatchedCapability, ...]:
    return match_capability_requirements(
        ActionDescriptor(actionId=binding.action_uuid, requirements=requirements),
        device,
        device_ref,
        binding.control_id,
        configured_capability_ids=(
            binding.input_capability_ids
            | binding.output_capability_ids
            | binding.state_capability_ids
            | binding.config_capability_ids
            | binding.diagnostic_capability_ids
        ),
    )


def select_requirements(binding, requirements, device):
    matches = matched_capabilities(
        binding,
        requirements,
        DeviceRef(
            managerId="planning",
            deviceId=device.device_id,
            fingerprint=device.fingerprint,
        ),
        device,
    )
    capabilities = {cap.capability_id: cap for cap in binding.control.capabilities}
    return tuple(
        (match.requirement_name, capabilities[match.capability.capability_id])
        for match in matches
    )
