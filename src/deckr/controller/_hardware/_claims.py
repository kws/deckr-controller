from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import Any
from uuid import uuid4

import anyio
from deckr.concord import (
    DEFAULT_CONCORD_TOKEN_REFRESH_SECONDS,
    Concord,
    ConcordAgreementSpec,
    ConcordConflict,
    ConcordUnavailable,
    ContractValidity,
    ContractValidityStatus,
)
from deckr.contracts.messages import controller_address
from deckr.hardware.profiles import (
    HARDWARE_CLAIM_PROFILE_ID,
    HardwareClaimDevice,
    HardwareClaimTerms,
)

from deckr.controller._hardware._discovery import (
    hardware_claim_current_sessions,
    manager_session_changed,
    owned_manager_session_id,
    unmatched_hardware_signature,
)
from deckr.controller._hardware._models import (
    HardwareCandidate,
    HardwareServiceCallbacks,
    OwnedHardwareClaim,
    OwnedHardwareClaimSnapshot,
    ref_key,
)
from deckr.controller._hardware._routes import DeviceRouteRegistry, LiveDeviceRoute
from deckr.controller._hardware._validity import (
    HARDWARE_CLAIM_TERMINAL_STATUSES,
    pending_hardware_claim_validity,
    validate_owned_claim,
)
from deckr.controller.config import DeviceConfigService

logger = logging.getLogger(__name__)

CLAIM_HEARTBEAT_SECONDS = DEFAULT_CONCORD_TOKEN_REFRESH_SECONDS


