from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping

from deckr.beacon import Candidate
from deckr.contracts.messages import controller_address, hardware_manager_address
from deckr.hardware.descriptors import DeviceDescriptor, DeviceRef
from deckr.hardware.profiles import (
    HardwareBeaconPayload,
    hardware_payload_from_advertisement,
)

from deckr.controller._hardware._models import (
    HardwareCandidate,
    OwnedHardwareClaim,
    ref_key,
)

logger = logging.getLogger(__name__)


def valid_hardware_payload(candidate: Candidate) -> HardwareBeaconPayload | None:
    try:
        return hardware_payload_from_advertisement(candidate.advertisement)
    except ValueError:
        logger.warning(
            "Ignoring invalid hardware Beacon advertisement %s",
            candidate.key,
        )
        return None


def parse_hardware_candidate(candidate: Candidate) -> tuple[HardwareCandidate, ...]:
    payload = valid_hardware_payload(candidate)
    if payload is None:
        return ()

    hardware_candidates: list[HardwareCandidate] = []
    for device_id, item in payload.devices.items():
        if item.device_ref.device_id != device_id:
            continue
        hardware_candidates.append(
            HardwareCandidate(
                advertisement_key=candidate.key,
                advertisement_id=candidate.advertisement.advertisement_id,
                advertisement_endpoint=candidate.advertisement.endpoint,
                advertisement_session_id=candidate.advertisement.session_id,
                advertisement_revision=candidate.revision,
                advertisement_refresh_seq=candidate.advertisement.refresh_seq,
                payload=payload,
                ref=item.device_ref,
                device=item.descriptor,
                labels=payload.labels,
            )
        )
    return tuple(hardware_candidates)


def select_hardware_candidates(
    records: Iterable[HardwareCandidate],
) -> dict[tuple[str, str], HardwareCandidate]:
    candidates: dict[tuple[str, str], HardwareCandidate] = {}
    for hardware_candidate in records:
        key = ref_key(hardware_candidate.ref)
        selected = candidates.get(key)
        if selected is not None:
            log_duplicate_hardware_candidate(
                hardware_candidate.ref,
                selected=selected,
                skipped=hardware_candidate,
            )
            continue
        candidates[key] = hardware_candidate
    return candidates


def log_duplicate_hardware_candidate(
    ref: DeviceRef,
    *,
    selected: HardwareCandidate,
    skipped: HardwareCandidate,
) -> None:
    logger.info(
        "Multiple hardware Beacon advertisements describe device %s/%s; "
        "selected key=%s endpoint=%s session=%s revision=%s refreshSeq=%s; "
        "skipped key=%s endpoint=%s session=%s revision=%s refreshSeq=%s",
        ref.manager_id,
        ref.device_id,
        selected.advertisement_key,
        selected.advertisement_endpoint,
        selected.advertisement_session_id,
        selected.advertisement_revision,
        selected.advertisement_refresh_seq,
        skipped.advertisement_key,
        skipped.advertisement_endpoint,
        skipped.advertisement_session_id,
        skipped.advertisement_revision,
        skipped.advertisement_refresh_seq,
    )


def unmatched_hardware_signature(
    device: DeviceDescriptor,
    labels: Mapping[str, str],
) -> tuple[str, tuple[tuple[str, str], ...]]:
    return device.fingerprint, tuple(sorted(labels.items()))


def manager_session_changed(
    owned: OwnedHardwareClaim,
    candidate: HardwareCandidate,
) -> bool:
    return (
        owned.current_sessions.get(str(candidate.payload.manager_endpoint))
        != candidate.payload.session_id
    )


def hardware_claim_current_sessions(
    controller_id: str,
    controller_session_id: str,
    candidate: HardwareCandidate,
) -> dict[str, str]:
    return {
        str(controller_address(controller_id)): controller_session_id,
        str(candidate.payload.manager_endpoint): candidate.payload.session_id,
    }


def owned_manager_session_id(owned: OwnedHardwareClaim) -> str | None:
    return owned.current_sessions.get(
        str(hardware_manager_address(owned.ref.manager_id))
    )
