from __future__ import annotations

import logging
from dataclasses import dataclass

import anyio
from deckr.actions.endpoints import action_provider_address
from deckr.concord import (
    ConcordParticipantManager,
    ConcordService,
    ContractHandle,
    ContractRecord,
    ContractValidityStatus,
    ParticipantHandle,
)
from deckr.contracts.messages import controller_address
from deckr.contracts.models import thaw_json
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
    contract: ContractHandle
    controller_token: ParticipantHandle | None = None


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
        self._participant_manager = ConcordParticipantManager(
            concord=concord,
            participant=controller_address(controller_id),
            session_id=controller_session_id,
            profile=ACTION_PROVIDER_SESSION_PROFILE_ID,
            refresh_interval=PROVIDER_SESSION_HEARTBEAT_SECONDS,
            log_label="ActionProviderSession",
            accept_contract=self._accept_session_contract,
            current_sessions=self._session_current_sessions,
        )
        self._participant_manager.start_soon(start_soon)
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
                return True
            await self._cancel_unlocked(
                action.provider_instance_id,
                reason="provider_session_changed",
            )

        contract = await self._concord.create_contract(
            (
                controller_address(self._controller_id),
                action_provider_address(action.provider_instance_id),
            ),
            profile=ACTION_PROVIDER_SESSION_PROFILE_ID,
            terms=ActionProviderSessionTerms(
                sessionId=provider_session_id,
                controllerEndpoint=controller_address(self._controller_id),
                providerEndpoint=action_provider_address(action.provider_instance_id),
                providerInstanceId=action.provider_instance_id,
                providerId=action.provider_id,
            ),
            created_by=controller_address(self._controller_id),
            log_label="ActionProviderSession",
        )
        session = ProviderSessionLease(
            provider_instance_id=action.provider_instance_id,
            provider_id=action.provider_id,
            provider_session_id=provider_session_id,
            contract=contract,
        )
        self._sessions[action.provider_instance_id] = session
        managed = {
            item.contract.key: item
            for item in await self._participant_manager.reconcile(
                reason="new action provider session"
            )
        }.get(contract.key)
        if managed is not None:
            session.controller_token = managed.token
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
        managed = self._participant_manager.managed_contract(session.contract)
        if managed is None:
            managed = {
                item.contract.key: item
                for item in await self._participant_manager.reconcile(
                    reason="action provider session validity"
                )
            }.get(session.contract.key)
        if managed is not None:
            session.controller_token = managed.token
        validity = await self._concord.validate(
            session.contract,
            current_sessions={
                str(controller_address(self._controller_id)): self._controller_session_id,
                str(action_provider_address(provider_instance_id)): provider_session_id,
            },
            log_label="ActionProviderSession",
        )
        return validity.status == ContractValidityStatus.VALID

    async def cancel(self, provider_instance_id: str, *, reason: str) -> None:
        async with self._lock:
            await self._cancel_unlocked(provider_instance_id, reason=reason)

    async def _cancel_unlocked(self, provider_instance_id: str, *, reason: str) -> None:
        session = self._sessions.pop(provider_instance_id, None)
        if session is None:
            return
        try:
            await self._concord.cancel(
                session.contract,
                controller_address(self._controller_id),
                reason=reason,
                log_label="ActionProviderSession",
            )
        except (StateConflict, StateUnavailable, ValueError):
            logger.warning(
                "Could not cancel action provider session contract for %s",
                provider_instance_id,
            )
        await self._participant_manager.release(session.contract, reason=reason)

    async def _accept_session_contract(
        self,
        contract: ContractHandle,
        record: ContractRecord,
    ) -> bool:
        session = next(
            (
                item
                for item in self._sessions.values()
                if item.contract.key == contract.key
            ),
            None,
        )
        if session is None:
            return False
        try:
            terms = ActionProviderSessionTerms.model_validate(
                thaw_json(record.terms or {})
            )
        except ValueError:
            return False
        return (
            terms.controller_endpoint == controller_address(self._controller_id)
            and terms.provider_endpoint
            == action_provider_address(session.provider_instance_id)
            and terms.provider_instance_id == session.provider_instance_id
            and terms.provider_id == session.provider_id
            and terms.session_id == session.provider_session_id
            and terms.provider_endpoint in contract.participants
        )

    def _session_current_sessions(self, _contract: ContractHandle) -> dict[str, str]:
        return {str(controller_address(self._controller_id)): self._controller_session_id}

    async def aclose(self) -> None:
        async with self._lock:
            for provider_instance_id in tuple(self._sessions):
                await self._cancel_unlocked(
                    provider_instance_id,
                    reason="controller_stop",
                )
            await self._participant_manager.aclose()
