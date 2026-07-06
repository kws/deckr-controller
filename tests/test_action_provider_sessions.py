from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock

import anyio
import pytest
from deckr.actions.endpoints import action_provider_address
from deckr.concord import (
    ConcordConflict,
    ConcordUnavailable,
    ContractHandle,
    ContractState,
    ContractValidity,
    ContractValidityStatus,
    ParticipantHandle,
)
from deckr.contracts.messages import controller_address

from deckr.controller import _action_provider_sessions as provider_sessions_module
from deckr.controller._action_provider_sessions import (
    ActionProviderSessionManager,
    provider_session_key,
)
from deckr.controller.action_provider.provider import ActionMetadata

CONTROLLER_ID = "controller-main"
CONTROLLER_SESSION_ID = "controller-session"
CONTROLLER_ADDR = controller_address(CONTROLLER_ID)
PROVIDER_INSTANCE_ID = "python"
PROVIDER_ID = "test.provider"
PROVIDER_SESSION_ID = "provider-session"


def _action(*, session_id: str = PROVIDER_SESSION_ID) -> ActionMetadata:
    return ActionMetadata(
        uuid="action.alpha",
        provider_instance_id=PROVIDER_INSTANCE_ID,
        provider_id=PROVIDER_ID,
        provider_session_id=session_id,
    )


def _validity(
    status: ContractValidityStatus,
    *,
    reason: str | None = None,
) -> ContractValidity:
    return ContractValidity(status, reason=reason)


def _contract_handle(contract_id: str) -> ContractHandle:
    provider = action_provider_address(PROVIDER_INSTANCE_ID)
    participants = tuple(sorted((CONTROLLER_ADDR, provider), key=str))
    return ContractHandle(
        key=f"contracts.{contract_id}.1.meta",
        contract_id=contract_id,
        generation=1,
        participants=participants,
        attached_participants=participants,
        revision=1,
        state=ContractState.OPEN,
        profile="dev.deckr.profile.action_provider_session.v1",
    )


def _participant_handle(contract_id: str) -> ParticipantHandle:
    return ParticipantHandle(
        key=f"contracts.{contract_id}.1.participants.controller",
        contract_id=contract_id,
        generation=1,
        participant=CONTROLLER_ADDR,
        session_id=CONTROLLER_SESSION_ID,
        token_id=f"{contract_id}-controller-token",
        revision=1,
        refresh_seq=1,
        ttl_seconds=120,
    )


class _FakeAgreement:
    def __init__(
        self,
        contract_id: str,
        *,
        initial: ContractValidity | None = None,
        refresh_results: list[ContractValidity | BaseException] | None = None,
        cancel_error: BaseException | None = None,
    ) -> None:
        self.contract = _contract_handle(contract_id)
        self._validity = initial or _validity(
            ContractValidityStatus.NOT_YET_FULFILLED
        )
        self.local_token = _participant_handle(contract_id)
        self._refresh_results = list(refresh_results or [self._validity])
        self._cancel_error = cancel_error
        self.refresh = AsyncMock(side_effect=self._refresh)
        self.cancel = AsyncMock(side_effect=self._cancel)

    @property
    def validity(self) -> ContractValidity:
        return self._validity

    async def _refresh(self) -> ContractValidity:
        result = (
            self._refresh_results.pop(0)
            if self._refresh_results
            else self._validity
        )
        if isinstance(result, BaseException):
            raise result
        self._validity = result
        return result

    async def _cancel(self, *, reason: str) -> bool:
        if self._cancel_error is not None:
            raise self._cancel_error
        self.cancel_reason = reason
        return True


class _EventStream:
    def __init__(self, events: list[object], *, stop: anyio.Event | None = None) -> None:
        self._events = list(events)
        self._stop = stop

    def __aiter__(self) -> _EventStream:
        return self

    async def __anext__(self) -> object:
        if self._events:
            return self._events.pop(0)
        if self._stop is not None:
            self._stop.set()
        raise StopAsyncIteration