class HardwareClaimCoordinator:
    """Owns controller-side Concord claims for hardware devices."""

    def __init__(
        self,
        *,
        concord: Concord,
        config_service: DeviceConfigService,
        route_registry: DeviceRouteRegistry,
        callbacks: HardwareServiceCallbacks,
        controller_id: str,
        controller_session_id: str,
    ) -> None:
        self._concord = concord
        self._config_service = config_service
        self._route_registry = route_registry
        self._callbacks = callbacks
        self._controller_id = controller_id
        self._controller_session_id = controller_session_id
        self._owned_claims: dict[tuple[str, str], OwnedHardwareClaim] = {}
        self._unmatched_hardware_signatures: dict[
            tuple[str, str],
            tuple[str, tuple[tuple[str, str], ...]],
        ] = {}
        self._start_soon: Callable[..., Any] | None = None

    def set_start_soon(self, start_soon: Callable[..., Any] | None) -> None:
        self._start_soon = start_soon

    def snapshot(self) -> tuple[OwnedHardwareClaimSnapshot, ...]:
        return tuple(
            OwnedHardwareClaimSnapshot.from_claim(owned)
            for owned in self._owned_claims.values()
        )

    async def reconcile(
        self,
        candidates: Mapping[tuple[str, str], HardwareCandidate],
        *,
        reason: str,
    ) -> None:
        logger.debug("Reconciling hardware current state via %s", reason)

        for owned in tuple(self._owned_claims.values()):
            candidate = candidates.get(ref_key(owned.ref))
            if candidate is not None and manager_session_changed(owned, candidate):
                validity = await self.validate_claim(
                    owned,
                    current_sessions=hardware_claim_current_sessions(
                        self._controller_id,
                        self._controller_session_id,
                        candidate,
                    ),
                )
                owned.controller_token = owned.agreement.local_token
                if validity.status == ContractValidityStatus.UNAVAILABLE:
                    continue
                await self._revoke_owned_claim(
                    owned,
                    cancel_contract=True,
                    reason=(
                        f"hardware manager session changed during {reason}: "
                        f"{candidate.payload.session_id}"
                    ),
                )
                continue

            validity = await self.validate_claim(owned)
            owned.controller_token = owned.agreement.local_token

            if candidate is None:
                if validity.status == ContractValidityStatus.UNAVAILABLE:
                    continue
                if validity.status == ContractValidityStatus.VALID:
                    if not owned.live:
                        await self._connect_owned_claim(owned, None)
                    continue
                if pending_hardware_claim_validity(validity):
                    continue
                await self._revoke_terminal_owned_claim(
                    owned,
                    validity=validity,
                    reason=f"hardware claim invalid during {reason}: {validity.status}",
                )
                continue

            if validity.status == ContractValidityStatus.VALID:
                if await self._rematch_owned_claim_if_needed(owned, candidate):
                    continue
                if not owned.live:
                    await self._connect_owned_claim(owned, candidate)
                else:
                    await self._refresh_live_descriptor(owned, candidate)
                continue

            if validity.status == ContractValidityStatus.UNAVAILABLE:
                continue

            if not owned.live and pending_hardware_claim_validity(validity):
                continue

            await self._revoke_terminal_owned_claim(
                owned,
                validity=validity,
                reason=f"hardware claim invalid during {reason}: {validity.status}",
            )

        for candidate in candidates.values():
            key = ref_key(candidate.ref)
            if key in self._owned_claims:
                continue
            if self._route_registry.get_by_ref(candidate.ref) is not None:
                continue
            await self._try_claim_hardware_candidate(candidate)

        for key in tuple(self._unmatched_hardware_signatures):
            if key not in candidates:
                self._unmatched_hardware_signatures.pop(key, None)

    async def _try_claim_hardware_candidate(
        self,
        candidate: HardwareCandidate,
    ) -> None:
        device = candidate.device
        labels = candidate.labels
        key = ref_key(candidate.ref)
        unmatched_signature = unmatched_hardware_signature(device, labels)
        try:
            config = await self._config_service.match_device(
                fingerprint=device.fingerprint,
                labels=labels,
            )
        except ValueError:
            logger.exception(
                "Ambiguous config for hardware fingerprint=%s labels=%s manager=%s",
                device.fingerprint,
                dict(labels),
                candidate.ref.manager_id,
            )
            return
        if config is None:
            if self._unmatched_hardware_signatures.get(key) != unmatched_signature:
                logger.info(
                    "No controller config matched hardware fingerprint=%s "
                    "labels=%s manager=%s",
                    device.fingerprint,
                    dict(labels),
                    candidate.ref.manager_id,
                )
                self._unmatched_hardware_signatures[key] = unmatched_signature
            return
        self._unmatched_hardware_signatures.pop(key, None)

        claim_id = str(uuid4())
        terms = HardwareClaimTerms(
            claimId=claim_id,
            controllerEndpoint=controller_address(self._controller_id),
            managerEndpoint=candidate.payload.manager_endpoint,
            devices=(
                HardwareClaimDevice(
                    deviceRef=candidate.ref,
                    instanceCount=1,
                ),
            ),
        )
        current_sessions = hardware_claim_current_sessions(
            self._controller_id,
            self._controller_session_id,
            candidate,
        )
        try:
            agreement = await self._concord.propose(
                ConcordAgreementSpec(
                    profile=HARDWARE_CLAIM_PROFILE_ID,
                    participants=(
                        controller_address(self._controller_id),
                        candidate.payload.manager_endpoint,
                    ),
                    local_participant=controller_address(self._controller_id),
                    local_session_id=self._controller_session_id,
                    terms=terms,
                    current_sessions=current_sessions,
                    refresh_interval=CLAIM_HEARTBEAT_SECONDS,
                    log_label="ControllerHardware",
                ),
                start_soon=self._start_soon,
            )
        except (ConcordConflict, ConcordUnavailable):
            logger.warning("Could not create hardware claim for %s", candidate.ref)
            return

        owned = OwnedHardwareClaim(
            claim_id=claim_id,
            config_id=config.id,
            ref=candidate.ref,
            device=device,
            agreement=agreement,
            current_sessions=current_sessions,
        )
        self._owned_claims[key] = owned
        validity = await self.validate_claim(owned)
        owned.controller_token = owned.agreement.local_token
        if validity.status == ContractValidityStatus.VALID:
            await self._connect_owned_claim(owned, candidate)

    async def validate_claim(
        self,
        owned: OwnedHardwareClaim,
        *,
        current_sessions: Mapping[str, str] | None = None,
    ) -> ContractValidity:
        return await validate_owned_claim(
            self._concord,
            owned,
            current_sessions=current_sessions,
        )

    async def _connect_owned_claim(
        self,
        owned: OwnedHardwareClaim,
        candidate: HardwareCandidate | None,
    ) -> None:
        config = await self._config_service.get_config(owned.config_id)
        if config is None or not config.enabled:
            logger.info(
                "Config %s is unavailable; keeping hardware claim %s idle",
                owned.config_id,
                owned.claim_id,
            )
            return
        device = candidate.device if candidate is not None else owned.device
        manager_session_id = (
            candidate.payload.session_id
            if candidate is not None
            else owned_manager_session_id(owned)
        )
        if manager_session_id is None:
            logger.warning(
                "Claim %s is valid but manager session is unknown; keeping idle",
                owned.claim_id,
            )
            return
        live = self._route_registry.connect(
            config_id=owned.config_id,
            ref=owned.ref,
            device=device,
            contract=owned.contract_pointer,
            manager_session_id=manager_session_id,
        )
        owned.device = device
        owned.live = True
        await self._callbacks.on_hardware_connected(live, initial_config=config)

    async def _revoke_terminal_owned_claim(
        self,
        owned: OwnedHardwareClaim,
        *,
        validity: ContractValidity,
        reason: str,
        current_sessions: Mapping[str, str] | None = None,
    ) -> None:
        if validity.status not in HARDWARE_CLAIM_TERMINAL_STATUSES:
            return
        try:
            exact = await self._concord.validate_exact(
                owned.contract,
                current_sessions=current_sessions or owned.current_sessions,
            )
        except ConcordUnavailable:
            return
        owned.agreement._validity = exact  # noqa: SLF001
        if exact.status not in HARDWARE_CLAIM_TERMINAL_STATUSES:
            return
        await self._revoke_owned_claim(
            owned,
            cancel_contract=True,
            reason=reason,
        )

    async def _rematch_owned_claim_if_needed(
        self,
        owned: OwnedHardwareClaim,
        candidate: HardwareCandidate,
    ) -> bool:
        current_config = await self._config_service.get_config(owned.config_id)
        if current_config is not None and current_config.enabled:
            return False
        try:
            matched = await self._config_service.match_device(
                fingerprint=candidate.device.fingerprint,
                labels=candidate.labels,
            )
        except ValueError:
            logger.exception(
                "Ambiguous rematch config for claimed hardware fingerprint=%s "
                "labels=%s manager=%s",
                candidate.device.fingerprint,
                dict(candidate.labels),
                candidate.ref.manager_id,
            )
            return False
        if matched is None:
            return False
        if matched.id == owned.config_id:
            return False
        await self._migrate_owned_claim_config(
            owned,
            candidate,
            next_config=matched,
        )
        return True

    async def _migrate_owned_claim_config(
        self,
        owned: OwnedHardwareClaim,
        candidate: HardwareCandidate,
        *,
        next_config,
    ) -> None:
        previous_config_id = owned.config_id
        logger.info(
            "Rematching claimed hardware %s/%s from config %s to %s",
            owned.ref.manager_id,
            owned.ref.device_id,
            previous_config_id,
            next_config.id,
        )
        if self._route_registry.get(previous_config_id) is not None:
            self._route_registry.disconnect_config(previous_config_id)
        await self._callbacks.on_hardware_disconnected(
            previous_config_id,
            reason=f"hardware rematched to config {next_config.id}",
        )
        owned.config_id = next_config.id
        owned.device = candidate.device
        live = self._route_registry.connect(
            config_id=next_config.id,
            ref=owned.ref,
            device=candidate.device,
            contract=owned.contract_pointer,
            manager_session_id=candidate.payload.session_id,
        )
        owned.live = True
        await self._callbacks.on_hardware_connected(live, initial_config=next_config)

    async def _refresh_live_descriptor(
        self,
        owned: OwnedHardwareClaim,
        candidate: HardwareCandidate,
    ) -> None:
        live = self._route_registry.get_by_ref(owned.ref)
        if live is None:
            return
        if (
            candidate.device == live.device
            and candidate.payload.session_id == live.manager_session_id
        ):
            return
        updated = self._route_registry.update_descriptor(
            ref=owned.ref,
            device=candidate.device,
            contract=owned.contract_pointer,
            manager_session_id=candidate.payload.session_id,
        )
        if updated is None:
            return
        owned.device = candidate.device
        await self._callbacks.on_hardware_descriptor_changed(
            updated.config_id,
            updated.device,
        )

    async def _revoke_owned_claim(
        self,
        owned: OwnedHardwareClaim,
        *,
        cancel_contract: bool,
        reason: str,
    ) -> None:
        self._owned_claims.pop(ref_key(owned.ref), None)
        live = self._route_registry.get_by_ref(owned.ref)
        if live is not None:
            self._route_registry.disconnect_config(live.config_id)
            owned.live = False
            await self._callbacks.on_hardware_disconnected(
                live.config_id,
                reason=reason,
            )
        if cancel_contract:
            await self._cancel_hardware_claim(owned, reason=reason)
        else:
            await owned.agreement.aclose()

    async def _cancel_hardware_claim(
        self,
        owned: OwnedHardwareClaim,
        *,
        reason: str,
    ) -> None:
        with anyio.CancelScope(shield=True):
            try:
                await owned.agreement.cancel(reason=reason)
            except (ConcordConflict, ConcordUnavailable, ValueError):
                logger.info("Could not cancel hardware claim %s", owned.claim_id)

    async def disconnect_config(
        self,
        config_id: str,
        *,
        release_claim: bool,
        reason: str,
    ) -> None:
        live = self._route_registry.get(config_id)
        if live is None:
            return
        await self.disconnect_live(
            live,
            release_claim=release_claim,
            reason=reason,
        )

    async def disconnect_live(
        self,
        live: LiveDeviceRoute,
        *,
        release_claim: bool,
        reason: str,
    ) -> None:
        owned = self._owned_claims.get(ref_key(live.ref))
        if release_claim and owned is not None:
            await self._revoke_owned_claim(
                owned,
                cancel_contract=True,
                reason=reason,
            )
            return
        self._route_registry.disconnect_config(live.config_id)
        if owned is not None:
            owned.live = False
        await self._callbacks.on_hardware_disconnected(live.config_id, reason=reason)

    async def release_all(self, *, reason: str) -> None:
        for owned in tuple(self._owned_claims.values()):
            await self._revoke_owned_claim(
                owned,
                cancel_contract=True,
                reason=reason,
            )
