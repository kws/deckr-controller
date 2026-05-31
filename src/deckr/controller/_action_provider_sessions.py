from __future__ import annotations

import logging
from dataclasses import dataclass

import anyio
from deckr.actions.endpoints import action_provider_address
from deckr.concord import (
    ConcordAgreement,
    ConcordAgreementSpec,
    ConcordService,
    ContractHandle,
    ContractValidityStatus,
    ParticipantHandle,
)
from deckr.contracts.messages import controller_address
from deckr.profiles import (
    ACTION_PROVIDER_SESSION_PROFILE_ID,
    ActionProviderSessionTerms,
)
from deckr.state import StateConflict, StateUnavailable

from deckr.controller.action_provider.provider import ActionMetadata

logger = logging.getLogger(__name__)

PROVIDER_SESSION_HEARTBEAT_SECONDS = 5.0


@dataclass(slots=True)
class ProviderSessionLease:
    provider_instance_id: str
    provider_id: str
    provider_session_id: str
    agreement: ConcordAgreement
    current_sessions: dict[str, str]
    controller_token: ParticipantHandle | None = None

    @property
    def contract(self) -> ContractHandle:
        return self.agreement.contract


class ActionProviderSessionManager:
    """Concord-backed runtime session agreements for action provider endpoints."""

    def __init__(
        self,
        *,
        controller_id: str,
        controller_session_id: str,
        concord: ConcordService,
        start_soon,
    ) -> None:
        self._controller_id = controller_id
        self._controller_session_id = controller_session_id
        self._concord = concord
        self._sessions: dict[str, ProviderSessionLease] = {}
        self._start_soon = start_soon
        self._lock = anyio.Lock()

    async def ensure(self, action: ActionMetadata) -> bool:
        async with self._lock:
            return await self._ensure_unlocked(action)

    async def _ensure_unlocked(self, action: ActionMetadata) -> bool:
        provider_session_id = action.provider_session_id
        if provider_session_id is None:
            return False

        existing = self._sessions.get(action.provider_instance_id)
        if existing is not None:
            if (
                existing.provider_id == action.provider_id
                and existing.provider_session_id == provider_session_id
            ):
                validity = await existing.agreement.refresh()
                existing.controller_token = existing.agreement.local_token
                if not _terminal_session_status(validity.status):
                    return True
            await self._cancel_unlocked(
                action.provider_instance_id,
                reason="provider_session_changed",
            )

        provider_endpoint = action_provider_address(action.provider_instance_id)
        controller_endpoint = controller_address(self._controller_id)
        current_sessions = {
            str(controller_endpoint): self._controller_session_id,
            str(provider_endpoint): provider_session_id,
        }
        agreement = await self._concord.ensure_agreement(
            ConcordAgreementSpec(
                profile=ACTION_PROVIDER_SESSION_PROFILE_ID,
                participants=(controller_endpoint, provider_endpoint),
                local_participant=controller_endpoint,
                local_session_id=self._controller_session_id,
                terms=ActionProviderSessionTerms(
                    sessionId=provider_session_id,
                    controllerEndpoint=controller_endpoint,
                    providerEndpoint=provider_endpoint,
                    providerInstanceId=action.provider_instance_id,
                    providerId=action.provider_id,
                ),
                current_sessions=current_sessions,
                refresh_interval=PROVIDER_SESSION_HEARTBEAT_SECONDS,
                log_label="ActionProviderSession",
            ),
            start_soon=self._start_soon,
        )
        session = ProviderSessionLease(
            provider_instance_id=action.provider_instance_id,
            provider_id=action.provider_id,
            provider_session_id=provider_session_id,
            agreement=agreement,
            current_sessions=current_sessions,
            controller_token=agreement.local_token,
        )
        self._sessions[action.provider_instance_id] = session
        return True

    async def valid(
        self,
        *,
        provider_instance_id: str,
        provider_id: str,
        provider_session_id: str | None,
    ) -> bool:
        async with self._lock:
            return await self._valid_unlocked(
                provider_instance_id=provider_instance_id,
                provider_id=provider_id,
                provider_session_id=provider_session_id,
            )

    async def _valid_unlocked(
        self,
        *,
        provider_instance_id: str,
        provider_id: str,
        provider_session_id: str | None,
    ) -> bool:
        if provider_session_id is None:
            return False
        session = self._sessions.get(provider_instance_id)
        if session is None:
            return False
        if (
            session.provider_id != provider_id
            or session.provider_session_id != provider_session_id
        ):
            return False
        session.current_sessions[
            str(action_provider_address(provider_instance_id))
        ] = provider_session_id
        validity = await session.agreement.refresh()
        session.controller_token = session.agreement.local_token
        if _terminal_session_status(validity.status):
            await self._cancel_unlocked(
                provider_instance_id,
                reason=f"provider_session_{validity.status.value}",
            )
            return False
        return validity.status == ContractValidityStatus.VALID

    async def cancel(self, provider_instance_id: str, *, reason: str) -> None:
        async with self._lock:
            await self._cancel_unlocked(provider_instance_id, reason=reason)

    async def _cancel_unlocked(self, provider_instance_id: str, *, reason: str) -> None:
        session = self._sessions.pop(provider_instance_id, None)
        if session is None:
            return
        try:
            await session.agreement.cancel(reason=reason)
        except (StateConflict, StateUnavailable, ValueError):
            logger.warning(
                "Could not cancel action provider session contract for %s",
                provider_instance_id,
            )

    async def aclose(self) -> None:
        async with self._lock:
            for provider_instance_id in tuple(self._sessions):
                await self._cancel_unlocked(
                    provider_instance_id,
                    reason="controller_stop",
                )


def _terminal_session_status(status: ContractValidityStatus) -> bool:
    return status in {
        ContractValidityStatus.CANCELLED,
        ContractValidityStatus.MISSING_CONTRACT,
        ContractValidityStatus.INVALID_CONTRACT,
        ContractValidityStatus.INVALID_TOKEN,
        ContractValidityStatus.MISSING_TOKEN,
        ContractValidityStatus.GENERATION_MISMATCH,
        ContractValidityStatus.SESSION_MISMATCH,
        ContractValidityStatus.TERMS_HASH_MISMATCH,
    }