@dataclass
class _FakeConcord:
    agreements: list[_FakeAgreement]
    validate_results: list[ContractValidity | BaseException] | None = None
    watch_results: list[object | BaseException] | None = None

    def __post_init__(self) -> None:
        self.propose_calls: list[tuple[object, object]] = []
        self.validate_calls: list[tuple[object, dict[str, str]]] = []
        self.watch_calls: list[tuple[object, object]] = []
        self.validate = AsyncMock(side_effect=self._validate)

    async def propose(self, spec, *, start_soon):
        self.propose_calls.append((spec, start_soon))
        if self.agreements:
            return self.agreements.pop(0)
        return _FakeAgreement(f"contract-{len(self.propose_calls)}")

    async def _validate(self, contract, *, current_sessions):
        self.validate_calls.append((contract, current_sessions))
        results = self.validate_results or [
            _validity(ContractValidityStatus.VALID)
        ]
        result = results.pop(0) if len(results) > 1 else results[0]
        if isinstance(result, BaseException):
            raise result
        return result

    def watch(self, profile: str, *, participant):
        @asynccontextmanager
        async def _watch():
            self.watch_calls.append((profile, participant))
            results = self.watch_results or [_EventStream([])]
            result = results.pop(0) if len(results) > 1 else results[0]
            if isinstance(result, BaseException):
                raise result
            yield result

        return _watch()


def _manager(concord: _FakeConcord, scheduled: list[tuple[Any, tuple[Any, ...]]] | None = None):
    scheduled = scheduled if scheduled is not None else []
    return ActionProviderSessionManager(
        controller_id=CONTROLLER_ID,
        controller_session_id=CONTROLLER_SESSION_ID,
        concord=concord,
        start_soon=lambda fn, *args: scheduled.append((fn, args)),
    )


@pytest.mark.asyncio
async def test_terminal_provider_session_status_allows_successor_contract() -> None:
    action = _action()
    key = provider_session_key(action)
    assert key is not None
    first = _FakeAgreement(
        "contract-first",
        refresh_results=[
            _validity(ContractValidityStatus.NOT_YET_FULFILLED),
            _validity(ContractValidityStatus.CANCELLED, reason="provider_stopped"),
        ],
    )
    second = _FakeAgreement("contract-second")
    concord = _FakeConcord([first, second])
    manager = _manager(concord)

    pending = await manager.prepare(action)
    assert pending is not None
    assert pending.ready is False
    assert pending.terminal is False
    old_contract_id = manager._sessions[key].contract.contract_id

    snapshot = (await manager.refresh_many((key,)))[key]

    assert snapshot.ready is False
    assert snapshot.terminal is True
    assert snapshot.status == ContractValidityStatus.CANCELLED
    assert snapshot.reason == "provider_stopped"
    assert manager._sessions == {}
    first.cancel.assert_awaited_once_with(reason="provider_stopped")

    successor = await manager.prepare(action)
    assert successor is not None
    assert successor.ready is False
    assert successor.terminal is False
    assert manager._sessions[key].contract.contract_id != old_contract_id
    assert manager._sessions[key].contract.contract_id == "contract-second"


@pytest.mark.asyncio
async def test_explicit_provider_session_retire_blocks_reuse() -> None:
    action = _action()
    key = provider_session_key(action)
    assert key is not None
    agreement = _FakeAgreement("contract-retired")
    concord = _FakeConcord([agreement])
    manager = _manager(concord)

    await manager.prepare(action)
    await manager.retire(key, reason="provider_session_replaced")

    retired = await manager.prepare(action)
    assert retired is not None
    assert retired.ready is False
    assert retired.terminal is True
    assert retired.reason == "provider_session_retired"
    assert manager._sessions == {}
    assert len(concord.propose_calls) == 1
    agreement.cancel.assert_awaited_once_with(reason="provider_session_replaced")


@pytest.mark.asyncio
async def test_existing_lease_refresh_conflict_uses_concord_validation() -> None:
    action = _action()
    key = provider_session_key(action)
    assert key is not None
    agreement = _FakeAgreement(
        "contract-conflict",
        refresh_results=[
            _validity(ContractValidityStatus.NOT_YET_FULFILLED),
            ConcordConflict("conflict"),
        ],
    )
    concord = _FakeConcord(
        [agreement],
        validate_results=[_validity(ContractValidityStatus.VALID)],
    )
    manager = _manager(concord)

    await manager.prepare(action)
    snapshot = (await manager.refresh_many((key,)))[key]

    assert snapshot.ready is True
    assert snapshot.terminal is False
    concord.validate.assert_awaited_once_with(
        agreement.contract,
        current_sessions=manager._sessions[key].current_sessions,
    )


