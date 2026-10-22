"""Configured controller scope passed to the core requirement matcher."""

from deckr.actions.bindings import CapabilityRequirement, MatchedCapability
from deckr.actions.catalog import ActionDescriptor
from deckr.actions.matching import match_capability_requirements
from deckr.hardware.descriptors import DECKR_OUTPUT_RASTER, DeviceDescriptor, DeviceRef

from deckr.controller._binding_resolution import ResolvedControlBinding


def matched_capabilities(
    binding: ResolvedControlBinding,
    requirements: tuple[CapabilityRequirement, ...] | None,
    device_ref: DeviceRef,
    device: DeviceDescriptor,
) -> tuple[MatchedCapability, ...]:
    matches = match_capability_requirements(
        ActionDescriptor(actionId=binding.action_id, requirements=requirements),
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

    capabilities = {cap.capability_id: cap for cap in binding.control.capabilities}
    outputs = {
        match.capability.capability_id: capabilities[match.capability.capability_id]
        for match in matches
        if capabilities[match.capability.capability_id].direction == "output"
    }
    if len(outputs) > 1 or any(
        cap.family != DECKR_OUTPUT_RASTER or cap.capability_type != "bitmap"
        for cap in outputs.values()
    ):
        raise ValueError("Controller bindings support at most one raster bitmap output")
    return matches


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
