from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass

import anyio
from deckr.actions.endpoints import action_provider_address
from deckr.concord import (
    DEFAULT_CONCORD_TOKEN_REFRESH_SECONDS,
    Concord,
    ConcordAgreementLease,
    ConcordAgreementSpec,
    ConcordConflict,
    ConcordUnavailable,
    ContractHandle,
    ContractValidity,
    ContractValidityStatus,
    ParticipantHandle,
)
from deckr.contracts.authority import ContractPointer
from deckr.contracts.messages import controller_address
from deckr.profiles import (
    ACTION_PROVIDER_SESSION_PROFILE_ID,
    ActionProviderSessionTerms,
)

from deckr.controller._stop_aware import cancel_on_stopping, sleep_until_stopping
from deckr.controller.action_provider.provider import ActionMetadata

logger = logging.getLogger(__name__)

PROVIDER_SESSION_HEARTBEAT_SECONDS = DEFAULT_CONCORD_TOKEN_REFRESH_SECONDS
_WATCH_RETRY_SECONDS = 1.0


@dataclass(frozen=True, slots=True)
class ProviderSessionKey:
    provider_instance_id: str
    provider_id: str
    provider_session_id: str


_ProviderSessionChangeCallback = Callable[[], object]


@dataclass(frozen=True, slots=True)
class ProviderSessionSnapshot:
    key: ProviderSessionKey
    ready: bool
    terminal: bool
    status: ContractValidityStatus | None
    reason: str | None = None


@dataclass(slots=True)
class ProviderSessionLease:
    key: ProviderSessionKey
    provider_instance_id: str
    provider_id: str
    provider_session_id: str
    agreement: ConcordAgreementLease
    current_sessions: dict[str, str]
    controller_token: ParticipantHandle | None = None

    @property
    def contract(self) -> ContractHandle:
        return self.agreement.contract

    @property
    def contract_pointer(self) -> ContractPointer:
        return ContractPointer(
            contractId=self.contract.contract_id,
            generation=self.contract.generation,
        )