@pytest.mark.asyncio
async def test_validate_unavailable_fallback_returns_nonready_snapshot() -> None:
    action = _action()
    key = provider_session_key(action)
    assert key is not None
    agreement = _FakeAgreement(
        "contract-unavailable",
        refresh_results=[
            _validity(ContractValidityStatus.NOT_YET_FULFILLED),
            ConcordConflict("conflict"),
        ],
    )
    concord = _FakeConcord(
        [agreement],
        validate_results=[ConcordUnavailable("offline")],
    )
    manager = _manager(concord)

    await manager.prepare(action)
    snapshot = (await manager.refresh_many((key,)))[key]

    assert snapshot.ready is False
    assert snapshot.terminal is False
    assert snapshot.status == ContractValidityStatus.UNAVAILABLE
    assert manager._sessions[key].agreement.validity.status == (
        ContractValidityStatus.UNAVAILABLE
    )


@pytest.mark.asyncio
async def test_terminal_status_cancels_and_removes_lease() -> None:
    action = _action()
    key = provider_session_key(action)
    assert key is not None
    agreement = _FakeAgreement(
        "contract-terminal",
        refresh_results=[
            _validity(ContractValidityStatus.NOT_YET_FULFILLED),
            _validity(ContractValidityStatus.INVALID_TOKEN, reason="bad_token"),
        ],
    )
    manager = _manager(_FakeConcord([agreement]))

    await manager.prepare(action)
    snapshot = (await manager.refresh_many((key,)))[key]

    assert snapshot.ready is False
    assert snapshot.terminal is True
    assert snapshot.status == ContractValidityStatus.INVALID_TOKEN
    assert snapshot.reason == "bad_token"
    assert manager._sessions == {}
    agreement.cancel.assert_awaited_once_with(reason="bad_token")


@pytest.mark.asyncio
async def test_cancel_exceptions_are_swallowed_and_logged(caplog) -> None:
    action = _action()
    agreement = _FakeAgreement(
        "contract-cancel-fails",
        cancel_error=ConcordUnavailable("offline"),
    )
    manager = _manager(_FakeConcord([agreement]))
    await manager.prepare(action)

    await manager.cancel(PROVIDER_INSTANCE_ID, reason="provider_stopped")

    assert manager._sessions == {}
    assert "Could not cancel action provider session contract" in caplog.text


def test_start_is_idempotent_and_schedules_watch_loop_once() -> None:
    scheduled: list[tuple[Any, tuple[Any, ...]]] = []
    manager = _manager(_FakeConcord([]), scheduled)
    stopping = anyio.Event()

    manager.start(stopping)
    manager.start(stopping)

    assert scheduled == [(manager._watch_loop, (stopping,))]


@pytest.mark.asyncio
async def test_watch_loop_notifies_on_current_events_and_retries_unavailable(
    monkeypatch,
) -> None:
    stopping = anyio.Event()
    stream = _EventStream(["current"], stop=stopping)
    concord = _FakeConcord(
        [],
        watch_results=[
            ConcordUnavailable("offline"),
            stream,
        ],
    )
    manager = _manager(concord)
    notifications = []
    sleep_calls = []
    manager.set_change_callback(lambda: notifications.append("changed"))

    async def fast_sleep_until_stopping(stopping_event, seconds):
        sleep_calls.append(seconds)

    monkeypatch.setattr(
        provider_sessions_module,
        "sleep_until_stopping",
        fast_sleep_until_stopping,
    )

    await manager._watch_loop(stopping)

    assert len(concord.watch_calls) == 2
    assert notifications == ["changed", "changed"]
    assert sleep_calls == [
        provider_sessions_module._WATCH_RETRY_SECONDS,
        provider_sessions_module._WATCH_RETRY_SECONDS,
    ]