class ActionProviderSessionManager:
    """Concord-backed runtime session agreements for action provider endpoints."""

    def __init__(
        self,
        *,
        controller_id: str,
        controller_session_id: str,
        concord: Concord,
        start_soon,
    ) -> None:
        self._controller_id = controller_id
        self._controller_session_id = controller_session_id
        self._concord = concord
        self._sessions: dict[ProviderSessionKey, ProviderSessionLease] = {}
        self._retired_provider_session_ids: set[str] = set()
        self._start_soon = start_soon
        self._lock = anyio.Lock()
        self._change_callback: _ProviderSessionChangeCallback | None = None
        self._started = False

    def set_change_callback(
        self,
        callback: _ProviderSessionChangeCallback | None,
    ) -> None:
        self._change_callback = callback

    def start(self, stopping: anyio.Event) -> None:
        if self._started:
            return
        self._started = True
        self._start_soon(self._watch_loop, stopping)

    async def ensure(self, action: ActionMetadata) -> bool:
        snapshot = await self.prepare(action)
        return snapshot is not None and not snapshot.terminal

    async def prepare(
        self,
        action: ActionMetadata,
    ) -> ProviderSessionSnapshot | None:
        snapshots = await self.prepare_many((action,))
        key = provider_session_key(action)
        return snapshots.get(key) if key is not None else None

    async def prepare_many(
        self,
        actions: Iterable[ActionMetadata],
    ) -> dict[ProviderSessionKey, ProviderSessionSnapshot]:
        async with self._lock:
            snapshots: dict[ProviderSessionKey, ProviderSessionSnapshot] = {}
            for action in actions:
                key = provider_session_key(action)
                if key is None or key in snapshots:
                    continue
                snapshots[key] = await self._ensure_unlocked(action, key=key)
            return snapshots

    async def _ensure_unlocked(
        self,
        action: ActionMetadata,
        *,
        key: ProviderSessionKey,
    ) -> ProviderSessionSnapshot:
        if key.provider_session_id in self._retired_provider_session_ids:
            return _retired_snapshot(key)
        existing = self._sessions.get(key)
        if existing is not None:
            snapshot = await self._refresh_unlocked(key)
            if snapshot.terminal:
                return await self._ensure_unlocked(action, key=key)
            return snapshot

        provider_endpoint = action_provider_address(action.provider_instance_id)
        controller_endpoint = controller_address(self._controller_id)
        current_sessions = {
            str(controller_endpoint): self._controller_session_id,
            str(provider_endpoint): key.provider_session_id,
        }
        agreement = await self._concord.propose(
            ConcordAgreementSpec(
                profile=ACTION_PROVIDER_SESSION_PROFILE_ID,
                participants=(controller_endpoint, provider_endpoint),
                local_participant=controller_endpoint,
                local_session_id=self._controller_session_id,
                terms=ActionProviderSessionTerms(
                    sessionId=key.provider_session_id,
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
            key=key,
            provider_instance_id=action.provider_instance_id,
            provider_id=action.provider_id,
            provider_session_id=key.provider_session_id,
            agreement=agreement,
            current_sessions=current_sessions,
            controller_token=agreement.local_token,
        )
        self._sessions[key] = session
        return await self._refresh_unlocked(key)

    async def refresh_many(
        self,
        keys: Iterable[ProviderSessionKey],
    ) -> dict[ProviderSessionKey, ProviderSessionSnapshot]:
        async with self._lock:
            snapshots: dict[ProviderSessionKey, ProviderSessionSnapshot] = {}
            for key in keys:
                if key in snapshots:
                    continue
                snapshots[key] = await self._refresh_unlocked(key)
            return snapshots

    async def _refresh_unlocked(
        self,
        key: ProviderSessionKey,
    ) -> ProviderSessionSnapshot:
        session = self._sessions.get(key)
        if session is None:
            if key.provider_session_id in self._retired_provider_session_ids:
                return _retired_snapshot(key)
            return _missing_snapshot(key)
        try:
            validity = await session.agreement.refresh()
        except ConcordConflict:
            try:
                validity = await self._concord.validate(
                    session.contract,
                    current_sessions=session.current_sessions,
                )
            except ConcordUnavailable:
                validity = ContractValidity(ContractValidityStatus.UNAVAILABLE)
        session.agreement._validity = validity  # noqa: SLF001
        session.controller_token = session.agreement.local_token
        if _terminal_session_status(validity.status):
            reason = _terminal_session_reason(validity)
            await self._cancel_key_unlocked(
                key,
                reason=reason,
            )
            return ProviderSessionSnapshot(
                key=key,
                ready=False,
                terminal=True,
                status=validity.status,
                reason=reason,
            )
        return _snapshot(session)

    def cached_ready(self, key: ProviderSessionKey) -> bool:
        session = self._sessions.get(key)
        return session is not None and _snapshot(session).ready

    def contract_pointer(self, key: ProviderSessionKey) -> ContractPointer | None:
        session = self._sessions.get(key)
        if session is None or not _snapshot(session).ready:
            return None
        return session.contract_pointer

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
        snapshot = await self._refresh_unlocked(
            ProviderSessionKey(
                provider_instance_id,
                provider_id,
                provider_session_id,
            )
        )
        return snapshot.ready

    async def cancel(self, provider_instance_id: str, *, reason: str) -> None:
        async with self._lock:
            await self._cancel_unlocked(provider_instance_id, reason=reason)

    async def retire(self, key: ProviderSessionKey, *, reason: str) -> None:
        async with self._lock:
            await self._cancel_key_unlocked(key, reason=reason)
            self._retired_provider_session_ids.add(key.provider_session_id)

    async def _cancel_unlocked(self, provider_instance_id: str, *, reason: str) -> None:
        for key in tuple(self._sessions):
            if key.provider_instance_id == provider_instance_id:
                await self._cancel_key_unlocked(key, reason=reason)

    async def _cancel_key_unlocked(
        self,
        key: ProviderSessionKey,
        *,
        reason: str,
    ) -> None:
        session = self._sessions.pop(key, None)
        if session is None:
            return
        try:
            await session.agreement.cancel(reason=reason)
        except (ConcordConflict, ConcordUnavailable, ValueError):
            logger.warning(
                "Could not cancel action provider session contract for %s",
                key.provider_instance_id,
            )

    async def aclose(self) -> None:
        async with self._lock:
            for key in tuple(self._sessions):
                await self._cancel_key_unlocked(key, reason="controller_stop")

    async def _watch_loop(self, stopping: anyio.Event) -> None:
        controller_endpoint = controller_address(self._controller_id)
        while not stopping.is_set():
            try:
                async with (
                    self._concord.watch(
                        ACTION_PROVIDER_SESSION_PROFILE_ID,
                        participant=controller_endpoint,
                    ) as stream,
                    cancel_on_stopping(stopping),
                ):
                    self._notify_changed()
                    async for _event in stream:
                        if stopping.is_set():
                            return
                        self._notify_changed()
            except ConcordUnavailable:
                logger.warning(
                    "Action provider session Concord watch unavailable; retrying",
                    exc_info=True,
                )
            except Exception:
                logger.warning(
                    "Action provider session Concord watch failed; retrying",
                    exc_info=True,
                )
            await sleep_until_stopping(stopping, _WATCH_RETRY_SECONDS)

    def _notify_changed(self) -> None:
        callback = self._change_callback
        if callback is not None:
            callback()


def provider_session_key(action: ActionMetadata) -> ProviderSessionKey | None:
    provider_session_id = action.provider_session_id
    if provider_session_id is None:
        return None
    return ProviderSessionKey(
        provider_instance_id=action.provider_instance_id,
        provider_id=action.provider_id,
        provider_session_id=provider_session_id,
    )


def _snapshot(session: ProviderSessionLease) -> ProviderSessionSnapshot:
    status = session.agreement.validity.status
    terminal = _terminal_session_status(status)
    return ProviderSessionSnapshot(
        key=session.key,
        ready=status == ContractValidityStatus.VALID,
        terminal=terminal,
        status=status,
        reason=session.agreement.validity.reason,
    )


def _retired_snapshot(key: ProviderSessionKey) -> ProviderSessionSnapshot:
    return ProviderSessionSnapshot(
        key=key,
        ready=False,
        terminal=True,
        status=None,
        reason="provider_session_retired",
    )


def _missing_snapshot(key: ProviderSessionKey) -> ProviderSessionSnapshot:
    return ProviderSessionSnapshot(
        key=key,
        ready=False,
        terminal=False,
        status=None,
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


def _terminal_session_reason(validity: ContractValidity) -> str:
    if (
        validity.status == ContractValidityStatus.CANCELLED
        and validity.contract is not None
        and validity.contract.cancel_reason
    ):
        return validity.contract.cancel_reason
    if validity.reason:
        return validity.reason
    return f"provider_session_{validity.status.value}"
